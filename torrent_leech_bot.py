"""
Torrent Leech Bot
Downloads files from magnet links using aria2, uploads to Telegram, then deletes files.
Optimized for GCP/Linux deployment.
"""

import os
import sys
import asyncio
import subprocess
import shutil
import re
import time
import platform

# Try to use uvloop on Linux for better performance
try:
    import uvloop
    import asyncio
    uvloop.install()
    # Create event loop for Python 3.11+ compatibility
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    print("✅ Using uvloop for better performance")
except ImportError:
    pass

# Use pyrotgfork if available, otherwise fallback to pyrogram
try:
    from pyrogram import Client, filters
    from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
    from pyrogram.errors import FloodWait, MessageNotModified
except ImportError:
    print("❌ pyrogram/pyrotgfork not installed!")
    sys.exit(1)

import aria2p

# System monitoring
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("⚠️ psutil not installed - system stats disabled")

# ===================== CONFIGURATION =====================
# Telegram Bot Configuration
API_ID = 27686895  # Get from https://my.telegram.org
API_HASH = "0e996bd3891969ec5dfebf8bb3e39e94"  # Get from https://my.telegram.org
BOT_TOKEN = "8586739455:AAE7ixc8r3uaPhb2VRwS9O-1DuQKQJYf_NQ"  # Get from @BotFather
CHANNEL_ID = -1003321519174  # Your Telegram channel ID (with -100 prefix)

# Authorized Users (Telegram user IDs who can use the bot)
AUTHORIZED_USERS = [1246987713]  # Owner ID

# Download Configuration
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
MAX_SPLIT_SIZE = 2000 * 1024 * 1024  # 2GB (Telegram limit)

# Aria2 Configuration
ARIA2_HOST = "http://localhost"
ARIA2_PORT = 6800
ARIA2_SECRET = ""  # Set if you have a secret token

# Best public trackers for faster peer discovery (updated regularly from ngosang/trackerslist)
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonoid.ch:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://wepzone.net:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.srv00.com:6969/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://tracker.filemail.com:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://tracker.bittor.pw:1337/announce",
    "udp://tracker.0x7c0.com:6969/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://t.overflow.biz:6969/announce",
    "udp://run.publictracker.xyz:6969/announce",
    "udp://retracker01-msk-virt.corbina.net:80/announce",
    "udp://p4p.arenabg.com:1337/announce",
    "udp://opentracker.io:6969/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://open.dstud.io:6969/announce",
]
TRACKERS_STRING = ",".join(TRACKERS)

# ===================== SETUP =====================
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
BOT_START_TIME = time.time()

# Current upload channel (can be changed with /channel command)
current_channel = CHANNEL_ID

# Custom thumbnail path (can be set with /setthumb command)
THUMB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thumb.jpg")
custom_thumb = THUMB_PATH if os.path.exists(THUMB_PATH) else None

# Track active downloads: {gid: {"message": Message, "start_time": time, "cancelled": bool}}
active_downloads = {}

# Initialize Pyrogram Client
app = Client(
    "torrent_leech_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Initialize aria2
aria2 = None


def init_aria2():
    """Initialize aria2 connection"""
    global aria2
    try:
        aria2 = aria2p.API(
            aria2p.Client(
                host=ARIA2_HOST,
                port=ARIA2_PORT,
                secret=ARIA2_SECRET
            )
        )
        # Test connection
        aria2.get_stats()
        print("✅ Connected to aria2")
        return True
    except Exception as e:
        print(f"❌ Failed to connect to aria2: {e}")
        print("Make sure aria2 is running with RPC enabled:")
        print("aria2c --enable-rpc --rpc-listen-all=true --rpc-allow-origin-all")
        return False


def is_authorized(user_id: int) -> bool:
    """Check if user is authorized"""
    return user_id in AUTHORIZED_USERS


def get_readable_size(size_bytes: int) -> str:
    """Convert bytes to readable format"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} PB"


def get_readable_time(seconds: int) -> str:
    """Convert seconds to readable time"""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"


def get_elapsed_time(start_time: float) -> str:
    """Get elapsed time from start"""
    elapsed = int(time.time() - start_time)
    return get_readable_time(elapsed)


def get_cancel_button(gid: str) -> InlineKeyboardMarkup:
    """Create cancel button for a download"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{gid}")]
    ])


async def safe_edit_text(message: Message, text: str, reply_markup: InlineKeyboardMarkup = None):
    """Safely edit message text, handling common errors"""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except MessageNotModified:
        pass  # Ignore if message content is the same
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        pass


# Track last progress update time and bytes for speed calculation
_last_progress_update = {}
_last_progress_bytes = {}
_upload_start_time = {}

# Track active uploads for cancellation
active_uploads = {}

