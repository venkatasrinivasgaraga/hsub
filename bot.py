import os
import asyncio
import subprocess
from uuid import uuid4
from typing import List, Tuple, Dict, Any

from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    CallbackQuery,
)

# =========================
# Config
# =========================
API_ID = int(os.getenv("API_ID", "25259066"))
API_HASH = os.getenv("API_HASH", "caad2cdad2fe06057f2bf8f8a8e58950")
BOT_TOKEN = os.getenv("BOT_TOKEN", "7978709069:AAF1QJdpYaQVBTYL3wPBt5okvwI16dgyysA")

# Optional: simple guard
if not API_ID or not API_HASH or not BOT_TOKEN:
    print("[WARN] Missing API_ID/API_HASH/BOT_TOKEN in environment.")

app = Client(
    "hardsub-audio-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# =========================
# State
# =========================
# Per-user temporary data (paths, chosen mode, etc.)
user_data: Dict[int, Dict[str, Any]] = {}
# Per-user FIFO queue of jobs
user_queues: Dict[int, asyncio.Queue] = {}
# Track job state per user: {user_id: {"status": "idle|running", "cancel": bool}}
active_jobs: Dict[int, Dict[str, Any]] = {}

# =========================
# Helpers
# =========================
async def run_ffmpeg(cmd: List[str]) -> None:
    """Run a blocking ffmpeg command in a thread pool with error checking."""
    def _runner():
        subprocess.run(cmd, check=True)
    await asyncio.to_thread(_runner)


def unique_output_path(user_id: int, ext: str = "mp4") -> str:
    return f"output_{user_id}_{uuid4().hex}.{ext}"


def get_ext_from_path(path: str, default: str = "mp4") -> str:
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext if ext else default


def get_audio_tracks(video_path: str) -> List[Tuple[int, str]]:
    """Return a list of (sequential_index_for_ffmpeg_map, language_tag).

    We use enumeration order for safe mapping with -map 0:a:{n}.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index:stream_tags=language",
        "-of", "csv=p=0",
        video_path,
    ]
    try:
        out = subprocess.check_output(cmd).decode().strip().split("\n")
    except subprocess.CalledProcessError:
        out = []

    tracks: List[Tuple[int, str]] = []
    for n, line in enumerate(out):
        if not line:
            continue
        parts = line.split(",")
        lang = parts[1] if len(parts) > 1 and parts[1] else "und"
        tracks.append((n, lang))  # n is the sequential map index
    return tracks


async def process_queue(user_id: int):
    """Process all queued jobs for a user in FIFO order."""
    q = user_queues[user_id]
    active_jobs[user_id] = {"status": "running", "cancel": False}

    try:
        while not q.empty():
            job = await q.get()
            if active_jobs[user_id].get("cancel"):
                # Discard remaining jobs if cancel requested
                q.task_done()
                continue

            mode = job["mode"]
            video_path = job["video"]
            chat_id = job["chat"]
            reply_msg_id = job["msg"]

            try:
                if mode == "hardsub":
                    subs_path = job["subs"]
                    await app.send_message(chat_id, "🎬 Processing hard-sub, please wait…", reply_to_message_id=reply_msg_id)

                    # Hardsub always re-encode video (subtitles filter requires it)
                    output_path = unique_output_path(user_id, "mp4")
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-i", video_path,
                        "-vf", f"subtitles={subs_path}",
                        "-c:a", "copy",
                        output_path,
                    ]

                    await run_ffmpeg(cmd)

                    await app.send_video(chat_id, output_path, caption="✅ HardSub done!", reply_to_message_id=reply_msg_id)

                elif mode == "audio":
                    keep_tracks: List[int] = job["keep"]
                    await app.send_message(chat_id, "🎧 Removing selected audio tracks…", reply_to_message_id=reply_msg_id)

                    # Copy video, copy only selected audio streams
                    in_ext = get_ext_from_path(video_path, "mp4")
                    output_path = unique_output_path(user_id, in_ext)

                    # Build -map arguments: always map all video streams, then chosen audio
                    map_args: List[str] = ["-map", "0:v"]
                    for t in keep_tracks:
                        map_args += ["-map", f"0:a:{t}"]

                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-i", video_path,
                        *map_args,
                        "-c", "copy",
                        output_path,
                    ]

                    await run_ffmpeg(cmd)

                    await app.send_document(chat_id, output_path, caption="✅ Audio cleaned!", reply_to_message_id=reply_msg_id)

            except subprocess.CalledProcessError as e:
                await app.send_message(chat_id, f"❌ FFmpeg error (code {e.returncode}).", reply_to_message_id=reply_msg_id)
            except Exception as e:
                await app.send_message(chat_id, f"❌ Error: {e}", reply_to_message_id=reply_msg_id)
            finally:
                # Cleanup outputs and temporary subtitle files
                try:
                    for f in [job.get("subs")]:
                        if f and os.path.exists(f):
                            os.remove(f)
                except Exception:
                    pass

                # NOTE: remove original video after the job finishes (we downloaded it for this job)
                try:
                    if os.path.exists(video_path):
                        os.remove(video_path)
                except Exception:
                    pass

                # Remove any outputs created during this iteration
                # We don't keep a direct reference here; rely on directory scan heuristic:
                # (Skip to keep things simple; outputs already sent, Telegram stores a copy.)

                q.task_done()

            if active_jobs[user_id].get("cancel"):
                # if cancel requested after finishing current item, stop
                break
    finally:
        # Drain queue if cancelled
        if active_jobs.get(user_id, {}).get("cancel"):
            try:
                q._queue.clear()
            except Exception:
                pass
        active_jobs[user_id] = {"status": "idle", "cancel": False}


# =========================
# Commands & Handlers
# =========================
@app.on_message(filters.command(["start"]))
async def start_cmd(_, message: Message):
    await message.reply_text(
        """
👋 Send me a video and pick an option:

• 🎬 HardSub → send .srt/.ass after choosing
• 🎧 Audio Remove → choose which audio tracks to KEEP

Commands:
/queue – show your queue status
/cancel – cancel pending jobs (and stop after current one)
        """.strip()
    )


@app.on_message(filters.command(["queue"]))
async def queue_cmd(_, message: Message):
    uid = message.from_user.id
    qsize = user_queues.get(uid).qsize() if uid in user_queues else 0
    status = active_jobs.get(uid, {}).get("status", "idle")
    await message.reply_text(f"🧾 Queue: {qsize} pending\n⚙️ Status: {status}")


@app.on_message(filters.command(["cancel"]))
async def cancel_cmd(_, message: Message):
    uid = message.from_user.id
    if uid in active_jobs and active_jobs[uid].get("status") == "running":
        active_jobs[uid]["cancel"] = True
        await message.reply_text("⏹️ Cancel requested. I will stop after the current job.")
    elif uid in user_queues and not user_queues[uid].empty():
        try:
            user_queues[uid]._queue.clear()
        except Exception:
            pass
        await message.reply_text("❌ Cleared your pending jobs.")
    else:
        await message.reply_text("ℹ️ Nothing to cancel.")


# Accept only videos (video messages or documents flagged as video)
video_filter = (filters.video | filters.document.video)


@app.on_message(video_filter)
async def ask_options(_, message: Message):
    uid = message.from_user.id

    # Optional: basic size limit check (e.g., 2.5 GB)
    size = (message.video.file_size if message.video else message.document.file_size) or 0
    max_size = 2_500_000_000
    if size > max_size:
        return await message.reply_text("⚠️ File too large for this bot. Please send a smaller file.")

    video_path = await message.download()
    user_data[uid] = {"video_path": video_path}

    buttons = [
        [InlineKeyboardButton("🎬 HardSub", callback_data="choose_hardsub")],
        [InlineKeyboardButton("🎧 Audio Remove", callback_data="choose_audio")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")],
    ]
    await message.reply_text(
        "⚙️ Choose what you want to do with this file:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@app.on_callback_query()
async def handle_callbacks(_, query: CallbackQuery):
    uid = query.from_user.id
    data = query.data

    # --- Cancel Job ---
    if data == "cancel_job":
        # If running, request graceful cancel after current job
        if uid in active_jobs and active_jobs[uid].get("status") == "running":
            active_jobs[uid]["cancel"] = True
            return await query.answer("⏹️ Will cancel after current job.", show_alert=True)

        # Otherwise, clear pending queue
        if uid in user_queues and not user_queues[uid].empty():
            try:
                user_queues[uid]._queue.clear()
            except Exception:
                pass
            user_data.pop(uid, None)
            return await query.edit_message_text("❌ Pending jobs cancelled.")
        return await query.answer("ℹ️ No pending job to cancel.", show_alert=True)

    # Require a video first
    if uid not in user_data or "video_path" not in user_data[uid]:
        return await query.answer("⚠️ Please send a video first.", show_alert=True)

    # --- HardSub Mode ---
    if data == "choose_hardsub":
        user_data[uid]["mode"] = "hardsub"
        await query.edit_message_text(
            "📥 Please send your .srt or .ass subtitle file now.\n\n❌ You can cancel anytime.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")]]),
        )

    # --- Audio Remove Mode ---
    elif data == "choose_audio":
        video = user_data[uid]["video_path"]
        tracks = get_audio_tracks(video)
        if not tracks:
            return await query.edit_message_text("⚠️ No audio tracks detected.")

        user_data[uid]["tracks"] = tracks
        user_data[uid]["keep"] = []

        buttons = [
            [InlineKeyboardButton(f"Keep {lang} (id:{idx})", callback_data=f"keep_{idx}")] for idx, lang in tracks
        ]
        buttons.append([InlineKeyboardButton("✅ Done", callback_data="audio_done")])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")])

        await query.edit_message_text(
            "🎧 Select audio tracks to KEEP:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("keep_"):
        idx = int(data.split("_", 1)[1])
        keep_list: List[int] = user_data.get(uid, {}).get("keep", [])
        if idx not in keep_list:
            keep_list.append(idx)
        user_data[uid]["keep"] = keep_list
        await query.answer(f"Selected audio {idx}")

    elif data == "audio_done":
        if "keep" not in user_data.get(uid, {}) or not user_data[uid]["keep"]:
            return await query.answer("⚠️ You must select at least one track!", show_alert=True)

        if uid not in user_queues:
            user_queues[uid] = asyncio.Queue()

        await user_queues[uid].put({
            "mode": "audio",
            "video": user_data[uid]["video_path"],
            "keep": user_data[uid]["keep"],
            "chat": query.message.chat.id,
            "msg": query.message.id,
        })

        await query.edit_message_text(
            "📌 Added to queue for Audio Cleaning",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")]]),
        )
        if user_queues[uid].qsize() == 1 and active_jobs.get(uid, {}).get("status") != "running":
            asyncio.create_task(process_queue(uid))


# --- Subtitles handler (only after choosing HardSub) ---
@app.on_message(filters.document & (filters.file_extension("srt") | filters.file_extension("ass")))
async def handle_subtitles(_, message: Message):
    uid = message.from_user.id
    if "mode" not in user_data.get(uid, {}) or user_data[uid]["mode"] != "hardsub":
        return await message.reply_text("⚠️ Please first send a video and select HardSub option!")

    subs_path = await message.download()

    if uid not in user_queues:
        user_queues[uid] = asyncio.Queue()

    await user_queues[uid].put({
        "mode": "hardsub",
        "video": user_data[uid]["video_path"],
        "subs": subs_path,
        "chat": message.chat.id,
        "msg": message.id,
    })

    await message.reply_text(
        "📌 Added to queue for HardSub",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")]]),
    )

    if user_queues[uid].qsize() == 1 and active_jobs.get(uid, {}).get("status") != "running":
        asyncio.create_task(process_queue(uid))


print("🤖 Bot with Inline Options + Cancel running…")
app.run()
