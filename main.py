import json
import logging
import os
import re
import tempfile
from urllib.parse import urlparse, urlunparse

import requests
from telegram import (
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    ReplyParameters,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

with open("config.json", "r") as f:
    CONFIG = json.load(f)

BOT_TOKEN: str = CONFIG["bot_token"]
AUTHORIZED_CHAT_ID: int = CONFIG["authorized_chat_id"]
TARGET_CHANNEL: str = CONFIG["target_channel"]
CHANNEL_TAG: str = CONFIG["channel_tag"]

__version__ = "1.2.0"

# Maximum number of ancestor tweets to walk up when resolving a reply chain.
# Each level requires a separate fxtwitter fetch, so this caps both latency
# and API load for very long threads.
MAX_REPLY_DEPTH = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    specials = r"\_*[]()~`>#+-=|{}.!"
    for ch in specials:
        text = text.replace(ch, f"\\{ch}")
    return text


def rewrite_to_fxtwitter(url: str) -> str:
    """Replace host with api.fxtwitter.com."""
    parsed = urlparse(url)
    rewritten = parsed._replace(netloc="api.fxtwitter.com")
    return urlunparse(rewritten)


def build_fxtwitter_url(screen_name: str, status_id: str) -> str:
    """Build an api.fxtwitter.com URL from screen name and status ID."""
    return f"https://api.fxtwitter.com/{screen_name}/status/{status_id}"


# Matches an x.com / twitter.com status URL anywhere within a larger message,
# e.g. "check this out https://x.com/user/status/12345 amazing". Captures up
# to (but not including) any trailing query string, fragment, or punctuation.
TWEET_URL_RE = re.compile(r"https?://(?:www\.)?(?:x\.com|twitter\.com)/\w+/status/\d+")


def parse_video_index_from_message(message_text: str) -> int:
    """Extract ::N suffix from the message text as a 0-based index."""
    parts = message_text.split("::")
    if len(parts) > 1:
        try:
            n = int(parts[1].strip())
            return max(n - 1, 0)
        except ValueError:
            pass
    return 0


def should_skip_quote(message_text: str) -> bool:
    """Return True if the user appended &nq to skip the quoted tweet."""
    return "&nq" in message_text.split("&")


def should_skip_reply(message_text: str) -> bool:
    """Return True if the user appended &nr to skip the reply-parent tweet."""
    return "&nr" in message_text.split("&")


def strip_reply_mentions(text: str) -> str:
    """
    Remove leading @mention tokens that Twitter prepends to reply text.
    e.g. "@zjdelicious @grok can you..." → "can you..."
    Strips any run of whitespace-separated tokens starting with '@' at the
    very beginning of the text, then removes any leftover leading whitespace.
    """
    tokens = text.split(" ")
    i = 0
    while i < len(tokens) and tokens[i].startswith("@"):
        i += 1
    return " ".join(tokens[i:]).lstrip()


def extract_photos(media_node: dict) -> list[str]:
    """Return list of photo URLs from a fxtwitter media node."""
    photos = media_node.get("photos") or []
    return [p["url"] for p in photos if p.get("url")]


TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # 50 MB — Telegram bot URL upload limit


def best_variant_url(variants: list[dict], duration_seconds: float = 0.0, top_url: str = "") -> str:
    """
    Pick the best video variant URL.

    Strategy:
    1. If fxtwitter provides a top-level url on the video object (top_url), try
       it first — it already represents the highest quality fxtwitter chose.
       Only skip it if its estimated size exceeds the Telegram 50MB limit.
    2. Otherwise walk variants sorted by bitrate descending, picking the first
       whose estimated size fits.
    3. If duration is unknown (0), skip size checks entirely.

    Peak bitrate over-estimates actual file size. We apply a 0.4 correction
    factor (empirical: actual ≈ peak_estimate * 0.4) to avoid wrongly skipping
    high-quality variants that would actually fit.
    """
    CORRECTION = 0.4

    mp4 = [v for v in variants if "video/mp4" in v.get("content_type", "") or v.get("url", "").endswith(".mp4")]
    candidates = mp4 if mp4 else variants
    candidates_sorted = sorted(candidates, key=lambda v: v.get("bitrate", 0), reverse=True)

    if not candidates_sorted and not top_url:
        return ""

    if duration_seconds <= 0:
        result = top_url or (candidates_sorted[0].get("url", "") if candidates_sorted else "")
        logger.info("best_variant_url: no duration, selecting %s", result[:80])
        return result

    def fits(bitrate: int) -> bool:
        return (bitrate / 8) * duration_seconds * CORRECTION <= TELEGRAM_MAX_BYTES

    # Try top_url first (fxtwitter's own highest-quality choice)
    if top_url:
        top_bitrate = next((v.get("bitrate", 0) for v in candidates_sorted if v.get("url", "") == top_url), 0)
        if top_bitrate <= 0 or fits(top_bitrate):
            logger.info("best_variant_url: selected top_url bitrate=%d url=%s", top_bitrate, top_url[:80])
            return top_url

    # top_url too large — walk variants from highest to lowest bitrate
    for variant in candidates_sorted:
        bitrate = variant.get("bitrate", 0)
        url = variant.get("url", "")
        if bitrate <= 0:
            logger.info("best_variant_url: no bitrate, selecting %s", url[:80])
            return url
        if fits(bitrate):
            logger.info("best_variant_url: selected bitrate=%d est=%.1fMB url=%s",
                        bitrate, (bitrate / 8) * duration_seconds * CORRECTION / 1024 / 1024, url[:80])
            return url

    result = candidates_sorted[-1].get("url", "") if candidates_sorted else top_url
    logger.info("best_variant_url: all exceed limit, using lowest bitrate %s", result[:80])
    return result


def extract_videos(
    media_node: dict, selected_index: int
) -> tuple[list[str], list[str], str, str, bool, int, int, int]:
    """
    Return (video_urls, thumb_urls, primary_video_url, primary_thumb_url,
            primary_is_gif, primary_width, primary_height, primary_duration).
    primary_* correspond to selected_index.
    primary_is_gif is True when the selected video is a Twitter GIF (looping mp4).
    Telegram requires send_animation for GIFs instead of send_video.
    """
    videos = media_node.get("videos") or []
    video_urls: list[str] = []
    thumb_urls: list[str] = []
    primary_video = ""
    primary_thumb = ""
    primary_is_gif = False
    primary_width = 0
    primary_height = 0
    primary_duration = 0

    for i, video in enumerate(videos):
        variants = video.get("variants") or []
        if not variants:
            continue
        duration = video.get("duration") or 0.0
        top_url = video.get("url", "")
        url = best_variant_url(variants, duration_seconds=duration, top_url=top_url)
        if not url:
            continue
        thumb = video.get("thumbnail_url", "")
        is_gif = video.get("type") == "gif"
        video_urls.append(url)
        thumb_urls.append(thumb)
        if i == selected_index:
            primary_video = url
            primary_thumb = thumb
            primary_is_gif = is_gif
            primary_width = int(video.get("width") or 0)
            primary_height = int(video.get("height") or 0)
            primary_duration = int(duration)

    # Fallback: if selected_index was out of range
    if not primary_video and video_urls:
        primary_video = video_urls[0]
        primary_thumb = thumb_urls[0] if thumb_urls else ""
        if videos:
            v0 = videos[0]
            primary_is_gif = v0.get("type") == "gif"
            primary_width = int(v0.get("width") or 0)
            primary_height = int(v0.get("height") or 0)
            primary_duration = int(v0.get("duration") or 0)

    return video_urls, thumb_urls, primary_video, primary_thumb, primary_is_gif, primary_width, primary_height, primary_duration


def build_caption(text: str, author: str, url: str, tag: str = CHANNEL_TAG) -> str:
    """Build a standard MarkdownV2 caption for a tweet."""
    caption = normalize(text)
    caption += f"\n\n[{normalize(author)}]({normalize(url)})"
    caption += tag
    return caption


def build_media_group(
    photo_urls: list[str],
    video_urls: list[str],
    thumb_urls: list[str],
    caption: str,
) -> list[InputMediaPhoto | InputMediaVideo]:
    """
    Build a mixed media group containing all photos and videos.
    Caption goes on the first item.
    """
    items: list[InputMediaPhoto | InputMediaVideo] = []
    first = True

    for url in photo_urls:
        if first:
            items.append(InputMediaPhoto(media=url, caption=caption, parse_mode=ParseMode.MARKDOWN_V2))
            first = False
        else:
            items.append(InputMediaPhoto(media=url))

    for i, url in enumerate(video_urls):
        thumb = thumb_urls[i] if i < len(thumb_urls) else None
        if first:
            items.append(InputMediaVideo(media=url, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, thumbnail=thumb, supports_streaming=True))
            first = False
        else:
            items.append(InputMediaVideo(media=url, thumbnail=thumb, supports_streaming=True))

    return items


def fetch_tweet(fx_url: str) -> dict | None:
    """Fetch a tweet from fxtwitter. Returns the tweet dict or None on failure."""
    try:
        response = requests.get(fx_url, timeout=30)
        data = response.json()
    except Exception as e:
        logger.error("Failed to fetch fxtwitter URL %s: %s", fx_url, e)
        return None
    if data.get("code") != 200:
        logger.error("fxtwitter returned non-200 code %s for %s", data.get("code"), fx_url)
        return None
    return data.get("tweet") or data.get("status") or {}


# ---------------------------------------------------------------------------
# Video download helper
# ---------------------------------------------------------------------------

async def download_video(url: str) -> str | None:
    """
    Download a video URL to a local temp file and return its path.
    Returns None on failure. Caller is responsible for deleting the file.
    Twitter CDN URLs require a proper User-Agent and may not be directly
    fetchable by Telegram's servers, so we download and re-upload.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://x.com/",
    }
    try:
        with requests.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            suffix = "." + url.split("?")[0].rsplit(".", 1)[-1] if "." in url.split("?")[0] else ".mp4"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    f.write(chunk)
                tmp_path = f.name
        logger.info("Downloaded video to %s (%.1f MB)", tmp_path, os.path.getsize(tmp_path) / 1024 / 1024)
        return tmp_path
    except Exception as e:
        logger.error("Failed to download video %s: %s", url, e)
        return None


async def send_video_with_fallback(bot, url: str, is_gif: bool, thumb_url: str, caption: str, parse_mode,
                                   width: int = 0, height: int = 0, duration: int = 0, **kwargs):
    """
    Try sending video by URL first. If Telegram rejects it, download locally
    and re-upload as a file. Cleans up the temp file afterwards.
    """

    async def _send(video_src, write_timeout: int = 20, thumb_override=None):
        if is_gif:
            return await bot.send_animation(
                animation=video_src,
                caption=caption,
                parse_mode=parse_mode,
                write_timeout=write_timeout,
                **kwargs,
            )
        extra = {}
        if isinstance(video_src, IOBase := __import__("io").IOBase) or hasattr(video_src, "read"):
            if width:
                extra["width"] = width
            if height:
                extra["height"] = height
            if duration:
                extra["duration"] = duration
            extra["thumbnail"] = thumb_override or thumb_url or None
        else:
            extra["thumbnail"] = thumb_url or None
        return await bot.send_video(
            video=video_src,
            caption=caption,
            parse_mode=parse_mode,
            supports_streaming=True,
            write_timeout=write_timeout,
            **extra,
            **kwargs,
        )

    # First attempt: by URL
    try:
        return await _send(url)
    except Exception as e:
        if "Wrong type" not in str(e) and "wrong type" not in str(e).lower() and "400" not in str(e):
            raise  # unexpected error — propagate
        logger.warning("URL send rejected (%s), falling back to local download", e)

    # Second attempt: download and re-upload
    tmp_path = await download_video(url)
    if not tmp_path:
        raise RuntimeError(f"Could not download video for re-upload: {url}")
    tmp_thumb = await download_video(thumb_url) if thumb_url else None
    try:
        with open(tmp_path, "rb") as f:
            thumb_src = open(tmp_thumb, "rb") if tmp_thumb else None
            try:
                return await _send(f, write_timeout=120, thumb_override=thumb_src)
            finally:
                if thumb_src:
                    thumb_src.close()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        if tmp_thumb:
            try:
                os.unlink(tmp_thumb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Core sending logic
# ---------------------------------------------------------------------------

async def send_tweet_content(
    bot,
    chat_id: str,
    caption: str,
    photo_urls: list[str],
    video_urls: list[str],
    thumb_urls: list[str],
    primary_video_url: str,
    primary_thumb_url: str,
    primary_is_gif: bool = False,
    primary_width: int = 0,
    primary_height: int = 0,
    primary_duration: int = 0,
    reply_parameters=None,
    link_preview_options=None,
):
    """
    Send tweet content to Telegram, picking the right message type based on
    what media is present. Returns the sent Message (or first message of a group).

    GIFs (Twitter looping mp4s) must be sent via send_animation; send_video
    rejects them with "Wrong type of the web page content".
    """
    has_photo = bool(photo_urls)
    has_photos = len(photo_urls) >= 2
    has_video = bool(video_urls)
    has_videos = len(video_urls) >= 2

    kwargs = dict(
        chat_id=chat_id,
        reply_parameters=reply_parameters,
    )

    # No media
    if not has_photo and not has_video:
        return await bot.send_message(
            text=caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            link_preview_options=link_preview_options,
            **kwargs,
        )

    # Single photo only
    if has_photo and not has_photos and not has_video:
        return await bot.send_photo(
            photo=photo_urls[0],
            caption=caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            **kwargs,
        )

    # Single video only (including GIF)
    if has_video and not has_videos and not has_photo:
        logger.info("send_tweet_content: primary_video_url=%s is_gif=%s", primary_video_url, primary_is_gif)
        return await send_video_with_fallback(
            bot=bot,
            url=primary_video_url,
            is_gif=primary_is_gif,
            thumb_url=primary_thumb_url,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            width=primary_width,
            height=primary_height,
            duration=primary_duration,
            **kwargs,
        )

    # Mixed or multiple — build a combined media group.
    # Telegram does not support InputMediaAnimation in media groups, so GIFs
    # are sent as InputMediaVideo there (they render fine, just don't loop).
    media_group = build_media_group(photo_urls, video_urls, thumb_urls, caption)
    if media_group:
        try:
            messages = await bot.send_media_group(media=media_group, **kwargs)
            return messages[0] if messages else None
        except Exception as e:
            logger.error("send_media_group failed with URLs (%s), retrying with local downloads", e)
            # The CDN URLs require auth headers Telegram can't send — download
            # every item locally and rebuild the group with file objects.
            tmp_files: list[tuple[str, object]] = []  # (path, file_handle)
            try:
                dl_photo_srcs = []
                for url in photo_urls:
                    tmp = await download_video(url)
                    if tmp:
                        fh = open(tmp, "rb")
                        tmp_files.append((tmp, fh))
                        dl_photo_srcs.append(fh)
                    else:
                        dl_photo_srcs.append(url)  # best-effort fallback

                dl_video_srcs = []
                dl_thumb_srcs = []
                for i, url in enumerate(video_urls):
                    tmp = await download_video(url)
                    if tmp:
                        fh = open(tmp, "rb")
                        tmp_files.append((tmp, fh))
                        dl_video_srcs.append(fh)
                    else:
                        dl_video_srcs.append(url)
                    thumb = thumb_urls[i] if i < len(thumb_urls) else None
                    if thumb:
                        tmp_t = await download_video(thumb)
                        if tmp_t:
                            fh_t = open(tmp_t, "rb")
                            tmp_files.append((tmp_t, fh_t))
                            dl_thumb_srcs.append(fh_t)
                        else:
                            dl_thumb_srcs.append(thumb)
                    else:
                        dl_thumb_srcs.append(None)

                dl_group = build_media_group(dl_photo_srcs, dl_video_srcs, dl_thumb_srcs, caption)
                if dl_group:
                    messages = await bot.send_media_group(media=dl_group, write_timeout=120, **kwargs)
                    return messages[0] if messages else None
            finally:
                for path, fh in tmp_files:
                    try:
                        fh.close()
                    except OSError:
                        pass
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
    return None


# ---------------------------------------------------------------------------
# Update handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    if message.text is None:
        return

    message_text: str = message.text
    chat = message.chat

    # -------------------------------------------------------------------
    # Two entry modes:
    #
    #   1. Authorized private chat (existing behavior): the message must
    #      itself BE the tweet URL (optionally with ::N / &nq / &nr flags).
    #      Output is posted to TARGET_CHANNEL with CHANNEL_TAG appended.
    #
    #   2. Any group/supergroup the bot is a member of: a tweet URL may
    #      appear ANYWHERE in the message text. No flags are parsed (full
    #      chain, no skip-quote/skip-reply, default video). Output is
    #      posted back into the SAME chat, as a reply to the triggering
    #      message, with no channel tag.
    #
    # Any other chat (random private DMs, channel posts, etc.) is ignored.
    # -------------------------------------------------------------------
    if chat.id == AUTHORIZED_CHAT_ID:
        # Strip the ::N suffix and all &flag qualifiers before URL validation
        base_url = message_text.split("::")[0].split("&")[0]
        if not (base_url.startswith("https://x.com/") or base_url.startswith("https://twitter.com/")):
            return

        skip_quote = should_skip_quote(message_text)
        skip_reply = should_skip_reply(message_text)
        selected_video_index = parse_video_index_from_message(message_text)
        dest_chat_id = TARGET_CHANNEL
        tag = CHANNEL_TAG
        pending_reply_to_id: int | None = None

    elif chat.type in ("group", "supergroup"):
        match = TWEET_URL_RE.search(message_text)
        if not match:
            return
        base_url = match.group(0)

        skip_quote = False
        skip_reply = False
        selected_video_index = 0
        dest_chat_id = chat.id
        tag = ""
        pending_reply_to_id = message.message_id

    else:
        return

    logger.info("Received URL: %s in chat %d", base_url, chat.id)
    link_preview_options = LinkPreviewOptions(is_disabled=True)

    # --- Fetch main tweet ---
    fx_url = rewrite_to_fxtwitter(base_url)
    logger.info("Fetching fxtwitter: %s", fx_url)
    tweet = fetch_tweet(fx_url)
    if not tweet:
        return

    tweet_url = tweet.get("url", "")
    tweet_text = tweet.get("text", "")
    tweet_author = tweet.get("author", {}).get("screen_name", "")

    # Strip Twitter-injected leading @mentions from reply text.
    # We do this now (before we know whether it's a reply) because the field
    # is always populated and stripping a non-reply no-ops cleanly.
    if tweet.get("replying_to"):
        tweet_text = strip_reply_mentions(tweet_text)
    tweet_media = tweet.get("media") or {}
    tweet_photo_urls = extract_photos(tweet_media)
    tweet_video_urls, tweet_thumb_urls, tweet_primary_video, tweet_primary_thumb, tweet_primary_is_gif, tweet_primary_width, tweet_primary_height, tweet_primary_duration = (
        extract_videos(tweet_media, selected_video_index) if tweet_media.get("videos") else ([], [], "", "", False, 0, 0, 0)
    )

    # --- Walk up the full reply chain (if this tweet is a reply) ---
    #
    # fxtwitter exposes `replying_to` (screen_name) and `replying_to_status`
    # (tweet ID) on every tweet but does NOT embed the parent object, so each
    # ancestor must be fetched individually. We walk up to MAX_REPLY_DEPTH
    # levels, stopping early if a tweet has no parent or a fetch fails.
    #
    # ancestors_near_to_far[0] is the immediate parent of the main tweet,
    # ancestors_near_to_far[1] is its parent, etc.
    ancestors_near_to_far: list[dict] = []
    if not skip_reply:
        cur = tweet
        for depth in range(MAX_REPLY_DEPTH):
            rname = cur.get("replying_to")
            rid = cur.get("replying_to_status")
            if not (rname and rid):
                break
            parent_fx_url = build_fxtwitter_url(rname, rid)
            logger.info("Fetching reply ancestor (depth %d): %s", depth + 1, parent_fx_url)
            parent = fetch_tweet(parent_fx_url)
            if not parent:
                break
            ancestors_near_to_far.append(parent)
            cur = parent

    # Oldest first, ending with the immediate parent of the main tweet.
    chain_old_to_new = list(reversed(ancestors_near_to_far))

    # Group consecutive same-author tweets so they render as one merged message.
    groups: list[list[dict]] = []
    for t in chain_old_to_new:
        author = (t.get("author", {}).get("screen_name", "") or "").lower()
        if groups and (groups[-1][0].get("author", {}).get("screen_name", "") or "").lower() == author:
            groups[-1].append(t)
        else:
            groups.append([t])

    # Does the main tweet merge into the last ancestor group (same author)?
    same_author_reply = bool(groups) and (
        (groups[-1][0].get("author", {}).get("screen_name", "") or "").lower() == tweet_author.lower()
    )
    merge_group: list[dict] | None = groups.pop() if same_author_reply else None

    # -----------------------------------------------------------------------
    # Sending model
    #
    #   1. Each remaining ancestor group is sent oldest-first, standalone for
    #      the first group, then each subsequent group replies to the
    #      previously sent group's message.
    #   2. The first group, if it is a single tweet that itself quotes
    #      something, has that quoted tweet sent first (standalone) and the
    #      group quote-replies to it — this preserves the original
    #      "quoted → quoting tweet → ..." ordering for threads that start
    #      with a quote-tweet.
    #   3. If the main tweet shares its author with the last ancestor in the
    #      chain (merge_group), they are merged into a single message instead
    #      of being sent separately.
    #   4. If the main tweet itself has a quote (tweet["quote"]), that is sent
    #      standalone and the main tweet quote-replies to it, taking priority
    #      as the anchor only if no ancestor chain anchor already exists.
    #
    #   Telegram's ReplyParameters can only target ONE other message at a
    #   time, so quote-highlighting on intermediate (non-first, non-last)
    #   groups is intentionally skipped — those groups simply reply to the
    #   previous group's message without a quote highlight.
    # -----------------------------------------------------------------------

    def _resolve_reply_params(reply_to_msg=None, quote_text: str = ""):
        """
        Build ReplyParameters for a message about to be sent.

        - If reply_to_msg is given (optionally with quote_text), reply/quote
          that message as before.
        - Otherwise, if this is the very first message of the whole response
          AND we're in group mode (pending_reply_to_id is set), reply to the
          user's triggering message instead — so the bot's response appears
          threaded under their link in the group.
        - pending_reply_to_id is consumed on first use; only the first
          message of the response ever replies to the source message.
        """
        nonlocal pending_reply_to_id
        if reply_to_msg and quote_text:
            rp = ReplyParameters(
                message_id=reply_to_msg.message_id,
                quote=quote_text,
                quote_parse_mode=ParseMode.MARKDOWN_V2,
            )
        elif reply_to_msg:
            rp = ReplyParameters(message_id=reply_to_msg.message_id)
        elif pending_reply_to_id is not None:
            rp = ReplyParameters(message_id=pending_reply_to_id)
        else:
            rp = None
        pending_reply_to_id = None
        return rp

    async def send_quote_tweet(quote: dict, reply_to_msg=None):
        """Send a quoted tweet as standalone (or optionally replying to reply_to_msg)."""
        q_url    = quote.get("url", "")
        q_text   = quote.get("text", "")
        q_author = quote.get("author", {}).get("screen_name", "")
        q_caption = normalize(q_text)
        q_caption += f"\n\n[{normalize(q_author)}]({normalize(q_url)})"
        q_caption += tag

        q_media = quote.get("media") or {}
        q_photos = extract_photos(q_media)
        q_videos, q_thumbs, q_primary_video, q_primary_thumb, q_primary_is_gif, q_primary_width, q_primary_height, q_primary_duration = (
            extract_videos(q_media, selected_video_index) if q_media.get("videos") else ([], [], "", "", False, 0, 0, 0)
        )
        rp = _resolve_reply_params(reply_to_msg)
        return await send_tweet_content(
            bot=context.bot,
            chat_id=dest_chat_id,
            caption=q_caption,
            photo_urls=q_photos,
            video_urls=q_videos,
            thumb_urls=q_thumbs,
            primary_video_url=q_primary_video,
            primary_thumb_url=q_primary_thumb,
            primary_is_gif=q_primary_is_gif,
            primary_width=q_primary_width,
            primary_height=q_primary_height,
            primary_duration=q_primary_duration,
            reply_parameters=rp,
            link_preview_options=link_preview_options,
        )

    def merge_tweets(tweets: list[dict]) -> tuple[str, list[str], list[str], list[str], str, str, bool, int, int, int]:
        """
        Merge one or more same-author tweets into a single caption + media set.
        Returns (caption, photo_urls, video_urls, thumb_urls, primary_video_url,
                 primary_thumb_url, primary_is_gif, primary_width, primary_height,
                 primary_duration).
        Media is concatenated in chronological order; the LAST tweet that has a
        video provides the primary_* values (so a media group's caption-bearing
        item lines up with the most recent tweet's video when present).
        """
        last = tweets[-1]
        last_url = last.get("url", "")
        last_author = last.get("author", {}).get("screen_name", "")

        texts = []
        for t in tweets:
            txt = t.get("text", "")
            if t.get("replying_to"):
                txt = strip_reply_mentions(txt)
            texts.append(normalize(txt))

        if len(texts) == 1:
            caption = texts[0]
        else:
            caption = "\n\n\\-\\-\\-\n\n".join(texts)
        caption += f"\n\n[{normalize(last_author)}]({normalize(last_url)})"
        caption += tag

        all_photos: list[str] = []
        all_videos: list[str] = []
        all_thumbs: list[str] = []
        primary_video, primary_thumb, primary_is_gif = "", "", False
        primary_width, primary_height, primary_duration = 0, 0, 0

        for t in tweets:
            media = t.get("media") or {}
            photos = extract_photos(media)
            if media.get("videos"):
                videos, thumbs, p_video, p_thumb, p_gif, p_w, p_h, p_dur = extract_videos(media, selected_video_index)
            else:
                videos, thumbs, p_video, p_thumb, p_gif, p_w, p_h, p_dur = [], [], "", "", False, 0, 0, 0
            all_photos += photos
            all_videos += videos
            all_thumbs += thumbs
            if p_video:
                primary_video, primary_thumb, primary_is_gif = p_video, p_thumb, p_gif
                primary_width, primary_height, primary_duration = p_w, p_h, p_dur

        return caption, all_photos, all_videos, all_thumbs, primary_video, primary_thumb, primary_is_gif, primary_width, primary_height, primary_duration

    async def send_unit(tweets: list[dict], reply_to_msg=None, quote_text: str = ""):
        """Send one or more same-author tweets merged into a single message."""
        caption, photos, videos, thumbs, p_video, p_thumb, p_gif, p_w, p_h, p_dur = merge_tweets(tweets)

        rp = _resolve_reply_params(reply_to_msg, quote_text)

        return await send_tweet_content(
            bot=context.bot,
            chat_id=dest_chat_id,
            caption=caption,
            photo_urls=photos,
            video_urls=videos,
            thumb_urls=thumbs,
            primary_video_url=p_video,
            primary_thumb_url=p_thumb,
            primary_is_gif=p_gif,
            primary_width=p_w,
            primary_height=p_h,
            primary_duration=p_dur,
            reply_parameters=rp,
            link_preview_options=link_preview_options,
        )

    main_tweet_quote = tweet.get("quote")

    # Message that the main tweet should reply to in the channel
    anchor_message = None
    # For the main tweet's quote highlight (only used when main tweet itself quotes)
    quote_highlight: str = ""

    # --- Send ancestor chain groups, oldest first ---
    for i, group in enumerate(groups):
        group_quote = group[0].get("quote") if len(group) == 1 else None
        # Only the FIRST group can be quote-anchored: ReplyParameters can only
        # target one message, and non-first groups already need reply_to_msg
        # pointing at the previous group.
        if i == 0 and group_quote and not skip_quote and anchor_message is None:
            try:
                gq_msg = await send_quote_tweet(group_quote)
            except Exception as e:
                logger.error("Failed to send chain-start quote: %s", e)
                gq_msg = None
            gq_highlight = normalize(group_quote.get("text", "")) if gq_msg else ""
            try:
                anchor_message = await send_unit(group, reply_to_msg=gq_msg, quote_text=gq_highlight)
            except Exception as e:
                logger.error("Failed to send reply-chain ancestor: %s", e)
        else:
            try:
                anchor_message = await send_unit(group, reply_to_msg=anchor_message)
            except Exception as e:
                logger.error("Failed to send reply-chain ancestor: %s", e)

    # --- Main tweet's own quote ---
    if main_tweet_quote and not skip_quote:
        quote_highlight = normalize(main_tweet_quote.get("text", ""))
        try:
            q_msg = await send_quote_tweet(main_tweet_quote)
            # If an ancestor chain already produced an anchor, the main tweet
            # replies to that instead; only use the quote as anchor when there
            # is no reply chain.
            if anchor_message is None:
                anchor_message = q_msg
        except Exception as e:
            logger.error("Failed to send quote: %s", e)

    # --- Build ReplyParameters for the main tweet ---
    # _resolve_reply_params also handles the group-mode fallback: if no
    # ancestor chain or quote was sent before this (anchor_message is None),
    # and we're in group mode, this becomes the first message and replies to
    # the user's triggering message instead.
    main_reply_parameters = _resolve_reply_params(
        anchor_message,
        quote_highlight if (quote_highlight and main_tweet_quote) else "",
    )

    # --- Step 4: build caption + media for the main tweet ---
    if merge_group:
        main_caption, merged_photo_urls, merged_video_urls, merged_thumb_urls, merged_primary_video, merged_primary_thumb, merged_primary_is_gif, merged_primary_width, merged_primary_height, merged_primary_duration = merge_tweets(
            merge_group + [tweet]
        )
    else:
        main_caption = build_caption(tweet_text, tweet_author, tweet_url, tag=tag)
        merged_photo_urls = tweet_photo_urls
        merged_video_urls = tweet_video_urls
        merged_thumb_urls = tweet_thumb_urls
        merged_primary_video = tweet_primary_video
        merged_primary_thumb = tweet_primary_thumb
        merged_primary_is_gif = tweet_primary_is_gif
        merged_primary_width = tweet_primary_width
        merged_primary_height = tweet_primary_height
        merged_primary_duration = tweet_primary_duration

    # --- Step 5: send main tweet ---
    try:
        await send_tweet_content(
            bot=context.bot,
            chat_id=dest_chat_id,
            caption=main_caption,
            photo_urls=merged_photo_urls,
            video_urls=merged_video_urls,
            thumb_urls=merged_thumb_urls,
            primary_video_url=merged_primary_video,
            primary_thumb_url=merged_primary_thumb,
            primary_is_gif=merged_primary_is_gif,
            primary_width=merged_primary_width,
            primary_height=merged_primary_height,
            primary_duration=merged_primary_duration,
            reply_parameters=main_reply_parameters,
            link_preview_options=link_preview_options,
        )
    except Exception as e:
        logger.error("Failed to send main tweet: %s", e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()