async def progress_callback(current: int, total: int, message: Message, start_text: str, upload_id: str = None):
    """Update progress during upload with rate limiting, speed, and cancel button"""
    global _last_progress_update, _last_progress_bytes, _upload_start_time
    
    msg_id = message.id
    now = time.time()
    
    # Initialize tracking for new uploads
    if msg_id not in _upload_start_time:
        _upload_start_time[msg_id] = now
        _last_progress_bytes[msg_id] = 0
    
    # Check if upload was cancelled
    if upload_id and upload_id in active_uploads and active_uploads[upload_id].get("cancelled"):
        raise Exception("Upload cancelled by user")
    
    # Rate limit: only update every 3 seconds
    if msg_id in _last_progress_update and now - _last_progress_update[msg_id] < 3:
        return
    
    # Calculate speed
    time_diff = now - _last_progress_update.get(msg_id, now - 1)
    bytes_diff = current - _last_progress_bytes.get(msg_id, 0)
    speed = bytes_diff / time_diff if time_diff > 0 else 0
    
    _last_progress_update[msg_id] = now
    _last_progress_bytes[msg_id] = current
    
    percent = (current / total) * 100
    progress_bar = "█" * int(percent // 5) + "░" * (20 - int(percent // 5))
    
    # Calculate ETA
    if speed > 0:
        remaining = total - current
        eta_seconds = int(remaining / speed)
        eta = get_readable_time(eta_seconds)
    else:
        eta = "-"
    
    text = (
        f"{start_text}\n\n"
        f"📊 Progress: [{progress_bar}] {percent:.1f}%\n"
        f"📁 Size: {get_readable_size(current)} / {get_readable_size(total)}\n"
        f"⚡ Speed: {get_readable_size(speed)}/s\n"
        f"⏱️ ETA: {eta}"
    )
    
    # Add cancel button if upload_id is provided
    reply_markup = None
    if upload_id:
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancelup_{upload_id}")]
        ])
    
    await safe_edit_text(message, text, reply_markup=reply_markup)


