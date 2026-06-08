# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.1.0] - 2026-06-08

### Added
- **Reply-chain support.** When a tweet sent to the bot is itself a reply to another tweet, the bot now fetches and posts the full chain:
  - *Same author:* the parent and reply are merged into a single Telegram message, separated by a `---` divider, with all media combined.
  - *Different author:* the parent tweet is posted first, followed by the received tweet as a Telegram reply to it.
  - *Reply to a quote-tweet:* the quoted content is posted standalone, the parent quote-tweets it, and the reply threads off the parent — preserving the full three-level chain.
- **`&nr` flag** to skip fetching the reply parent, analogous to the existing `&nq` for quotes.
- **Automatic @mention stripping.** Twitter injects `@username` tokens at the start of reply text; these are now stripped before posting so captions read cleanly.

### Fixed
- **GIFs posted via `send_animation`.** Twitter GIFs are stored as looping MP4s with `type: gif` in the fxtwitter response. Telegram rejects these at `sendVideo` with *"Wrong type of the web page content"*. They are now routed through `send_animation`. GIFs inside media groups fall back to `InputMediaVideo` since Telegram does not support `InputMediaAnimation` in groups.
- **Video variant capped to Telegram's 50 MB bot upload limit.** Previously the highest-bitrate variant was always chosen, causing long videos to silently fail. Variants are now sorted by bitrate descending and the first whose estimated size `(bitrate / 8 × duration)` fits under 50 MB is selected.
- **Local download fallback for auth-gated CDN URLs.** Twitter's `amplify_video` and `ext_tw_video` CDN paths require a browser `User-Agent` and `Referer: https://x.com/` header that Telegram's servers do not send, producing *"Wrong type of the web page content"* even for correctly sized MP4s. The bot now attempts the URL send first and, on rejection, downloads the file locally and re-uploads it. `width`, `height`, `duration`, and thumbnail are supplied explicitly on upload so Telegram renders correct metadata. The thumbnail is also downloaded locally since `pbs.twimg.com` has the same access restrictions.

---

## [1.0.0] - Initial release

### Added
- Forward any `x.com` or `twitter.com` URL sent to the bot to a configured Telegram channel.
- Full quote-tweet support: quoted tweet is posted first, main tweet is posted as a Telegram reply to it.
- `&nq` flag to skip fetching the quoted tweet.
- `::N` suffix to select a specific video from a multi-video tweet (1-based index).
- MarkdownV2 captions with tweet text, author attribution link, and configurable channel tag.
- Mixed media group support: photos and videos from the same tweet sent as a single Telegram media group.
- Authorization by chat ID: only processes messages from a configured chat.
