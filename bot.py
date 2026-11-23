import asyncio
import csv
import datetime
import html
import io
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from urllib.parse import quote
from zoneinfo import ZoneInfo

import edge_tts
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    ChatMember,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ttsbot")
HTML = ParseMode.HTML
esc = html.escape

# ====================== CONFIG ======================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.getenv("ADMIN_ID", "6884927602"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/+Yo9epgwk0mw1MTVk")
_cid = os.getenv("CHANNEL_ID", "").strip()
CHANNEL_ID = int(_cid) if _cid else None  # needed to check channel membership

FREE_DAILY_CHARS = int(os.getenv("FREE_DAILY_CHARS", "2000"))
CHANNEL_DAILY_CHARS = int(os.getenv("CHANNEL_DAILY_CHARS", "3000"))
MAX_CHARS_PER_REQUEST = int(os.getenv("MAX_CHARS_PER_REQUEST", "1000"))
REF_BONUS_CHARS = int(os.getenv("REF_BONUS_CHARS", "1500"))
MAX_PARALLEL_JOBS = int(os.getenv("MAX_PARALLEL_JOBS", "2"))

try:
    TZ = ZoneInfo(os.getenv("TIMEZONE", "Africa/Lagos"))
except Exception:
    TZ = datetime.timezone.utc

HAS_FFMPEG = shutil.which("ffmpeg") is not None
job_slots = asyncio.Semaphore(MAX_PARALLEL_JOBS)
WELCOME_IMG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome.jpg")

_welcome_file_id = None
_member_cache = {}  # user_id -> (is_member, timestamp)
busy_users = set()
broadcast_running = False

# ====================== VOICES ======================
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
VOICE_LABEL = {vid: f"{lang} - {label}" for lang, lst in VOICES.items() for label, vid in lst}
DEFAULT_VOICE = "en-NG-EzinneNeural"

SPEEDS = {"Slow": -25, "Normal": 0, "Fast": 25, "Faster": 50}
SPEED_LABEL = {v: k for k, v in SPEEDS.items()}

# ====================== BOT UI TRANSLATIONS ======================
# Missing keys fall back to English. Please have native speakers review fr/ha/yo.
LANG_NAMES = {"en": "English", "fr": "Français", "ha": "Hausa", "yo": "Yorùbá"}

