import os
import re
import json
import subprocess
import uuid
import logging
import asyncio
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, 
    ReplyKeyboardMarkup, KeyboardButton, InputSticker, 
    BotCommand, LabeledPrice
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, PreCheckoutQueryHandler, ContextTypes, filters
)

from PIL import Image
from rembg import remove, new_session

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "8953691717:AAEftbdxTdAdE-ALhdzujE3Pve_DOtIzCp8")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "979805620"))  # <-- O'z Telegram ID ingizni yozing

PACKS_FILE = "user_packs.json"
USAGE_FILE = "user_usage.json"
user_sessions = {}

# RAM tejamkorligi uchun rembg sessiyasi (faqat rasmlar uchun)
REMBG_SESSION = new_session("u2netp")

# ---------- Health Check Server (Render uchun) ----------
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
    if key not in usage_data:
        usage_data[key] = {
            "free_count": 0,
            "paid_credits": 0,
            "vip_until": None
        }
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

# ---------- Bot Commands ----------
async def post_init(application):
    commands = [
        BotCommand("start", "Botni qayta ishga tushirish"),
        BotCommand("buy", "Stars orqali VIP/Stiker sotib olish"),
        BotCommand("mypacks", "Mening stiker to'plamlarim"),
        BotCommand("users", "Foydalanuvchilar statistikasi (Admin)"),
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
    await update.message.reply_text(
        "Salom! @Vid2Sticker_bot ga xush kelibsiz! 🎬\n\n"
        "Men sizga video va rasmlaringizdan orqa fonsiz animatsion stiker hamda custom emojilar tayyorlab beraman.\n\n"
        "🎁 Dastlabki 3 ta stikeringiz mutlaqo bepul!\n\n"
        "Boshlash uchun \"➕ Yangi to'plam yaratish\" tugmasini bosing.",
        reply_markup=main_menu_markup()
    )

async def show_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u_data = get_user_data(user_id)
    
    if user_id == ADMIN_ID:
        await update.message.reply_text("👑 Siz bot egasisiz! Sizda barcha cheklovlar olib tashlangan (Cheksiz limit).")
        return

    vip_status = "👑 VIP Obunangiz faol!" if is_vip_active(u_data) else "❌ VIP obuna yo'q"
    credits = u_data.get("paid_credits", 0)
    free_used = u_data.get("free_count", 0)
    free_left = max(0, 3 - free_used)

    text = (
        f"📊 Sizning balansingiz:\n"
        f"• Bepul limit: {free_left}/3 ta\n"
        f"• Sotib olingan stikerlar: {credits} ta\n"
        f"• Holat: {vip_status}\n\n"
        "⭐ Telegram Stars orqali tariflar:\n"
        "• 1 ta stiker — 10 Stars\n"
        "• 10 talik paket — 50 Stars (50% chegirma)\n"
        "• 1 haftalik VIP — 100 Stars\n"
        "• 1 oylik VIP — 200 Stars\n"
        "• 1 yillik VIP — 1000 Stars (Eng hamyonbop!)\n\n"
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

async def admin_users_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Bu buyruq faqat bot egasi uchun!")
        return

    usage_data = load_json(USAGE_FILE)
    packs_data = load_json(PACKS_FILE)
    
    total_users = len(usage_data)
    text = f"📊 **Bot foydalanuvchilari statistikasi:**\n\nJami foydalanuvchilar: {total_users} ta\n\n"
    
    count = 0
    for uid, info in usage_data.items():
        if count >= 30:  # Xabar uzun bo'lib ketmasligi uchun oxirgi 30tasini ko'rsatamiz
            text += "\n...va boshqa foydalanuvchilar."
            break
        free = info.get("free_count", 0)
        paid = info.get("paid_credits", 0)
        vip = "Ha" if is_vip_active(info) else "Yo'q"
        p_count = len(packs_data.get(uid, []))
        
        text += f"👤 `{uid}` | Bepul: {free}/3 | Balans: {paid} | VIP: {vip} | To'plamlar: {p_count} ta\n"
        count += 1

    await update.message.reply_text(text, parse_mode="Markdown")

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
        await update.message.reply_text("Sizda hali to'plamlar yo'q. \"➕ Yangi to'plam yaratish\" tugmasini bosing.")
        return
    lines = ["📦 Sizning to'plamlaringiz:\n"]
    for p in packs:
        ptype = "✨ Emoji" if p.get("type") == "custom_emoji" else "🖼 Stiker"
        lines.append(f"• {p['title']} ({ptype})\nhttps://t.me/addstickers/{p['name']}\n")
    await update.message.reply_text("\n".join(lines))

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
        await update.message.reply_text("1️⃣ To'plam uchun nom kiriting (masalan: Mening Stikerlarim):")
    elif text == "📌 Joriy to'plamga qo'shish":
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
        await update.message.reply_text("Sizda hali to'plamlar yo'q. Avval \"➕ Yangi to'plam yaratish\" tugmasini bosing.")
        return
    keyboard = [[InlineKeyboardButton(f"{p['title']} ({'✨ Emoji' if p.get('type')=='custom_emoji' else '🖼 Stiker'})", callback_data=f"addto={p['name']}")] for p in packs]
    await update.message.reply_text("Qaysi to'plamga qo'shmoqchisiz?", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    session = user_sessions.get(user_id)

    if not session or session.get('step') != 'WAITING_FILE':
        await message.reply_text("Iltimos, avval \"➕ Yangi to'plam yaratish\" tugmasini bosib, nom kiriting.")
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
            description=f"@Vid2Sticker_bot xizmati uchun {stars} Stars to'lov",
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
        await query.edit_message_text(f"To'plam nomi: {session['pack_title']}\nTuri: {size_text}\n\n3️⃣ Endi menga rasm yoki video yuboring.")

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
        await query.edit_message_text(f"To'plam: {pack['title']} ({size_text})\n\n2️⃣ Endi menga rasm yoki video yuboring.")

    elif data.startswith("color="):
        if not session or not os.path.exists(session.get('input_path', '')):
            await query.edit_message_text("❌ Fayl topilmadi. Qaytadan boshlang.")
            return
        session['color_choice'] = data.split("=", 1)[1]
        await process_and_add_directly(query, context, user_id)

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

async def process_and_add_directly_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    session = user_sessions.get(user_id)
    if not session:
        return
    
    class FakeQuery:
        def __init__(self, msg):
            self.message = msg
        async def edit_message_text(self, text, **kwargs):
            return await self.message.reply_text(text, **kwargs)

    fake_q = FakeQuery(update.message)
    await process_and_add_directly(fake_q, context, user_id)

# ---------- Processing logic (Rembg + Colorkey) ----------
def process_image_rembg(input_path: str, output_path: str, target_size: int):
    input_img = Image.open(input_path).convert("RGBA")
    input_img.thumbnail((512, 512), Image.Resampling.LANCZOS)
    
    output_img = remove(input_img, session=REMBG_SESSION)

    w, h = output_img.size
    if w > h:
        new_w = target_size
        new_h = int(h * (target_size / w))
    else:
        new_h = target_size
        new_w = int(w * (target_size / h))

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

    # --- ADMIN VA LİMİT TEKSHIRISH ---
    u_data = get_user_data(user_id)
    can_proceed = False

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
        await query.edit_message_text(
            "⚠️ Sizning bepul 3 ta stiker yaratish limitigingiz tugadi!\n\n"
            "Davom etish uchun VIP obuna yoki stiker paketi sotib oling: /buy"
        )
        cleanup_session(user_id)
        return

    await query.edit_message_text("⏳ Stiker ishlanmoqda va to'plamga qo'shilmoqda...")

    output_path = None
    try:
        if media_type == 'video':
            color_code = session.get('color_choice', '0x00FF00')
            if color_code == "auto":
                color_code = await detect_corner_color(input_path)
            tol = "0.3"
            
            vf = (
                f"colorkey={color_code}:{tol}:0.1,"
                f"format=yuva420p,"
                f"scale={target_size}:{target_size}:force_original_aspect_ratio=decrease,"
                f"pad={target_size}:{target_size}:(ow-iw)/2:(oh-ih)/2:color=black@0"
            )
            
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
            f"✅ Stiker muvaffaqiyatli qo'shildi!\n\n"
            f"🔗 To'plam: https://t.me/addstickers/{pack_name}\n"
        )
        
        await query.edit_message_text(res_text)

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
    app.add_handler(CommandHandler("buy", show_tariffs))
    app.add_handler(CommandHandler("mypacks", mypacks))
    app.add_handler(CommandHandler("users", admin_users_stats))
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
