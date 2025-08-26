import os, asyncio, subprocess
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

app = Client("hardsub-audio-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

user_data = {}
user_queues = {}
active_jobs = {}  # track currently processing jobs

# ========== Get audio tracks ==========
def get_audio_tracks(video_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index:stream_tags=language",
        "-of", "csv=p=0",
        video_path
    ]
    result = subprocess.check_output(cmd).decode().strip().split("\n")
    tracks = []
    for line in result:
        if not line:
            continue
        parts = line.split(",")
        idx = parts[0]
        lang = parts[1] if len(parts) > 1 else "und"
        tracks.append((idx, lang))
    return tracks

# ========== Worker ==========
async def process_queue(user_id: int):
    while not user_queues[user_id].empty():
        job = await user_queues[user_id].get()
        mode = job["mode"]
        video_path = job["video"]
        output_path = f"output_{user_id}.mp4"

        active_jobs[user_id] = True  # mark as processing

        try:
            if mode == "hardsub":
                subtitle_path = job["subs"]
                await app.send_message(job["chat"], "🎬 Processing hard-sub, please wait...", reply_to_message_id=job["msg"])
                cmd = f'ffmpeg -i "{video_path}" -vf subtitles="{subtitle_path}" -c:a copy "{output_path}" -y'
                os.system(cmd)

            elif mode == "audio":
                keep_tracks = job["keep"]
                await app.send_message(job["chat"], "🎧 Removing selected audio tracks...", reply_to_message_id=job["msg"])
                maps = "-map 0:v "
                for t in keep_tracks:
                    maps += f"-map 0:a:{t} "
                cmd = f'ffmpeg -i "{video_path}" {maps} -c copy "{output_path}" -y'
                os.system(cmd)

            await app.send_video(job["chat"], output_path, caption="✅ Processing done!", reply_to_message_id=job["msg"])

        except Exception as e:
            await app.send_message(job["chat"], f"❌ Error: {e}", reply_to_message_id=job["msg"])

        finally:
            for f in [video_path, output_path, job.get("subs")]:
                if f and os.path.exists(f): os.remove(f)
            user_data.pop(user_id, None)
            active_jobs.pop(user_id, None)  # clear active status

        user_queues[user_id].task_done()

# ========== Handlers ==========
@app.on_message(filters.document | filters.video)
async def ask_options(client, message):
    user_id = message.from_user.id
    video_path = await message.download()
    user_data[user_id] = {"video_path": video_path}

    buttons = [
        [InlineKeyboardButton("🎬 HardSub", callback_data="choose_hardsub")],
        [InlineKeyboardButton("🎧 Audio Remove", callback_data="choose_audio")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")]
    ]
    await message.reply_text("⚙ Choose what you want to do with this file:",
                             reply_markup=InlineKeyboardMarkup(buttons))

@app.on_callback_query()
async def handle_callbacks(client, query):
    user_id = query.from_user.id
    data = query.data

    # --- Cancel Job ---
    if data == "cancel_job":
        if user_id in active_jobs:
            return await query.answer("⚠ Already processing, cannot cancel!", show_alert=True)
        if user_id in user_queues and not user_queues[user_id].empty():
            user_queues[user_id]._queue.clear()
            user_data.pop(user_id, None)
            return await query.edit_message_text("❌ Job cancelled successfully.")
        return await query.answer("⚠ No pending job to cancel.", show_alert=True)

    # --- HardSub Mode ---
    if data == "choose_hardsub":
        user_data[user_id]["mode"] = "hardsub"
        await query.edit_message_text("📥 Please send me your .srt or .ass subtitle file now.\n\n❌ You can cancel anytime.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")]]))

    # --- Audio Remove Mode ---
    elif data == "choose_audio":
        video = user_data[user_id]["video_path"]
        tracks = get_audio_tracks(video)
        user_data[user_id]["tracks"] = tracks
        user_data[user_id]["keep"] = []

        buttons = [[InlineKeyboardButton(f"Keep {lang} (id:{idx})", callback_data=f"keep_{idx}")]
                   for idx, lang in tracks]
        buttons.append([InlineKeyboardButton("✅ Done", callback_data="audio_done")])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")])

        await query.edit_message_text("🎧 Select audio tracks to KEEP:",
                                      reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("keep_"):
        idx = data.split("_")[1]
        if idx not in user_data[user_id]["keep"]:
            user_data[user_id]["keep"].append(idx)
        await query.answer(f"Selected audio {idx}")

    elif data == "audio_done":
        video = user_data[user_id]["video_path"]
        keep = user_data[user_id]["keep"]

        if not keep:
            return await query.answer("⚠ You must select at least one track!", show_alert=True)

        if user_id not in user_queues:
            user_queues[user_id] = asyncio.Queue()

        await user_queues[user_id].put({
            "mode": "audio",
            "video": video,
            "keep": keep,
            "chat": query.message.chat.id,
            "msg": query.message.id
        })

        await query.edit_message_text("📌 Added to queue for Audio Cleaning",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")]]))
        if user_queues[user_id].qsize() == 1:
            asyncio.create_task(process_queue(user_id))

# --- Subtitles handler (only after choosing HardSub) ---
@app.on_message(filters.document & (filters.file_extension("srt") | filters.file_extension("ass")))
async def handle_subtitles(client, message):
    user_id = message.from_user.id
    if "mode" not in user_data.get(user_id, {}) or user_data[user_id]["mode"] != "hardsub":
        return await message.reply_text("⚠ Please first send a video and select HardSub option!")

    subs_path = await message.download()
    video = user_data[user_id]["video_path"]

    if user_id not in user_queues:
        user_queues[user_id] = asyncio.Queue()

    await user_queues[user_id].put({
        "mode": "hardsub",
        "video": video,
        "subs": subs_path,
        "chat": message.chat.id,
        "msg": message.id
    })

    await message.reply_text("📌 Added to queue for HardSub",
                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")]]))
    if user_queues[user_id].qsize() == 1:
        asyncio.create_task(process_queue(user_id))

print("🤖 Bot with Inline Options + Cancel running...")
app.run()