STRINGS = {
    "en": {
        "welcome": (
            "🎙 <b>Welcome to the Text-to-Speech Bot, {name}!</b>\n\n"
            "Turn any text into natural-sounding speech in seconds.\n\n"
            "• Many voices and African accents\n"
            "• Adjustable speed\n"
            "• Voice notes you can forward anywhere\n\n"
            "Tap <b>Convert text to speech</b> to begin."
        ),
        "btn_convert": "🎙 Convert text to speech",
        "btn_lang": "🌐 Change language",
        "btn_help": "❓ Help",
        "btn_channel": "📢 Update channel",
        "btn_voice": "🗣 Change voice",
        "btn_speed": "⏩ Change speed",
        "btn_invite": "🎁 Invite & Earn",
        "btn_home": "🏠 Home",
        "btn_join": "📢 Join channel for more daily usage",
        "send_text": "✍️ Send me the text you want converted to speech.\n\nVoice: <b>{voice}</b> • Speed: <b>{speed}</b>",
        "help_text": (
            "<b>How to use me</b>\n\n"
            "1. Send or forward any text and I'll reply with a voice note.\n"
            "2. /voice - choose the speech language and voice\n"
            "3. /speed - change speaking speed\n"
            "4. /usage - see your daily limit\n"
            "5. /referral - invite friends and earn bonus characters\n"
            "6. /language - change the bot's menu language\n\n"
            "Limits: {max} characters per message. {free} characters per day, "
            "or {channel} if you have joined our update channel."
        ),
        "choose_lang": (
            "🌐 Choose your language for the bot menus.\n\n"
            "This doesn't change the voice used for speech. Use /voice for that."
        ),
        "lang_saved": "✅ Language updated.",
        "too_long": "That text is {n} characters. Please keep it under {max}.",
        "limit_reached": (
            "⚠️ You've reached your daily limit. You have {left} characters left, "
            "and the limit resets tomorrow.\n\n"
            "Invite friends for bonus characters, or join our update channel for a higher daily limit."
        ),
        "busy": "⏳ Still working on your previous request. Please wait a moment.",
        "error": "Sorry, something went wrong. Please try again in a moment.",
    },
    "fr": {
        "welcome": (
            "🎙 <b>Bienvenue, {name} !</b>\n\n"
            "Transformez n'importe quel texte en voix naturelle en quelques secondes.\n\n"
            "• Nombreuses voix et accents africains\n"
            "• Vitesse réglable\n"
            "• Messages vocaux faciles à partager\n\n"
            "Appuyez sur <b>Convertir du texte en voix</b> pour commencer."
        ),
        "btn_convert": "🎙 Convertir du texte en voix",
        "btn_lang": "🌐 Changer de langue",
        "btn_help": "❓ Aide",
        "btn_channel": "📢 Canal d'actualités",
        "send_text": "✍️ Envoyez-moi le texte à convertir en voix.\n\nVoix : <b>{voice}</b> • Vitesse : <b>{speed}</b>",
        "help_text": (
            "<b>Comment m'utiliser</b>\n\n"
            "1. Envoyez ou transférez un texte et je réponds avec un message vocal.\n"
            "2. /voice - choisir la voix\n"
            "3. /speed - changer la vitesse\n"
            "4. /usage - voir votre limite du jour\n"
            "5. /referral - inviter des amis et gagner des caractères\n"
            "6. /language - changer la langue du menu\n\n"
            "Limite : {max} caractères par message."
        ),
        "choose_lang": "🌐 Choisissez la langue des menus du bot.\n\nCela ne change pas la voix. Utilisez /voice pour cela.",
        "lang_saved": "✅ Langue mise à jour.",
        "too_long": "Ce texte fait {n} caractères. Merci de rester sous {max}.",
        "limit_reached": "⚠️ Limite quotidienne atteinte. Il vous reste {left} caractères. La limite se réinitialise demain.",
    },
    "ha": {
        "welcome": (
            "🎙 <b>Barka da zuwa, {name}!</b>\n\n"
            "Ka mayar da duk wani rubutu zuwa murya cikin ɗan lokaci kaɗan.\n\n"
            "• Muryoyi da yawa da lafazin Afirka\n"
            "• Za a iya sauya gudu\n"
            "• Saƙon murya da za a iya tura wa kowa\n\n"
            "Danna <b>Mayar da rubutu zuwa murya</b> don farawa."
        ),
        "btn_convert": "🎙 Mayar da rubutu zuwa murya",
        "btn_lang": "🌐 Canja harshe",
        "btn_help": "❓ Taimako",
        "btn_channel": "📢 Tashar labarai",
        "send_text": "✍️ Aiko mini rubutun da kake son mayarwa murya.\n\nMurya: <b>{voice}</b> • Gudu: <b>{speed}</b>",
        "help_text": (
            "<b>Yadda ake amfani da ni</b>\n\n"
            "1. Aiko ko tura rubutu, zan amsa da saƙon murya.\n"
            "2. /voice - zaɓar murya\n"
            "3. /speed - zaɓar gudu\n"
            "4. /usage - ganin iyakar yau\n"
            "5. /referral - gayyatar abokai ka samu haruffa\n"
            "6. /language - canja harshen menu\n\n"
            "Iyaka: haruffa {max} a kowane saƙo."
        ),
        "choose_lang": "🌐 Zaɓi harshen menu na bot ɗin.\n\nWannan ba ya canja muryar magana. Yi amfani da /voice don haka.",
        "lang_saved": "✅ An sabunta harshe.",
        "too_long": "Rubutun nan yana da haruffa {n}. Don Allah kada ya wuce {max}.",
        "limit_reached": "⚠️ Ka kai iyakar yau. Saura haruffa {left}. Za a sabunta gobe.",
    },
    "yo": {
        "welcome": (
            "🎙 <b>Káàbọ̀, {name}!</b>\n\n"
            "Yí ọ̀rọ̀ èyíkéyìí padà sí ohùn àdánidá ní ìṣẹ́jú kan.\n\n"
            "• Ọ̀pọ̀ ohùn àti ìró Áfíríkà\n"
            "• O lè yí ìyára padà\n"
            "• Ohùn tí o lè fi ránṣẹ́ sí ẹnikẹ́ni\n\n"
            "Tẹ <b>Yí ọ̀rọ̀ padà sí ohùn</b> láti bẹ̀rẹ̀."
        ),
        "btn_convert": "🎙 Yí ọ̀rọ̀ padà sí ohùn",
        "btn_lang": "🌐 Yí èdè padà",
        "btn_help": "❓ Ìrànlọ́wọ́",
        "btn_channel": "📢 Ìkànnì ìròyìn",
        "send_text": "✍️ Fi ọ̀rọ̀ tí o fẹ́ yí sí ohùn ránṣẹ́ sí mi.\n\nOhùn: <b>{voice}</b> • Ìyára: <b>{speed}</b>",
        "help_text": (
            "<b>Bí a ṣe ń lò mí</b>\n\n"
            "1. Fi ọ̀rọ̀ ránṣẹ́, màá dáhùn pẹ̀lú ohùn.\n"
            "2. /voice - yan ohùn\n"
            "3. /speed - yan ìyára\n"
            "4. /usage - wo ààlà òní\n"
            "5. /referral - pe àwọn ọ̀rẹ́ kí o gba ẹ̀dá-ọ̀rọ̀ kún\n"
            "6. /language - yí èdè menu padà\n\n"
            "Ààlà: {max} fún ìránṣẹ́ kan."
        ),
        "choose_lang": "🌐 Yan èdè fún menu bot náà.\n\nÈyí kò yí ohùn ọ̀rọ̀ padà. Lo /voice fún ìyẹn.",
        "lang_saved": "✅ A ti yí èdè padà.",
        "too_long": "Ọ̀rọ̀ yìí jẹ́ ẹ̀dá-ọ̀rọ̀ {n}. Jọ̀wọ́ má ṣe ju {max} lọ.",
        "limit_reached": "⚠️ O ti dé ààlà òní. O ṣẹ́ku {left}. Yóò tún bẹ̀rẹ̀ lọ́la.",
    },
}


def t(lang: str, key: str, **kw) -> str:
    s = STRINGS.get(lang, {}).get(key) or STRINGS["en"][key]
    return s.format(**kw) if kw else s


