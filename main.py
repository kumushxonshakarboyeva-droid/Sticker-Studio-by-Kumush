import asyncio
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

from telegram import Update, InputSticker
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

# Boshqa foydalanuvchilar uchun KUNIGA 5 TA
DAILY_STICKER_LIMIT = int(
    os.getenv("DAILY_STICKER_LIMIT", "5")
)

MAX_FILE_MB = int(
    os.getenv("MAX_FILE_MB", "50")
)

MAX_FILE_SIZE = MAX_FILE_MB * 1024 * 1024

DB_PATH = os.getenv(
    "DB_PATH",
    "bot.db"
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

logger = logging.getLogger("Vid2StickerBot")


# ============================================================
# USER SESSIONS
# ============================================================

user_sessions = {}

session_locks = {}


def get_user_lock(user_id):
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
            PRIMARY KEY(user_id, usage_date)
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
# HELPERS
# ============================================================

def today():
    return datetime.utcnow().date().isoformat()


def is_admin(user_id):
    return user_id == ADMIN_ID


def register_user(user):

    if not user:
        return

    now = datetime.utcnow().isoformat()

    conn = get_db()

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
        now,
        now
    ))

    conn.commit()
    conn.close()


# ============================================================
# LIMIT SYSTEM
# ============================================================

def get_today_usage(user_id):

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
        today()
    )).fetchone()

    conn.close()

    if not row:
        return 0

    return row["sticker_count"]


def consume_sticker(user_id):

    # ADMIN = UNLIMITED
    if is_admin(user_id):
        return True, None

    conn = get_db()

    try:

        conn.execute("BEGIN IMMEDIATE")

        current_date = today()

        row = conn.execute("""
            SELECT sticker_count
            FROM daily_usage
            WHERE user_id = ?
            AND usage_date = ?
        """, (
            user_id,
            current_date
        )).fetchone()

        used = row["sticker_count"] if row else 0

        if used >= DAILY_STICKER_LIMIT:

            conn.rollback()

            return False, 0

        if row:

            conn.execute("""
                UPDATE daily_usage
                SET sticker_count = sticker_count + 1
                WHERE user_id = ?
                AND usage_date = ?
            """, (
                user_id,
                current_date
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
                current_date
            ))

        conn.commit()

        remaining = DAILY_STICKER_LIMIT - used - 1

        return True, remaining

    except Exception:

        conn.rollback()

        logger.exception("Limit error")

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
# FILE CLEANUP
# ============================================================

