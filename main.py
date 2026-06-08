import json
import logging
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

__version__ = "1.1.0"


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


def best_variant_url(variants: list[dict], duration_seconds: float = 0.0) -> str:
    """
    Pick the highest-bitrate mp4 variant whose estimated file size fits within
    Telegram's 50 MB bot upload limit.

    Estimation: size_bytes ≈ (bitrate_bps / 8) * duration_seconds
    If duration is unknown (0), skip the size check and just pick the highest bitrate.
    Falls back through progressively lower qualities until one fits.
    If nothing fits (or no mp4s), return the lowest-bitrate mp4 as last resort.
    """
    mp4 = [v for v in variants if "video/mp4" in v.get("content_type", "") or v.get("url", "").endswith(".mp4")]
    candidates = mp4 if mp4 else variants
    # Sort highest bitrate first; variants without bitrate sort to bottom
    candidates_sorted = sorted(candidates, key=lambda v: v.get("bitrate", 0), reverse=True)

    if not candidates_sorted:
        return ""

    if duration_seconds <= 0:
        return candidates_sorted[0].get("url", "")

    for variant in candidates_sorted:
        bitrate = variant.get("bitrate", 0)
        if bitrate <= 0:
            # No bitrate info — optimistically include it
            logger.info("best_variant_url: no bitrate, selecting %s", variant.get("url", "")[:80])
            return variant.get("url", "")
        estimated_bytes = (bitrate / 8) * duration_seconds
        if estimated_bytes <= TELEGRAM_MAX_BYTES:
            logger.info("best_variant_url: selected bitrate=%d est=%.1fMB url=%s", bitrate, estimated_bytes/1024/1024, variant.get("url","")[:80])
            return variant.get("url", "")

    # Everything exceeds limit — return lowest bitrate as last resort
    return candidates_sorted[-1].get("url", "")


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
        url = best_variant_url(variants, duration_seconds=duration)
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


def build_caption(text: str, author: str, url: str) -> str:
    """Build a standard MarkdownV2 caption for a tweet."""
    caption = normalize(text)
    caption += f"\n\n[{normalize(author)}]({normalize(url)})"
    caption += CHANNEL_TAG
    return caption


