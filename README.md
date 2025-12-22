# 🚀 Torrent Leech Bot

High-speed Telegram bot that downloads torrents using aria2, YouTube videos using yt-dlp, and uploads them to your Telegram channel.

## Features

- ⚡ **High-speed downloads** using aria2 with 16 connections
- 📺 **YouTube downloads** using yt-dlp
- 📤 **Auto-upload** to Telegram channel
- 🗑️ **Auto-delete** files after upload
- ❌ **Cancel button** for each download
- � **System stats** (CPU, RAM, Disk, Network)
- 🚀 **Speed test** for server
- 🔒 **User authorization**

## Commands

| Command | Description |
|---------|-------------|
| `/leech <magnet>` | Download torrent and upload |
| `/ytdl <url>` | Download YouTube video |
| `/status` | Show active downloads |
| `/stats` | System statistics |
| `/speedtest` | Run speed test |
| `/cancel` | Cancel all downloads |

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Copy `config.example.py` to `config.py` and fill in your credentials:

```python
API_ID = 123456  # From https://my.telegram.org
API_HASH = "your_api_hash"
BOT_TOKEN = "your_bot_token"  # From @BotFather
CHANNEL_ID = -100123456789  # Your channel ID
AUTHORIZED_USERS = [your_user_id]
```

Or edit these values directly in `torrent_leech_bot.py`.

### 3. Install aria2

**Ubuntu/Debian:**
```bash
sudo apt install aria2
```

**CentOS/RHEL:**
```bash
sudo yum install aria2
```

### 4. Start aria2

```bash
aria2c --enable-rpc --rpc-listen-all=true --rpc-allow-origin-all \
       --max-connection-per-server=16 --split=16 --min-split-size=1M \
       --seed-time=0 --bt-max-peers=0
```

### 5. Run Bot

```bash
python torrent_leech_bot.py
```

## GCP Deployment

For best performance on GCP:

```bash
# Install system packages
sudo apt update
sudo apt install aria2 python3-pip -y

# Clone and setup
git clone https://github.com/Priyanshu91930/leech.git
cd leech
pip3 install -r requirements.txt

# Configure (edit the file with your credentials)
nano torrent_leech_bot.py

# Run in background
nohup aria2c --enable-rpc --rpc-listen-all=true --rpc-allow-origin-all &
nohup python3 torrent_leech_bot.py &
```

## Requirements

- Python 3.8+
- aria2
- Telegram Bot Token
- Telegram API credentials

## License

MIT License
