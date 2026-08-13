import os
import re
import sqlite3
import logging
import asyncio
import subprocess
import tempfile
import threading

from pathlib import Path
from datetime import date

from telegram import Update, InputSticker
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

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

MAX_FILE_MB = int(
    os.getenv("MAX_FILE_MB", "50")
)

MAX_FILE_SIZE = (
    MAX_FILE_MB * 1024 * 1024
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
    "MotionLabBot"
)


# ============================================================
# USER SESSIONS
# ============================================================

user_sessions = {}


# ============================================================
# DATABASE
# ============================================================

def get_db():

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
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
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()

    logger.info(
        "Database initialized"
    )


# ============================================================
# USERS
# ============================================================

def is_admin(user_id):

    return user_id == ADMIN_ID


def register_user(user):

    if not user:
        return

    conn = get_db()

    conn.execute("""
        INSERT INTO users (
            user_id,
            username,
            first_name,
            last_name
        )

        VALUES (?, ?, ?, ?)

        ON CONFLICT(user_id)
        DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            last_seen = CURRENT_TIMESTAMP
    """, (
        user.id,
        user.username,
        user.first_name,
        user.last_name
    ))

    conn.commit()
    conn.close()


# ============================================================
# LIMIT SYSTEM
# ============================================================

def today():

    return date.today().isoformat()


def get_today_usage(user_id):

    conn = get_db()

    row = conn.execute("""
        SELECT sticker_count

        FROM daily_usage

        WHERE user_id = ?
        AND usage_date = ?
    """, (
        user_id,
        today()
    )).fetchone()

    conn.close()

    if row:
        return row["sticker_count"]

    return 0


def consume_sticker(user_id):

    # ADMIN = UNLIMITED
    if is_admin(user_id):

        return True, None

    conn = get_db()

    try:

        # Prevent simultaneous requests
        conn.execute(
            "BEGIN IMMEDIATE"
        )

        row = conn.execute("""
            SELECT sticker_count

            FROM daily_usage

            WHERE user_id = ?
            AND usage_date = ?
        """, (
            user_id,
            today()
        )).fetchone()

        used = (
            row["sticker_count"]
            if row
            else 0
        )

        # LIMIT REACHED
        if used >= DAILY_LIMIT:

            conn.rollback()

            return False, 0

        # UPDATE
        if row:

            conn.execute("""
                UPDATE daily_usage

                SET sticker_count =
                    sticker_count + 1

                WHERE user_id = ?
                AND usage_date = ?
            """, (
                user_id,
                today()
            ))

        # INSERT
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
                today()
            ))

        conn.commit()

        remaining = (
            DAILY_LIMIT
            - used
            - 1
        )

        return True, remaining

    except Exception:

        conn.rollback()

        logger.exception(
            "Limit error"
        )

        raise

    finally:

        conn.close()


def rollback_sticker(user_id):

    if is_admin(user_id):
        return

    conn = get_db()

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
        today()
    ))

    conn.commit()
    conn.close()


# ============================================================
# PACK DATABASE
# ============================================================

def get_pack(
    user_id,
    sticker_format
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
    user_id,
    pack_name,
    pack_title,
    sticker_format
):

    conn = get_db()

    conn.execute("""
        INSERT OR IGNORE INTO packs (
            user_id,
            pack_name,
            pack_title,
            sticker_format
        )

        VALUES (?, ?, ?, ?)
    """, (
        user_id,
        pack_name,
        pack_title,
        sticker_format
    ))

    conn.commit()
    conn.close()


# ============================================================
# PACK NAME
# ============================================================

def generate_pack_name(
    user_id,
    sticker_format
):

    prefix = (
        "motion"
        if sticker_format == "video"
        else "static"
    )

    # Telegram pack name must end with _by_<bot>
    bot_username = (
        os.getenv(
            "BOT_USERNAME",
            "MotionLabBot"
        )
        .replace("@", "")
    )

    return (
        f"{prefix}_{user_id}_"
        f"by_{bot_username}"
    )


# ============================================================
# FILE CLEANUP
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
            "Could not remove file: %s",
            path
        )


def cleanup_session(user_id):

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

def run_ffmpeg(command):

    logger.info(
        "FFmpeg command: %s",
        " ".join(
            map(str, command)
        )
    )

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        logger.error(
            "FFmpeg ERROR:\n%s",
            result.stderr
        )

        raise RuntimeError(
            "FFmpeg processing failed"
        )

    return result


# ============================================================
# STATIC STICKER
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
            "(oh-ih)/2"
        ),

        "-frames:v",
        "1",

        str(output_path)
    ]

    run_ffmpeg(command)


# ============================================================
# VIDEO → WEBM
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

        "-vf",
        (
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

        "-t",
        "3",

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
    similarity
):

    try:

        similarity = float(
            similarity
        )

    except Exception:

        similarity = 0.25

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

        "-vf",

        (
            f"chromakey="
            f"{color}:"
            f"{similarity}:"
            f"0.05,"
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

        "-t",
        "3",

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
            "👑 <b>MotionLab Admin</b>\n\n"
            "♾️ Sizda kunlik limit yo'q.\n\n"
            "📊 /stats\n"
            "👥 /users\n"
            "🎨 /mypacks\n"
            "📈 /limit"
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
            "👋 <b>MotionLab Sticker Bot</b>\n\n"
            "🎨 Video, GIF yoki rasm yuboring.\n\n"
            f"📊 Bugungi limit: "
            f"{remaining}/{DAILY_LIMIT}\n\n"
            "🔄 Limit har kuni avtomatik yangilanadi."
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML"
    )