# ====================== DATABASE ======================
@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def today_str() -> str:
    return datetime.datetime.now(TZ).date().isoformat()


NEW_COLS = {
    "first_name": "TEXT DEFAULT ''",
    "username": "TEXT DEFAULT ''",
    "lang": "TEXT DEFAULT 'en'",
    "joined_date": "TEXT",
    "last_active": "TEXT",
    "bonus_chars": "INTEGER DEFAULT 0",
    "referred_by": "INTEGER",
    "ref_rewarded": "INTEGER DEFAULT 0",
    "blocked": "INTEGER DEFAULT 0",
    "total_chars": "INTEGER DEFAULT 0",
}


def init_db():
    with conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                voice TEXT DEFAULT 'en-NG-EzinneNeural',
                speed INTEGER DEFAULT 0,
                used_chars INTEGER DEFAULT 0,
                day TEXT DEFAULT '',
                created TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        existing = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        for col, ddl in NEW_COLS.items():  # lets you upgrade an existing bot.db safely
            if col not in existing:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        c.execute("UPDATE users SET joined_date = substr(created, 1, 10) WHERE joined_date IS NULL")
        c.execute(
            """CREATE TABLE IF NOT EXISTS stats (
                day TEXT PRIMARY KEY,
                conversions INTEGER DEFAULT 0,
                chars INTEGER DEFAULT 0
            )"""
        )


def fetch_user(user_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def set_field(user_id: int, field: str, value):
    assert field in ("voice", "speed", "lang")
    with conn() as c:
        c.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))


def mark_blocked(user_id: int, blocked: bool = True):
    with conn() as c:
        c.execute("UPDATE users SET blocked = ? WHERE user_id = ?", (1 if blocked else 0, user_id))


def consume(user_id: int, n: int, limit: int):
    """Spend from the daily allowance first, then from bonus characters."""
    with conn() as c:
        r = c.execute("SELECT used_chars FROM users WHERE user_id = ?", (user_id,)).fetchone()
        daily_left = max(0, limit - r["used_chars"])
        from_daily = min(n, daily_left)
        from_bonus = n - from_daily
        c.execute(
            """UPDATE users SET used_chars = used_chars + ?,
               bonus_chars = MAX(0, bonus_chars - ?), total_chars = total_chars + ?
               WHERE user_id = ?""",
            (from_daily, from_bonus, n, user_id),
        )
        c.execute(
            """INSERT INTO stats (day, conversions, chars) VALUES (?, 1, ?)
               ON CONFLICT(day) DO UPDATE SET conversions = conversions + 1, chars = chars + ?""",
            (today_str(), n, n),
        )


async def ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE, referrer=None):
    """Register/refresh the user. Notifies the admin when someone is brand new."""
    tg = update.effective_user
    today = today_str()
    start_lang = (tg.language_code or "en")[:2]
    if start_lang not in LANG_NAMES:
        start_lang = "en"

    with conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO users (user_id, day, joined_date, lang) VALUES (?, ?, ?, ?)",
            (tg.id, today, today, start_lang),
        )
        is_new = cur.rowcount == 1
        if is_new and referrer and referrer != tg.id:
            if c.execute("SELECT 1 FROM users WHERE user_id = ?", (referrer,)).fetchone():
                c.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer, tg.id))
        c.execute(
            "UPDATE users SET first_name = ?, username = ?, last_active = ?, blocked = 0 WHERE user_id = ?",
            (tg.first_name or "", tg.username or "", today, tg.id),
        )
        row = c.execute("SELECT * FROM users WHERE user_id = ?", (tg.id,)).fetchone()
        if row["day"] != today:  # new day: reset the daily counter
            c.execute("UPDATE users SET used_chars = 0, day = ? WHERE user_id = ?", (today, tg.id))
            row = c.execute("SELECT * FROM users WHERE user_id = ?", (tg.id,)).fetchone()

    if is_new:
        await notify_new_user(context, row)
    return row


async def notify_new_user(context, u):
    if u["user_id"] == ADMIN_ID:
        return
    today = today_str()
    with conn() as c:
        new_today = c.execute("SELECT COUNT(*) FROM users WHERE joined_date = ?", (today,)).fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        recent = c.execute(
            "SELECT first_name, username, user_id FROM users WHERE joined_date = ? ORDER BY rowid DESC LIMIT 5",
            (today,),
        ).fetchall()

    name = esc(u["first_name"] or "No name")
    uname = f"@{esc(u['username'])}" if u["username"] else "none"
    ref = f"<code>{u['referred_by']}</code>" if u["referred_by"] else "Organic"
    recent_lines = "\n".join(
        f"• {esc(r['first_name'] or 'No name')} ({'@' + esc(r['username']) if r['username'] else r['user_id']})"
        for r in recent
    )
    text = (
        "🆕 <b>New user joined</b>\n\n"
        f"👤 Name: <a href=\"tg://user?id={u['user_id']}\">{name}</a>\n"
        f"🔖 Username: {uname}\n"
        f"🆔 ID: <code>{u['user_id']}</code>\n"
        f"🌍 Language: {u['lang']}\n"
        f"🔗 Referred by: {ref}\n\n"
        "📈 <b>Today's summary</b>\n"
        f"New users today: <b>{new_today}</b>\n"
        f"Total users: <b>{total}</b>\n\n"
        f"<b>Latest joiners today:</b>\n{recent_lines}"
    )
    try:
        await context.bot.send_message(ADMIN_ID, text, parse_mode=HTML)
    except TelegramError:
        log.exception("Could not notify admin")

    # tell the referrer someone joined with their link
    if u["referred_by"]:
        try:
            await context.bot.send_message(
                u["referred_by"],
                f"👋 <b>{name}</b> joined using your invite link. You'll get "
                f"<b>{REF_BONUS_CHARS}</b> bonus characters once they convert their first text.",
                parse_mode=HTML,
            )
        except TelegramError:
            pass