def remove_file(path):

    if not path:
        return

    try:

        p = Path(path)

        if p.exists():
            p.unlink()

    except Exception:

        logger.exception(
            "Cannot remove file: %s",
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

def ensure_ffmpeg():

    ffmpeg = shutil.which("ffmpeg")

    if not ffmpeg:

        raise RuntimeError(
            "FFmpeg topilmadi. "
            "Dockerfile orqali FFmpeg o'rnatilishi kerak."
        )

    result = subprocess.run(
        [ffmpeg, "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        raise RuntimeError(
            "FFmpeg ishlamayapti."
        )

    logger.info(
        "FFmpeg found: %s",
        ffmpeg
    )


def run_ffmpeg(command):

    logger.info("Running FFmpeg")

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        logger.error(
            "FFmpeg error: %s",
            result.stderr[-1500:]
        )

        raise RuntimeError("FFmpeg xatosi")


# ============================================================
# CONVERTERS
# ============================================================

VIDEO_LIMIT_BYTES = 256 * 1024
PHOTO_LIMIT_BYTES = 512 * 1024


def convert_to_webm(src, dst, green=False):

    vf = "fps=30,scale=512:512:force_original_aspect_ratio=decrease"

    if green:
        vf = "chromakey=0x00FF00:0.3:0.1," + vf

    vf += ",format=yuva420p"

    # Hajm 256 KB dan oshmaguncha bitrate'ni kamaytiramiz
    for bitrate in ("450K", "300K", "200K", "120K", "70K"):

        command = [
            "ffmpeg", "-y",
            "-i", src,
            "-t", "3",
            "-vf", vf,
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-b:v", bitrate,
            "-an",
            "-auto-alt-ref", "0",
            dst
        ]

        run_ffmpeg(command)

        if os.path.getsize(dst) <= VIDEO_LIMIT_BYTES:
            return

    raise RuntimeError("WEBM hajmi 256 KB dan katta bo'lib ketdi")


def convert_to_webp(src, dst):

    for quality in ("90", "75", "55", "35"):

        command = [
            "ffmpeg", "-y",
            "-i", src,
            "-frames:v", "1",
            "-vf",
            "scale=512:512:force_original_aspect_ratio=decrease",
            "-c:v", "libwebp",
            "-quality", quality,
            dst
        ]

        run_ffmpeg(command)

        if os.path.getsize(dst) <= PHOTO_LIMIT_BYTES:
            return

    raise RuntimeError("WEBP hajmi 512 KB dan katta bo'lib ketdi")


# ============================================================
# STICKER PACKS
# ============================================================

def get_pack(user_id, sticker_format):

    conn = get_db()

    row = conn.execute("""
        SELECT pack_name, pack_title
        FROM packs
        WHERE user_id = ?
        AND sticker_format = ?
        ORDER BY id DESC
        LIMIT 1
    """, (user_id, sticker_format)).fetchone()

    conn.close()

    return row


def save_pack(user_id, pack_name, pack_title, sticker_format):

    conn = get_db()

    conn.execute("""
        INSERT OR IGNORE INTO packs (
            user_id, pack_name, pack_title,
            sticker_format, created_at
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        user_id,
        pack_name,
        pack_title,
        sticker_format,
        datetime.utcnow().isoformat()
    ))

    conn.commit()
    conn.close()


async def add_to_pack(bot, user_id, file_path, sticker_format):

    input_sticker = InputSticker(
        sticker=Path(file_path).read_bytes(),
        emoji_list=["😀"],
        format=sticker_format
    )

    pack = get_pack(user_id, sticker_format)

    if pack:

        await bot.add_sticker_to_set(
            user_id=user_id,
            name=pack["pack_name"],
            sticker=input_sticker
        )

        return pack["pack_name"]

    me = await bot.get_me()

    pack_name = (
        f"s{user_id}_{uuid.uuid4().hex[:6]}_by_{me.username}"
    )

    pack_title = "Sticker Studio"

    await bot.create_new_sticker_set(
        user_id=user_id,
        name=pack_name,
        title=pack_title,
        stickers=[input_sticker]
    )

    save_pack(user_id, pack_name, pack_title, sticker_format)

    return pack_name


# ============================================================
# HANDLERS
# ============================================================

START_TEXT = (
    "Salom! 👋\n\n"
    "Menga video, GIF yoki rasm yuboring — stikerga aylantiraman.\n\n"
    f"Kunlik limit: {DAILY_STICKER_LIMIT} ta stiker.\n"
    "Yashil fon olib tashlash uchun videoga izoh sifatida "
    "«green» deb yozing."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    register_user(update.effective_user)

    await update.message.reply_text(START_TEXT)


async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if is_admin(user_id):
        await update.message.reply_text("Siz adminsiz — limit yo'q ♾")
        return

    used = get_today_usage(user_id)

    await update.message.reply_text(
        f"Bugun: {used}/{DAILY_STICKER_LIMIT}"
    )


def pick_media(message):

    """(file_obj, kind) qaytaradi; kind = 'video' yoki 'photo'"""

    if message.animation:
        return message.animation, "video"

    if message.video:
        return message.video, "video"

    if message.video_note:
        return message.video_note, "video"

    if message.photo:
        return message.photo[-1], "photo"

    doc = message.document

    if doc and doc.mime_type:

        if doc.mime_type.startswith("video/") \
                or doc.mime_type == "image/gif":
            return doc, "video"

        if doc.mime_type.startswith("image/"):
            return doc, "photo"

    return None, None


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    register_user(user)

    media, kind = pick_media(message)

    if not media:
        await message.reply_text(
            "Video, GIF yoki rasm yuboring."
        )
        return

    size = getattr(media, "file_size", 0) or 0

    if size > MAX_FILE_SIZE:
        await message.reply_text(
            f"Fayl juda katta (maks. {MAX_FILE_MB} MB)."
        )
        return

    if size > 20 * 1024 * 1024:
        await message.reply_text(
            "Telegram bot orqali 20 MB dan katta fayl "
            "yuklab olishga ruxsat bermaydi."
        )
        return

    async with get_user_lock(user.id):

        ok, remaining = consume_sticker(user.id)

        if not ok:
            await message.reply_text(
                "Bugungi limit tugadi. Ertaga qayta urinib ko'ring."
            )
            return

        status = await message.reply_text("⏳ Tayyorlanmoqda...")

        src = TEMP_DIR / f"{uuid.uuid4().hex}_in"
        dst = TEMP_DIR / (
            f"{uuid.uuid4().hex}."
            + ("webm" if kind == "video" else "webp")
        )

        try:

            tg_file = await media.get_file()

            await tg_file.download_to_drive(custom_path=src)

            if kind == "video":

                caption = (message.caption or "").lower()

                green = "green" in caption or "yashil" in caption

                await asyncio.to_thread(
                    convert_to_webm, str(src), str(dst), green
                )

                sticker_format = "video"

            else:

                await asyncio.to_thread(
                    convert_to_webp, str(src), str(dst)
                )

                sticker_format = "static"

            with open(dst, "rb") as f:
                await message.reply_sticker(sticker=f)

            try:

                pack_name = await add_to_pack(
                    context.bot,
                    user.id,
                    str(dst),
                    sticker_format
                )

                text = (
                    "✅ Tayyor!\n"
                    f"Paket: https://t.me/addstickers/{pack_name}"
                )

            except TelegramError:

                logger.exception("Pack error")

                text = (
                    "✅ Stiker tayyor, lekin paketga qo'shib "
                    "bo'lmadi."
                )

            if remaining is not None:
                text += f"\nQolgan limit: {remaining}"

            await status.edit_text(text)

        except Exception:

            logger.exception("Convert error")

            rollback_sticker(user.id)

            await status.edit_text(
                "❌ Xatolik yuz berdi. Limit qaytarildi, "
                "qayta urinib ko'ring."
            )

        finally:

            remove_file(src)
            remove_file(dst)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):

    logger.error(
        "Unhandled error",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN o'rnatilmagan")

    ensure_ffmpeg()

    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("limit", limit_cmd))

    media_filter = (
        filters.VIDEO
        | filters.ANIMATION
        | filters.VIDEO_NOTE
        | filters.PHOTO
        | filters.Document.VIDEO
        | filters.Document.IMAGE
    )

    app.add_handler(MessageHandler(media_filter, handle_media))

    app.add_error_handler(on_error)

    logger.info("Bot ishga tushdi")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
