import os
import re
import json
import subprocess
import uuid
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, 
    ReplyKeyboardMarkup, KeyboardButton, InputSticker, BotCommand
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ContextTypes, filters
)

from PIL import Image
from rembg import remove, new_session

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "8953691717:AAEftbdxTdAdE-ALhdzujE3Pve_DOtIzCp8")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "979805620")) 

PACKS_FILE = "user_packs.json"
user_sessions = {}

REMBG_SESSION = new_session("u2netp")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

def load_json(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def save_pack_record(user_id: int, name: str, title: str, pack_type: str = "regular"):
    data = load_json(PACKS_FILE)
    key = str(user_id)
    packs = data.setdefault(key, [])
    if not any(p["name"] == name for p in packs):
        packs.append({"name": name, "title": title, "type": pack_type})
    data[key] = packs
    save_json(PACKS_FILE, data)

async def post_init(application):
    commands = [
        BotCommand("start", "Botni qayta ishga tushirish"),
        BotCommand("mypacks", "Mening stiker to'plamlarim"),
        BotCommand("help", "Yo'riqnoma va yordam")
    ]
    await application.bot.set_my_commands(commands)

def main_menu_markup():
    menu_buttons = [
        [KeyboardButton("➕ Yangi to'plam yaratish")],
        [KeyboardButton("📌 Joriy to'plamga qo'shish")],
        [KeyboardButton("📦 Mening to'plamlarim")]
    ]
    return ReplyKeyboardMarkup(menu_buttons, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_session(update.effective_user.id)
    await update.message.reply_text(
        "Salom! @Vid2Sticker_bot ga xush kelibsiz! 🎬\n\n"
        "Men sizga video va rasmlaringizdan orqa fonsiz animatsion stiker hamda custom emojilar tayyorlab beraman.\n\n"
        "✨ **Barcha xizmatlar mutlaqo bepul va cheklovlarsiz!**\n\n"
        "Boshlash uchun \"➕ Yangi to'plam yaratish\" tugmasini bosing.",
        reply_markup=main_menu_markup()
    )

def sanitize_pack_name(raw_text: str, user_id: int, bot_username: str, is_emoji: bool = False) -> str:
    suffix = f"_by_{bot_username}"
    prefix = "e_" if is_emoji else ""
    base = re.sub(r'[^A-Za-z0-9]+', '_', raw_text).strip('_')
    if not base:
        base = "pack"
    if not base[0].isalpha():
        base = "p" + base
    core = f"{prefix}{base}_{user_id}"
    max_core_len = 64 - len(suffix)
    core = core[:max_core_len]
    return f"{core}{suffix}"

def build_pack_title(raw_text: str, bot_username: str) -> str:
    return f"{raw_text.strip()} | @{bot_username}"[:64]

async def mypacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = load_json(PACKS_FILE)
    packs = data.get(str(user_id), [])
    if not packs:
        await update.message.reply_text("Sizda hali to'plamlar yo'q.")
        return
    lines = ["📦 Sizning to'plamlaringiz:\n"]
    for p in packs:
        ptype = "✨ Emoji" if p.get("type") == "custom_emoji" else "🖼 Stiker"
        lines.append(f"• {p['title']} ({ptype})\nhttps://t.me/addstickers/{p['name']}\n")
    await update.message.reply_text("\n".join(lines))

async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.message.from_user.id
    session = user_sessions.get(user_id)

    if session and session.get('step') == 'WAITING_NAME':
        session['raw_title'] = text
        session['step'] = 'WAITING_TYPE'
        keyboard = [
            [InlineKeyboardButton("🖼 Oddiy Stiker (512x512)", callback_data="type=regular")],
            [InlineKeyboardButton("✨ Custom Emoji (100x100)", callback_data="type=custom_emoji")]
        ]
        await update.message.reply_text(f"To'plam nomi: {text}\n\n2️⃣ To'plam turini tanlang:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if session and session.get('step') == 'WAITING_EMOJI':
        emojis = [e for e in re.split(r'[\s,]+', text.strip()) if e]
        if not emojis:
            emojis = list(text.strip())
        session['emoji_list'] = emojis[:20]
        
        if session.get('media_type') == 'video':
            session['step'] = 'WAITING_COLOR'
            keyboard = [
                [InlineKeyboardButton("🪄 Avto-aniqlash", callback_data="color=auto")],
                [InlineKeyboardButton("🟢 Yashil", callback_data="color=0x00FF00"), InlineKeyboardButton("⚪ Oq", callback_data="color=0xFFFFFF"), InlineKeyboardButton("🖤 Qora", callback_data="color=0x000000")]
            ]
            await update.message.reply_text("4️⃣ Video orqa fon rangini tanlang:", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            session['color_choice'] = 'rembg'
            await process_and_add_directly_wrapper(update, context, user_id)
        return

    if text == "➕ Yangi to'plam yaratish":
        user_sessions[user_id] = {'step': 'WAITING_NAME'}
        await update.message.reply_text("1️⃣ To'plam uchun nom kiriting:")
    elif text == "📌 Joriy to'plamga qo'shish":
        await start_add_to_existing_pack(update, context)
    elif text == "📦 Mening to'plamlarim":
        await mypacks(update, context)

async def start_add_to_existing_pack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = load_json(PACKS_FILE)
    packs = data.get(str(user_id), [])
    if not packs:
        await update.message.reply_text("Sizda hali to'plamlar yo'q.")
        return
    keyboard = [[InlineKeyboardButton(f"{p['title']}", callback_data=f"addto={p['name']}")] for p in packs]
    await update.message.reply_text("Qaysi to'plamga qo'shmoqchisiz?", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    session = user_sessions.get(user_id)

    if not session or session.get('step') != 'WAITING_FILE':
        await message.reply_text("Iltimos, avval \"➕ Yangi to'plam yaratish\" tugmasini bosing.")
        return

    video = message.video or message.animation
    photo = message.photo[-1] if message.photo else None

    try:
        if video:
            file = await context.bot.get_file(video.file_id)
            media_type = 'video'
            ext = 'mp4'
        elif photo:
            file = await context.bot.get_file(photo.file_id)
            media_type = 'photo'
            ext = 'jpg'
        else:
            await message.reply_text("Iltimos, rasm yoki video yuboring.")
            return

        unique_id = str(uuid.uuid4())[:8]
        input_path = f"input_{user_id}_{unique_id}.{ext}"
        await file.download_to_drive(input_path)
        session['input_path'] = input_path
        session['media_type'] = media_type
        session['step'] = 'WAITING_EMOJI'
        await update.message.reply_text("3️⃣ Ushbu stiker uchun mos keladigan emoji(lar)ni yuboring:")
    except Exception as e:
        logger.error(f"Media download error: {e}")
        await message.reply_text(f"❌ Xatolik: {e}")

async def detect_corner_color(input_path):
    try:
        cmd = [
            "ffmpeg", "-i", input_path, "-vf",
            "split=4[a][b][c][d];[a]crop=1:1:0:0[tl];[b]crop=1:1:iw-1:0[tr];[c]crop=1:1:0:ih-1[bl];[d]crop=1:1:iw-1:ih-1[br];[tl][tr][bl][br]hstack=4,format=rgb24",
            "-vframes", "1", "-f", "rawvideo", "pipe:1"
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if len(res.stdout) >= 3:
            corners = []
            for i in range(0, min(len(res.stdout) - 2, 12), 3):
                r, g, b = res.stdout[i], res.stdout[i + 1], res.stdout[i + 2]
                corners.append(f"0x{r:02X}{g:02X}{b:02X}")
            if corners:
                return max(set(corners), key=corners.count)
    except Exception as e:
        logger.warning(f"Corner detection failed: {e}")
    return "0x00FF00"

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    session = user_sessions.get(user_id)

    if data.startswith("type="):
        pack_type = data.split("=", 1)[1]
        is_emoji = (pack_type == "custom_emoji")
        bot_username = (await context.bot.get_me()).username
        raw_title = session['raw_title']
        
        session['pack_type'] = pack_type
        session['pack_name'] = sanitize_pack_name(raw_title, user_id, bot_username, is_emoji=is_emoji)
        session['pack_title'] = build_pack_title(raw_title, bot_username)
        session['step'] = 'WAITING_FILE'

        await query.edit_message_text(f"To'plam nomi: {session['pack_title']}\n\n3️⃣ Endi menga rasm yoki video yuboring.")

    elif data.startswith("addto="):
        pack_name = data.split("=", 1)[1]
        packs = load_json(PACKS_FILE).get(str(user_id), [])
        pack = next((p for p in packs if p['name'] == pack_name), None)
        if not pack:
            await query.edit_message_text("❌ To'plam topilmadi.")
            return
        user_sessions[user_id] = {
            'step': 'WAITING_FILE',
            'pack_name': pack['name'],
            'pack_title': pack['title'],
            'pack_type': pack.get('type', 'regular')
        }
        await query.edit_message_text(f"To'plam tanlandi.\n\n2️⃣ Endi menga rasm yoki video yuboring.")

    elif data.startswith("color="):
        if not session or not os.path.exists(session.get('input_path', '')):
            await query.edit_message_text("❌ Fayl topilmadi.")
            return
        session['color_choice'] = data.split("=", 1)[1]
        await process_and_add_directly(query, context, user_id)

async def process_and_add_directly_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    session = user_sessions.get(user_id)
    if not session:
        return
    class FakeQuery:
        def __init__(self, msg):
            self.message = msg
        async def edit_message_text(self, text, **kwargs):
            return await self.message.reply_text(text, **kwargs)
    await process_and_add_directly(FakeQuery(update.message), context, user_id)

def process_image_rembg(input_path: str, output_path: str, target_size: int):
    input_img = Image.open(input_path).convert("RGBA")
    input_img.thumbnail((512, 512), Image.Resampling.LANCZOS)
    output_img = remove(input_img, session=REMBG_SESSION)
    w, h = output_img.size
    if w > h:
        new_w, new_h = target_size, int(h * (target_size / w))
    else:
        new_h, new_w = target_size, int(w * (target_size / h))
    resized_img = output_img.resize((max(1, new_w), max(1, new_h)), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
    canvas.paste(resized_img, ((target_size - new_w) // 2, (target_size - new_h) // 2), resized_img)
    canvas.save(output_path, "PNG")

async def process_and_add_directly(query, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    session = user_sessions.get(user_id)
    if not session:
        return

    input_path = session['input_path']
    media_type = session['media_type']
    pack_type = session.get('pack_type', 'regular')
    emoji_list = session['emoji_list']
    pack_name = session['pack_name']
    pack_title = session['pack_title']
    target_size = 100 if pack_type == 'custom_emoji' else 512

    await query.edit_message_text("⏳ Stiker ishlanmoqda va to'plamga qo'shilmoqda...")

    output_path = None
    try:
        if media_type == 'video':
            color_code = session.get('color_choice', '0x00FF00')
            if color_code == "auto":
                color_code = await detect_corner_color(input_path)
            vf = f"colorkey={color_code}:0.3:0.1,format=yuva420p,scale={target_size}:{target_size}:force_original_aspect_ratio=decrease,pad={target_size}:{target_size}:(ow-iw)/2:(oh-ih)/2:color=black@0"
            output_path = f"out_{user_id}_{str(uuid.uuid4())[:5]}.webm"
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-ss", "0", "-t", "3", "-i", input_path,
                "-vf", vf, "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                "-auto-alt-ref", "0", "-b:v", "128k" if target_size==100 else "256k", output_path
            ]
            subprocess.run(ffmpeg_cmd, check=True)
            st_format = "video"
        else:
            output_path = f"out_{user_id}_{str(uuid.uuid4())[:5]}.png"
            await asyncio.to_thread(process_image_rembg, input_path, output_path, target_size)
            st_format = "static"

        with open(output_path, 'rb') as sticker_file:
            input_sticker = InputSticker(sticker=sticker_file, emoji_list=emoji_list, format=st_format)
            try:
                await context.bot.add_sticker_to_set(user_id=user_id, name=pack_name, sticker=input_sticker)
            except Exception:
                sticker_file.seek(0)
                input_sticker = InputSticker(sticker=sticker_file, emoji_list=emoji_list, format=st_format)
                await context.bot.create_new_sticker_set(
                    user_id=user_id, name=pack_name, title=pack_title,
                    stickers=[input_sticker], sticker_type=pack_type
                )

        save_pack_record(user_id, pack_name, pack_title, pack_type)
        await query.edit_message_text(f"✅ Stiker muvaffaqiyatli qo'shildi!\n\n🔗 To'plam: https://t.me/addstickers/{pack_name}")

    except Exception as e:
        logger.error(f"Sticker process error: {e}")
        await query.edit_message_text(f"❌ Xatolik yuz berdi: {e}")
    finally:
        if output_path and os.path.exists(output_path):
            os.remove(output_path)
        cleanup_session(user_id)

def cleanup_session(user_id):
    if user_id in user_sessions:
        s = user_sessions[user_id]
        if s.get('input_path') and os.path.exists(s['input_path']):
            os.remove(s['input_path'])
        del user_sessions[user_id]

async def main_async():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).connect_timeout(30).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mypacks", mypacks))
    app.add_handler(MessageHandler(filters.VIDEO | filters.ANIMATION | filters.PHOTO, handle_media))
    app.add_handler(CallbackQueryHandler(button_click))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message_text))

    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == '__main__':
    threading.Thread(target=run_health_check_server, daemon=True).start()
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        pass