async def maybe_reward_referrer(context, u):
    if not u["referred_by"] or u["ref_rewarded"]:
        return
    with conn() as c:
        cur = c.execute(
            "UPDATE users SET ref_rewarded = 1 WHERE user_id = ? AND ref_rewarded = 0", (u["user_id"],)
        )
        if cur.rowcount != 1:
            return
        c.execute(
            "UPDATE users SET bonus_chars = bonus_chars + ? WHERE user_id = ?",
            (REF_BONUS_CHARS, u["referred_by"]),
        )
    try:
        await context.bot.send_message(
            u["referred_by"],
            f"🎉 <b>{esc(u['first_name'] or 'Your friend')}</b> just used the bot. "
            f"You earned <b>{REF_BONUS_CHARS}</b> bonus characters!",
            parse_mode=HTML,
        )
    except TelegramError:
        pass


# ====================== CHANNEL MEMBERSHIP ======================
async def is_channel_member(bot, user_id: int) -> bool:
    if not CHANNEL_ID:
        return False
    now = time.time()
    cached = _member_cache.get(user_id)
    if cached and now - cached[1] < (600 if cached[0] else 30):
        return cached[0]
    try:
        m = await bot.get_chat_member(CHANNEL_ID, user_id)
        ok = m.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER) or (
            m.status == ChatMember.RESTRICTED and getattr(m, "is_member", False)
        )
    except TelegramError as e:
        log.warning("Channel check failed (is the bot an admin of the channel?): %s", e)
        return False
    _member_cache[user_id] = (ok, now)
    return ok


# ====================== AUDIO ======================
async def synthesize(text: str, voice: str, speed: int):
    """Returns (path, is_voice_note). Caller must call cleanup(path)."""
    tmp_dir = tempfile.mkdtemp()
    mp3 = os.path.join(tmp_dir, "out.mp3")
    await edge_tts.Communicate(text, voice, rate=f"{speed:+d}%").save(mp3)
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


# ====================== KEYBOARDS / SCREENS ======================
def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


def home_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_convert"), callback_data="menu:tts")],
        [
            InlineKeyboardButton(t(lang, "btn_lang"), callback_data="menu:lang"),
            InlineKeyboardButton(t(lang, "btn_help"), callback_data="menu:help"),
        ],
        [InlineKeyboardButton(t(lang, "btn_channel"), url=CHANNEL_URL)],
    ])


async def send_home(bot, chat_id: int, u):
    """Welcome picture + caption + the 4 buttons."""
    global _welcome_file_id
    lang = u["lang"] or "en"
    caption = t(lang, "welcome", name=esc(u["first_name"] or "friend"))
    kb = home_keyboard(lang)
    try:
        if _welcome_file_id:
            await bot.send_photo(chat_id, _welcome_file_id, caption=caption, parse_mode=HTML, reply_markup=kb)
            return
        if os.path.exists(WELCOME_IMG):
            with open(WELCOME_IMG, "rb") as f:
                msg = await bot.send_photo(chat_id, f, caption=caption, parse_mode=HTML, reply_markup=kb)
            _welcome_file_id = msg.photo[-1].file_id  # reuse, no re-upload next time
            return
    except Forbidden:
        raise
    except TelegramError:
        log.exception("Welcome photo failed, falling back to text")
    await bot.send_message(chat_id, caption, parse_mode=HTML, reply_markup=kb)


async def send_voice_menu(bot, chat_id: int):
    buttons = [InlineKeyboardButton(n, callback_data=f"l:{i}") for i, n in enumerate(LANGS)]
    await bot.send_message(
        chat_id, "🗣 Choose a language for the speech voice:",
        reply_markup=InlineKeyboardMarkup(chunk(buttons, 2)),
    )


async def send_speed_menu(bot, chat_id: int):
    row = [InlineKeyboardButton(label, callback_data=f"s:{val}") for label, val in SPEEDS.items()]
    await bot.send_message(chat_id, "⏩ Choose speaking speed:", reply_markup=InlineKeyboardMarkup([row]))


async def send_ui_lang_menu(bot, chat_id: int, lang: str):
    buttons = [InlineKeyboardButton(name, callback_data=f"ui:{code}") for code, name in LANG_NAMES.items()]
    await bot.send_message(chat_id, t(lang, "choose_lang"), reply_markup=InlineKeyboardMarkup(chunk(buttons, 2)))


async def send_help(bot, chat_id: int, lang: str):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(t(lang, "btn_invite"), callback_data="menu:ref"),
        InlineKeyboardButton(t(lang, "btn_home"), callback_data="menu:home"),
    ]])
    text = t(lang, "help_text", max=MAX_CHARS_PER_REQUEST, free=FREE_DAILY_CHARS, channel=CHANNEL_DAILY_CHARS)
    await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)