async def split_file(file_path: str, status_message: Message, split_size: int = MAX_SPLIT_SIZE) -> list:
    """
    Split a large file into smaller parts with progress updates.
    Uses memory-efficient chunked reading (10MB buffer) to avoid OOM.
    Returns a list of paths to the split files.
    """
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    split_dir = os.path.dirname(file_path)
    
    # Memory-efficient buffer size (10MB)
    BUFFER_SIZE = 10 * 1024 * 1024
    
    parts = []
    part_num = 1
    total_read = 0
    last_update = 0
    
    num_parts = (file_size + split_size - 1) // split_size  # Calculate total parts
    
    with open(file_path, 'rb') as f:
        while total_read < file_size:
            # Create part filename
            part_name = f"part{part_num:02d}_{file_name}"
            part_path = os.path.join(split_dir, part_name)
            
            bytes_for_this_part = 0
            
            with open(part_path, 'wb') as part_file:
                while bytes_for_this_part < split_size:
                    # Read in small chunks to avoid OOM
                    remaining_for_part = split_size - bytes_for_this_part
                    to_read = min(BUFFER_SIZE, remaining_for_part)
                    
                    chunk = f.read(to_read)
                    if not chunk:
                        break
                    
                    part_file.write(chunk)
                    bytes_for_this_part += len(chunk)
                    total_read += len(chunk)
                    
                    # Update progress every 100MB
                    if total_read - last_update >= 100 * 1024 * 1024:
                        last_update = total_read
                        percent = (total_read / file_size) * 100
                        progress_bar = "█" * int(percent // 5) + "░" * (20 - int(percent // 5))
                        await safe_edit_text(
                            status_message,
                            f"📦 **Splitting large file...**\n\n"
                            f"📁 `{file_name[:40]}{'...' if len(file_name) > 40 else ''}`\n"
                            f"📊 [{progress_bar}] {percent:.1f}%\n"
                            f"📄 Creating part {part_num}/{num_parts}\n"
                            f"💾 {get_readable_size(total_read)} / {get_readable_size(file_size)}"
                        )
                        await asyncio.sleep(0.1)  # Allow other tasks to run
            
            if bytes_for_this_part > 0:
                parts.append(part_path)
                part_num += 1
    
    return parts


async def upload_file(client: Client, file_path: str, channel_id: int, status_message: Message):
    """Upload a single file to Telegram channel"""
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    
    # If file is larger than 2GB, split it into parts
    if file_size > MAX_SPLIT_SIZE:
        await safe_edit_text(status_message, f"📦 File `{file_name}` is larger than 2GB. Splitting into parts...")
        
        try:
            # Split the file (memory-efficient with progress)
            parts = await split_file(file_path, status_message)
            total_parts = len(parts)
            
            await safe_edit_text(status_message, f"✅ Split into {total_parts} parts. Starting upload...")
            
            uploaded_parts = 0
            failed_parts = 0
            
            for i, part_path in enumerate(parts, 1):
                part_name = os.path.basename(part_path)
                part_size = os.path.getsize(part_path)
                
                await safe_edit_text(
                    status_message, 
                    f"📤 Uploading part {i}/{total_parts}: `{part_name}`\n"
                    f"📁 Size: {get_readable_size(part_size)}"
                )
                
                # Upload part as document
                max_retries = 3
                success = False
                for attempt in range(max_retries):
                    try:
                        await client.send_document(
                            chat_id=channel_id,
                            document=part_path,
                            caption=f"📄 {part_name} (Part {i}/{total_parts})",
                            progress=progress_callback,
                            progress_args=(status_message, f"📤 Uploading: `{part_name}` (Part {i}/{total_parts})")
                        )
                        success = True
                        uploaded_parts += 1
                        break
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except Exception as e:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(3)
                        else:
                            print(f"Failed to upload part {part_name}: {e}")
                            failed_parts += 1
                
                # Delete the part file after upload
                try:
                    os.remove(part_path)
                except Exception:
                    pass
            
            # Delete original file after all parts uploaded
            try:
                os.remove(file_path)
            except Exception:
                pass
            
            if failed_parts > 0:
                return False, f"Failed to upload {failed_parts} parts"
            return True, None
            
        except Exception as e:
            return False, f"Failed to split file: {str(e)}"
    
    # Generate unique upload ID for cancellation
    upload_id = f"up_{int(time.time())}_{os.path.basename(file_path)[:20]}"
    active_uploads[upload_id] = {"cancelled": False, "file": file_name}
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Check if cancelled before starting
            if active_uploads.get(upload_id, {}).get("cancelled"):
                if upload_id in active_uploads:
                    del active_uploads[upload_id]
                return False, "Upload cancelled"
            
            await safe_edit_text(status_message, f"📤 Uploading: `{file_name}`\n📁 Size: {get_readable_size(file_size)}")
            
            # Determine file type and upload accordingly
            if file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
                await client.send_video(
                    chat_id=channel_id,
                    video=file_path,
                    thumb=custom_thumb if custom_thumb and os.path.exists(custom_thumb) else None,
                    caption=f"📹 {file_name}",
                    progress=progress_callback,
                    progress_args=(status_message, f"📤 Uploading video: `{file_name}`", upload_id)
                )
            elif file_name.lower().endswith(('.mp3', '.flac', '.wav', '.aac', '.ogg')):
                await client.send_audio(
                    chat_id=channel_id,
                    audio=file_path,
                    caption=f"🎵 {file_name}",
                    progress=progress_callback,
                    progress_args=(status_message, f"📤 Uploading audio: `{file_name}`", upload_id)
                )
            elif file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                await client.send_photo(
                    chat_id=channel_id,
                    photo=file_path,
                    caption=f"🖼️ {file_name}"
                )
            else:
                await client.send_document(
                    chat_id=channel_id,
                    document=file_path,
                    caption=f"📄 {file_name}",
                    progress=progress_callback,
                    progress_args=(status_message, f"📤 Uploading file: `{file_name}`", upload_id)
                )
            
            # Clean up tracking
            if upload_id in active_uploads:
                del active_uploads[upload_id]
            
            return True, None
        except FloodWait as e:
            wait_time = e.value
            await safe_edit_text(status_message, f"⏳ Telegram rate limit. Waiting {wait_time}s... (Attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(wait_time)
        except Exception as e:
            error_msg = str(e)
            print(f"Upload error: {error_msg}")
            if attempt < max_retries - 1:
                await safe_edit_text(status_message, f"⚠️ Upload failed, retrying... ({attempt + 1}/{max_retries})\nError: {error_msg[:100]}")
                await asyncio.sleep(3)
            else:
                await safe_edit_text(status_message, f"❌ Failed to upload `{file_name}`:\n{error_msg[:200]}")
                return False, error_msg
    
    return False, "Max retries exceeded"


async def upload_directory(client: Client, dir_path: str, channel_id: int, status_message: Message):
    """Upload all files in a directory to Telegram channel"""
    uploaded = 0
    failed = 0
    
    for root, dirs, files in os.walk(dir_path):
        for file in files:
            file_path = os.path.join(root, file)
            success, error = await upload_file(client, file_path, channel_id, status_message)
            if success:
                uploaded += 1
            else:
                failed += 1
    
    return uploaded, failed


def delete_path(path: str):
    """Delete file or directory"""
    try:
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
        return True
    except Exception as e:
        print(f"Failed to delete {path}: {e}")
        return False


@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    """Handle /start command"""
    uptime = get_readable_time(int(time.time() - BOT_START_TIME))
    await message.reply_text(
        "🚀 **Torrent Leech Bot**\n\n"
        "High-speed torrent downloader with aria2!\n\n"
        "**📥 Download Commands:**\n"
        "• `/leech <magnet>` - Download torrent\n"
        "• `/ytdl <url>` - Download from YouTube\n\n"
        "**📊 Status Commands:**\n"
        "• `/status` - Active downloads\n"
        "• `/stats` - System statistics\n"
        "• `/speedtest` - Server speed test\n\n"
        "**🛠️ Settings:**\n"
        "• `/channel` - View/change upload channel\n"
        "• `/setthumb` - Set custom thumbnail\n\n"
        "**🔧 Control:**\n"
        "• `/cancel` - Cancel all downloads\n\n"
        f"⏱️ **Uptime:** {uptime}"
    )


@app.on_message(filters.command("channel"))
async def channel_command(client: Client, message: Message):
    """View current channel and show instructions to change"""
    global current_channel
    
    if not is_authorized(message.from_user.id):
        await message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    # Show current channel and instructions
    try:
        chat = await client.get_chat(current_channel)
        channel_name = chat.title or "Unknown"
        channel_info = f"**Name:** {channel_name}\n**ID:** `{current_channel}`"
    except Exception:
        channel_info = f"**ID:** `{current_channel}`"
    
    await message.reply_text(
        f"📺 **Current Upload Channel**\n\n"
        f"{channel_info}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"**📝 To change the upload channel:**\n\n"
        f"1️⃣ Add the bot as **Admin** to your channel\n"
        f"2️⃣ Forward any message from that channel here\n"
        f"3️⃣ Click the button to set it as upload channel"
    )


@app.on_message(filters.forwarded & filters.private)
async def forwarded_message_handler(client: Client, message: Message):
    """Handle forwarded messages to detect and set channel"""
    if not is_authorized(message.from_user.id):
        return
    
    # Check if message is forwarded from a channel
    if message.forward_from_chat and message.forward_from_chat.type.value == "channel":
        channel = message.forward_from_chat
        channel_id = channel.id
        channel_name = channel.title or "Unknown Channel"
        
        # Create button to set this channel
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "✅ Set as Upload Channel", 
                callback_data=f"setchannel_{channel_id}"
            )],
            [InlineKeyboardButton(
                "❌ Cancel", 
                callback_data="cancel_setchannel"
            )]
        ])
        
        await message.reply_text(
            f"📺 **Channel Detected!**\n\n"
            f"**Name:** {channel_name}\n"
            f"**ID:** `{channel_id}`\n\n"
            f"⚠️ Make sure the bot is added as **Admin** with permission to post messages.\n\n"
            f"Click below to set this as your upload channel:",
            reply_markup=keyboard
        )


