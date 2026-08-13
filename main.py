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

MAX_FILE_SIZE = MAX_FILE_MB * 1024 * 1024

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
    "MotionStickerBot"
)


# ============================================================
# SESSION
# ============================================================

user_sessions = {}


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    conn = db()

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
        CREATE TABLE IF NOT EXISTS usage (
            user_id INTEGER NOT NULL,
            usage_date TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,

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

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_usage_date
        ON usage(usage_date)
    """)

    conn.commit()
    conn.close()

    logger.info(
        "Database initialized"
    )


# ============================================================
# USERS
# ============================================================

def register_user(user):

    if not user:
        return

    conn = db()

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


def is_admin(user_id):

    return user_id == ADMIN_ID


# ============================================================
# LIMIT
# ============================================================

def today():

    return date.today().isoformat()


def get_usage(user_id):

    conn = db()

    row = conn.execute("""
        SELECT count

        FROM usage

        WHERE user_id = ?
        AND usage_date = ?
    """, (
        user_id,
        today()
    )).fetchone()

    conn.close()

    if row:
        return row["count"]

    return 0


def consume_usage(user_id):

    if is_admin(user_id):
        return True, None

    conn = db()

    try:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        row = conn.execute("""
            SELECT count

            FROM usage

            WHERE user_id = ?
            AND usage_date = ?
        """, (
            user_id,
            today()
        )).fetchone()

        used = (
            row["count"]
            if row
            else 0
        )

        if used >= DAILY_LIMIT:

            conn.rollback()

            return False, 0

        if row:

            conn.execute("""
                UPDATE usage

                SET count = count + 1

                WHERE user_id = ?
                AND usage_date = ?
            """, (
                user_id,
                today()
            ))

        else:

            conn.execute("""
                INSERT INTO usage (
                    user_id,
                    usage_date,
                    count
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
            "Usage error"
        )

        raise

    finally:

        conn.close()


def rollback_usage(user_id):

    if is_admin(user_id):
        return

    conn = db()

    conn.execute("""
        UPDATE usage

        SET count =
            CASE
                WHEN count > 0
                THEN count - 1
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
# PACKS
# ============================================================

def get_pack(
    user_id,
    sticker_format
):

    conn = db()

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

    conn = db()

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

def create_pack_name(
    user_id,
    sticker_format
):

    suffix = (
        "by_motionlab_bot"
    )

    if sticker_format == "video":
        prefix = "motion"

    else:
        prefix = "static"

    return (
        f"{prefix}_{user_id}_"
        f"{suffix}"
    )


# ============================================================
# FILE HELPERS
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

def ffmpeg(command):

    logger.info(
        "FFmpeg: %s",
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
            result.stderr
        )

        raise RuntimeError(
            "FFmpeg processing failed"
        )

    return result


# ============================================================
# HEX COLOR
# ============================================================

def normalize_hex_color(value):

    value = value.strip()

    if value.startswith("#"):
        value = value[1:]

    if len(value) != 6:
        raise ValueError(
            "Rang #RRGGBB formatida bo'lishi kerak."
        )

    if not re.fullmatch(
        r"[0-9a-fA-F]{6}",
        value
    ):
        raise ValueError(
            "Noto'g'ri rang."
        )

    return "#" + value


# ============================================================
# VIDEO → TRANSPARENT WEBM
# ============================================================

def make_transparent_webm(
    input_path,
    output_path,
    color="#00FF00",
    similarity="0.25"
):

    color = normalize_hex_color(
        color
    )

    try:
        similarity_float = float(
            similarity
        )
    except ValueError:
        similarity_float = 0.25

    similarity_float = max(
        0.01,
        min(
            similarity_float,
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
            f"{similarity_float}:"
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

    ffmpeg(command)


# ============================================================
# VIDEO WITHOUT CHROMAKEY
# ============================================================

def make_webm(
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

    ffmpeg(command)


# ============================================================
# PHOTO
# ============================================================

def make_png(
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

    ffmpeg(command)


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

        await update.message.reply_text(
            "👑 <b>ADMIN MODE</b>\n\n"
            "♾️ Siz uchun limit cheksiz.\n\n"
            "📊 /stats\n"
            "👥 /users\n"
            "🎨 /mypacks\n"
            "📈 /limit",
            parse_mode="HTML"
        )

        return

    remaining = (
        DAILY_LIMIT
        - get_usage(user.id)
    )

    await update.message.reply_text(
        "👋 <b>MotionLab Sticker Bot</b>\n\n"
        "🎨 Video, GIF yoki rasm yuboring.\n\n"
        f"📊 Bugungi limit: "
        f"{remaining}/{DAILY_LIMIT}\n\n"
        "🔄 Limit har kuni yangilanadi.",
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
            "👑 ADMIN\n\n"
            "♾️ Sizda kunlik limit yo'q."
        )

        return

    used = get_usage(
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
# MEDIA
# ============================================================

async def handle_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    register_user(user)

    user_id = user.id

    # --------------------------------------------------------
    # LIMIT
    # --------------------------------------------------------

    allowed, remaining = consume_usage(
        user_id
    )

    if not allowed:

        await update.message.reply_text(
            "⛔ <b>Bugungi limitingiz tugadi.</b>\n\n"
            f"📦 Limit: {DAILY_LIMIT} ta\n"
            "🔄 Ertaga yangilanadi.",
            parse_mode="HTML"
        )

        return

    input_path = None
    output_path = None

    try:

        # ----------------------------------------------------
        # MEDIA TYPE
        # ----------------------------------------------------

        if update.message.video:

            media = update.message.video

            extension = ".mp4"

            file_size = (
                media.file_size or 0
            )

            source_type = "video"

        elif update.message.animation:

            media = update.message.animation

            extension = ".gif"

            file_size = (
                media.file_size or 0
            )

            source_type = "video"

        elif update.message.photo:

            media = (
                update.message.photo[-1]
            )

            extension = ".jpg"

            file_size = (
                media.file_size or 0
            )

            source_type = "static"

        else:

            rollback_usage(
                user_id
            )

            return

        # ----------------------------------------------------
        # FILE SIZE
        # ----------------------------------------------------

        if file_size > MAX_FILE_SIZE:

            rollback_usage(
                user_id
            )

            await update.message.reply_text(
                "❌ Fayl juda katta.\n\n"
                f"📦 Maksimal: "
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

        output_extension = (
            ".webm"
            if source_type == "video"
            else ".png"
        )

        output_path = (
            TEMP_DIR /
            f"{unique}{output_extension}"
        )

        user_sessions[user_id] = {
            "input": str(input_path),
            "output": str(output_path)
        }

        # ----------------------------------------------------
        # STATUS
        # ----------------------------------------------------

        status = await update.message.reply_text(
            "⏳ <b>Fayl yuklanmoqda...</b>",
            parse_mode="HTML"
        )

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        telegram_file = await media.get_file()

        await telegram_file.download_to_drive(
            custom_path=str(input_path)
        )

        # ----------------------------------------------------
        # PROCESS
        # ----------------------------------------------------

        if source_type == "static":

            await status.edit_text(
                "⚙️ <b>Sticker tayyorlanmoqda...</b>",
                parse_mode="HTML"
            )

            await asyncio.to_thread(
                make_png,
                input_path,
                output_path
            )

            sticker_format = "static"

        else:

            keyboard = [
                [
                    "🟢 Yashil",
                    "⚪ Oq"
                ],
                [
                    "⚫ Qora",
                    "🔵 Ko'k"
                ],
                [
                    "❌ Fonsiz"
                ]
            ]

            # Sessionga processing holatini yozamiz
            user_sessions[user_id][
                "status_message_id"
            ] = status.message_id

            await status.edit_text(
                "🎨 <b>Fon rangini tanlang:</b>\n\n"
                "🟢 Yashil\n"
                "⚪ Oq\n"
                "⚫ Qora\n"
                "🔵 Ko'k\n"
                "❌ Fonsiz — chromakey ishlatilmaydi",
                parse_mode="HTML"
            )

            user_sessions[user_id][
                "waiting_color"
            ] = True

            return

        # ----------------------------------------------------
        # SEND STATIC
        # ----------------------------------------------------

        await send_sticker(
            update,
            context,
            output_path,
            sticker_format,
            remaining
        )

    except Exception:

        logger.exception(
            "Media processing failed"
        )

        rollback_usage(
            user_id
        )

        await update.message.reply_text(
            "❌ <b>Xatolik yuz berdi.</b>\n\n"
            "Faylni qayta yuborib ko'ring.",
            parse_mode="HTML"
        )

    finally:

        # video color selectionda session kerak
        if (
            user_id not in user_sessions
            or not user_sessions[user_id].get(
                "waiting_color"
            )
        ):

            cleanup_session(
                user_id
            )


# ============================================================
# COLOR TEXT
# ============================================================

async def handle_color_selection(
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

    color_map = {

        "🟢 Yashil": "#00FF00",

        "⚪ Oq": "#FFFFFF",

        "⚫ Qora": "#000000",

        "🔵 Ko'k": "#0000FF",

    }

    if text == "❌ Fonsiz":

        color = None

    elif text in color_map:

        color = color_map[text]

    else:

        return False

    input_path = session[
        "input"
    ]

    output_path = session[
        "output"
    ]

    try:

        status = await update.message.reply_text(
            "⚙️ <b>Transparent sticker tayyorlanmoqda...</b>",
            parse_mode="HTML"
        )

        if color:

            await asyncio.to_thread(
                make_transparent_webm,
                input_path,
                output_path,
                color,
                "0.25"
            )

        else:

            await asyncio.to_thread(
                make_webm,
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
            "video",
            (
                None
                if is_admin(user_id)
                else (
                    DAILY_LIMIT
                    - get_usage(user_id)
                )
            )
        )

        await status.delete()

    except Exception:

        logger.exception(
            "Color processing error"
        )

        rollback_usage(
            user_id
        )

        await update.message.reply_text(
            "❌ Video processingda xatolik.",
            parse_mode="HTML"
        )

    finally:

        cleanup_session(
            user_id
        )

    return True


# ============================================================
# SEND / ADD STICKER
# ============================================================

async def send_sticker(
    update,
    context,
    output_path,
    sticker_format,
    remaining
):

    user = update.effective_user

    user_id = user.id

    pack = get_pack(
        user_id,
        sticker_format
    )

    emoji_list = ["😀"]

    # --------------------------------------------------------
    # EXISTING PACK
    # --------------------------------------------------------

    if pack:

        try:

            with open(
                output_path,
                "rb"
            ) as sticker_file:

                input_sticker = InputSticker(
                    sticker=sticker_file,
                    emoji_list=emoji_list
                )

                await context.bot.add_sticker_to_set(
                    user_id=user_id,
                    name=pack["pack_name"],
                    sticker=input_sticker
                )

            pack_name = pack[
                "pack_name"
            ]

        except TelegramError as error:

            logger.warning(
                "Could not add to existing pack: %s",
                error
            )

            # Telegram error sababini foydalanuvchiga
            # ko'rsatamiz, yangi packni ko'r-ko'rona
            # yaratmaymiz.

            await update.message.reply_text(
                "❌ Mavjud sticker packga qo'shib bo'lmadi.\n\n"
                f"{error}"
            )

            raise

    # --------------------------------------------------------
    # NEW PACK
    # --------------------------------------------------------

    else:

        pack_name = create_pack_name(
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

            input_sticker = InputSticker(
                sticker=sticker_file,
                emoji_list=emoji_list
            )

            await context.bot.create_new_sticker_set(
                user_id=user_id,
                name=pack_name,
                title=pack_title,
                stickers=[
                    input_sticker
                ],
                sticker_format=sticker_format
            )

        save_pack(
            user_id,
            pack_name,
            pack_title,
            sticker_format
        )

    # --------------------------------------------------------
    # RESULT
    # --------------------------------------------------------

    if is_admin(user_id):

        limit_text = (
            "👑 Admin: ♾️ Unlimited"
        )

    else:

        remaining_now = (
            DAILY_LIMIT
            - get_usage(user_id)
        )

        limit_text = (
            f"📊 Bugun qolgan: "
            f"{remaining_now} ta"
        )

    await update.message.reply_text(
        "✅ <b>Sticker tayyor!</b>\n\n"
        f"🔗 "
        f"https://t.me/addstickers/"
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

    conn = db()

    rows = conn.execute("""
        SELECT
            pack_name,
            pack_title,
            sticker_format,
            created_at

        FROM packs

        WHERE user_id = ?

        ORDER BY id DESC
    """, (
        user.id,
    )).fetchall()

    conn.close()

    if not rows:

        await update.message.reply_text(
            "📦 Sizda hali pack yo'q."
        )

        return

    lines = [
        "🎨 <b>Sizning packlaringiz:</b>",
        ""
    ]

    for index, row in enumerate(
        rows,
        start=1
    ):

        lines.append(
            f"{index}. "
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
            "⛔ Faqat admin uchun."
        )

        return

    conn = db()

    total_users = conn.execute("""
        SELECT COUNT(*)
        FROM users
    """).fetchone()[0]

    active_today = conn.execute("""
        SELECT COUNT(*)
        FROM usage

        WHERE usage_date = ?
        AND count > 0
    """, (
        today(),
    )).fetchone()[0]

    stickers_today = conn.execute("""
        SELECT COALESCE(
            SUM(count),
            0
        )

        FROM usage

        WHERE usage_date = ?
    """, (
        today(),
    )).fetchone()[0]

    total_stickers = conn.execute("""
        SELECT COALESCE(
            SUM(count),
            0
        )

        FROM usage
    """).fetchone()[0]

    total_packs = conn.execute("""
        SELECT COUNT(*)
        FROM packs
    """).fetchone()[0]

    conn.close()

    await update.message.reply_text(
        "📊 <b>BOT STATISTIKASI</b>\n\n"

        f"👥 Jami userlar: "
        f"<b>{total_users}</b>\n"

        f"🟢 Bugun faol: "
        f"<b>{active_today}</b>\n"

        f"🎨 Bugun sticker: "
        f"<b>{stickers_today}</b>\n"

        f"🎨 Jami sticker: "
        f"<b>{total_stickers}</b>\n"

        f"📦 Jami pack: "
        f"<b>{total_packs}</b>\n\n"

        f"👑 Admin: Unlimited\n"
        f"👤 User limit: "
        f"{DAILY_LIMIT}/kun",
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

    conn = db()

    rows = conn.execute("""
        SELECT
            user_id,
            username,
            first_name,
            last_name

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
            "👑 Siz adminsiz.\n\n"
            "♾️ Limit: Unlimited"
        )

        return

    await update.message.reply_text(
        "💎 <b>TARIFLAR</b>\n\n"

        "🆓 FREE\n"
        f"• {DAILY_LIMIT} sticker / kun\n\n"

        "💎 PREMIUM\n"
        "• Yuqori kunlik limit\n"
        "• Premium funksiyalar\n"
        "• Ko'proq imkoniyatlar\n\n"

        "🚧 Premium payment tizimi keyingi bosqichda.",
        parse_mode="HTML"
    )


# ============================================================
# TEXT
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    handled = await handle_color_selection(
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
                    b"OK"
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
            "Health server: %s",
            port
        )

        server.serve_forever()

    except Exception:

        logger.exception(
            "Health server error"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "BOT_TOKEN topilmadi."
        )

    if ADMIN_ID == 0:

        raise RuntimeError(
            "ADMIN_ID topilmadi."
        )

    init_db()

    app = (
        ApplicationBuilder()
        .token(TOKEN)

        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)

        .build()
    )

    # COMMANDS

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "buy",
            buy
        )
    )

    app.add_handler(
        CommandHandler(
            "limit",
            limit_command
        )
    )

    app.add_handler(
        CommandHandler(
            "mypacks",
            mypacks
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            stats
        )
    )

    app.add_handler(
        CommandHandler(
            "users",
            users_command
        )
    )

    # MEDIA

    app.add_handler(
        MessageHandler(
            filters.VIDEO |
            filters.ANIMATION |
            filters.PHOTO,
            handle_media
        )
    )

    # CALLBACK

    app.add_handler(
        CallbackQueryHandler(
            button_click
        )
    )

    # TEXT

    app.add_handler(
        MessageHandler(
            filters.TEXT &
            ~filters.COMMAND,
            text_handler
        )
    )

    logger.info(
        "BOT STARTING..."
    )

    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# ENTRY
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
            "BOT STOPPED"
)