async def send_referral(bot, chat_id: int, user_id: int):
    u = fetch_user(user_id)
    link = f"https://t.me/{bot.username}?start=ref_{user_id}"
    with conn() as c:
        joined = c.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)).fetchone()[0]
        qualified = c.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by = ? AND ref_rewarded = 1", (user_id,)
        ).fetchone()[0]
    share = "https://t.me/share/url?url={}&text={}".format(
        quote(link), quote("Turn text into speech with many voices and African accents 🎙")
    )
    text = (
        "🎁 <b>Invite & Earn</b>\n\n"
        f"Share your link. When a friend joins and converts their first text, you get "
        f"<b>{REF_BONUS_CHARS}</b> bonus characters. Bonus characters don't reset daily.\n\n"
        f"🔗 Your link:\n<code>{link}</code>\n\n"
        f"👥 Friends joined: <b>{joined}</b>\n"
        f"✅ Qualified (rewarded): <b>{qualified}</b>\n"
        f"💰 Bonus balance: <b>{u['bonus_chars']}</b> characters"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Share my link", url=share)]])
    await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)


# ====================== USER COMMANDS ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ref = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            ref = int(context.args[0][4:])
        except ValueError:
            pass
    u = await ensure_user(update, context, referrer=ref)
    await send_home(context.bot, update.effective_chat.id, u)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await ensure_user(update, context)
    await send_help(context.bot, update.effective_chat.id, u["lang"])


async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update, context)
    await send_voice_menu(context.bot, update.effective_chat.id)


async def speed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update, context)
    await send_speed_menu(context.bot, update.effective_chat.id)


async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await ensure_user(update, context)
    await send_ui_lang_menu(context.bot, update.effective_chat.id, u["lang"])


async def referral_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await ensure_user(update, context)
    await send_referral(context.bot, update.effective_chat.id, u["user_id"])


async def usage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await ensure_user(update, context)
    member = await is_channel_member(context.bot, u["user_id"])
    limit = CHANNEL_DAILY_CHARS if member else FREE_DAILY_CHARS
    daily_left = max(0, limit - u["used_chars"])
    plan = f"Channel member ({limit}/day)" if member else f"Standard ({limit}/day)"
    text = (
        "📊 <b>Your usage today</b>\n\n"
        f"Plan: <b>{plan}</b>\n"
        f"Used today: <b>{u['used_chars']}/{limit}</b>\n"
        f"Daily characters left: <b>{daily_left}</b>\n"
        f"Bonus characters: <b>{u['bonus_chars']}</b>\n"
        f"Available now: <b>{daily_left + u['bonus_chars']}</b>"
    )
    rows = []
    if not member:
        text += f"\n\n📢 Join our update channel to raise your daily limit to <b>{CHANNEL_DAILY_CHARS}</b>."
        rows.append([InlineKeyboardButton("📢 Join update channel", url=CHANNEL_URL)])
    rows.append([InlineKeyboardButton(t(u["lang"], "btn_invite"), callback_data="menu:ref")])
    await update.message.reply_text(text, parse_mode=HTML, reply_markup=InlineKeyboardMarkup(rows))


# ====================== BUTTON CALLBACKS ======================
async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = await ensure_user(update, context)
    lang = u["lang"]
    chat_id = update.effective_chat.id
    action = q.data.split(":", 1)[1]

    if action == "tts":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "btn_voice"), callback_data="menu:voice"),
            InlineKeyboardButton(t(lang, "btn_speed"), callback_data="menu:speed"),
        ]])
        text = t(
            lang, "send_text",
            voice=VOICE_LABEL.get(u["voice"], u["voice"]),
            speed=SPEED_LABEL.get(u["speed"], "Normal"),
        )
        await context.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)
    elif action == "voice":
        await send_voice_menu(context.bot, chat_id)
    elif action == "speed":
        await send_speed_menu(context.bot, chat_id)
    elif action == "lang":
        await send_ui_lang_menu(context.bot, chat_id, lang)
    elif action == "help":
        await send_help(context.bot, chat_id, lang)
    elif action == "ref":
        await send_referral(context.bot, chat_id, u["user_id"])
    elif action == "home":
        await send_home(context.bot, chat_id, u)


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = await ensure_user(update, context)
    uid = u["user_id"]
    kind, _, value = q.data.partition(":")

    if kind == "l":
        try:
            name = LANGS[int(value)]
        except (ValueError, IndexError):
            return
        buttons = [InlineKeyboardButton(label, callback_data=f"v:{vid}") for label, vid in VOICES[name]]
        await q.edit_message_text(f"{name}: pick a voice", reply_markup=InlineKeyboardMarkup([buttons]))
    elif kind == "v" and value in ALL_VOICE_IDS:
        set_field(uid, "voice", value)
        await q.edit_message_text(f"✅ Voice saved: {VOICE_LABEL[value]}\n\nNow send me some text!")
    elif kind == "s":
        try:
            speed = int(value)
        except ValueError:
            return
        if speed in SPEED_LABEL:
            set_field(uid, "speed", speed)
            await q.edit_message_text(f"✅ Speed saved: {SPEED_LABEL[speed]}\n\nNow send me some text!")
    elif kind == "ui" and value in LANG_NAMES:
        set_field(uid, "lang", value)
        await q.edit_message_text(t(value, "lang_saved"))
        await send_home(context.bot, update.effective_chat.id, fetch_user(uid))