@app.on_callback_query(filters.regex(r"^setchannel_"))
async def set_channel_callback(client: Client, callback_query: CallbackQuery):
    """Handle set channel button click"""
    global current_channel
    
    user_id = callback_query.from_user.id
    
    if not is_authorized(user_id):
        await callback_query.answer("❌ You are not authorized!", show_alert=True)
        return
    
    # Extract channel ID from callback data
    channel_id = int(callback_query.data.replace("setchannel_", ""))
    
    try:
        # Verify bot has access to the channel
        chat = await client.get_chat(channel_id)
        channel_name = chat.title or "Unknown"
        
        # Try to get bot's permissions in the channel
        try:
            me = await client.get_me()
            member = await client.get_chat_member(channel_id, me.id)
            
            if member.status.value not in ["administrator", "creator"]:
                await callback_query.answer("⚠️ Bot is not an admin in this channel!", show_alert=True)
                await callback_query.message.edit_text(
                    f"❌ **Failed to set channel**\n\n"
                    f"The bot is not an **Admin** in **{channel_name}**.\n\n"
                    f"Please add the bot as admin with permission to post messages, then try again."
                )
                return
        except Exception:
            pass  # Some channels might not allow this check
        
        # Set the new channel
        old_channel = current_channel
        current_channel = channel_id
        
        await callback_query.answer("✅ Channel updated!", show_alert=False)
        await callback_query.message.edit_text(
            f"✅ **Upload Channel Updated!**\n\n"
            f"**Channel:** {channel_name}\n"
            f"**ID:** `{channel_id}`\n\n"
            f"All uploads will now go to this channel."
        )
        
    except Exception as e:
        await callback_query.answer("❌ Failed to access channel!", show_alert=True)
        await callback_query.message.edit_text(
            f"❌ **Failed to set channel**\n\n"
            f"Error: {str(e)[:100]}\n\n"
            f"Make sure the bot is added as admin to the channel."
        )


@app.on_callback_query(filters.regex(r"^cancel_setchannel$"))
async def cancel_setchannel_callback(client: Client, callback_query: CallbackQuery):
    """Handle cancel button for channel selection"""
    await callback_query.answer("Cancelled", show_alert=False)
    await callback_query.message.edit_text("❌ Channel selection cancelled.")


