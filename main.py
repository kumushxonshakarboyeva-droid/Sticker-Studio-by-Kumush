import os
import re
import sqlite3
import logging
import asyncio
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from telegram import (
    Update,
    InputSticker,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import StickerFormat
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

# BU YERGA O'Z TELEGRAM ID INGIZNI ENV ORQALI BERISH TAVSIYA ETILADI
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))

# Oddiy user uchun kunlik limit
DAILY_STICKER_LIMIT = int(
    os.getenv("DAILY_STICKER_LIMIT", "20")
)

DB_PATH = os.getenv("DB_PATH", "bot.db")

MAX_FILE_SIZE_MB = int(
    os.getenv("MAX_FILE_SIZE_MB", "50")
)

MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

TEMP_DIR = Path(
    os.getenv("TEMP_DIR", tempfile.gettempdir())
)

TEMP_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger("StickerBot")


# ============================================================
# GLOBAL SESSION STORAGE
# ============================================================

user_sessions = {}


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
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
        CREATE TABLE IF NOT EXISTS sticker_packs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            pack_name TEXT NOT NULL,
            pack_title TEXT NOT NULL,
            pack_type TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_last_seen
        ON users(last_seen)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_daily_usage_date
        ON daily_usage(usage_date)
    """)

    conn.commit()
    conn.close()

    logger.info("Database initialized")


# ============================================================
# USER MANAGEMENT
# ============================================================

def is_admin(user_id: int) -> bool:
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
        user.last_name,
    ))

    conn.commit()
    conn.close()


# ============================================================
# LIMIT SYSTEM
# ============================================================

def get_today_usage(user_id: int) -> int:
    today = date.today().isoformat()

    conn = get_db()

    row = conn.execute("""
        SELECT sticker_count
        FROM daily_usage
        WHERE user_id = ?
        AND usage_date = ?
    """, (
        user_id,
        today,
    )).fetchone()

    conn.close()

    if row:
        return int(row["sticker_count"])

    return 0


def get_remaining_limit(user_id: int):
    if is_admin(user_id):
        return None

    used = get_today_usage(user_id)

    return max(
        0,
        DAILY_STICKER_LIMIT - used
    )


def consume_sticker(user_id: int):
    """
    Atomically consumes one daily sticker.
    Admin has unlimited usage.
    """

    if is_admin(user_id):
        return True, None

    today = date.today().isoformat()

    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("""
            SELECT sticker_count
            FROM daily_usage
            WHERE user_id = ?
            AND usage_date = ?
        """, (
            user_id,
            today,
        )).fetchone()

        current_usage = (
            int(row["sticker_count"])
            if row else 0
        )

        if current_usage >= DAILY_STICKER_LIMIT:
            conn.rollback()

            return False, 0

        if row:

            conn.execute("""
                UPDATE daily_usage
                SET sticker_count =
                    sticker_count + 1

                WHERE user_id = ?
                AND usage_date = ?
            """, (
                user_id,
                today,
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
                today,
            ))

        conn.commit()

        remaining = (
            DAILY_STICKER_LIMIT
            - current_usage
            - 1
        )

        return True, remaining

    except Exception:
        conn.rollback()

        logger.exception(
            "Failed to consume sticker"
        )

        raise

    finally:
        conn.close()


# ============================================================
# PACK DATABASE
# ============================================================

def save_pack_record(
    user_id,
    pack_name,
    pack_title,
    pack_type,
):
    conn = get_db()

    conn.execute("""
        INSERT INTO sticker_packs (
            user_id,
            pack_name,
            pack_title,
            pack_type
        )
        VALUES (?, ?, ?, ?)
    """, (
        user_id,
        pack_name,
        pack_title,
        pack_type,
    ))

    conn.commit()
    conn.close()


def get_user_packs(user_id):
    conn = get_db()

    rows = conn.execute("""
        SELECT
            pack_name,
            pack_title,
            pack_type,
            created_at

        FROM sticker_packs

        WHERE user_id = ?

        ORDER BY id DESC
    """, (
        user_id,
    )).fetchall()

    conn.close()

    return rows


# ============================================================
# FILE CLEANUP
# ============================================================

def safe_remove(path):
    if not path:
        return

    try:
        path = Path(path)

        if path.exists():
            path.unlink()

    except Exception:
        logger.exception(
            "Could not remove file: %s",
            path,
        )


def cleanup_session(user_id):
    session = user_sessions.pop(
        user_id,
        None,
    )

    if not session:
        return

    input_path = session.get(
        "input_path"
    )

    output_path = session.get(
        "output_path"
    )

    safe_remove(input_path)
    safe_remove(output_path)


# ============================================================
# FFmpeg
# ============================================================

def run_ffmpeg(command):
    logger.info(
        "Running FFmpeg: %s",
        " ".join(map(str, command)),
    )

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if result.returncode != 0:

        logger.error(
            "FFmpeg error:\n%s",
            result.stderr,
        )

        raise RuntimeError(
            "FFmpeg processing failed"
        )

    return result


def convert_to_webm(
    input_path,
    output_path,
):
    """
    Converts video/GIF to transparent WebM-compatible
    animated sticker format.

    NOTE:
    This version expects the input to already have
    transparency if transparency is required.
    """

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

        "-c:v",
        "libvpx-vp9",

        "-pix_fmt",
        "yuva420p",

        "-b:v",
        "0",

        "-crf",
        "35",

        "-an",

        "-t",
        "3",

        str(output_path),
    ]

    run_ffmpeg(command)


def convert_photo_to_png(
    input_path,
    output_path,
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

        str(output_path),
    ]

    run_ffmpeg(command)


# ============================================================
# PACK NAME
# ============================================================

def sanitize_pack_name(text):
    text = text.lower()

    text = re.sub(
        r"[^a-z0-9_]",
        "_",
        text,
    )

    text = re.sub(
        r"_+",
        "_",
        text,
    )

    text = text.strip("_")

    if not text:
        text = "sticker"

    return text[:30]


def make_pack_name(user_id):
    """
    Telegram sticker pack names must be unique.
    """

    bot_suffix = "by_my_sticker_bot"

    return (
        f"pack_{user_id}_"
        f"{date.today().strftime('%m%d')}_"
        f"{bot_suffix}"
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    register_user(user)

    if is_admin(user.id):

        text = (
            "👑 <b>Admin panel</b>\n\n"
            "Sizda kunlik limit yo'q.\n\n"
            "📊 /stats — statistika\n"
            "👥 /users — foydalanuvchilar\n"
            "🎨 /mypacks — packlar"
        )

    else:

        remaining = get_remaining_limit(
            user.id
        )

        text = (
            "👋 <b>Sticker Bot</b>\n\n"
            "🎨 Video, GIF yoki rasm yuboring "
            "va uni stickerga aylantiring.\n\n"
            f"📊 Bugungi limitingiz: "
            f"{remaining}/{DAILY_STICKER_LIMIT}\n\n"
            "🔄 Limit har kuni avtomatik yangilanadi."
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# ============================================================
# LIMIT COMMAND
# ============================================================

async def limit_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    register_user(user)

    if is_admin(user.id):

        await update.message.reply_text(
            "👑 Siz ADMINsiz.\n\n"
            "♾️ Kunlik limit: CHEKSIZ"
        )

        return

    used = get_today_usage(
        user.id
    )

    remaining = max(
        0,
        DAILY_STICKER_LIMIT - used
    )

    await update.message.reply_text(
        "📊 <b>Bugungi limitingiz</b>\n\n"
        f"🎨 Ishlatilgan: {used}\n"
        f"🟢 Qolgan: {remaining}\n"
        f"📦 Jami limit: "
        f"{DAILY_STICKER_LIMIT}\n\n"
        "🔄 Ertaga avtomatik reset bo'ladi.",
        parse_mode="HTML",
    )


# ============================================================
# MEDIA HANDLER
# ============================================================

async def handle_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
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
            f"📦 Kunlik limit: "
            f"{DAILY_STICKER_LIMIT} ta\n"
            "🔄 Limit ertaga yangilanadi.",
            parse_mode="HTML",
        )

        return

    input_path = None
    output_path = None

    try:

        # ----------------------------------------------------
        # DETERMINE MEDIA
        # ----------------------------------------------------

        if update.message.video:

            media = update.message.video

            extension = ".mp4"

            file_size = media.file_size or 0

        elif update.message.animation:

            media = update.message.animation

            extension = ".gif"

            file_size = media.file_size or 0

        elif update.message.photo:

            media = update.message.photo[-1]

            extension = ".jpg"

            file_size = media.file_size or 0

        else:

            await update.message.reply_text(
                "❌ Qo'llab-quvvatlanmaydigan format."
            )

            return

        if file_size > MAX_FILE_SIZE:

            await update.message.reply_text(
                "❌ Fayl juda katta.\n\n"
                f"📦 Maksimal hajm: "
                f"{MAX_FILE_SIZE_MB} MB"
            )

            return

        # ----------------------------------------------------
        # TEMP FILES
        # ----------------------------------------------------

        unique_name = (
            f"{user_id}_"
            f"{update.message.message_id}"
        )

        input_path = (
            TEMP_DIR /
            f"{unique_name}{extension}"
        )

        output_path = (
            TEMP_DIR /
            f"{unique_name}.webm"
        )

        user_sessions[user_id] = {
            "input_path": str(input_path),
            "output_path": str(output_path),
        }

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        status_message = await update.message.reply_text(
            "⏳ <b>Fayl yuklanmoqda...</b>",
            parse_mode="HTML",
        )

        telegram_file = await media.get_file()

        await telegram_file.download_to_drive(
            custom_path=str(input_path)
        )

        await status_message.edit_text(
            "⚙️ <b>Sticker tayyorlanmoqda...</b>",
            parse_mode="HTML",
        )

        # ----------------------------------------------------
        # PROCESS
        # ----------------------------------------------------

        if update.message.photo:

            await asyncio.to_thread(
                convert_photo_to_png,
                input_path,
                output_path,
            )

            # Static sticker uchun PNG kerak.
            sticker_file_path = output_path

            sticker_format = "static"

        else:

            await asyncio.to_thread(
                convert_to_webm,
                input_path,
                output_path,
            )

            sticker_file_path = output_path

            sticker_format = "video"

        # ----------------------------------------------------
        # PACK
        # ----------------------------------------------------

        pack_name = make_pack_name(
            user_id
        )

        pack_title = (
            f"{user.first_name or 'User'} "
            f"Sticker Pack"
        )

        emoji_list = ["😀"]

        # ----------------------------------------------------
        # CREATE STICKER
        # ----------------------------------------------------

        with open(
            sticker_file_path,
            "rb",
        ) as sticker_file:

            input_sticker = InputSticker(
                sticker=sticker_file,
                emoji_list=emoji_list,
            )

            try:

                await context.bot.create_new_sticker_set(
                    user_id=user_id,
                    name=pack_name,
                    title=pack_title,
                    stickers=[input_sticker],
                    sticker_format=sticker_format,
                )

            except TelegramError as create_error:

                logger.error(
                    "Sticker pack creation error: %s",
                    create_error,
                )

                await update.message.reply_text(
                    "❌ Sticker pack yaratishda "
                    "Telegram xatosi yuz berdi.\n\n"
                    f"<code>{create_error}</code>",
                    parse_mode="HTML",
                )

                return

        # ----------------------------------------------------
        # SAVE DATABASE
        # ----------------------------------------------------

        save_pack_record(
            user_id=user_id,
            pack_name=pack_name,
            pack_title=pack_title,
            pack_type=sticker_format,
        )

        # ----------------------------------------------------
        # RESULT
        # ----------------------------------------------------

        if is_admin(user_id):

            limit_text = (
                "👑 Siz admin: ♾️ Unlimited"
            )

        else:

            limit_text = (
                f"📊 Bugun qolgan: "
                f"{remaining} ta"
            )

        await status_message.edit_text(
            "✅ <b>Sticker muvaffaqiyatli yaratildi!</b>\n\n"
            f"🔗 "
            f"https://t.me/addstickers/{pack_name}\n\n"
            f"{limit_text}",
            parse_mode="HTML",
        )

    except Exception as error:

        logger.exception(
            "Sticker process error"
        )

        # Agar processing muvaffaqiyatsiz bo'lsa,
        # sarflangan limitni qaytarish uchun
        # usage rollback qilamiz.
        if not is_admin(user_id):

            rollback_usage(
                user_id
            )

        await update.message.reply_text(
            "❌ <b>Xatolik yuz berdi.</b>\n\n"
            "Iltimos, boshqa fayl bilan urinib ko'ring.",
            parse_mode="HTML",
        )

    finally:

        cleanup_session(
            user_id
        )


# ============================================================
# ROLLBACK LIMIT
# ============================================================

def rollback_usage(user_id):
    if is_admin(user_id):
        return

    today = date.today().isoformat()

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
            today,
        ))

        conn.commit()

    finally:

        conn.close()


# ============================================================
# MY PACKS
# ============================================================

async def mypacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    register_user(user)

    packs = get_user_packs(
        user.id
    )

    if not packs:

        await update.message.reply_text(
            "📦 Sizda hali sticker pack yo'q."
        )

        return

    lines = [
        "🎨 <b>Sizning sticker packlaringiz:</b>\n"
    ]

    for index, pack in enumerate(
        packs,
        start=1,
    ):

        lines.append(
            f"{index}. "
            f"<a href=\"https://t.me/addstickers/"
            f"{pack['pack_name']}\">"
            f"{pack['pack_title']}"
            f"</a>"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ============================================================
# ADMIN STATS
# ============================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    register_user(user)

    if not is_admin(user.id):

        await update.message.reply_text(
            "⛔ Bu buyruq faqat admin uchun."
        )

        return

    conn = get_db()

    total_users = conn.execute("""
        SELECT COUNT(*) AS count
        FROM users
    """).fetchone()["count"]

    today = date.today().isoformat()

    today_active = conn.execute("""
        SELECT COUNT(*) AS count

        FROM daily_usage

        WHERE usage_date = ?
        AND sticker_count > 0
    """, (
        today,
    )).fetchone()["count"]

    today_stickers = conn.execute("""
        SELECT COALESCE(
            SUM(sticker_count),
            0
        ) AS count

        FROM daily_usage

        WHERE usage_date = ?
    """, (
        today,
    )).fetchone()["count"]

    total_stickers = conn.execute("""
        SELECT COALESCE(
            SUM(sticker_count),
            0
        ) AS count

        FROM daily_usage
    """).fetchone()["count"]

    total_packs = conn.execute("""
        SELECT COUNT(*) AS count
        FROM sticker_packs
    """).fetchone()["count"]

    conn.close()

    await update.message.reply_text(
        "📊 <b>BOT STATISTIKASI</b>\n\n"

        f"👥 Jami foydalanuvchilar: "
        f"<b>{total_users}</b>\n"

        f"🟢 Bugun faol: "
        f"<b>{today_active}</b>\n"

        f"🎨 Bugungi stickerlar: "
        f"<b>{today_stickers}</b>\n"

        f"🎨 Jami stickerlar: "
        f"<b>{total_stickers}</b>\n"

        f"📦 Jami packlar: "
        f"<b>{total_packs}</b>\n\n"

        f"👑 Admin: <b>Unlimited</b>\n"
        f"👤 User limit: "
        f"<b>{DAILY_STICKER_LIMIT}/kun</b>",
        parse_mode="HTML",
    )


# ============================================================
# ADMIN USERS
# ============================================================

async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not is_admin(user.id):

        await update.message.reply_text(
            "⛔ Bu buyruq faqat admin uchun."
        )

        return

    conn = get_db()

    rows = conn.execute("""
        SELECT
            user_id,
            username,
            first_name,
            last_name,
            created_at

        FROM users

        ORDER BY last_seen DESC

        LIMIT 50
    """).fetchall()

    conn.close()

    if not rows:

        await update.message.reply_text(
            "👥 Hali foydalanuvchilar yo'q."
        )

        return

    lines = [
        "👥 <b>Foydalanuvchilar</b>\n"
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
            f"• {name} — "
            f"{username}\n"
            f"  ID: <code>{row['user_id']}</code>"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


# ============================================================
# BUY
# ============================================================

async def show_tariffs(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
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
        "💎 <b>Tariflar</b>\n\n"

        "🆓 FREE\n"
        f"• {DAILY_STICKER_LIMIT} sticker / kun\n\n"

        "💎 PREMIUM\n"
        "• Ko'proq sticker\n"
        "• Yuqori limit\n"
        "• Premium funksiyalar\n\n"

        "🚧 Premium tizimi tez orada ishga tushadi.",
        parse_mode="HTML",
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def handle_message_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    register_user(user)

    await update.message.reply_text(
        "🎨 Sticker yaratish uchun "
        "video, GIF yoki rasm yuboring.\n\n"
        "📊 /limit — bugungi limit\n"
        "🎨 /mypacks — packlaringiz"
    )


# ============================================================
# CALLBACK
# ============================================================

async def button_click(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()


# ============================================================
# HEALTH SERVER
# ============================================================

def run_health_check_server():
    """
    Optional health endpoint for Render/Replit-like platforms.
    """

    try:

        from http.server import (
            BaseHTTPRequestHandler,
            HTTPServer,
        )

        class HealthHandler(
            BaseHTTPRequestHandler
        ):

            def do_GET(self):

                self.send_response(200)

                self.send_header(
                    "Content-Type",
                    "text/plain",
                )

                self.end_headers()

                self.wfile.write(
                    b"Sticker Bot is running"
                )

            def log_message(
                self,
                format,
                *args,
            ):
                return

        port = int(
            os.getenv(
                "PORT",
                "8080"
            )
        )

        server = HTTPServer(
            (
                "0.0.0.0",
                port,
            ),
            HealthHandler,
        )

        logger.info(
            "Health server running on port %s",
            port,
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
            "BOT_TOKEN environment variable is missing."
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

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "buy",
            show_tariffs,
        )
    )

    application.add_handler(
        CommandHandler(
            "limit",
            limit_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "mypacks",
            mypacks,
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats,
        )
    )

    application.add_handler(
        CommandHandler(
            "users",
            users_command,
        )
    )

    # --------------------------------------------------------
    # MEDIA
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.VIDEO |
            filters.ANIMATION |
            filters.PHOTO,
            handle_media,
        )
    )

    # --------------------------------------------------------
    # CALLBACK
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            button_click
        )
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT &
            ~filters.COMMAND,
            handle_message_text,
        )
    )

    logger.info(
        "Sticker Bot starting..."
    )

    application.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    # Health server
    threading = None

    try:

        import threading

        threading.Thread(
            target=run_health_check_server,
            daemon=True,
        ).start()

    except Exception:

        logger.exception(
            "Could not start health server"
        )

    try:

        main()

    except (
        KeyboardInterrupt,
        SystemExit,
    ):

        logger.info(
            "Bot stopped"
)
