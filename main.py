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

    logger.info(
        "Running FFmpeg"
    )

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
   
