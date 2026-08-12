import os
import re
import json
import subprocess
import uuid
import logging
import asyncio
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

from PIL import Image
from rembg import remove

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, 
    ReplyKeyboardMarkup, KeyboardButton, InputSticker, 
    BotCommand, LabeledPrice
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, PreCheckoutQueryHandler, ContextTypes, filters
)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 123456789))  # O'zingizning Telegram ID'ingizni qo'ying yoki muhit o'zgaruvchisi orqali bering

PACKS_FILE = "user_packs.json"
USAGE_FILE = "user_usage.json"
user_sessions = {}

# ---------- Health Check Server (Render/Railway uchun) ----------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# ---------- Database / Persistence ----------
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

def get_user_data(user_id: int):
    usage_data = load_json(USAGE_FILE)
    key = str(user_id)
    today_str = datetime.now().strftime("%Y-%m-%d")

    if key not in usage_data:
        usage_data[key] = {
            "free_count": 0,
            "paid_credits": 0,
            "vip_until": None,
            "last_usage_date": today_str
        }
        save_json(USAGE_FILE, usage_data)
    else:
        # Yangi kun boshlangan bo'lsa, bepul limitni tiklaymiz (Kunlik 3 ta bepul stiker)
        if usage_data[key].get("last_usage_date") != today_str:
            usage_data[key]["free_count"] = 0
            usage_data[key]["last_usage_date"] = today_str
            save_json(USAGE_FILE, usage_data)

    return usage_data[key]

def update_user_data(user_id: int, user_info: dict):
    usage_data = load_json(USAGE_FILE)
    usage_data[str(user_id)] = user_info
    save_json(USAGE_FILE, usage_data)

def is_vip_active(user_info: dict) -> bool:
    vip_until = user_info.get("vip_until")
    if not vip_until:
        return False
    try:
        expire_dt = datetime.fromisoformat(vip_until)
        return datetime.now() < expire_dt
    except Exception:
        return False

# ---------- Bot Commands & Keyboards ----------
async def post_init(application):
    commands = [
        BotCommand("start", "Botni qayta ishga tushirish"),
        BotCommand("newpack", "➕ Yangi to'plam yaratish"),
        BotCommand("addpack", "📌 Joriy to'plamga qo'shish"),
        BotCommand("buy", "Stars orqali VIP/Stiker sotib olish"),
        BotCommand("mypacks", "Mening stiker to'plamlarim"),
        BotCommand("help", "Yo'riqnoma va yordam")
    ]
    await application.bot.set_my_commands(commands)