# ============================================================
# LIMIT
# ============================================================

async def limit_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    register_user(user)

    if is_admin(user.id):

        await update.message.reply_text(
            "👑 ADMIN\n\n"
            "♾️ Siz uchun limit cheksiz."
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
        f"🎨 Ishlatilgan: {used}\n"
        f"🟢 Qolgan: {remaining}\n"
        f"📦 Kunlik limit: {DAILY_LIMIT}",
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

    # --------------------------------------------------------
    # LIMIT CHECK
    # --------------------------------------------------------

    allowed, remaining = consume_sticker(
        user_id
    )

    if not allowed:

        await update.message.reply_text(
            "⛔ <b>Bugungi limitingiz tugadi.</b>\n\n"
            f"📦 Kunlik limit: {DAILY_LIMIT}\n"
            "🔄 Ertaga avtomatik yangilanadi.",
            parse_mode="HTML"
        )

        return

    input_path = None
    output_path = None

    try:

        # ----------------------------------------------------
        # DETECT FILE
        # ----------------------------------------------------

        if update.message.video:

            media = update.message.video
            extension = ".mp4"
            source_type = "video"

        elif update.message.animation:

            media = update.message.animation
            extension = ".gif"
            source_type = "video"

        elif update.message.photo:

            media = update.message.photo[-1]
            extension = ".jpg"
            source_type = "static"

        else:

            rollback_sticker(
                user_id
            )

            return

        file_size = (
            media.file_size or 0
        )

        if file_size > MAX_FILE_SIZE:

            rollback_sticker(
                user_id
            )

            await update.message.reply_text(
                "❌ Fayl juda katta.\n\n"
                f"📦 Maksimal hajm: "
                f"{MAX_FILE_MB} MB"
            )

            return

        # ----------------------------------------------------
        # PATH
        # ----------------------------------------------------

        unique = (
            f"{user_id}_"
            f"{update.message.message_id}"
        )

        input_path = (
            TEMP_DIR /
            f"{unique}{extension}"
        )

        if source_type == "video":

            output_path = (
                TEMP_DIR /
                f"{unique}.webm"
            )

        else:

            output_path = (
                TEMP_DIR /
                f"{unique}.png"
            )

        user_sessions[user_id] = {
            "input": str(input_path),
            "output": str(output_path),
            "source_type": source_type
        }

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        status = await update.message.reply_text(
            "⏳ <b>Fayl yuklanmoqda...</b>",
            parse_mode="HTML"
        )

        telegram_file = await media.get_file()

        await telegram_file.download_to_drive(
            custom_path=str(input_path)
        )

        # ----------------------------------------------------
        # STATIC
        # ----------------------------------------------------

        if source_type == "static":

            await status.edit_text(
                "⚙️ <b>Sticker tayyorlanmoqda...</b>",
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
                "static"
            )

            return

        # ----------------------------------------------------
        # VIDEO
        # ----------------------------------------------------

        await status.edit_text(
            "🎨 <b>Fon rangini tanlang:</b>\n\n"
            "🟢 Yashil\n"
            "⚪ Oq\n"
            "⚫ Qora\n"
            "🔵 Ko'k\n"
            "❌ Fonsiz",
            parse_mode="HTML"
        )

        user_sessions[user_id][
            "waiting_color"
        ] = True

    except Exception:

        logger.exception(
            "Media error"
        )

        rollback_sticker(
            user_id
        )

        await update.message.reply_text(
            "❌ <b>Xatolik yuz berdi.</b>\n\n"
            "Qayta urinib ko'ring.",
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
    )

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

        return False

    input_path = session[
        "input"
    ]

    output_path = session[
        "output"
    ]

    try:

        await update.message.reply_text(
            "⚙️ <b>Transparent WEBM tayyorlanmoqda...</b>",
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

        session[
            "waiting_color"
        ] = False

        await send_sticker(
            update,
            context,
            output_path,
            "video"
        )

        return True

    except Exception:

        logger.exception(
            "Color processing error"
        )

        rollback_sticker(
            user_id
        )

        await update.message.reply_text(
            "❌ Video processingda xatolik.",
            parse_mode="HTML"
        )

        return True

    finally:

        cleanup_session(
            user_id
        )


# ============================================================
# SEND STICKER
# ============================================================

async def send_sticker(
    update,
    context,
    output_path,
    sticker_format
):

    user = update.effective_user

    user_id = user.id

    pack = get_pack(
        user_id,
        sticker_format
    )

    emoji_list = ["😀"]

    # ========================================================
    # EXISTING PACK
    # ========================================================

    if pack:

        try:

            with open(
                output_path,
                "rb"
            ) as sticker_file:

                sticker = InputSticker(
                    sticker=sticker_file,
                    emoji_list=emoji_list
                )

                await context.bot.add_sticker_to_set(
                    user_id=user_id,
                    name=pack["pack_name"],
                    sticker=sticker
                )

            pack_name = pack[
                "pack_name"
            ]

        except TelegramError as error:

            logger.exception(
                "Could not add sticker to pack"
            )

            raise RuntimeError(
                f"Telegram pack error: {error}"
            )

    # ========================================================
    # CREATE NEW PACK
    # ========================================================

    else:

        pack_name = generate_pack_name(
            user_id,
            sticker_format
        )

        pack_title = (
            f"{user.first_name or 'User'} "
            f"Sticker Pack"
        )

        with open(
            output_path,
            "rb"
        ) as sticker_file:

            sticker = InputSticker(
                sticker=sticker_file,
                emoji_list=emoji_list
            )

            await context.bot.create_new_sticker_set(
                user_id=user_id,
                name=pack_name,
                title=pack_title,
                stickers=[sticker],
                sticker_format=sticker_format
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

        remaining = max(
            0,
            DAILY_LIMIT -
            get_today_usage(user_id)
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
            if row["sticker_format"] == "video"
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
        today(),
    )).fetchone()[0]

    today_stickers = conn.execute("""
        SELECT COALESCE(
            SUM(sticker_count),
            0
        )

        FROM daily_usage

        WHERE usage_date = ?
    """, (
        today(),
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
        "📊 <b>MOTIONLAB STATISTIKA</b>\n\n"
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
        "👑 Admin: ♾️ Unlimited\n"
        f"👤 User limit: {DAILY_LIMIT}/kun",
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

    rows = conn.execute("""
        SELECT
            user_id,
            username,
            first_name

        FROM users

        ORDER BY last_seen DESC

        LIMIT 50
    """).fetchall()

    conn.close()

    if not rows:

        await update.message.reply_text(
            "👥 Userlar hali yo'q."
        )

        return

    lines = [
        "👥 <b>FOYDALANUVCHILAR</b>",
        ""
    ]

    for row in rows:

        username = (
            f"@{row['username']}"
            if row["username"]
            else "username yo'q"
        )

        name = (
            row["first_name"]
            or "No name"
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
            "👑 Siz admin hisoblanasiz.\n\n"
            "♾️ Siz uchun limit cheksiz."
        )

        return

    await update.message.reply_text(
        "💎 <b>TARIFLAR</b>\n\n"
        "🆓 FREE\n"
        f"• {DAILY_LIMIT} sticker / kun\n\n"
        "💎 PREMIUM\n"
        "• Yuqori limit\n"
        "• Premium funksiyalar\n"
        "• Ko'proq imkoniyatlar\n\n"
        "🚧 Premium tizimi tez orada.",
        parse_mode="HTML"
    )


# ============================================================
# TEXT
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    # First check whether this is a color selection
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
        "📊 /limit\n"
        "🎨 /mypacks"
    )


# ============================================================
# CALLBACK
# ============================================================

async def button_click(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()


# ============================================================
# HEALTH SERVER
# ============================================================

def health_server():

    try:

        from http.server import (
            BaseHTTPRequestHandler,
            HTTPServer
        )

        class Handler(
            BaseHTTPRequestHandler
        ):

            def do_GET(self):

                self.send_response(
                    200
                )

                self.send_header(
                    "Content-Type",
                    "text/plain"
                )

                self.end_headers()

                self.wfile.write(
                    b"MotionLab Bot is running"
                )

            def log_message(
                self,
                format,
                *args
            ):
                return

        port = int(
            os.getenv(
                "PORT",
                "8080"
            )
        )

        server = HTTPServer(
            ("0.0.0.0", port),
            Handler
        )

        logger.info(
            "Health server started on %s",
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
            "BOT_TOKEN environment variable topilmadi."
        )

    if ADMIN_ID == 0:

        raise RuntimeError(
            "ADMIN_ID environment variable topilmadi."
        )

    init_db()

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )

    # Commands

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

    # Media

    application.add_handler(
        MessageHandler(
            filters.VIDEO |
            filters.ANIMATION |
            filters.PHOTO,
            handle_media
        )
    )

    # Callback

    application.add_handler(
        CallbackQueryHandler(
            button_click
        )
    )

    # Text

    application.add_handler(
        MessageHandler(
            filters.TEXT &
            ~filters.COMMAND,
            text_handler
        )
    )

    logger.info(
        "MotionLab Bot is starting..."
    )

    # ========================================================
    # IMPORTANT
    # ========================================================
    #
    # DO NOT use:
    #
    # asyncio.run(...)
    # asyncio.get_event_loop()
    # loop.run_until_complete(...)
    #
    # run_polling() manages the event loop itself.
    #

    application.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        threading.Thread(
            target=health_server,
            daemon=True
        ).start()

    except Exception:

        logger.exception(
            "Health server thread failed"
        )

    try:

        main()

    except KeyboardInterrupt:

        logger.info(
            "MotionLab Bot stopped."
        )

    except Exception:

        logger.exception(
            "MotionLab Bot crashed."
)