# ====================== TEXT -> SPEECH ======================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    u = await ensure_user(update, context)
    uid, lang = u["user_id"], u["lang"]

    if uid in busy_users:
        await update.message.reply_text(t(lang, "busy"))
        return
    if len(text) > MAX_CHARS_PER_REQUEST:
        await update.message.reply_text(t(lang, "too_long", n=len(text), max=MAX_CHARS_PER_REQUEST))
        return

    member = await is_channel_member(context.bot, uid)
    limit = CHANNEL_DAILY_CHARS if member else FREE_DAILY_CHARS
    available = max(0, limit - u["used_chars"]) + u["bonus_chars"]
    if len(text) > available:
        rows = []
        if not member:
            rows.append([InlineKeyboardButton(t(lang, "btn_join"), url=CHANNEL_URL)])
        rows.append([InlineKeyboardButton(t(lang, "btn_invite"), callback_data="menu:ref")])
        await update.message.reply_text(
            t(lang, "limit_reached", left=available), reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    busy_users.add(uid)
    path = None
    try:
        await context.bot.send_chat_action(update.effective_chat.id, "record_voice")
        async with job_slots:
            path, is_voice = await synthesize(text, u["voice"], u["speed"])
        with open(path, "rb") as f:
            if is_voice:
                await update.message.reply_voice(f)
            else:
                await update.message.reply_audio(f, title="Speech")
        consume(uid, len(text), limit)
        await maybe_reward_referrer(context, u)
    except Exception:
        log.exception("TTS failed")
        await update.message.reply_text(t(lang, "error"))
    finally:
        busy_users.discard(uid)
        if path:
            cleanup(path)


# ====================== ADMIN DASHBOARD ======================
def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


def dashboard_text() -> str:
    today = today_str()
    week_ago = (datetime.date.fromisoformat(today) - datetime.timedelta(days=6)).isoformat()
    with conn() as c:
        one = lambda sql, *a: c.execute(sql, a).fetchone()[0] or 0
        total = one("SELECT COUNT(*) FROM users")
        blocked = one("SELECT COUNT(*) FROM users WHERE blocked = 1")
        new_today = one("SELECT COUNT(*) FROM users WHERE joined_date = ?", today)
        new_week = one("SELECT COUNT(*) FROM users WHERE joined_date >= ?", week_ago)
        active_today = one("SELECT COUNT(*) FROM users WHERE last_active = ?", today)
        ref_total = one("SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL")
        ref_ok = one("SELECT COUNT(*) FROM users WHERE ref_rewarded = 1")
        st = c.execute("SELECT conversions, chars FROM stats WHERE day = ?", (today,)).fetchone()
        st_all = c.execute("SELECT SUM(conversions), SUM(chars) FROM stats").fetchone()
    now = datetime.datetime.now(TZ).strftime("%H:%M:%S")
    return (
        f"📊 <b>Admin Dashboard</b>  <i>({now})</i>\n\n"
        "👥 <b>Users</b>\n"
        f"Total: <b>{total}</b>  (active {total - blocked}, blocked {blocked})\n"
        f"New today: <b>{new_today}</b>  •  last 7 days: <b>{new_week}</b>\n"
        f"Active today: <b>{active_today}</b>\n\n"
        "🎙 <b>Usage</b>\n"
        f"Today: <b>{st['conversions'] if st else 0}</b> conversions, "
        f"<b>{st['chars'] if st else 0}</b> characters\n"
        f"All time: <b>{st_all[0] or 0}</b> conversions, <b>{st_all[1] or 0}</b> characters\n\n"
        "🔗 <b>Referrals</b>\n"
        f"Referred users: <b>{ref_total}</b>  •  rewarded: <b>{ref_ok}</b>\n\n"
        f"📢 Channel check: <b>{'on' if CHANNEL_ID else 'OFF (set CHANNEL_ID)'}</b>"
    )


def dashboard_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="adm:refresh"),
            InlineKeyboardButton("📣 Broadcast", callback_data="adm:bc"),
        ],
        [
            InlineKeyboardButton("🏆 Top referrers", callback_data="adm:top"),
            InlineKeyboardButton("🆕 Recent users", callback_data="adm:recent"),
        ],
        [InlineKeyboardButton("📁 Export users (CSV)", callback_data="adm:export")],
    ])


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(dashboard_text(), parse_mode=HTML, reply_markup=dashboard_kb())


