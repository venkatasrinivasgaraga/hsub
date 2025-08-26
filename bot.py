import os
import math
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

# ------------------- CONFIG -------------------
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
SESSION_STRING = os.getenv("SESSION_STRING")

# ------------------- GLOBALS -------------------
user_queues = {}         # per-user queue system
wait_for_subtitle = {}   # user_id -> video_path waiting for subs
MAX_UPLOAD = 2_000_000_000
SESSION_MODE = False

# ------------------- CLIENT INIT -------------------
if BOT_TOKEN:
    # Normal Bot Mode → max upload 2GB
    app = Client("hardsub-audio-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
    MAX_UPLOAD = 2_000_000_000
else:
    # User Session Mode
    app = Client("hardsub-audio-bot", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)
    SESSION_MODE = True

    async def check_premium():
        async with app:
            me = await app.get_me()
            if getattr(me, "is_premium", False):
                print("✅ Premium session detected: 4GB upload enabled")
                return 4_000_000_000
            print("⚠️ Non-premium session: only 2GB upload supported")
            return 2_000_000_000

    MAX_UPLOAD = asyncio.get_event_loop().run_until_complete(check_premium())

# ------------------- FILE UPLOAD -------------------
async def split_and_send(message: Message, file_path: str, caption=""):
    file_size = os.path.getsize(file_path)

    if file_size <= MAX_UPLOAD:
        await message.reply_document(document=file_path, caption=caption)
        return

    if file_size > 4_000_000_000:
        await message.reply_text("❌ File too large. Max 4GB supported by Telegram.")
        return

    # Split for 2GB limit
    part_size = 2_000_000_000
    total_parts = math.ceil(file_size / part_size)

    await message.reply_text(f"📂 Splitting file into {total_parts} parts (2GB each)...")

    with open(file_path, "rb") as f:
        for i in range(total_parts):
            part_name = f"{file_path}.part{i+1}"
            with open(part_name, "wb") as chunk:
                chunk.write(f.read(part_size))

            await message.reply_document(
                document=part_name,
                caption=f"{caption}\nPart {i+1}/{total_parts}"
            )
            os.remove(part_name)

    await message.reply_text("✅ Upload complete.")

# ------------------- PROCESSING -------------------
async def hardsub(file_path, subtitle_path, output_path):
    cmd = [
        "ffmpeg", "-i", file_path, "-vf", f"subtitles={subtitle_path}",
        "-c:a", "copy", output_path
    ]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.communicate()

async def remove_audio(file_path, output_path, tracks_to_remove):
    map_cmds = []
    for track in tracks_to_remove:
        map_cmds.extend(["-map", f"-0:a:{track}"])
    cmd = ["ffmpeg", "-i", file_path, "-c", "copy"] + map_cmds + [output_path]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.communicate()

# ------------------- QUEUE HANDLER -------------------
async def process_queue(user_id):
    while user_queues.get(user_id):
        message, file_path, mode, extra = user_queues[user_id].pop(0)

        if mode == "hardsub":
            subtitle_path = extra
            output_path = f"{file_path}_hardsub.mp4"
            await hardsub(file_path, subtitle_path, output_path)
            await split_and_send(message, output_path, caption="🎬 Hardsubbed Video")
            os.remove(output_path)

        elif mode == "audio_remove":
            output_path = f"{file_path}_noaudio.mp4"
            await remove_audio(file_path, output_path, extra)
            await split_and_send(message, output_path, caption="🎵 Audio Removed Video")
            os.remove(output_path)

        if os.path.exists(file_path):
            os.remove(file_path)

# ------------------- COMMAND HANDLERS -------------------
@app.on_message(filters.private & filters.document)
async def file_handler(client, message):
    user_id = message.from_user.id

    # If waiting for subtitle
    if user_id in wait_for_subtitle:
        video_path = wait_for_subtitle.pop(user_id)
        subtitle_path = await message.download()
        if user_id not in user_queues:
            user_queues[user_id] = []
        user_queues[user_id].append((message, video_path, "hardsub", subtitle_path))
        if len(user_queues[user_id]) == 1:
            await process_queue(user_id)
        return

    # Otherwise treat as video
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 HardSub", callback_data="hardsub"),
        InlineKeyboardButton("🎵 Remove Audio", callback_data="audio_remove")
    ]])
    await message.reply("Choose an action:", reply_markup=keyboard)

@app.on_callback_query()
async def callback_handler(client, callback_query):
    message = callback_query.message.reply_to_message
    user_id = message.from_user.id
    file_path = await message.download()

    if callback_query.data == "hardsub":
        wait_for_subtitle[user_id] = file_path
        await callback_query.message.reply("📂 Send subtitle file (srt/ass) now")

    elif callback_query.data == "audio_remove":
        if user_id not in user_queues:
            user_queues[user_id] = []
        # Example: remove track 1
        user_queues[user_id].append((message, file_path, "audio_remove", [1]))
        if len(user_queues[user_id]) == 1:
            await process_queue(user_id)

# ------------------- START -------------------
app.run()
