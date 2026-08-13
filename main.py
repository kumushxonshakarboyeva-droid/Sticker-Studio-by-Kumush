Import os
import re
import sqlite3
import logging
import asyncio
import subprocess
import tempfile
import threading
import shutil

from pathlib import Path
from datetime import datetime, timezone

from telegram import (
    Update,
    InputSticker,
)
from telegram.constants import StickerFormat
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_ID = int(
    os.getenv("ADMIN_ID", "0")
)

DAILY_LIMIT = int(
    os.getenv("DAILY_STICKER_LIMIT", "20")
)

DB_PATH = os.getenv(
    "DB_PATH",
    "bot.db"
)

BOT_USERNAME = os.getenv(
    "BOT_USERNAME",
    "Vid2Sticker_bot"
).replace("@", "").strip()

MAX_FILE_MB = int(
    os.getenv("MAX_FILE_MB", "50")
)

MAX_FILE_SIZE = (
    MAX_FILE_MB * 1024 * 1024
)

MAX_DURATION = int(
    os.getenv("MAX_DURATION", "3")
)

TEMP_DIR = Path(
    os.getenv(
        "TEMP_DIR",
        tempfile.gettempdir()
    )
)

TEMP_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    )
)

logger = logging.getLogger(
    "Vid2StickerBot"
)


# ============================================================
# SESSION STORAGE
# ============================================================

user_sessions = {}

session_locks = {}


def get_user_lock(user_id: int):

    if user_id not in session_locks:
        session_locks[user_id] = asyncio.Lock()

    return session_locks[user_id]


# ============================================================
# DATABASE
# ============================================================

def get_db():

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA journal_mode=WAL"
    )

    conn.execute(
        "PRAGMA busy_timeout=30000"
    )

    return conn


def init_db():

    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id INTEGER NOT NULL,
            usage_date TEXT NOT NULL,
            sticker_count INTEGER NOT NULL DEFAULT 0,

            PRIMARY KEY (
                user_id,
                usage_date
            )
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS packs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            pack_name TEXT NOT NULL UNIQUE,
            pack_title TEXT NOT NULL,
            sticker_format TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

    logger.info("Database initialized")


# ============================================================
# TIME
# ============================================================

def current_date():

    # UTC date.
    # Server timezonega bog'lanib qolmaydi.
    return datetime.now(
        timezone.utc
    ).date().isoformat()


def now_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


# ============================================================
# ADMIN
# ============================================================

def is_admin(user_id: int):

    return (
        ADMIN_ID != 0
        and user_id == ADMIN_ID
    )


# ============================================================
# USER REGISTRATION
# ============================================================

def register_user(user):

    if not user:
        return

    conn = get_db()

    timestamp = now_iso()

    conn.execute("""
        INSERT INTO users (
            user_id,
            username,
            first_name,
            last_name,
            created_at,
            last_seen
        )

        VALUES (?, ?, ?, ?, ?, ?)

        ON CONFLICT(user_id)
        DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            last_seen = excluded.last_seen
    """, (
        user.id,
        user.username,
        user.first_name,
        user.last_name,
        timestamp,
        timestamp
    ))

    conn.commit()
    conn.close()


# ============================================================
# LIMIT
# ============================================================

def get_today_usage(
    user_id: int
):

    if is_admin(user_id):
        return 0

    conn = get_db()

    row = conn.execute("""
        SELECT sticker_count

        FROM daily_usage

        WHERE user_id = ?
        AND usage_date = ?
    """, (
        user_id,
        current_date()
    )).fetchone()

    conn.close()

    if row:
        return int(
            row["sticker_count"]
        )

    return 0


def consume_sticker(
    user_id: int
):

    # ADMIN = UNLIMITED
    if is_admin(user_id):

        return True

    conn = get_db()

    try:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        today = current_date()

        row = conn.execute("""
            SELECT sticker_count

            FROM daily_usage

            WHERE user_id = ?
            AND usage_date = ?
        """, (
            user_id,
            today
        )).fetchone()

        used = (
            int(row["sticker_count"])
            if row
            else 0
        )

        if used >= DAILY_LIMIT:

            conn.rollback()

            return False

        if row:

            conn.execute("""
                UPDATE daily_usage

                SET sticker_count =
                    sticker_count + 1

                WHERE user_id = ?
                AND usage_date = ?
            """, (
                user_id,
                today
            ))

        else:

            conn.execute("""
                INSERT INTO daily_usage (
                    user_id,
                    usage_date,
                    sticker_count
                )

                VALUES (?, ?, 1)
            """, (
                user_id,
                today
            ))

        conn.commit()

        return True

    except Exception:

        conn.rollback()

        logger.exception(
            "Could not consume limit"
        )

        raise

    finally:

        conn.close()