@app.on_message(filters.command("setthumb"))
async def setthumb_command(client: Client, message: Message):
    """Set custom thumbnail for uploads"""
    global custom_thumb
    
    if not is_authorized(message.from_user.id):
        await message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    # Check if replying to a photo
    if message.reply_to_message and message.reply_to_message.photo:
        try:
            # Download the photo
            photo = message.reply_to_message.photo
            await client.download_media(photo.file_id, file_name=THUMB_PATH)
            custom_thumb = THUMB_PATH
            
            await message.reply_photo(
                photo=THUMB_PATH,
                caption="✅ **Thumbnail Set!**\n\nThis thumbnail will be used for all video uploads."
            )
        except Exception as e:
            await message.reply_text(f"❌ Failed to save thumbnail: {str(e)[:100]}")
        return
    
    # Show current thumbnail status with buttons
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Delete Thumbnail", callback_data="delthumb")]
    ])
    
    if custom_thumb and os.path.exists(custom_thumb):
        await message.reply_photo(
            photo=custom_thumb,
            caption=(
                "🖼️ **Current Thumbnail**\n\n"
                "To set a new thumbnail:\n"
                "Reply to a photo with `/setthumb`"
            ),
            reply_markup=keyboard
        )
    else:
        await message.reply_text(
            "🖼️ **No Thumbnail Set**\n\n"
            "To set a thumbnail:\n"
            "1️⃣ Send a photo\n"
            "2️⃣ Reply to it with `/setthumb`"
        )


@app.on_callback_query(filters.regex(r"^delthumb$"))
async def delete_thumb_callback(client: Client, callback_query: CallbackQuery):
    """Handle delete thumbnail button"""
    global custom_thumb
    
    if not is_authorized(callback_query.from_user.id):
        await callback_query.answer("❌ You are not authorized!", show_alert=True)
        return
    
    try:
        if custom_thumb and os.path.exists(custom_thumb):
            os.remove(custom_thumb)
        custom_thumb = None
        
        await callback_query.answer("✅ Thumbnail deleted!", show_alert=False)
        await callback_query.message.edit_caption(
            "✅ **Thumbnail Deleted!**\n\n"
            "Uploads will now use default thumbnails."
        )
    except Exception as e:
        await callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


@app.on_callback_query(filters.regex(r"^cancelup_"))
async def cancel_upload_callback(client: Client, callback_query: CallbackQuery):
    """Handle cancel upload button click"""
    if not is_authorized(callback_query.from_user.id):
        await callback_query.answer("❌ You are not authorized!", show_alert=True)
        return
    
    # Extract upload ID from callback data
    upload_id = callback_query.data.replace("cancelup_", "")
    
    if upload_id in active_uploads:
        active_uploads[upload_id]["cancelled"] = True
        await callback_query.answer("⏹️ Cancelling upload...", show_alert=False)
        await callback_query.message.edit_text("❌ **Upload Cancelled!**")
    else:
        await callback_query.answer("⚠️ Upload not found or already completed.", show_alert=True)


@app.on_message(filters.command("status"))
async def status_command(client: Client, message: Message):
    """Check aria2 status"""
    if not is_authorized(message.from_user.id):
        await message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    if not aria2:
        await message.reply_text("❌ aria2 is not connected.")
        return
    
    try:
        stats = aria2.get_stats()
        downloads = aria2.get_downloads()
        active = [d for d in downloads if d.is_active]
        
        text = (
            "📊 **Aria2 Status**\n\n"
            f"🌐 Download Speed: {get_readable_size(stats.download_speed)}/s\n"
            f"📤 Upload Speed: {get_readable_size(stats.upload_speed)}/s\n"
            f"📥 Active Downloads: {len(active)}\n\n"
        )
        
        for d in active[:5]:
            progress = d.progress
            text += (
                f"📁 `{d.name[:30]}...`\n"
                f"   Progress: {progress:.1f}% | Speed: {get_readable_size(d.download_speed)}/s\n"
                f"   ETA: {get_readable_time(d.eta.total_seconds()) if d.eta else 'Unknown'}\n\n"
            )
        
        await message.reply_text(text)
    except Exception as e:
        await message.reply_text(f"❌ Error getting status: {e}")


@app.on_message(filters.command("cancel"))
async def cancel_command(client: Client, message: Message):
    """Cancel all downloads"""
    if not is_authorized(message.from_user.id):
        await message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    if not aria2:
        await message.reply_text("❌ aria2 is not connected.")
        return
    
    try:
        downloads = aria2.get_downloads()
        cancelled = 0
        for d in downloads:
            if d.is_active or d.is_waiting:
                d.remove(force=True)
                cancelled += 1
        
        # Also mark active downloads as cancelled
        for gid in list(active_downloads.keys()):
            active_downloads[gid]["cancelled"] = True
        
        await message.reply_text(f"✅ Cancelled {cancelled} download(s).")
    except Exception as e:
        await message.reply_text(f"❌ Error cancelling downloads: {e}")


