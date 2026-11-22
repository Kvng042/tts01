import asyncio
import datetime
import logging
import os
import shutil
import sqlite3
import tempfile

import edge_tts
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ttsbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.getenv("DB_PATH", "bot.db")
FREE_DAILY_CHARS = int(os.getenv("FREE_DAILY_CHARS", "2000"))
MAX_CHARS_PER_REQUEST = int(os.getenv("MAX_CHARS_PER_REQUEST", "1000"))
MAX_PARALLEL_JOBS = int(os.getenv("MAX_PARALLEL_JOBS", "2"))

HAS_FFMPEG = shutil.which("ffmpeg") is not None
job_slots = asyncio.Semaphore(MAX_PARALLEL_JOBS)

# language label -> list of (button label, edge-tts voice id)
VOICES = {
    "Nigerian English": [("Ezinne (F)", "en-NG-EzinneNeural"), ("Abeo (M)", "en-NG-AbeoNeural")],
    "US English": [("Aria (F)", "en-US-AriaNeural"), ("Guy (M)", "en-US-GuyNeural")],
    "UK English": [("Sonia (F)", "en-GB-SoniaNeural"), ("Ryan (M)", "en-GB-RyanNeural")],
    "Swahili": [("Zuri (F)", "sw-KE-ZuriNeural"), ("Rafiki (M)", "sw-KE-RafikiNeural")],
    "Zulu": [("Thando (F)", "zu-ZA-ThandoNeural"), ("Themba (M)", "zu-ZA-ThembaNeural")],
    "Amharic": [("Mekdes (F)", "am-ET-MekdesNeural"), ("Ameha (M)", "am-ET-AmehaNeural")],
    "French": [("Denise (F)", "fr-FR-DeniseNeural"), ("Henri (M)", "fr-FR-HenriNeural")],
    "Arabic": [("Salma (F)", "ar-EG-SalmaNeural"), ("Shakir (M)", "ar-EG-ShakirNeural")],
}
LANGS = list(VOICES.keys())
ALL_VOICE_IDS = {vid for lst in VOICES.values() for _, vid in lst}
DEFAULT_VOICE = "en-NG-EzinneNeural"

SPEEDS = {"Slow": -25, "Normal": 0, "Fast": 25, "Faster": 50}


# ---------- database ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                voice TEXT DEFAULT 'en-NG-EzinneNeural',
                speed INTEGER DEFAULT 0,
                used_chars INTEGER DEFAULT 0,
                day TEXT DEFAULT '',
                created TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )


def get_user(user_id: int) -> sqlite3.Row:
    today = datetime.date.today().isoformat()
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id, day) VALUES (?, ?)", (user_id, today))
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row["day"] != today:  # new day, reset the counter
            conn.execute("UPDATE users SET used_chars = 0, day = ? WHERE user_id = ?", (today, user_id))
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row


def set_field(user_id: int, field: str, value):
    assert field in ("voice", "speed", "used_chars")
    with db() as conn:
        conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))


# ---------- audio ----------
async def synthesize(text: str, voice: str, speed: int):
    """Returns (path, is_voice_note). Caller must delete the file."""
    rate = f"{speed:+d}%"
    tmp_dir = tempfile.mkdtemp()
    mp3 = os.path.join(tmp_dir, "out.mp3")
    await edge_tts.Communicate(text, voice, rate=rate).save(mp3)

    if not HAS_FFMPEG:
        return mp3, False

    ogg = os.path.join(tmp_dir, "out.ogg")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", mp3, "-c:a", "libopus", "-b:a", "32k", ogg,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode == 0 and os.path.exists(ogg):
        return ogg, True
    return mp3, False


def cleanup(path: str):
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)


# ---------- handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_user(update.effective_user.id)
    await update.message.reply_text(
        "Hi! I turn text into speech.\n\n"
        "Just send me any text and I'll reply with a voice note.\n\n"
        "/voice - choose language and voice\n"
        "/speed - change speaking speed\n"
        "/usage - see your daily limit\n"
        "/help - how to use me"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send or forward any text (up to "
        f"{MAX_CHARS_PER_REQUEST} characters) and I'll speak it.\n"
        f"Free plan: {FREE_DAILY_CHARS} characters per day.\n\n"
        "/voice - pick a voice\n/speed - pick a speed\n/usage - check your limit"
    )


async def usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    left = max(0, FREE_DAILY_CHARS - u["used_chars"])
    await update.message.reply_text(
        f"Used today: {u['used_chars']}/{FREE_DAILY_CHARS} characters\nRemaining: {left}"
    )


async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = [[InlineKeyboardButton(name, callback_data=f"l:{i}")] for i, name in enumerate(LANGS)]
    await update.message.reply_text("Choose a language:", reply_markup=InlineKeyboardMarkup(rows))


async def speed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = [InlineKeyboardButton(label, callback_data=f"s:{val}") for label, val in SPEEDS.items()]
    await update.message.reply_text("Choose speaking speed:", reply_markup=InlineKeyboardMarkup([row]))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kind, _, value = q.data.partition(":")
    user_id = q.from_user.id
    get_user(user_id)

    if kind == "l":
        name = LANGS[int(value)]
        buttons = [InlineKeyboardButton(label, callback_data=f"v:{vid}") for label, vid in VOICES[name]]
        await q.edit_message_text(f"{name}: pick a voice", reply_markup=InlineKeyboardMarkup([buttons]))
    elif kind == "v" and value in ALL_VOICE_IDS:
        set_field(user_id, "voice", value)
        await q.edit_message_text("Voice saved. Send me some text!")
    elif kind == "s" and int(value) in SPEEDS.values():
        set_field(user_id, "speed", int(value))
        await q.edit_message_text("Speed saved. Send me some text!")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    user_id = update.effective_user.id
    u = get_user(user_id)

    if len(text) > MAX_CHARS_PER_REQUEST:
        await update.message.reply_text(
            f"That's {len(text)} characters. Please keep it under {MAX_CHARS_PER_REQUEST}."
        )
        return

    left = FREE_DAILY_CHARS - u["used_chars"]
    if len(text) > left:
        await update.message.reply_text(
            f"Not enough daily characters left ({max(left, 0)} remaining). Your limit resets tomorrow."
        )
        return

    await context.bot.send_chat_action(update.effective_chat.id, "record_voice")
    path = None
    try:
        async with job_slots:
            path, is_voice = await synthesize(text, u["voice"], u["speed"])
        with open(path, "rb") as f:
            if is_voice:
                await update.message.reply_voice(f)
            else:
                await update.message.reply_audio(f, title="Speech")
        set_field(user_id, "used_chars", u["used_chars"] + len(text))
    except Exception:
        log.exception("TTS failed")
        await update.message.reply_text("Sorry, something went wrong. Please try again in a moment.")
    finally:
        if path:
            cleanup(path)


def main():
    init_db()
    if not HAS_FFMPEG:
        log.warning("ffmpeg not found: sending MP3 audio instead of voice notes")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("usage", usage))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("speed", speed_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