def build_merged_caption(
    parent_text: str, parent_author: str, parent_url: str,
    reply_text: str, reply_url: str,
) -> str:
    """
    Build a single caption merging a parent tweet and its same-author reply.
    The reply URL is used as the primary attribution link.
    """
    caption = normalize(parent_text)
    caption += "\n\n\\-\\-\\-\n\n"
    caption += normalize(reply_text)
    caption += f"\n\n[{normalize(parent_author)}]({normalize(reply_url)})"
    caption += CHANNEL_TAG
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
    import tempfile, os
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
    and re-upload as a file. Cleans up the temp file afterward.
    """
    import os

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
            logger.error("send_media_group failed: %s", e)
            # Fallback: send primary item via download-if-needed helper
            if primary_video_url:
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
    return None


# ---------------------------------------------------------------------------
# Update handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    if message.chat.id != AUTHORIZED_CHAT_ID:
        return
    if message.text is None:
        return

    message_text: str = message.text

    # Strip the ::N suffix and all &flag qualifiers before URL validation
    base_url = message_text.split("::")[0].split("&")[0]

    if not (base_url.startswith("https://x.com/") or base_url.startswith("https://twitter.com/")):
        return

    logger.info("Received URL: %s in chat %d", message_text, message.chat.id)

    skip_quote = should_skip_quote(message_text)
    skip_reply = should_skip_reply(message_text)
    selected_video_index = parse_video_index_from_message(message_text)
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

    # --- Fetch reply-parent tweet (if this tweet is a reply) ---
    #
    # fxtwitter exposes `replying_to` (screen_name) and `replying_to_status`
    # (tweet ID) but does NOT embed the full parent object, so we fetch it
    # separately.  Two cases:
    #   1. Same author → merge into one combined message (no threading needed).
    #   2. Different author → send parent first, then send main tweet as a reply.
    #
    reply_parent: dict | None = None
    same_author_reply = False

    replying_to_name = tweet.get("replying_to")      # screen_name or None
    replying_to_id   = tweet.get("replying_to_status")  # snowflake str or None

    if replying_to_name and replying_to_id and not skip_reply:
        parent_fx_url = build_fxtwitter_url(replying_to_name, replying_to_id)
        logger.info("Fetching reply-parent: %s", parent_fx_url)
        reply_parent = fetch_tweet(parent_fx_url)
        if reply_parent:
            parent_author = reply_parent.get("author", {}).get("screen_name", "")
            same_author_reply = (
                parent_author.lower() == tweet_author.lower()
            )

    # -----------------------------------------------------------------------
    # Determine send order and build each piece.
    #
    # There are two independent dimensions:
    #   A) Does the main tweet have a quote?      → quote lives on tweet["quote"]
    #   B) Is the main tweet a reply?             → reply_parent is set
    #      B1) reply-parent is itself a quote-tweet → reply_parent["quote"] exists
    #
    # The correct channel sequence for every combination:
    #
    #   A only (main tweet is a quote-tweet, not a reply):
    #     1. quoted tweet  (standalone)
    #     2. main tweet    (replies to #1)
    #
    #   B only (main tweet is a reply, reply-parent has no quote):
    #     1. reply-parent  (standalone)
    #     2. main tweet    (replies to #1)
    #
    #   B1 (main tweet is a reply, reply-parent is itself a quote-tweet):
    #     1. quoted tweet  (standalone — the thing reply-parent quoted)
    #     2. reply-parent  (replies to #1)
    #     3. main tweet    (replies to #2)
    #
    #   A + B (main tweet both replies AND quotes — unusual but possible):
    #     1. reply-parent  (standalone)
    #     2. quoted tweet  (standalone)
    #     3. main tweet    (replies to #1, with quote highlight of #2)
    #     → treat as two independent threads; simpler and avoids ambiguity.
    #
    # Key rule: the quote is ALWAYS standalone (no reply_parameters).
    #           The reply-parent replies to the quote only in the B1 case.
    #           The main tweet always replies to the message sent just before it.
    # -----------------------------------------------------------------------

    async def send_quote_tweet(quote: dict, reply_to_msg=None):
        """Send a quoted tweet as standalone (or optionally replying to reply_to_msg)."""
        q_url    = quote.get("url", "")
        q_text   = quote.get("text", "")
        q_author = quote.get("author", {}).get("screen_name", "")
        q_caption = normalize(q_text)
        q_caption += f"\n\n[{normalize(q_author)}]({normalize(q_url)})"
        q_caption += CHANNEL_TAG

        q_media = quote.get("media") or {}
        q_photos = extract_photos(q_media)
        q_videos, q_thumbs, q_primary_video, q_primary_thumb, q_primary_is_gif, q_primary_width, q_primary_height, q_primary_duration = (
            extract_videos(q_media, selected_video_index) if q_media.get("videos") else ([], [], "", "", False, 0, 0, 0)
        )
        rp = ReplyParameters(message_id=reply_to_msg.message_id) if reply_to_msg else None
        return await send_tweet_content(
            bot=context.bot,
            chat_id=TARGET_CHANNEL,
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

    async def send_reply_parent(parent: dict, reply_to_msg=None, quote_text: str = ""):
        """Send the reply-parent tweet.
        If reply_to_msg + quote_text are given, quotes that message (Telegram quote-reply).
        If only reply_to_msg is given, plain-replies.
        """
        p_url    = parent.get("url", "")
        p_text   = parent.get("text", "")
        p_author = parent.get("author", {}).get("screen_name", "")
        if parent.get("replying_to"):
            p_text = strip_reply_mentions(p_text)
        p_caption = build_caption(p_text, p_author, p_url)

        p_media = parent.get("media") or {}
        p_photos = extract_photos(p_media)
        p_videos, p_thumbs, p_primary_video, p_primary_thumb, p_primary_is_gif, p_primary_width, p_primary_height, p_primary_duration = (
            extract_videos(p_media, selected_video_index) if p_media.get("videos") else ([], [], "", "", False, 0, 0, 0)
        )
        if reply_to_msg and quote_text:
            rp = ReplyParameters(
                message_id=reply_to_msg.message_id,
                quote=quote_text,
                quote_parse_mode=ParseMode.MARKDOWN_V2,
            )
        elif reply_to_msg:
            rp = ReplyParameters(message_id=reply_to_msg.message_id)
        else:
            rp = None
        return await send_tweet_content(
            bot=context.bot,
            chat_id=TARGET_CHANNEL,
            caption=p_caption,
            photo_urls=p_photos,
            video_urls=p_videos,
            thumb_urls=p_thumbs,
            primary_video_url=p_primary_video,
            primary_thumb_url=p_primary_thumb,
            primary_is_gif=p_primary_is_gif,
            primary_width=p_primary_width,
            primary_height=p_primary_height,
            primary_duration=p_primary_duration,
            reply_parameters=rp,
            link_preview_options=link_preview_options,
        )

    main_tweet_quote = tweet.get("quote")
    parent_quote     = reply_parent.get("quote") if reply_parent else None

    # Message that the main tweet should reply to in the channel
    anchor_message = None
    # For the main tweet's quote highlight (only used when main tweet itself quotes)
    quote_highlight: str = ""

    # --- Case B1: reply whose parent is itself a quote-tweet ---
    # The quoted tweet is sent first (standalone), then the reply-parent is sent
    # quoting it (Telegram quote-reply), then the main tweet replies to the parent.
    if reply_parent and not same_author_reply and parent_quote and not skip_quote:
        try:
            q_msg = await send_quote_tweet(parent_quote)
        except Exception as e:
            logger.error("Failed to send parent's quote: %s", e)
            q_msg = None
        # Pass quote_text so the reply-parent quotes the quoted tweet rather
        # than plain-replying to it.
        pq_highlight = normalize(parent_quote.get("text", "")) if q_msg else ""
        try:
            anchor_message = await send_reply_parent(
                reply_parent, reply_to_msg=q_msg, quote_text=pq_highlight
            )
        except Exception as e:
            logger.error("Failed to send reply-parent: %s", e)

    # --- Case B only: reply whose parent has no quote ---
    elif reply_parent and not same_author_reply:
        try:
            anchor_message = await send_reply_parent(reply_parent)
        except Exception as e:
            logger.error("Failed to send reply-parent: %s", e)

    # --- Case A (possibly combined with B, handled independently): main tweet quotes ---
    if main_tweet_quote and not skip_quote:
        quote_highlight = normalize(main_tweet_quote.get("text", ""))
        try:
            q_msg = await send_quote_tweet(main_tweet_quote)
            # In A-only (no reply), main tweet replies to the quote message.
            # In A+B, main tweet replies to the reply-parent (anchor_message),
            # so we only set anchor here when there's no reply-parent thread.
            if anchor_message is None:
                anchor_message = q_msg
        except Exception as e:
            logger.error("Failed to send quote: %s", e)

    # --- Build ReplyParameters for the main tweet ---
    main_reply_parameters = None
    if anchor_message is not None:
        main_reply_parameters = ReplyParameters(
            message_id=anchor_message.message_id,
            quote=quote_highlight if quote_highlight and main_tweet_quote else None,
            quote_parse_mode=ParseMode.MARKDOWN_V2 if quote_highlight and main_tweet_quote else None,
        )

    # --- Step 4: build caption for the main tweet ---
    # Same-author reply: merge parent text above a separator
    if reply_parent and same_author_reply:
        parent_text   = reply_parent.get("text", "")
        parent_author = reply_parent.get("author", {}).get("screen_name", "")
        main_caption = build_merged_caption(
            parent_text=parent_text,
            parent_author=parent_author,
            parent_url=tweet_url,  # link points to the reply (the latest tweet)
            reply_text=tweet_text,
            reply_url=tweet_url,
        )
        # Also merge media: parent photos/videos first, then reply photos/videos
        parent_media = reply_parent.get("media") or {}
        parent_photo_urls = extract_photos(parent_media)
        parent_video_urls, parent_thumb_urls, parent_primary_video, parent_primary_thumb, parent_primary_is_gif, parent_primary_width, parent_primary_height, parent_primary_duration = (
            extract_videos(parent_media, selected_video_index) if parent_media.get("videos") else ([], [], "", "", False, 0, 0, 0)
        )
        merged_photo_urls = parent_photo_urls + tweet_photo_urls
        merged_video_urls = parent_video_urls + tweet_video_urls
        merged_thumb_urls = parent_thumb_urls + tweet_thumb_urls
        # Primary video: prefer the reply's, fall back to parent's
        merged_primary_video = tweet_primary_video or parent_primary_video
        merged_primary_thumb = tweet_primary_thumb or parent_primary_thumb
        merged_primary_is_gif = tweet_primary_is_gif if tweet_primary_video else parent_primary_is_gif
        merged_primary_width = tweet_primary_width if tweet_primary_video else parent_primary_width
        merged_primary_height = tweet_primary_height if tweet_primary_video else parent_primary_height
        merged_primary_duration = tweet_primary_duration if tweet_primary_video else parent_primary_duration
    else:
        main_caption = build_caption(tweet_text, tweet_author, tweet_url)
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
            chat_id=TARGET_CHANNEL,
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