def refund_sticker(
    user_id: int
):

    if is_admin(user_id):
        return

    conn = get_db()

    try:

        conn.execute("""
            UPDATE daily_usage

            SET sticker_count =
                CASE
                    WHEN sticker_count > 0
                    THEN sticker_count - 1
                    ELSE 0
                END

            WHERE user_id = ?
            AND usage_date = ?
        """, (
            user_id,
            current_date()
        ))

        conn.commit()

    finally:

        conn.close()


# ============================================================
# PACK DATABASE
# ============================================================

def get_pack(
    user_id: int,
    sticker_format: str
):

    conn = get_db()

    row = conn.execute("""
        SELECT *

        FROM packs

        WHERE user_id = ?
        AND sticker_format = ?

        ORDER BY id DESC

        LIMIT 1
    """, (
        user_id,
        sticker_format
    )).fetchone()

    conn.close()

    return row


def save_pack(
    user_id: int,
    pack_name: str,
    pack_title: str,
    sticker_format: str
):

    conn = get_db()

    conn.execute("""
        INSERT OR IGNORE INTO packs (
            user_id,
            pack_name,
            pack_title,
            sticker_format,
            created_at
        )

        VALUES (?, ?, ?, ?, ?)
    """, (
        user_id,
        pack_name,
        pack_title,
        sticker_format,
        now_iso()
    ))

    conn.commit()
    conn.close()


# ============================================================
# PACK NAME
# ============================================================

def generate_pack_name(
    user_id: int,
    sticker_format: str
):

    prefix = (
        "vid"
        if sticker_format == StickerFormat.VIDEO
        else "static"
    )

    # Telegram:
    # - only latin letters/digits/underscore
    # - starts with a letter
    # - ends with _by_<bot_username>
    # - max 64 chars

    name = (
        f"{prefix}_{user_id}"
        f"_by_{BOT_USERNAME}"
    )

    name = re.sub(
        r"[^A-Za-z0-9_]",
        "",
        name
    )

    name = re.sub(
        r"_+",
        "_",
        name
    )

    if not name[0].isalpha():
        name = "s_" + name

    if len(name) > 64:

        suffix = (
            f"_by_{BOT_USERNAME}"
        )

        prefix_part = (
            name[:-len(suffix)]
            if len(name) > len(suffix)
            else "vid"
        )

        max_prefix = (
            64 - len(suffix)
        )

        name = (
            prefix_part[:max_prefix]
            + suffix
        )

    return name


# ============================================================
# FILE UTILITIES
# ============================================================

def remove_file(path):

    if not path:
        return

    try:

        path = Path(path)

        if path.exists():
            path.unlink()

    except Exception:

        logger.exception(
            "Could not remove %s",
            path
        )


def cleanup_session(
    user_id: int
):

    session = user_sessions.pop(
        user_id,
        None
    )

    if not session:
        return

    remove_file(
        session.get("input")
    )

    remove_file(
        session.get("output")
    )


# ============================================================
# FFMPEG
# ============================================================

def ensure_ffmpeg():

    if shutil.which("ffmpeg") is None:

        raise RuntimeError(
            "FFmpeg topilmadi. "
            "Render/Docker serverga FFmpeg o'rnatilishi kerak."
        )


def run_ffmpeg(command):

    logger.info(
        "Running FFmpeg: %s",
        " ".join(
            map(str, command)
        )
    )

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180
    )

    if result.returncode != 0:

        logger.error(
            "FFmpeg error:\n%s",
            result.stderr[-5000:]
        )

        raise RuntimeError(
            "FFmpeg faylni qayta ishlay olmadi."
        )

    return result


# ============================================================
# IMAGE → PNG
# ============================================================