@app.on_message(filters.command("stats"))
async def stats_command(client: Client, message: Message):
    """Show system statistics"""
    if not is_authorized(message.from_user.id):
        await message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    uptime = get_readable_time(int(time.time() - BOT_START_TIME))
    
    if PSUTIL_AVAILABLE:
        # CPU Info
        cpu_percent = psutil.cpu_percent(interval=0.5)
        cpu_count = psutil.cpu_count()
        
        # Memory Info
        memory = psutil.virtual_memory()
        mem_total = get_readable_size(memory.total)
        mem_used = get_readable_size(memory.used)
        mem_free = get_readable_size(memory.available)
        mem_percent = memory.percent
        
        # Disk Info
        disk = psutil.disk_usage(DOWNLOAD_DIR)
        disk_total = get_readable_size(disk.total)
        disk_used = get_readable_size(disk.used)
        disk_free = get_readable_size(disk.free)
        disk_percent = disk.percent
        
        # Network (if available)
        try:
            net = psutil.net_io_counters()
            net_sent = get_readable_size(net.bytes_sent)
            net_recv = get_readable_size(net.bytes_recv)
            net_info = f"\n\n🌐 **Network:**\n📤 Sent: {net_sent}\n📥 Received: {net_recv}"
        except Exception:
            net_info = ""
        
        text = (
            f"📊 **System Statistics**\n\n"
            f"💻 **CPU:** {cpu_percent}% ({cpu_count} cores)\n\n"
            f"🧠 **RAM:** {mem_percent}%\n"
            f"├ Used: {mem_used}\n"
            f"├ Free: {mem_free}\n"
            f"└ Total: {mem_total}\n\n"
            f"💾 **Disk:** {disk_percent}%\n"
            f"├ Used: {disk_used}\n"
            f"├ Free: {disk_free}\n"
            f"└ Total: {disk_total}"
            f"{net_info}\n\n"
            f"⏱️ **Uptime:** {uptime}\n"
            f"🖥️ **Platform:** {platform.system()} {platform.release()}"
        )
    else:
        text = (
            f"📊 **Bot Statistics**\n\n"
            f"⏱️ **Uptime:** {uptime}\n"
            f"🖥️ **Platform:** {platform.system()} {platform.release()}\n\n"
            f"⚠️ Install `psutil` for detailed stats"
        )
    
    await message.reply_text(text)


