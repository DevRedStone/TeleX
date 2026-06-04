import json
import logging
from urllib.parse import urlparse, urlunparse

import requests
from telegram import (
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
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


def extract_photos(media_node: dict) -> list[str]:
    """Return list of photo URLs from a fxtwitter media node."""
    photos = media_node.get("photos") or []
    return [p["url"] for p in photos if p.get("url")]


def extract_videos(media_node: dict, selected_index: int) -> tuple[list[str], list[str], str, str]:
    """
    Return (video_urls, thumb_urls, primary_video_url, primary_thumb_url).
    primary_* correspond to selected_index.
    """
    videos = media_node.get("videos") or []
    video_urls: list[str] = []
    thumb_urls: list[str] = []
    primary_video = ""
    primary_thumb = ""

    for i, video in enumerate(videos):
        variants = video.get("variants") or []
        if not variants:
            continue
        url = variants[-1].get("url", "")
        thumb = video.get("thumbnail_url", "")
        video_urls.append(url)
        thumb_urls.append(thumb)
        if i == selected_index:
            primary_video = url
            primary_thumb = thumb

    # Fallback: if selected_index was out of range
    if not primary_video and video_urls:
        primary_video = video_urls[0]
        primary_thumb = thumb_urls[0] if thumb_urls else ""

    return video_urls, thumb_urls, primary_video, primary_thumb


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
    reply_parameters=None,
    link_preview_options=None,
):
    """
    Send tweet content to Telegram, picking the right message type based on
    what media is present.  Returns the sent Message (or first message of a group).
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

    # Single video only
    if has_video and not has_videos and not has_photo:
        return await bot.send_video(
            video=primary_video_url,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            thumbnail=primary_thumb_url or None,
            supports_streaming=True,
            **kwargs,
        )

    # Mixed or multiple — build a combined media group
    media_group = build_media_group(photo_urls, video_urls, thumb_urls, caption)
    if media_group:
        try:
            messages = await bot.send_media_group(media=media_group, **kwargs)
            return messages[0] if messages else None
        except Exception as e:
            logger.error("send_media_group failed: %s", e)
            # Fallback: send primary video
            if primary_video_url:
                return await bot.send_video(
                    video=primary_video_url,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    thumbnail=primary_thumb_url or None,
                    supports_streaming=True,
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

    # Strip the ::N suffix and &nq qualifier before URL validation
    base_url = message_text.split("::")[0].split("&")[0]

    if not (base_url.startswith("https://x.com/") or base_url.startswith("https://twitter.com/")):
        return

    logger.info("Received URL: %s in chat %d", message_text, message.chat.id)

    skip_quote = should_skip_quote(message_text)
    selected_video_index = parse_video_index_from_message(message_text)

    fx_url = rewrite_to_fxtwitter(base_url)
    logger.info("Fetching fxtwitter: %s", fx_url)

    try:
        response = requests.get(fx_url, timeout=30)
        data = response.json()
    except Exception as e:
        logger.error("Failed to fetch fxtwitter: %s", e)
        return

    if data.get("code") != 200:
        logger.error("fxtwitter returned non-200 code: %s", data.get("code"))
        return

    tweet = data.get("tweet", {})

    # --- Main tweet ---
    tweet_url = tweet.get("url", "")
    tweet_text = tweet.get("text", "")
    tweet_author = tweet.get("author", {}).get("screen_name", "")

    caption = normalize(tweet_text)
    caption += f"\n\n[{normalize(tweet_author)}]({normalize(tweet_url)})"
    caption += CHANNEL_TAG

    tweet_media = tweet.get("media") or {}
    tweet_photo_urls = extract_photos(tweet_media)
    tweet_video_urls, tweet_thumb_urls, tweet_primary_video, tweet_primary_thumb = (
        extract_videos(tweet_media, selected_video_index) if tweet_media.get("videos") else ([], [], "", "")
    )

    # --- Quoted tweet ---
    quote_message = None
    link_preview_options = LinkPreviewOptions(is_disabled=True)

    if tweet.get("quote") and not skip_quote:
        quote = tweet["quote"]
        quote_url = quote.get("url", "")
        quote_text = quote.get("text", "")
        quote_author = quote.get("author", {}).get("screen_name", "")

        quote_highlight = normalize(quote_text)
        quote_caption = quote_highlight
        quote_caption += f"\n\n[{normalize(quote_author)}]({normalize(quote_url)})"
        quote_caption += CHANNEL_TAG

        quote_media = quote.get("media") or {}
        quote_photo_urls = extract_photos(quote_media)
        quote_video_urls, quote_thumb_urls, quote_primary_video, quote_primary_thumb = (
            extract_videos(quote_media, selected_video_index) if quote_media.get("videos") else ([], [], "", "")
        )

        try:
            quote_message = await send_tweet_content(
                bot=context.bot,
                chat_id=TARGET_CHANNEL,
                caption=quote_caption,
                photo_urls=quote_photo_urls,
                video_urls=quote_video_urls,
                thumb_urls=quote_thumb_urls,
                primary_video_url=quote_primary_video,
                primary_thumb_url=quote_primary_thumb,
                link_preview_options=link_preview_options,
            )
        except Exception as e:
            logger.error("Failed to send quote: %s", e)

    # --- Reply parameters (reply to quote if one was posted) ---
    reply_parameters = None
    if quote_message is not None:
        from telegram import ReplyParameters
        reply_parameters = ReplyParameters(
            message_id=quote_message.message_id,
            quote=quote_highlight,
            quote_parse_mode=ParseMode.MARKDOWN_V2,
        )

    # --- Main tweet send ---
    try:
        await send_tweet_content(
            bot=context.bot,
            chat_id=TARGET_CHANNEL,
            caption=caption,
            photo_urls=tweet_photo_urls,
            video_urls=tweet_video_urls,
            thumb_urls=tweet_thumb_urls,
            primary_video_url=tweet_primary_video,
            primary_thumb_url=tweet_primary_thumb,
            reply_parameters=reply_parameters,
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