async def on_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Not allowed", show_alert=True)
        return
    await q.answer()
    action = q.data.split(":", 1)[1]

    if action == "refresh":
        try:
            await q.edit_message_text(dashboard_text(), parse_mode=HTML, reply_markup=dashboard_kb())
        except BadRequest:
            pass
    elif action == "top":
        with conn() as c:
            rows = c.execute(
                """SELECT r.referred_by AS rid, COUNT(*) AS n, SUM(r.ref_rewarded) AS ok,
                          u.first_name AS name, u.username AS uname
                   FROM users r LEFT JOIN users u ON u.user_id = r.referred_by
                   WHERE r.referred_by IS NOT NULL
                   GROUP BY r.referred_by ORDER BY n DESC LIMIT 10"""
            ).fetchall()
        if not rows:
            text = "No referrals yet."
        else:
            lines = [
                f"{i}. {esc(r['name'] or 'Unknown')} "
                f"({'@' + esc(r['uname']) if r['uname'] else r['rid']}) - {r['n']} joined, {r['ok'] or 0} qualified"
                for i, r in enumerate(rows, 1)
            ]
            text = "🏆 <b>Top referrers</b>\n\n" + "\n".join(lines)
        await context.bot.send_message(ADMIN_ID, text, parse_mode=HTML)
    elif action == "recent":
        with conn() as c:
            rows = c.execute(
                "SELECT user_id, first_name, username, joined_date FROM users ORDER BY rowid DESC LIMIT 15"
            ).fetchall()
        lines = [
            f"• {esc(r['first_name'] or 'No name')} "
            f"({'@' + esc(r['username']) if r['username'] else 'no username'}) "
            f"<code>{r['user_id']}</code> - {r['joined_date']}"
            for r in rows
        ]
        await context.bot.send_message(
            ADMIN_ID, "🆕 <b>Recent users</b>\n\n" + ("\n".join(lines) or "None yet."), parse_mode=HTML
        )
    elif action == "export":
        with conn() as c:
            rows = c.execute(
                """SELECT user_id, first_name, username, lang, joined_date, last_active,
                          total_chars, bonus_chars, referred_by, blocked FROM users ORDER BY rowid"""
            ).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["user_id", "first_name", "username", "lang", "joined", "last_active",
                    "total_chars", "bonus_chars", "referred_by", "blocked"])
        for r in rows:
            w.writerow(list(r))
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        await context.bot.send_document(ADMIN_ID, document=data, filename="users.csv")


# ====================== BROADCAST (admin only) ======================
BC_MSG, BC_BTN, BC_CONFIRM = range(3)
BTN_RE = re.compile(r"^(.+?)\s+-\s+((?:https?://|tg://|t\.me/)\S+)$")

BUTTON_HELP = (
    "🔘 <b>Add buttons?</b> Send them like this:\n\n"
    "<code>Button text - https://example.com</code>\n\n"
    "• One line = one row of buttons\n"
    "• Two buttons on the same row: separate them with <code>|</code>\n"
    "<code>Join channel - https://t.me/yourchannel | Website - https://example.com</code>\n\n"
    "Send /skip to broadcast without buttons, or /cancel to abort."
)


def parse_buttons(text: str) -> InlineKeyboardMarkup:
    rows = []
    for line in text.strip().splitlines():
        row = []
        for part in line.split("|"):
            part = part.strip()
            if not part:
                continue
            m = BTN_RE.match(part)
            if not m:
                raise ValueError(f"Couldn't read: {part}")
            url = m.group(2)
            if url.startswith("t.me/"):
                url = "https://" + url
            row.append(InlineKeyboardButton(m.group(1).strip(), url=url))
        if row:
            rows.append(row)
    if not rows:
        raise ValueError("No buttons found.")
    return InlineKeyboardMarkup(rows)


async def bc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not is_admin(update):
        return ConversationHandler.END
    if broadcast_running:
        await context.bot.send_message(ADMIN_ID, "A broadcast is already running. Please wait for it to finish.")
        return ConversationHandler.END
    context.user_data.pop("bc", None)
    await context.bot.send_message(
        ADMIN_ID,
        "📣 <b>New broadcast</b>\n\nSend the message you want to broadcast (text, photo, video, "
        "document...). Send one message only. Use /cancel to abort.",
        parse_mode=HTML,
    )
    return BC_MSG


async def bc_got_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    context.user_data["bc"] = {"chat_id": m.chat_id, "msg_id": m.message_id, "markup": None}
    await m.reply_text(BUTTON_HELP, parse_mode=HTML, disable_web_page_preview=True)
    return BC_BTN


async def bc_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bc = context.user_data["bc"]
    await context.bot.copy_message(ADMIN_ID, bc["chat_id"], bc["msg_id"], reply_markup=bc["markup"])
    with conn() as c:
        n = c.execute("SELECT COUNT(*) FROM users WHERE blocked = 0 AND user_id != ?", (ADMIN_ID,)).fetchone()[0]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Send to {n} users", callback_data="bc:go"),
        InlineKeyboardButton("❌ Cancel", callback_data="bc:no"),
    ]])
    await context.bot.send_message(ADMIN_ID, "👆 This is how it will look. Send it now?", reply_markup=kb)
    return BC_CONFIRM


async def bc_got_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["bc"]["markup"] = parse_buttons(update.message.text)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}\n\nTry again, or send /skip or /cancel.")
        return BC_BTN
    return await bc_preview(update, context)


async def bc_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await bc_preview(update, context)


async def bc_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global broadcast_running
    q = update.callback_query
    await q.answer()
    if not is_admin(update):
        return ConversationHandler.END
    if q.data == "bc:no":
        await q.edit_message_text("Broadcast cancelled.")
        return ConversationHandler.END
    if broadcast_running:
        await q.edit_message_text("A broadcast is already running.")
        return ConversationHandler.END

    bc = context.user_data.get("bc")
    if not bc:
        await q.edit_message_text("Nothing to send. Start again with /broadcast.")
        return ConversationHandler.END

    broadcast_running = True
    await q.edit_message_text("📤 Broadcast started...")
    # run in the background so the bot keeps answering other users meanwhile
    context.application.create_task(run_broadcast(context.bot, q.message.chat_id, q.message.message_id, dict(bc)))
    return ConversationHandler.END