@app.on_message(filters.command("speedtest"))
async def speedtest_command(client: Client, message: Message):
    """Run server speed test"""
    if not is_authorized(message.from_user.id):
        await message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    status_msg = await message.reply_text("🚀 Running speed test... This may take a minute.")
    
    try:
        # Run speedtest-cli
        result = subprocess.run(
            ["speedtest-cli", "--simple"],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        if result.returncode == 0:
            output = result.stdout.strip()
            # Parse output
            lines = output.split('\n')
            ping = lines[0] if len(lines) > 0 else "N/A"
            download = lines[1] if len(lines) > 1 else "N/A"
            upload = lines[2] if len(lines) > 2 else "N/A"
            
            await safe_edit_text(
                status_msg,
                f"🚀 **Speed Test Results**\n\n"
                f"📶 {ping}\n"
                f"📥 {download}\n"
                f"📤 {upload}"
            )
        else:
            await safe_edit_text(status_msg, f"❌ Speed test failed: {result.stderr}")
    except FileNotFoundError:
        await safe_edit_text(status_msg, "❌ speedtest-cli not installed.\nRun: `pip install speedtest-cli`")
    except subprocess.TimeoutExpired:
        await safe_edit_text(status_msg, "❌ Speed test timed out.")
    except Exception as e:
        await safe_edit_text(status_msg, f"❌ Error: {str(e)}")


@app.on_message(filters.command("ytdl"))
async def ytdl_command(client: Client, message: Message):
    """Download from YouTube using yt-dlp"""
    if not is_authorized(message.from_user.id):
        await message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    if len(message.command) < 2:
        await message.reply_text(
            "❌ Please provide a URL.\n\n"
            "Usage: `/ytdl <youtube_url>`"
        )
        return
    
    url = message.command[1]
    status_msg = await message.reply_text("📥 Starting YouTube download...")
    
    try:
        # Check if yt-dlp is available
        result = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            await safe_edit_text(status_msg, "❌ yt-dlp not installed.\nRun: `pip install yt-dlp`")
            return
        
        # Download video
        output_template = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
        process = subprocess.run(
            ["yt-dlp", "-f", "best", "-o", output_template, url],
            capture_output=True,
            text=True,
            timeout=600
        )
        
        if process.returncode == 0:
            # Find the downloaded file
            for f in os.listdir(DOWNLOAD_DIR):
                file_path = os.path.join(DOWNLOAD_DIR, f)
                if os.path.isfile(file_path):
                    await safe_edit_text(status_msg, f"✅ Downloaded: `{f}`\n📤 Uploading to Telegram...")
                    
                    # Upload to channel
                    success, error = await upload_file(client, file_path, current_channel, status_msg)
                    if success:
                        delete_path(file_path)
                        await safe_edit_text(status_msg, f"✅ **Done!**\n📁 `{f}`")
                    else:
                        await safe_edit_text(status_msg, f"❌ Failed to upload: `{f}`\n{error[:100] if error else ''}")
                    return
            
            await safe_edit_text(status_msg, "⚠️ Download completed but file not found.")
        else:
            await safe_edit_text(status_msg, f"❌ Download failed:\n```{process.stderr[:500]}```")
    except subprocess.TimeoutExpired:
        await safe_edit_text(status_msg, "❌ Download timed out (10 min limit).")
    except Exception as e:
        await safe_edit_text(status_msg, f"❌ Error: {str(e)}")


@app.on_message(filters.command("leech"))
async def leech_command(client: Client, message: Message):
    """Download torrent from magnet link and upload to Telegram"""
    if not is_authorized(message.from_user.id):
        await message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    if not aria2:
        await message.reply_text("❌ aria2 is not connected. Please start aria2 first.")
        return
    
    # Get magnet link from message
    if len(message.command) < 2:
        await message.reply_text(
            "❌ Please provide a magnet link.\n\n"
            "Usage: `/leech magnet:?xt=urn:btih:...`"
        )
        return
    
    magnet_link = " ".join(message.command[1:])
    
    if not magnet_link.startswith("magnet:"):
        await message.reply_text("❌ Invalid magnet link. Must start with `magnet:`")
        return
    
    status_msg = await message.reply_text("📥 Adding torrent to aria2...")
    
    try:
        # Extract info hash from magnet link to check for duplicates
        import re
        info_hash_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet_link)
        if info_hash_match:
            info_hash = info_hash_match.group(1).lower()
            # Remove any existing downloads with the same info hash
            existing_downloads = aria2.get_downloads()
            for d in existing_downloads:
                if d.info_hash and d.info_hash.lower() == info_hash:
                    try:
                        d.remove(force=True, files=True)
                        await safe_edit_text(status_msg, f"🔄 Removed existing download, adding fresh...")
                    except Exception:
                        pass
        
        # Add download to aria2 with optimized options
        download_options = {
            "dir": DOWNLOAD_DIR,
            "bt-tracker": TRACKERS_STRING,
            "bt-enable-lpd": "true",
            "enable-peer-exchange": "true",
            "bt-max-peers": "0",  # Unlimited peers
            "max-connection-per-server": "16",
            "split": "16",
            "min-split-size": "1M",
            "seed-time": "0",  # Don't seed after download
            "bt-request-peer-speed-limit": "0",  # No speed limit for peers
        }
        download = aria2.add_magnet(magnet_link, options=download_options)
        start_time = time.time()
        
        # Track this download
        active_downloads[download.gid] = {
            "message": status_msg,
            "start_time": start_time,
            "cancelled": False,
            "user_id": message.from_user.id
        }
        
        await safe_edit_text(
            status_msg,
            f"✅ Torrent added!\n\n"
            f"📁 Fetching metadata...\n"
            f"🆔 GID: `{download.gid}`",
            reply_markup=get_cancel_button(download.gid)
        )
        
        # Wait for metadata and download to complete
        last_update = 0
        while not download.is_complete:
            await asyncio.sleep(3)
            
            # Check if cancelled
            if download.gid in active_downloads and active_downloads[download.gid]["cancelled"]:
                try:
                    download.remove(force=True, files=True)
                except Exception:
                    pass
                await safe_edit_text(status_msg, "❌ **Download Cancelled!**")
                if download.gid in active_downloads:
                    del active_downloads[download.gid]
                return
            
            download.update()
            
            if download.has_failed:
                await safe_edit_text(status_msg, f"❌ Download failed: {download.error_message}")
                if download.gid in active_downloads:
                    del active_downloads[download.gid]
                return
            
            # Update progress every 5 seconds
            current_time = asyncio.get_event_loop().time()
            if current_time - last_update >= 5:
                last_update = current_time
                
                if download.name:
                    progress = download.progress
                    progress_bar = "█" * int(progress // 10) + "░" * (10 - int(progress // 10))
                    eta_str = get_readable_time(int(download.eta.total_seconds())) if download.eta else "-"
                    elapsed = get_elapsed_time(start_time)
                    
                    # Get active download count
                    active_count = len([d for d in aria2.get_downloads() if d.is_active])
                    
                    status_text = (
                        f"📥 **Downloading...**\n\n"
                        f"📁 `{download.name[:50]}{'...' if len(download.name) > 50 else ''}`\n"
                        f"📊 [{progress_bar}] {progress:.1f}%\n"
                        f"📦 Size: {get_readable_size(download.total_length)}\n"
                        f"⚡ Speed: {get_readable_size(download.download_speed)}/s\n"
                        f"⏱️ ETA: {eta_str}\n"
                        f"🌱 Seeders: {download.connections}\n"
                        f"⏳ Elapsed: {elapsed}\n"
                        f"🔧 Engine: Aria2 v1.37.0"
                    )
                    
                    await safe_edit_text(
                        status_msg,
                        status_text,
                        reply_markup=get_cancel_button(download.gid)
                    )
        
        # Remove from active downloads
        if download.gid in active_downloads:
            del active_downloads[download.gid]
        
        # Download complete - find the actual downloaded files
        actual_path = None
        
        # Method 1: Check aria2's reported file paths
        if download.files:
            for file in download.files:
                if file.path and os.path.exists(file.path):
                    actual_path = file.path
                    break
        
        # Method 2: Check for folder with torrent name in DOWNLOAD_DIR
        if not actual_path or not os.path.exists(actual_path):
            torrent_folder = os.path.join(DOWNLOAD_DIR, download.name)
            if os.path.exists(torrent_folder):
                actual_path = torrent_folder
        
        # Method 3: Scan DOWNLOAD_DIR for any new files/folders
        if not actual_path or not os.path.exists(actual_path):
            for item in os.listdir(DOWNLOAD_DIR):
                item_path = os.path.join(DOWNLOAD_DIR, item)
                # Skip aria2 control files
                if not item.endswith('.aria2'):
                    actual_path = item_path
                    break
        
        # If still not found, report error
        if not actual_path or not os.path.exists(actual_path):
            await safe_edit_text(status_msg, f"❌ Error: Downloaded files not found in {DOWNLOAD_DIR}")
            if download.gid in active_downloads:
                del active_downloads[download.gid]
            return
        
        await safe_edit_text(
            status_msg,
            f"✅ Download complete!\n\n"
            f"📁 `{download.name}`\n"
            f"📦 Size: {get_readable_size(download.total_length)}\n\n"
            f"📤 Starting upload to Telegram..."
        )
        
        # Upload to Telegram
        if os.path.isdir(actual_path):
            uploaded, failed = await upload_directory(client, actual_path, current_channel, status_msg)
            
            await safe_edit_text(
                status_msg,
                f"✅ **Upload Complete!**\n\n"
                f"📁 `{download.name}`\n"
                f"✅ Uploaded: {uploaded} file(s)\n"
                f"❌ Failed: {failed} file(s)\n\n"
                f"🗑️ Cleaning up..."
            )
        else:
            success, error = await upload_file(client, actual_path, current_channel, status_msg)
            if success:
                await safe_edit_text(
                    status_msg,
                    f"✅ **Upload Complete!**\n\n"
                    f"📁 `{download.name}`\n"
                    f"📦 Size: {get_readable_size(download.total_length)}\n\n"
                    f"🗑️ Cleaning up..."
                )
            else:
                # Error already shown by upload_file, don't overwrite it
                return
        
        # Delete files after upload
        await asyncio.sleep(2)
        if delete_path(actual_path):
            await safe_edit_text(
                status_msg,
                f"✅ **All Done!**\n\n"
                f"📁 `{download.name}`\n"
                f"📦 Size: {get_readable_size(download.total_length)}\n"
                f"🗑️ Files cleaned up!"
            )
        else:
            await safe_edit_text(
                status_msg,
                f"✅ **Upload Complete!**\n\n"
                f"📁 `{download.name}`\n"
                f"⚠️ Failed to delete files from server."
            )
        
        # Remove download from aria2
        download.remove()
        
    except Exception as e:
        await safe_edit_text(status_msg, f"❌ Error: {str(e)}")


@app.on_message(filters.regex(r"^magnet:\?"))
async def magnet_handler(client: Client, message: Message):
    """Handle magnet links sent directly"""
    if not is_authorized(message.from_user.id):
        await message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    # Treat as leech command
    message.command = ["leech", message.text]
    await leech_command(client, message)


@app.on_callback_query(filters.regex(r"^cancel_"))
async def cancel_callback(client: Client, callback_query: CallbackQuery):
    """Handle cancel button clicks"""
    user_id = callback_query.from_user.id
    
    if not is_authorized(user_id):
        await callback_query.answer("❌ You are not authorized!", show_alert=True)
        return
    
    # Extract GID from callback data
    gid = callback_query.data.replace("cancel_", "")
    
    # Check if download exists
    if gid in active_downloads:
        # Only allow the user who started the download or owner to cancel
        if active_downloads[gid]["user_id"] != user_id and user_id not in AUTHORIZED_USERS:
            await callback_query.answer("❌ You didn't start this download!", show_alert=True)
            return
        
        # Mark as cancelled
        active_downloads[gid]["cancelled"] = True
        await callback_query.answer("✅ Cancelling download...", show_alert=False)
    else:
        # Try to find and remove it directly from aria2
        try:
            downloads = aria2.get_downloads()
            for d in downloads:
                if d.gid == gid:
                    d.remove(force=True, files=True)
                    await callback_query.answer("✅ Download cancelled!", show_alert=True)
                    await safe_edit_text(callback_query.message, "❌ **Download Cancelled!**")
                    return
        except Exception:
            pass
        
        await callback_query.answer("⚠️ Download not found or already completed.", show_alert=True)

# ===================== STARTUP =====================
if __name__ == "__main__":
    print("🚀 Starting Torrent Leech Bot...")
    
    if not init_aria2():
        print("\n⚠️ Please start aria2 with RPC enabled and try again.")
        print("Run: aria2c --enable-rpc --rpc-listen-all=true --rpc-allow-origin-all --max-connection-per-server=16 --split=16 --min-split-size=1M")
        exit(1)
    
    print("✅ Bot is running!")
    app.run()
