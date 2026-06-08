# X/Twitter → Telegram Forwarder Bot

A personal Telegram bot that forwards tweets from X (Twitter) to a Telegram channel, including text, photos, videos, and quoted tweets. Built with Python and [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot).

---

## Support the Project

If you find this project useful and would like to support its development, you can donate using the following cryptocurrencies. We work on this project in our spare time alongside our full-time jobs, and even a small donation — as little as $1 — goes a long way in Iran and helps us dedicate more time to improving and maintaining it.

* BTC (Bitcoin): ```bc1qkxcvlvkrslx33g2kql45au438ua3qrvanangw2``` 
* ETH (Ethereum): ```0x575ef94aea4aa5bdf5c74c6b47311768118ad7c7```
* TRX (Tron): ```TRXMXafCkQ1sND7f9rfUNVDWazR866GHwF```
* USDT (Tron): ```TRXMXafCkQ1sND7f9rfUNVDWazR866GHwF```
* Toncoin (Ton): ```UQBcgZhcrJ-Qd-MUol1SvCLSqZCWZ01efrJJh4SUsoBpGtJw```

## How it works

1. You send an X/Twitter URL to the bot in a private chat.
2. The bot fetches the tweet data via [fxtwitter](https://github.com/FixTweet/FxTwitter).
3. It posts the tweet content (text + media) to your Telegram channel.
4. If the tweet contains a quote tweet, that is posted first, and the main tweet is posted as a reply to it with the quoted text highlighted.

---

## Requirements

- Python 3.10 or higher
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Your bot must be added as an **admin** of your target Telegram channel

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/DevRedStone/TeleX.git
cd TeleX
```

### 2. Create a virtual environment and install dependencies

Using a virtual environment is recommended, and required on newer versions of Ubuntu (23.04+) which restrict system-wide pip installs.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Once activated, your terminal prompt will show `(venv)`. You'll need to activate the virtual environment again each time you open a new terminal before running the bot.

### 3. Create your bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts.
3. Copy the **bot token** you receive.

### 4. Get your Telegram user ID

Send a message to [@userinfobot](https://t.me/userinfobot) on Telegram. It will reply with your numeric user ID.

### 5. Add the bot to your channel

In your Telegram channel settings, add your bot as an administrator with permission to **post messages**.

### 6. Configure `config.json`

Copy the example config:

```bash
cp config.example.json config.json
```

Then open `config.json` and replace the placeholders with your info:

```json
{
    "bot_token": "YOUR_BOT_TOKEN_HERE",
    "authorized_chat_id": 123456789,
    "target_channel": "@yourchannel",
    "channel_tag": "\n@yourchannel"
}
```

| Field                | Description                                                                    |
|----------------------|--------------------------------------------------------------------------------|
| `bot_token`          | The token from BotFather                                                       |
| `authorized_chat_id` | Your numeric Telegram user ID. Only messages from this user will be processed. |
| `target_channel`     | The username of your channel, e.g. `@mychannel`                                |
| `channel_tag`        | Text appended to every post, typically your channel handle                     |

### 7. Run the bot

Make sure your virtual environment is active, then run:

```bash
source venv/bin/activate   # skip if already active
python main.py
```

The bot will start polling for messages. Keep the process running while you want the bot to be active.

---

## Usage

Send any X or Twitter URL to your bot in a private chat:

```
https://x.com/i/status/1234567890
```

---

## File overview

```
.
├── main.py               # Main bot logic
├── config.json           # Your configuration (keep this private)
├── config.example.json   # Template — copy this to config.json
├── requirements.txt      # Python dependencies
├── venv/                 # Virtual environment (created during setup)
└── README.md
```

---

## Dependencies

| Package               | Purpose                            |
|-----------------------|------------------------------------|
| `python-telegram-bot` | Telegram Bot API client            |
| `requests`            | HTTP requests to the fxtwitter API |