def main_menu_markup():
    menu_buttons = [
        [KeyboardButton("➕ Yangi to'plam yaratish")],
        [KeyboardButton("📌 Joriy to'plamga qo'shish")],
        [KeyboardButton("💎 VIP va Tariflar"), KeyboardButton("📦 Mening to'plamlarim")]
    ]
    return ReplyKeyboardMarkup(menu_buttons, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_session(update.effective_user.id)
    # User ma'lumotlarini yaratib qo'yamiz (statistika va bazaga tushishi uchun)
    get_user_data(update.effective_user.id)
    
    await update.message.reply_text(
        "Salom! Xush kelibsiz! 🎬

"
        "Men sizga rasm va videolaringizdan orqa fonsiz sifatli stiker hamda custom emojilar tayyorlab beraman.

"
        "🎁 Har kuni 3 ta stikerni mutlaqo bepul yaratishingiz mumkin!

"
        "Boshlash uchun "➕ Yangi to'plam yaratish" tugmasini bosing.",
        reply_markup=main_menu_markup()
    )

async def start_new_pack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = {'step': 'WAITING_NAME'}
    await update.message.reply_text("1️⃣ To'plam uchun nom kiriting (masalan: Mening Stikerlarim):")

async def show_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u_data = get_user_data(user_id)
    
    vip_status = "👑 VIP Obunangiz faol!" if is_vip_active(u_data) else "❌ VIP obuna yo'q"
    credits = u_data.get("paid_credits", 0)
    free_used = u_data.get("free_count", 0)
    free_left = max(0, 3 - free_used)

    text = (
        f"📊 Sizning balansingiz:
"
        f"• Bugungi bepul limit: {free_left}/3 ta
"
        f"• Sotib olingan stikerlar: {credits} ta
"
        f"• Holat: {vip_status}

"
        "⭐ Telegram Stars orqali tariflar:
"
        "• 1 ta stiker — 10 Stars
"
        "• 10 talik paket — 50 Stars (50% chegirma)
"
        "• 1 haftalik VIP — 100 Stars
"
        "• 1 oylik VIP — 200 Stars
"
        "• 1 yillik VIP — 1000 Stars (Eng hamyonbop!)

"
        "Sotib olmoqchi bo'lgan tarifingizni tanlang:"
    )

    keyboard = [
        [InlineKeyboardButton("⚡ 1 ta stiker (10 ⭐)", callback_data="buy_1"), InlineKeyboardButton("📦 10 talik paket (50 ⭐)", callback_data="buy_10")],
        [InlineKeyboardButton("📅 1 haftalik VIP (100 ⭐)", callback_data="buy_week")],
        [InlineKeyboardButton("🌙 1 oylik VIP (200 ⭐)", callback_data="buy_month")],
        [InlineKeyboardButton("👑 1 yillik VIP (1000 ⭐)", callback_data="buy_year")]
    ]
    
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

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
    title = f"{raw_text.strip()} | @{bot_username}"
    return title[:64]

async def mypacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = load_json(PACKS_FILE)
    packs = data.get(str(user_id), [])
    if not packs:
        await update.message.reply_text("Sizda hali to'plamlar yo'q. "➕ Yangi to'plam yaratish" tugmasini bosing.")
        return
    lines = ["📦 Sizning to'plamlaringiz:
"]
    for p in packs:
        ptype = "✨ Emoji" if p.get("type") == "custom_emoji" else "🖼 Stiker"
        lines.append(f"• {p['title']} ({ptype})
https://t.me/addstickers/{p['name']}
")
    await update.message.reply_text("
".join(lines))

# ---------- Admin Commands ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    usage_data = load_json(USAGE_FILE)
    packs_data = load_json(PACKS_FILE)

    total_users = len(usage_data)
    total_packs = sum(len(packs) for packs in packs_data.values())

    await update.message.reply_text(
        f"📊 **Bot statistikasi:**

"
        f"👤 Jami foydalanuvchilar: **{total_users}** ta
"
        f"📦 Yaratilgan stiker to'plamlar: **{total_packs}** ta",
        parse_mode="Markdown"
    )

# ---------- Text & Media Handlers ----------
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
        await update.message.reply_text(f"To'plam nomi: {text}

2️⃣ To'plam turini tanlang:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if session and session.get('step') == 'WAITING_EMOJI':
        emojis = [e for e in re.split(r'[\s,]+', text.strip()) if e]
        if not emojis:
            emojis = list(text.strip())
        session['emoji_list'] = emojis[:20]
        
        # Orqa fonni AI orqali avtomatik olib tashlash va qo'shish
        await process_and_add_directly(update, context, user_id)
        return

    if text in ["➕ Yangi to'plam yaratish", "/newpack"]:
        await start_new_pack_cmd(update, context)
    elif text in ["📌 Joriy to'plamga qo'shish", "/addpack"]:
        await start_add_to_existing_pack(update, context)
    elif text in ["💎 VIP va Tariflar", "/buy"]:
        await show_tariffs(update, context)
    elif text == "📦 Mening to'plamlarim":
        await mypacks(update, context)

async def start_add_to_existing_pack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = load_json(PACKS_FILE)
    packs = data.get(str(user_id), [])
    if not packs:
        await update.message.reply_text("Sizda hali to'plamlar yo'q. Avval "➕ Yangi to'plam yaratish" tugmasini bosing.")
        return
    keyboard = [[InlineKeyboardButton(f"{p['title']} ({'✨ Emoji' if p.get('type')=='custom_emoji' else '🖼 Stiker'})", callback_data=f"addto={p['name']}")] for p in packs]
    await update.message.reply_text("Qaysi to'plamga qo'shmoqchisiz?", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    session = user_sessions.get(user_id)

    if not session or session.get('step') != 'WAITING_FILE':
        await message.reply_text("Iltimos, avval "➕ Yangi to'plam yaratish" tugmasini bosib, nom kiriting.")
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
        await update.message.reply_text("3️⃣ Ushbu stiker uchun mos keladigan emoji(lar)ni yuboring (masalan: 😂 yoki ❤️):")
    except Exception as e:
        logger.error(f"Media download error: {e}")
        await message.reply_text(f"❌ Faylni yuklab olishda xatolik: {e}")

# ---------- Callbacks & Payments ----------
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    session = user_sessions.get(user_id)

    if data.startswith("buy_"):
        plan = data.split("_")[1]
        prices = {
            "1": (10, "1 ta stiker yaratish"),
            "10": (50, "10 ta stiker yaratish"),
            "week": (100, "1 Haftalik VIP obuna"),
            "month": (200, "1 Oylik VIP obuna"),
            "year": (1000, "1 Yillik VIP obuna")
        }
        stars, title = prices[plan]
        await context.bot.send_invoice(
            chat_id=user_id,
            title=title,
            description=f"Bot xizmati uchun {stars} Stars to'lov",
            payload=f"plan_{plan}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(title, stars)]
        )
        return

    if data.startswith("type="):
        pack_type = data.split("=", 1)[1]
        is_emoji = (pack_type == "custom_emoji")
        bot_username = (await context.bot.get_me()).username
        raw_title = session['raw_title']
        
        session['pack_type'] = pack_type
        session['pack_name'] = sanitize_pack_name(raw_title, user_id, bot_username, is_emoji=is_emoji)
        session['pack_title'] = build_pack_title(raw_title, bot_username)
        session['step'] = 'WAITING_FILE'

        size_text = "100x100 Emoji" if is_emoji else "512x512 Stiker"
        await query.edit_message_text(f"To'plam nomi: {session['pack_title']}
Turi: {size_text}

3️⃣ Endi menga rasm yoki video yuboring.")

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
        size_text = "100x100 Emoji" if pack.get('type') == 'custom_emoji' else "512x512 Stiker"
        await query.edit_message_text(f"To'plam: {pack['title']} ({size_text})

2️⃣ Endi menga rasm yoki video yuboring.")

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    payload = update.message.successful_payment.invoice_payload
    u_data = get_user_data(user_id)
    now = datetime.now()

    if payload == "plan_1":
        u_data["paid_credits"] = u_data.get("paid_credits", 0) + 1
        msg = "🎉 To'lov muvaffaqiyatli! Sizga 1 ta stiker yaratish limiti qo'shildi."
    elif payload == "plan_10":
        u_data["paid_credits"] = u_data.get("paid_credits", 0) + 10
        msg = "🎉 To'lov muvaffaqiyatli! Sizga 10 ta stiker yaratish limiti qo'shildi."
    elif payload == "plan_week":
        u_data["vip_until"] = (now + timedelta(days=7)).isoformat()
        msg = "👑 VIP obunangiz 1 haftaga faollashtirildi!"
    elif payload == "plan_month":
        u_data["vip_until"] = (now + timedelta(days=30)).isoformat()
        msg = "👑 VIP obunangiz 1 oyga faollashtirildi!"
    elif payload == "plan_year":
        u_data["vip_until"] = (now + timedelta(days=365)).isoformat()
        msg = "👑 VIP obunangiz 1 yilga faollashtirildi!"

    update_user_data(user_id, u_data)
    await update.message.reply_text(msg)

# ---------- AI REMBG PROSESSOR & STICKER CREATOR ----------
def process_image_rembg(input_path: str, output_path: str, target_size: int):
    input_img = Image.open(input_path)
    output_img = remove(input_img) # AI orqali orqa fonni o'chirish

    w, h = output_img.size
    if w > h:
        new_w = target_size
        new_h = int(h * (target_size / w))
    else:
        new_h = target_size
        new_w = int(w * (target_size / h))

    resized_img = output_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
    canvas.paste(resized_img, ((target_size - new_w) // 2, (target_size - new_h) // 2), resized_img)
    canvas.save(output_path, "PNG")

def convert_video_to_webm(input_path: str, output_path: str, target_size: int):
    bitrate = "128k" if target_size == 100 else "256k"
    vf = f"scale={target_size}:{target_size}:force_original_aspect_ratio=decrease,pad={target_size}:{target_size}:(ow-iw)/2:(oh-ih)/2:color=black@0"
    ffmpeg_cmd = [
        "ffmpeg", "-y", "-ss", "0", "-t", "3", "-i", input_path,
        "-vf", vf, "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0", "-b:v", bitrate, output_path
    ]
    subprocess.run(ffmpeg_cmd, check=True)

async def process_and_add_directly(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    session = user_sessions.get(user_id)
    input_path = session['input_path']
    media_type = session['media_type']
    pack_type = session.get('pack_type', 'regular')
    emoji_list = session['emoji_list']
    pack_name = session['pack_name']
    pack_title = session['pack_title']
    
    target_size = 100 if pack_type == 'custom_emoji' else 512

    u_data = get_user_data(user_id)
    can_proceed = False

    # Admin uchun bezlimit / VIP foydalanuvchilar
    if user_id == ADMIN_ID or is_vip_active(u_data):
        can_proceed = True
    elif u_data.get("paid_credits", 0) > 0:
        can_proceed = True
        u_data["paid_credits"] -= 1
        update_user_data(user_id, u_data)
    elif u_data.get("free_count", 0) < 3:
        can_proceed = True
        u_data["free_count"] += 1
        update_user_data(user_id, u_data)

    if not can_proceed:
        await update.message.reply_text(
            "⚠️ Sizning bugungi 3 ta bepul stiker yaratish limitigingiz tugadi!

"
            "Ertagacha kutishingiz yoki hoziroq davom etish uchun VIP obuna / stiker paketi sotib olishingiz mumkin: /buy"
        )
        cleanup_session(user_id)
        return

    status_msg = await update.message.reply_text("✂️ Orqa fon olib tashlanmoqda va stiker yaratilmoqda...")

    output_path = None
    try:
        if media_type == 'video':
            output_path = f"out_{user_id}_{str(uuid.uuid4())[:5]}.webm"
            await asyncio.to_thread(convert_video_to_webm, input_path, output_path, target_size)
            st_format = "video"
        else:
            output_path = f"out_{user_id}_{str(uuid.uuid4())[:5]}.png"
            await asyncio.to_thread(process_image_rembg, input_path, output_path, target_size)
            st_format = "static"

        with open(output_path, 'rb') as sticker_file:
            input_sticker = InputSticker(
                sticker=sticker_file, 
                emoji_list=emoji_list, 
                format=st_format
            )
            try:
                await context.bot.add_sticker_to_set(user_id=user_id, name=pack_name, sticker=input_sticker)
            except Exception:
                sticker_file.seek(0)
                input_sticker = InputSticker(
                    sticker=sticker_file, 
                    emoji_list=emoji_list, 
                    format=st_format
                )
                await context.bot.create_new_sticker_set(
                    user_id=user_id,
                    name=pack_name,
                    title=pack_title,
                    stickers=[input_sticker],
                    sticker_type=pack_type
                )

        save_pack_record(user_id, pack_name, pack_title, pack_type)
        
        res_text = (
            f"✅ Stiker muvaffaqiyatli qo'shildi!

"
            f"🔗 To'plam: https://t.me/addstickers/{pack_name}
"
        )
        await status_msg.edit_text(res_text)

    except Exception as e:
        logger.error(f"Sticker process error: {e}")
        await status_msg.edit_text(f"❌ Xatolik yuz berdi: {e}")
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
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newpack", start_new_pack_cmd))
    app.add_handler(CommandHandler("addpack", start_add_to_existing_pack))
    app.add_handler(CommandHandler("buy", show_tariffs))
    app.add_handler(CommandHandler("mypacks", mypacks))
    app.add_handler(CommandHandler("stats", stats))
    
    app.add_handler(MessageHandler(filters.VIDEO | filters.ANIMATION | filters.PHOTO, handle_media))
    app.add_handler(CallbackQueryHandler(button_click))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
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