def convert_photo(
    input_path,
    output_path
):

    command = [
        "ffmpeg",
        "-y",

        "-i",
        str(input_path),

        "-vf",
        (
            "scale=512:512:"
            "force_original_aspect_ratio=decrease,"
            "pad=512:512:"
            "(ow-iw)/2:"
            "(oh-ih)/2:"
            "black"
        ),

        "-frames:v",
        "1",

        "-f",
        "image2",

        str(output_path)
    ]

    run_ffmpeg(command)


# ============================================================
# VIDEO → TRANSPARENT WEBM
# ============================================================

def convert_video(
    input_path,
    output_path
):

    command = [
        "ffmpeg",
        "-y",

        "-i",
        str(input_path),

        "-t",
        str(MAX_DURATION),

        "-vf",
        (
            "format=rgba,"
            "scale=512:512:"
            "force_original_aspect_ratio=decrease,"
            "pad=512:512:"
            "(ow-iw)/2:"
            "(oh-ih)/2:"
            "color=black@0"
        ),

        "-an",

        "-c:v",
        "libvpx-vp9",

        "-pix_fmt",
        "yuva420p",

        "-b:v",
        "0",

        "-crf",
        "35",

        "-deadline",
        "good",

        "-row-mt",
        "1",

        "-auto-alt-ref",
        "0",

        str(output_path)
    ]

    run_ffmpeg(command)


# ============================================================
# CHROMA KEY
# ============================================================

def convert_chromakey(
    input_path,
    output_path,
    color,
    similarity=0.25
):

    similarity = float(
        similarity
    )

    similarity = max(
        0.01,
        min(
            similarity,
            0.90
        )
    )

    command = [
        "ffmpeg",
        "-y",

        "-i",
        str(input_path),

        "-t",
        str(MAX_DURATION),

        "-vf",
        (
            f"format=rgba,"
            f"chromakey="
            f"{color}:"
            f"{similarity}:"
            f"0.05,"
            f"scale=512:512:"
            f"force_original_aspect_ratio=decrease,"
            f"pad=512:512:"
            f"(ow-iw)/2:"
            f"(oh-ih)/2:"
            f"color=black@0"
        ),

        "-an",

        "-c:v",
        "libvpx-vp9",

        "-pix_fmt",
        "yuva420p",

        "-b:v",
        "0",

        "-crf",
        "35",

        "-deadline",
        "good",

        "-row-mt",
        "1",

        "-auto-alt-ref",
        "0",

        str(output_path)
    ]

    run_ffmpeg(command)


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    register_user(user)

    if is_admin(user.id):

        text = (
            "👑 <b>Vid2Sticker ADMIN</b>\n\n"
            "♾️ Siz uchun limit cheksiz.\n\n"
            "📊 /stats — statistika\n"
            "👥 /users — foydalanuvchilar\n"
            "📦 /mypacks — packlar\n"
            "📈 /limit — limit"
        )

    else:

        used = get_today_usage(
            user.id
        )

        remaining = max(
            0,
            DAILY_LIMIT - used
        )

        text = (
            "👋 <b>Vid2Sticker Bot</b>\n\n"
            "🎬 Video yoki GIF yuboring.\n"
            "🖼 Rasm yuborsangiz — sticker "
            "qilib beradi.\n\n"
            f"📊 Bugungi limit: "
            f"<b>{remaining}/{DAILY_LIMIT}</b>"
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML"
    )


# ============================================================
# LIMIT COMMAND
# ============================================================

async def limit_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    register_user(user)

    if is_admin(user.id):

        await update.message.reply_text(
            "👑 <b>ADMIN</b>\n\n"
            "♾️ Siz uchun limit cheksiz.",
            parse_mode="HTML"
        )

        return

    used = get_today_usage(
        user.id
    )

    remaining = max(
        0,
        DAILY_LIMIT - used
    )

    await update.message.reply_text(
        "📊 <b>BUGUNGI LIMIT</b>\n\n"
        f"🎨 Ishlatilgan: <b>{used}</b>\n"
        f"🟢 Qolgan: <b>{remaining}</b>\n"
        f"📦 Kunlik limit: <b>{DAILY_LIMIT}</b>",
        parse_mode="HTML"
    )


# ============================================================
# MEDIA HANDLER
# ============================================================