async def bc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("bc", None)
    await update.message.reply_text("Broadcast cancelled.")
    return ConversationHandler.END


async def run_broadcast(bot, status_chat: int, status_msg: int, bc: dict):
    global broadcast_running
    sent = failed = blocked = 0
    started = time.time()
    try:
        with conn() as c:
            ids = [r[0] for r in c.execute(
                "SELECT user_id FROM users WHERE blocked = 0 AND user_id != ?", (ADMIN_ID,)
            )]
        total = len(ids)
        for i, uid in enumerate(ids, 1):
            for _ in range(2):
                try:
                    await bot.copy_message(uid, bc["chat_id"], bc["msg_id"], reply_markup=bc["markup"])
                    sent += 1
                    break
                except RetryAfter as e:
                    wait = e.retry_after
                    wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
                    await asyncio.sleep(wait + 1)
                except Forbidden:
                    blocked += 1
                    mark_blocked(uid)
                    break
                except TelegramError:
                    failed += 1
                    break
            else:
                failed += 1
            await asyncio.sleep(0.05)  # stay well under Telegram's ~30 msgs/sec limit
            if i % 100 == 0:
                try:
                    await bot.edit_message_text(
                        f"📤 Broadcasting... {i}/{total}", chat_id=status_chat, message_id=status_msg
                    )
                except TelegramError:
                    pass
        secs = int(time.time() - started)
        await bot.send_message(
            ADMIN_ID,
            "✅ <b>Broadcast finished</b>\n\n"
            f"Delivered: <b>{sent}</b>\nBlocked the bot: <b>{blocked}</b>\nFailed: <b>{failed}</b>\n"
            f"Time: {secs}s",
            parse_mode=HTML,
        )
    except Exception:
        log.exception("Broadcast crashed")
        await bot.send_message(ADMIN_ID, f"⚠️ Broadcast stopped unexpectedly after {sent} deliveries. Check logs.")
    finally:
        broadcast_running = False


# ====================== CHAT MEMBER EVENTS ======================
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ev = update.my_chat_member
    chat, status = ev.chat, ev.new_chat_member.status
    if chat.type == "private":
        mark_blocked(chat.id, status in (ChatMember.BANNED, ChatMember.LEFT))
    elif status in (ChatMember.ADMINISTRATOR, ChatMember.MEMBER):
        # handy way to discover your channel's numeric ID
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"ℹ️ I was added to <b>{esc(chat.title or 'a chat')}</b> ({chat.type}).\n"
                f"Chat ID: <code>{chat.id}</code>\n\n"
                "If this is your update channel, set this as <code>CHANNEL_ID</code> in your environment variables.",
                parse_mode=HTML,
            )
        except TelegramError:
            pass


# ====================== STARTUP ======================
async def post_init(app: Application):
    cmds = [
        BotCommand("start", "Home"),
        BotCommand("voice", "Choose voice"),
        BotCommand("speed", "Change speed"),
        BotCommand("usage", "Daily usage"),
        BotCommand("referral", "Invite & earn"),
        BotCommand("language", "Menu language"),
        BotCommand("help", "How to use"),
    ]
    await app.bot.set_my_commands(cmds)
    try:
        await app.bot.set_my_commands(
            cmds + [BotCommand("admin", "Admin dashboard"), BotCommand("broadcast", "Send a broadcast")],
            scope=BotCommandScopeChat(ADMIN_ID),
        )
    except TelegramError:
        log.warning("Admin commands not set (admin must press /start in the bot once)")


def main():
    init_db()
    if not HAS_FFMPEG:
        log.warning("ffmpeg not found: sending MP3 audio instead of voice notes")
    if not CHANNEL_ID:
        log.warning("CHANNEL_ID not set: nobody will get the channel-member bonus yet")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(32)  # users don't wait in one single line
        .post_init(post_init)
        .build()
    )

    admin_only = filters.User(user_id=ADMIN_ID)
    broadcast_conv = ConversationHandler(
        entry_points=[
            CommandHandler("broadcast", bc_start, filters=admin_only),
            CallbackQueryHandler(bc_start, pattern=r"^adm:bc$"),
        ],
        states={
            BC_MSG: [MessageHandler(admin_only & ~filters.COMMAND, bc_got_msg)],
            BC_BTN: [
                CommandHandler("skip", bc_skip, filters=admin_only),
                MessageHandler(admin_only & filters.TEXT & ~filters.COMMAND, bc_got_buttons),
            ],
            BC_CONFIRM: [CallbackQueryHandler(bc_confirm, pattern=r"^bc:(go|no)$")],
        },
        fallbacks=[CommandHandler("cancel", bc_cancel, filters=admin_only)],
        allow_reentry=True,
    )
    app.add_handler(broadcast_conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("speed", speed_cmd))
    app.add_handler(CommandHandler("usage", usage_cmd))
    app.add_handler(CommandHandler("language", language_cmd))
    app.add_handler(CommandHandler("referral", referral_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))

    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^(l|v|s|ui):"))
    app.add_handler(CallbackQueryHandler(on_admin_cb, pattern=r"^adm:(refresh|top|recent|export)$"))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