async def handle_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    register_user(user)

    user_id = user.id

    lock = get_user_lock(
        user_id
    )

    # One conversion per user at a time.
    if lock.locked():

        await update.message.reply_text(
            "⏳ Sizning oldingi faylingiz "
            "hali qayta ishlanmoqda."
        )

        return

    async with lock:

        await handle_media_locked(
            update,
            context
        )


async def handle_media_locked(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    user_id = user.id

    input_path = None
    output_path = None

    consumed = False

    try:

        # ----------------------------------------------------
        # LIMIT
        # ----------------------------------------------------

        if not consume_sticker(
            user_id
        ):

            await update.message.reply_text(
                "⛔ <b>Bugungi limitingiz tugadi.</b>\n\n"
                f"📦 Limit: {DAILY_LIMIT} ta/kun\n"
                "🔄 Ertaga yangilanadi.",
                parse_mode="HTML"
            )

            return

        consumed = True

        # ----------------------------------------------------
        # MEDIA DETECTION
        # ----------------------------------------------------

        if update.message.video:

            media = update.message.video

            source_type = "video"

            extension = ".mp4"

        elif update.message.animation:

            media = update.message.animation

            source_type = "video"

            extension = ".gif"

        elif update.message.photo:

            media = update.message.photo[-1]

            source_type = "static"

            extension = ".jpg"

        else:

            raise RuntimeError(
                "Qo'llab-quvvatlanmaydigan fayl."
            )

        # ----------------------------------------------------
        # SIZE
        # ----------------------------------------------------

        file_size = (
            media.file_size or 0
        )

        if file_size > MAX_FILE_SIZE:

            raise RuntimeError(
                f"Fayl hajmi juda katta. "
                f"Maksimum {MAX_FILE_MB} MB."
            )

        # ----------------------------------------------------
        # FILE PATHS
        # ----------------------------------------------------

        unique_id = (
            f"vid2sticker_"
            f"{user_id}_"
            f"{update.message.message_id}"
        )

        input_path = (
            TEMP_DIR /
            f"{unique_id}{extension}"
        )

        if source_type == "video":

            output_path = (
                TEMP_DIR /
                f"{unique_id}.webm"
            )

        else:

            output_path = (
                TEMP_DIR /
                f"{unique_id}.png"
            )

        user_sessions[user_id] = {
            "input": str(input_path),
            "output": str(output_path),
            "source_type": source_type,
            "waiting_color": False
        }

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        status = await update.message.reply_text(
            "⏳ <b>Fayl yuklanmoqda...</b>",
            parse_mode="HTML"
        )

        telegram_file = (
            await media.get_file()
        )

        await telegram_file.download_to_drive(
            custom_path=str(input_path)
        )

        # ----------------------------------------------------
        # STATIC
        # ----------------------------------------------------

        if source_type == "static":

            await status.edit_text(
                "⚙️ <b>PNG sticker tayyorlanmoqda...</b>",
                parse_mode="HTML"
            )

            await asyncio.to_thread(
                convert_photo,
                input_path,
                output_path
            )

            await send_sticker(
                update,
                context,
                output_path,
                StickerFormat.STATIC
            )

            consumed = False

            return

        # ----------------------------------------------------
        # VIDEO
        # ----------------------------------------------------

        user_sessions[user_id][
            "waiting_color"
        ] = True

        await status.edit_text(
            "🎨 <b>Fon rangini tanlang:</b>\n\n"
            "🟢 Yashil\n"
            "⚪ Oq\n"
            "⚫ Qora\n"
            "🔵 Ko'k\n"
            "❌ Fonsiz",
            parse_mode="HTML"
        )

    except Exception as error:

        logger.exception(
            "Media processing failed"
        )

        if consumed:

            refund_sticker(
                user_id
            )

        await update.message.reply_text(
            "❌ <b>Xatolik yuz berdi.</b>\n\n"
            f"{str(error)}",
            parse_mode="HTML"
        )

        cleanup_session(
            user_id
        )


# ============================================================
# COLOR PROCESSING
# ============================================================

async def process_color(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    user_id = user.id

    session = user_sessions.get(
        user_id
    )

    if not session:
        return False

    if not session.get(
        "waiting_color"
    ):
        return False

    text = (
        update.message.text
        or ""
    ).strip()

    colors = {

        "🟢 Yashil":
            "#00FF00",

        "⚪ Oq":
            "#FFFFFF",

        "⚫ Qora":
            "#000000",

        "🔵 Ko'k":
            "#0000FF",
    }

    if text == "❌ Fonsiz":

        selected_color = None

    elif text in colors:

        selected_color = colors[text]

    else:

        await update.message.reply_text(
            "❗ Iltimos, quyidagi variantlardan "
            "birini tanlang:\n\n"
            "🟢 Yashil\n"
            "⚪ Oq\n"
            "⚫ Qora\n"
            "🔵 Ko'k\n"
            "❌ Fonsiz"
        )

        return True

    input_path = session[
        "input"
    ]

    output_path = session[
        "output"
    ]

    try:

        session[
            "waiting_color"
        ] = False

        await update.message.reply_text(
            "⚙️ <b>Transparent WEBM "
            "tayyorlanmoqda...</b>",
            parse_mode="HTML"
        )

        if selected_color:

            await asyncio.to_thread(
                convert_chromakey,
                input_path,
                output_path,
                selected_color,
                0.25
            )

        else:

            await asyncio.to_thread(
                convert_video,
                input_path,
                output_path
            )

        await send_sticker(
            update,
            context,
            output_path,
            StickerFormat.VIDEO
        )

        return True

    except Exception as error:

        logger.exception(
            "Video processing failed"
        )

        refund_sticker(
            user_id
        )

        await update.message.reply_text(
            "❌ <b>Video processing xatosi.</b>\n\n"
            f"{str(error)}",
            parse_mode="HTML"
        )

        return True

    finally:

        cleanup_session(
            user_id
        )


# ============================================================
# SEND / CREATE STICKER
# ============================================================

async def send_sticker(
    update,
    context,
    output_path,
    sticker_format
):

    user = update.effective_user

    user_id = user.id

    emoji_list = ["😀"]

    pack = get_pack(
        user_id,
        sticker_format
    )

    # ========================================================
    # EXISTING PACK
    # ========================================================

    if pack:

        pack_name = pack[
            "pack_name"
        ]

        try:

            with open(
                output_path,
                "rb"
            ) as sticker_file:

                input_sticker = InputSticker(
                    sticker=sticker_file,
                    emoji_list=emoji_list,
                    format=sticker_format
                )

                await context.bot.add_sticker_to_set(
                    user_id=user_id,
                    name=pack_name,
                    sticker=input_sticker
                )

        except TelegramError as error:

            logger.exception(
                "Failed to add sticker to existing pack"
            )

            # Pack could have been deleted manually.
            # Remove stale DB record and report.
            conn = get_db()

            conn.execute("""
                DELETE FROM packs
                WHERE pack_name = ?
            """, (
                pack_name,
            ))

            conn.commit()
            conn.close()

            raise RuntimeError(
                "Sticker pack Telegram'da mavjud emas "
                "yoki unga sticker qo'shib bo'lmadi. "
                "Keyingi urinishda yangi pack yaratiladi."
            ) from error

    # ========================================================
    # CREATE NEW PACK
    # ========================================================

    else:

        pack_name = generate_pack_name(
            user_id,
            sticker_format
        )

        first_name = (
            user.first_name
            or "User"
        )

        pack_title = (
            f"{first_name} Sticker Pack"
        )

        # Telegram title max 64
        pack_title = pack_title[
            :64
        ]

        with open(
            output_path,
            "rb"
        ) as sticker_file:

            input_sticker = InputSticker(
                sticker=sticker_file,
                emoji_list=emoji_list,
                format=sticker_format
            )

            await context.bot.create_new_sticker_set(
                user_id=user_id,
                name=pack_name,
                title=pack_title,
                stickers=[
                    input_sticker
                ]
            )

        save_pack(
            user_id,
            pack_name,
            pack_title,
            sticker_format
        )

    # ========================================================
    # RESULT
    # ========================================================

    if is_admin(user_id):

        limit_text = (
            "👑 Admin: ♾️ Unlimited"
        )

    else:

        used = get_today_usage(
            user_id
        )

        remaining = max(
            0,
            DAILY_LIMIT - used
        )

        limit_text = (
            f"📊 Bugun qolgan: "
            f"{remaining} ta"
        )

    await update.message.reply_text(
        "✅ <b>Sticker muvaffaqiyatli yaratildi!</b>\n\n"
        f"🔗 https://t.me/addstickers/"
        f"{pack_name}\n\n"
        f"{limit_text}",
        parse_mode="HTML",
        disable_web_page_preview=True
    )


# ============================================================
# MY PACKS
# ============================================================

async def mypacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    register_user(user)

    conn = get_db()

    rows = conn.execute("""
        SELECT
            pack_name,
            pack_title,
            sticker_format

        FROM packs

        WHERE user_id = ?

        ORDER BY id DESC
    """, (
        user.id,
    )).fetchall()

    conn.close()

    if not rows:

        await update.message.reply_text(
            "📦 Sizda hali sticker pack yo'q."
        )

        return

    lines = [
        "🎨 <b>SIZNING PACKLARINGIZ</b>",
        ""
    ]

    for index, row in enumerate(
        rows,
        start=1
    ):

        icon = (
            "🎬"
            if row["sticker_format"]
            == StickerFormat.VIDEO
            else "🖼"
        )

        lines.append(
            f"{index}. {icon} "
            f"<a href=\"https://t.me/addstickers/"
            f"{row['pack_name']}\">"
            f"{row['pack_title']}"
            f"</a>"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


# ============================================================
# ADMIN STATS
# ============================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not is_admin(user.id):

        await update.message.reply_text(
            "⛔ Bu buyruq faqat admin uchun."
        )

        return

    conn = get_db()

    total_users = conn.execute("""
        SELECT COUNT(*)
        FROM users
    """).fetchone()[0]

    active_today = conn.execute("""
        SELECT COUNT(*)
        FROM daily_usage

        WHERE usage_date = ?
        AND sticker_count > 0
    """, (
        current_date(),
    )).fetchone()[0]

    today_stickers = conn.execute("""
        SELECT COALESCE(
            SUM(sticker_count),
            0
        )

        FROM daily_usage

        WHERE usage_date = ?
    """, (
        current_date(),
    )).fetchone()[0]

    total_stickers = conn.execute("""
        SELECT COALESCE(
            SUM(sticker_count),
            0
        )

        FROM daily_usage
    """).fetchone()[0]

    total_packs = conn.execute("""
        SELECT COUNT(*)
        FROM packs
    """).fetchone()[0]

    conn.close()

    await update.message.reply_text(
        "📊 <b>VID2STICKER STATISTIKA</b>\n\n"
        f"👥 Jami foydalanuvchilar: "
        f"<b>{total_users}</b>\n"
        f"🟢 Bugun faol: "
        f"<b>{active_today}</b>\n"
        f"🎨 Bugun stickerlar: "
        f"<b>{today_stickers}</b>\n"
        f"🎨 Jami stickerlar: "
        f"<b>{total_stickers}</b>\n"
        f"📦 Jami packlar: "
        f"<b>{total_packs}</b>\n\n"
        f"👤 User limit: "
        f"<b>{DAILY_LIMIT}/kun</b>\n"
        "👑 Admin: <b>♾️ Unlimited</b>",
        parse_mode="HTML"
    )


# ============================================================
# ADMIN USERS
# ============================================================

async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not is_admin(user.id):

        await update.message.reply_text(
            "⛔ Faqat admin uchun."
        )

        return

    conn = get_db()

    total = conn.execute("""
        SELECT COUNT(*)
        FROM users
    """).fetchone()[0]

    rows = conn.execute("""
        SELECT
            user_id,
            username,
            first_name,
            last_seen

        FROM users

        ORDER BY last_seen DESC

        LIMIT 50
    """).fetchall()

    conn.close()

    lines = [
        f"👥 <b>FOYDALANUVCHILAR: {total}</b>",
        "",
        "Oxirgi 50 ta:",
        ""
    ]

    for row in rows:

        name = (
            row["first_name"]
            or "No name"
        )

        username = (
            f"@{row['username']}"
            if row["username"]
            else "username yo'q"
        )

        lines.append(
            f"• {name} — {username}\n"
            f"  ID: <code>{row['user_id']}</code>"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


# ============================================================
# BUY
# ============================================================

async def buy(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    register_user(user)

    if is_admin(user.id):

        await update.message.reply_text(
            "👑 <b>ADMIN</b>\n\n"
            "♾️ Siz uchun limit cheksiz.",
            parse_mode="HTML"
        )

        return

    await update.message.reply_text(
        "💎 <b>TARIFLAR</b>\n\n"
        "🆓 FREE\n"
        f"• {DAILY_LIMIT} sticker / kun\n\n"
        "💎 PREMIUM\n"
        "• Ko'proq limit\n"
        "• Premium funksiyalar\n"
        "• Kengaytirilgan imkoniyatlar\n\n"
        "🚧 Premium tizimi keyingi bosqichda.",
        parse_mode="HTML"
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    handled = await process_color(
        update,
        context
    )

    if handled:
        return

    register_user(
        update.effective_user
    )

    await update.message.reply_text(
        "🎨 Sticker yaratish uchun "
        "video, GIF yoki rasm yuboring.\n\n"
        "📊 /limit — kunlik limit\n"
        "📦 /mypacks — mening packlarim"
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    error = context.error

    logger.error(
        "Unhandled exception: %s",
        error,
        exc_info=(
            type(error),
            error,
            error.__traceback__
        )
        if error
        else None
    )


# ============================================================
# HEALTH SERVER
# ============================================================

def health_server():

    try:

        from http.server import (
            BaseHTTPRequestHandler,
            HTTPServer
        )

        port = int(
            os.getenv(
                "PORT",
                "10000"
            )
        )

        class HealthHandler(
            BaseHTTPRequestHandler
        ):

            def do_GET(self):

                self.send_response(
                    200
                )

                self.send_header(
                    "Content-Type",
                    "text/plain; charset=utf-8"
                )

                self.end_headers()

                self.wfile.write(
                    b"Vid2Sticker Bot is running"
                )

            def log_message(
                self,
                format,
                *args
            ):

                return

        server = HTTPServer(
            ("0.0.0.0", port),
            HealthHandler
        )

        logger.info(
            "Health server listening on %s",
            port
        )

        server.serve_forever()

    except Exception:

        logger.exception(
            "Health server failed"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable "
            "topilmadi."
        )

    if ADMIN_ID == 0:

        raise RuntimeError(
            "ADMIN_ID environment variable "
            "topilmadi."
        )

    if DAILY_LIMIT < 1:

        raise RuntimeError(
            "DAILY_STICKER_LIMIT 1 dan katta "
            "bo'lishi kerak."
        )

    ensure_ffmpeg()

    init_db()

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(90)
        .write_timeout(90)
        .pool_timeout(30)
        .build()
    )

    # ========================================================
    # COMMANDS
    # ========================================================

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "buy",
            buy
        )
    )

    application.add_handler(
        CommandHandler(
            "limit",
            limit_command
        )
    )

    application.add_handler(
        CommandHandler(
            "mypacks",
            mypacks
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats
        )
    )

    application.add_handler(
        CommandHandler(
            "users",
            users_command
        )
    )

    # ========================================================
    # MEDIA
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.VIDEO |
            filters.ANIMATION |
            filters.PHOTO,
            handle_media
        )
    )

    # ========================================================
    # TEXT
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT &
            ~filters.COMMAND,
            text_handler
        )
    )

    # ========================================================
    # ERRORS
    # ========================================================

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "========================================"
    )

    logger.info(
        "Vid2Sticker Bot starting..."
    )

    logger.info(
        "Admin ID: %s",
        ADMIN_ID
    )

    logger.info(
        "Daily user limit: %s",
        DAILY_LIMIT
    )

    logger.info(
        "Bot username: @%s",
        BOT_USERNAME
    )

    logger.info(
        "========================================"
    )

    # ========================================================
    # IMPORTANT
    # ========================================================
    #
    # BU YERDA asyncio.run() YO'Q.
    #
    # app.updater.start_polling() YO'Q.
    #
    # loop.run_until_complete() YO'Q.
    #
    # run_polling() event loopni o'zi boshqaradi.
    #
    # ========================================================

    application.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    threading.Thread(
        target=health_server,
        daemon=True
    ).start()

    try:

        main()

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped by user."
        )

    except Exception:

        logger.exception(
            "FATAL: Bot crashed."
        )
