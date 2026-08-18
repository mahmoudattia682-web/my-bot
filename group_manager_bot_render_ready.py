# -*- coding: utf-8 -*-
"""
بوت تيليجرام لإدارة الجروبات - نسخة شاملة
المكتبة المطلوبة: python-telegram-bot (v20+) مع خاصية الجدولة (JobQueue)

التثبيت:
    pip install "python-telegram-bot[job-queue]" --upgrade

قبل التشغيل:
    1. اعمل بوت جديد عن طريق @BotFather في تيليجرام واحصل على التوكن (TOKEN)
    2. حط التوكن في المتغير BOT_TOKEN تحت
    3. فعّل صلاحية "Group Admin Rights" للبوت داخل الجروب
    4. عطّل خاصية Privacy Mode من BotFather (/setprivacy -> Disable) عشان البوت
       يقدر يقرأ كل الرسائل مش بس اللي بتبدأ بـ /

ملاحظة عن التوقيت:
    أوقات الجدولة (/schedule) بتتحسب بتوقيت UTC افتراضيًا. لو عايز تظبطها على
    توقيتك المحلي عدّل قيمة SCHEDULE_UTC_OFFSET تحت (مثلاً مصر/السعودية = 3).

ملاحظة عن اشتراط الاشتراك في قناة:
    عشان تفعّل خاصية إجبار الأعضاء على الاشتراك في قناتك، البوت لازم يكون
    "أدمن" في القناة نفسها كمان (مش بس في الجروب) عشان يقدر يتأكد من الأعضاء.

التشغيل:
    python group_manager_bot.py
"""

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, time as dt_time, timezone
from collections import defaultdict, deque

from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
)

# ================== الإعدادات ==================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required.")

# فرق التوقيت بين توقيتك المحلي و UTC (مثلاً مصر/السعودية عادة +3)
SCHEDULE_UTC_OFFSET = int(os.getenv("SCHEDULE_UTC_OFFSET", "3"))


# كلمات ممنوعة (اختياري) - أي رسالة تحتوي على كلمة منها هتتحذف تلقائيًا
BANNED_WORDS = ["كلمة_ممنوعة1", "كلمة_ممنوعة2"]

# لو True، أي رابط (لينك) يتبعت من غير الأدمن هيتحذف تلقائيًا
DELETE_LINKS = True

# عدد التحذيرات قبل ما العضو يتباند تلقائيًا
MAX_WARNINGS = 3

# اسم ملف قاعدة البيانات (لتخزين التحذيرات بشكل دائم حتى لو البوت اتقفل)
DB_FILE = "group_bot.db"

# ===== إعدادات الحماية من الفلود (السبام) =====
FLOOD_ENABLED = True
FLOOD_MAX_MESSAGES = 5       # أقصى عدد رسائل
FLOOD_TIME_WINDOW = 8        # خلال كام ثانية
FLOOD_MUTE_MINUTES = 10      # مدة الكتم التلقائي لو العضو عمل فلود

# ===== نظام نقاط الأعضاء النشطين =====
POINTS_PER_MESSAGE = 1

# نص رسالة الترحيب (هيتحط بدل {name} اسم العضو تلقائيًا)
WELCOME_MESSAGE = "أهلاً بيك يا {name} في الجروب! 👋\nياريت تقرأ القوانين بالأمر /rules"

# نص القوانين
RULES_TEXT = (
    "📜 *قوانين الجروب:*\n"
    "1. ممنوع السب والقذف.\n"
    "2. ممنوع نشر روابط بدون إذن الأدمن.\n"
    "3. الاحترام المتبادل بين الأعضاء.\n"
    "4. مخالفة القوانين تؤدي لتحذير ثم الطرد."
)

# ================== قاعدة البيانات (SQLite) ==================
# التحذيرات بتتخزن هنا بشكل دائم عشان متتصفرش لو البوت اتقفل واشتغل تاني

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS warnings (
            chat_id INTEGER,
            user_id INTEGER,
            count INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS points (
            chat_id INTEGER,
            user_id INTEGER,
            first_name TEXT,
            points INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS media_settings (
            chat_id INTEGER PRIMARY KEY,
            media_blocked INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scheduled_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            hour INTEGER,
            minute INTEGER,
            text TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS required_channel (
            chat_id INTEGER PRIMARY KEY,
            channel_identifier TEXT,
            invite_link TEXT
        )"""
    )
    conn.commit()
    conn.close()


def get_warning_count(chat_id: int, user_id: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def set_warning_count(chat_id: int, user_id: int, count: int):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """INSERT INTO warnings (chat_id, user_id, count) VALUES (?, ?, ?)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET count=excluded.count""",
        (chat_id, user_id, count),
    )
    conn.commit()
    conn.close()


# ---- نقاط الأعضاء النشطين ----

def add_points(chat_id: int, user_id: int, first_name: str, amount: int):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """INSERT INTO points (chat_id, user_id, first_name, points) VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
               points = points + excluded.points,
               first_name = excluded.first_name""",
        (chat_id, user_id, first_name, amount),
    )
    conn.commit()
    conn.close()


def get_points(chat_id: int, user_id: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT points FROM points WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def get_top_points(chat_id: int, limit: int = 10):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT first_name, points FROM points WHERE chat_id=? ORDER BY points DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    conn.close()
    return rows


# ---- إعدادات تقييد الوسائط (صور/فيديوهات) ----

def is_media_blocked(chat_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT media_blocked FROM media_settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    conn.close()
    return bool(row[0]) if row else False


def set_media_blocked(chat_id: int, blocked: bool):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """INSERT INTO media_settings (chat_id, media_blocked) VALUES (?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET media_blocked=excluded.media_blocked""",
        (chat_id, int(blocked)),
    )
    conn.commit()
    conn.close()


# ---- الرسائل المجدولة ----

def add_scheduled_message(chat_id: int, hour: int, minute: int, text: str) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute(
        "INSERT INTO scheduled_messages (chat_id, hour, minute, text) VALUES (?, ?, ?, ?)",
        (chat_id, hour, minute, text),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def delete_scheduled_message(schedule_id: int, chat_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute(
        "DELETE FROM scheduled_messages WHERE id=? AND chat_id=?", (schedule_id, chat_id)
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def get_scheduled_messages(chat_id: int = None):
    conn = sqlite3.connect(DB_FILE)
    if chat_id is None:
        rows = conn.execute("SELECT id, chat_id, hour, minute, text FROM scheduled_messages").fetchall()
    else:
        rows = conn.execute(
            "SELECT id, chat_id, hour, minute, text FROM scheduled_messages WHERE chat_id=?",
            (chat_id,),
        ).fetchall()
    conn.close()
    return rows


# ---- قناة الاشتراك الإجباري ----

def set_required_channel(chat_id: int, channel_identifier: str, invite_link: str):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """INSERT INTO required_channel (chat_id, channel_identifier, invite_link) VALUES (?, ?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET
               channel_identifier=excluded.channel_identifier,
               invite_link=excluded.invite_link""",
        (chat_id, channel_identifier, invite_link),
    )
    conn.commit()
    conn.close()


def remove_required_channel(chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM required_channel WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def get_required_channel(chat_id: int):
    """يرجع (channel_identifier, invite_link) أو None"""
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT channel_identifier, invite_link FROM required_channel WHERE chat_id=?", (chat_id,)
    ).fetchone()
    conn.close()
    return row if row else None


# ================== تتبع الفلود (في الذاكرة) ==================
# بيخزن أوقات آخر رسايل كل عضو عشان يكتشف السبام السريع
message_timestamps: dict[tuple[int, int], deque] = defaultdict(deque)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ================== أدوات مساعدة ==================

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """يتأكد إن اليوزر أدمن في الجروب"""
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        logger.warning(f"فشل التحقق من صلاحيات الأدمن: {e}")
        return False


def get_target_user(update: Update):
    """يجيب اليوزر المستهدف من رسالة الرد (reply)"""
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    return None


# ================== أوامر الأدمن ==================

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("رد على رسالة الشخص اللي عايز تباند عشان الأمر يشتغل.")
        return
    await context.bot.ban_chat_member(update.effective_chat.id, target.id)
    await update.message.reply_text(f"🚫 تم حظر {target.first_name} من الجروب.")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return
    if not context.args:
        await update.message.reply_text("استخدم الأمر كده: /unban <user_id>")
        return
    user_id = int(context.args[0])
    await context.bot.unban_chat_member(update.effective_chat.id, user_id)
    await update.message.reply_text("✅ تم فك الحظر.")


async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("رد على رسالة الشخص اللي عايز تطرده.")
        return
    chat_id = update.effective_chat.id
    await context.bot.ban_chat_member(chat_id, target.id)
    await context.bot.unban_chat_member(chat_id, target.id)  # طرد بدون حظر دائم
    await update.message.reply_text(f"👢 تم طرد {target.first_name} من الجروب.")


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("رد على رسالة الشخص اللي عايز تكتمه.")
        return

    minutes = 0
    if context.args:
        try:
            minutes = int(context.args[0])
        except ValueError:
            pass

    permissions = ChatPermissions(can_send_messages=False)
    until = None
    if minutes > 0:
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    await context.bot.restrict_chat_member(
        update.effective_chat.id, target.id, permissions=permissions, until_date=until
    )
    duration_text = f"لمدة {minutes} دقيقة" if minutes > 0 else "بشكل دائم"
    await update.message.reply_text(f"🔇 تم كتم {target.first_name} {duration_text}.")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("رد على رسالة الشخص اللي عايز تفك كتمه.")
        return
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    await context.bot.restrict_chat_member(
        update.effective_chat.id, target.id, permissions=permissions
    )
    await update.message.reply_text(f"🔊 تم فك كتم {target.first_name}.")


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("رد على رسالة الشخص اللي عايز تحذره.")
        return

    chat_id = update.effective_chat.id
    count = get_warning_count(chat_id, target.id) + 1
    set_warning_count(chat_id, target.id, count)

    if count >= MAX_WARNINGS:
        await context.bot.ban_chat_member(chat_id, target.id)
        set_warning_count(chat_id, target.id, 0)
        await update.message.reply_text(
            f"🚫 {target.first_name} وصل لـ {MAX_WARNINGS} تحذيرات وتم حظره تلقائيًا."
        )
    else:
        await update.message.reply_text(
            f"⚠️ تحذير لـ {target.first_name} ({count}/{MAX_WARNINGS})"
        )


async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("رد على رسالة الشخص اللي عايز تشيل تحذيره.")
        return
    chat_id = update.effective_chat.id
    current = get_warning_count(chat_id, target.id)
    set_warning_count(chat_id, target.id, max(0, current - 1))
    await update.message.reply_text(f"↩️ تم إنقاص تحذير عن {target.first_name}.")


async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("رد على الرسالة اللي عايز تثبتها.")
        return
    await context.bot.pin_chat_message(
        update.effective_chat.id, update.message.reply_to_message.message_id
    )
    await update.message.reply_text("📌 تم تثبيت الرسالة.")


async def cmd_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return
    await context.bot.unpin_all_chat_messages(update.effective_chat.id)
    await update.message.reply_text("📌 تم إلغاء تثبيت كل الرسائل.")


async def cmd_blockmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return
    set_media_blocked(update.effective_chat.id, True)
    await update.message.reply_text("🚫 تم منع إرسال الصور والفيديوهات في الجروب.")


async def cmd_allowmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return
    set_media_blocked(update.effective_chat.id, False)
    await update.message.reply_text("✅ تم السماح بإرسال الصور والفيديوهات في الجروب.")


# ================== الرسائل المجدولة ==================

async def send_scheduled_message(context: ContextTypes.DEFAULT_TYPE):
    """الدالة اللي بتشتغل تلقائيًا في وقت الجدولة وتبعت الرسالة"""
    job = context.job
    await context.bot.send_message(chat_id=job.chat_id, text=job.data["text"])


def register_schedule_job(app, schedule_id: int, chat_id: int, hour: int, minute: int, text: str):
    """يسجل الجوب في الـ JobQueue بناءً على التوقيت المحلي (بعد تحويله لـ UTC)"""
    utc_hour = (hour - SCHEDULE_UTC_OFFSET) % 24
    run_time = dt_time(hour=utc_hour, minute=minute)
    app.job_queue.run_daily(
        send_scheduled_message,
        time=run_time,
        chat_id=chat_id,
        name=f"sched_{schedule_id}",
        data={"text": text},
    )


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "استخدم الأمر كده:\n/schedule HH:MM النص اللي عايز تجدوله\n"
            "مثال: /schedule 09:00 صباح الخير يا شباب! 🌞"
        )
        return

    time_str = context.args[0]
    text = " ".join(context.args[1:])

    try:
        hour_str, minute_str = time_str.split(":")
        hour, minute = int(hour_str), int(minute_str)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ صيغة الوقت غلط. استخدم HH:MM زي 09:00 أو 21:30")
        return

    chat_id = update.effective_chat.id
    schedule_id = add_scheduled_message(chat_id, hour, minute, text)
    register_schedule_job(context.application, schedule_id, chat_id, hour, minute, text)

    await update.message.reply_text(
        f"✅ تم جدولة الرسالة الساعة {time_str} يوميًا (رقم الجدولة: {schedule_id})"
    )


async def cmd_unschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return

    if not context.args:
        await update.message.reply_text("استخدم الأمر كده: /unschedule <رقم الجدولة>\nشوف الأرقام بالأمر /schedules")
        return

    try:
        schedule_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ رقم الجدولة لازم يكون رقم صحيح.")
        return

    chat_id = update.effective_chat.id
    deleted = delete_scheduled_message(schedule_id, chat_id)
    if not deleted:
        await update.message.reply_text("مفيش جدولة بالرقم ده في الجروب ده.")
        return

    jobs = context.application.job_queue.get_jobs_by_name(f"sched_{schedule_id}")
    for job in jobs:
        job.schedule_removal()

    await update.message.reply_text(f"🗑️ تم إلغاء الجدولة رقم {schedule_id}.")


async def cmd_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = get_scheduled_messages(chat_id)
    if not rows:
        await update.message.reply_text("مفيش رسائل مجدولة في الجروب ده.")
        return

    lines = ["🗓️ *الرسائل المجدولة:*\n"]
    for schedule_id, _chat_id, hour, minute, text in rows:
        lines.append(f"#{schedule_id} — {hour:02d}:{minute:02d} — {text}")
    lines.append("\nلإلغاء أي جدولة: /unschedule <الرقم>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ================== اشتراط الاشتراك في قناة ==================

async def is_subscribed_to_channel(context: ContextTypes.DEFAULT_TYPE, channel_username: str, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(channel_username, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"مقدرتش أتحقق من اشتراك العضو في القناة: {e}")
        # لو حصل خطأ (البوت مش أدمن في القناة مثلاً)، منسمحش بالكتابة احتياطًا
        return False


async def cmd_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return

    if not context.args:
        await update.message.reply_text(
            "استخدم الأمر كده:\n"
            "• قناة عامة: /setchannel @اسم_القناة\n"
            "• قناة خاصة: /setchannel -100xxxxxxxxxx https://t.me/+xxxxxxxxxx\n"
            "  (رقم القناة + رابط الدعوة، الاتنين مطلوبين للقنوات الخاصة)\n\n"
            "مهم: البوت لازم يكون أدمن في القناة عشان الخاصية تشتغل."
        )
        return

    channel_input = context.args[0]

    if channel_input.lstrip("-").isdigit():
        # قناة خاصة: لازم رقم القناة + رابط دعوة
        channel_identifier = channel_input
        if len(context.args) < 2:
            await update.message.reply_text(
                "⚠️ القنوات الخاصة محتاجة رابط الدعوة كمان:\n"
                "/setchannel -100xxxxxxxxxx https://t.me/+xxxxxxxxxx"
            )
            return
        invite_link = context.args[1]
    else:
        # قناة عامة
        channel_identifier = channel_input if channel_input.startswith("@") else "@" + channel_input
        invite_link = f"https://t.me/{channel_identifier.lstrip('@')}"

    # تجربة الوصول للقناة للتأكد إن البوت أدمن فيها
    try:
        await context.bot.get_chat(channel_identifier)
    except Exception:
        await update.message.reply_text(
            "⚠️ مقدرتش أوصل للقناة دي. تأكد إن الرقم/اليوزرنيم صحيح وإن البوت أدمن فيها."
        )
        return

    set_required_channel(update.effective_chat.id, channel_identifier, invite_link)
    await update.message.reply_text(
        "✅ تم ربط الجروب بالقناة.\n"
        "أي عضو مش مشترك فيها هتتحذف رسايله ويتطلب منه يشترك الأول."
    )


async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⚠️ الأمر ده للأدمن بس.")
        return
    remove_required_channel(update.effective_chat.id)
    await update.message.reply_text("✅ تم إلغاء اشتراط الاشتراك في القناة.")


# ================== أوامر عامة ==================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! أنا بوت إدارة الجروب 🤖\nاكتب /help عشان تشوف كل الأوامر."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*أوامر الأدمن* (رد على رسالة العضو ثم اكتب الأمر):\n"
        "/ban - حظر عضو\n"
        "/unban <user_id> - فك حظر\n"
        "/kick - طرد عضو\n"
        "/mute <دقايق اختياري> - كتم عضو\n"
        "/unmute - فك كتم عضو\n"
        "/warn - تحذير عضو\n"
        "/unwarn - إنقاص تحذير\n"
        "/pin - تثبيت رسالة (رد عليها)\n"
        "/unpin - إلغاء تثبيت كل الرسائل\n"
        "/blockmedia - منع الصور والفيديوهات\n"
        "/allowmedia - السماح بالصور والفيديوهات\n"
        "/schedule HH:MM النص - جدولة رسالة يومية\n"
        "/unschedule <رقم> - إلغاء جدولة\n"
        "/setchannel @channel أو -100id link - إجبار الاشتراك في قناة قبل الكتابة\n"
        "/removechannel - إلغاء اشتراط القناة\n\n"
        "*أوامر عامة*:\n"
        "/rules - عرض قوانين الجروب\n"
        "/info - معلومات عن الجروب\n"
        "/points - رصيدك من نقاط النشاط\n"
        "/top - قائمة الأعضاء الأكثر نشاطًا\n"
        "/schedules - عرض الرسائل المجدولة"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    members_count = await context.bot.get_chat_member_count(chat.id)
    await update.message.reply_text(
        f"📊 معلومات الجروب:\nالاسم: {chat.title}\nعدد الأعضاء: {members_count}"
    )


async def cmd_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    points = get_points(update.effective_chat.id, user.id)
    await update.message.reply_text(f"⭐ رصيدك من النقاط: {points}")


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_top_points(update.effective_chat.id, limit=10)
    if not rows:
        await update.message.reply_text("لسه مفيش نقاط متسجلة في الجروب ده.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 *الأعضاء الأكثر نشاطًا:*\n"]
    for i, (name, points) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} {name} — {points} نقطة")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ================== الترحيب بالأعضاء الجدد ==================

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        await update.message.reply_text(WELCOME_MESSAGE.format(name=member.first_name))


# ================== كشف الفلود (السبام) ==================

async def check_flood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """يرجع True لو العضو عمل فلود وتم كتمه"""
    if not FLOOD_ENABLED:
        return False

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    key = (chat_id, user_id)
    now = time.time()

    timestamps = message_timestamps[key]
    timestamps.append(now)

    # امسح الرسايل الأقدم من فترة المراقبة
    while timestamps and now - timestamps[0] > FLOOD_TIME_WINDOW:
        timestamps.popleft()

    if len(timestamps) >= FLOOD_MAX_MESSAGES:
        timestamps.clear()
        permissions = ChatPermissions(can_send_messages=False)
        until = datetime.now(timezone.utc) + timedelta(minutes=FLOOD_MUTE_MINUTES)
        try:
            await context.bot.restrict_chat_member(
                chat_id, user_id, permissions=permissions, until_date=until
            )
            await context.bot.send_message(
                chat_id,
                f"🔇 تم كتم {update.effective_user.first_name} لمدة {FLOOD_MUTE_MINUTES} "
                f"دقيقة بسبب إرسال رسائل بشكل مفرط (سبام).",
            )
        except Exception as e:
            logger.warning(f"مقدرتش أكتم العضو: {e}")
        return True
    return False


# ================== فلترة الرسائل (روابط / كلمات ممنوعة) ==================

async def enforce_channel_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """يرجع True لو الرسالة اتحذفت لعدم اشتراك العضو في القناة المطلوبة"""
    chat_id = update.effective_chat.id
    channel_info = get_required_channel(chat_id)
    if not channel_info:
        return False

    channel_identifier, invite_link = channel_info

    user = update.effective_user
    if await is_subscribed_to_channel(context, channel_identifier, user.id):
        return False

    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"مقدرتش أحذف رسالة عضو غير مشترك: {e}")

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📢 اشترك في القناة", url=invite_link)]]
    )
    try:
        await context.bot.send_message(
            chat_id,
            f"⚠️ يا {user.first_name}، لازم تشترك في القناة الأول عشان تقدر تكتب في الجروب.",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning(f"مقدرتش أبعت تنبيه الاشتراك: {e}")
    return True


async def filter_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    text = message.text or message.caption
    if not text:
        return

    user_id = message.from_user.id
    if await is_admin(update, context, user_id):
        return  # الأدمن مستثنى من الفلترة والفلود

    if await enforce_channel_subscription(update, context):
        return

    if await check_flood(update, context):
        return

    # إضافة نقطة نشاط للعضو
    add_points(update.effective_chat.id, user_id, message.from_user.first_name, POINTS_PER_MESSAGE)

    text_lower = text.lower()

    # فلترة الروابط
    if DELETE_LINKS and ("http://" in text_lower or "https://" in text_lower or "t.me/" in text_lower):
        try:
            await message.delete()
            await context.bot.send_message(
                update.effective_chat.id,
                f"🚫 يا {message.from_user.first_name}، ممنوع إرسال روابط في الجروب.",
            )
        except Exception as e:
            logger.warning(f"مقدرتش أحذف الرسالة: {e}")
        return

    # فلترة الكلمات الممنوعة
    for word in BANNED_WORDS:
        if word.lower() in text_lower:
            try:
                await message.delete()
                await context.bot.send_message(
                    update.effective_chat.id,
                    f"🚫 يا {message.from_user.first_name}، الرسالة اتحذفت لمخالفتها القوانين.",
                )
            except Exception as e:
                logger.warning(f"مقدرتش أحذف الرسالة: {e}")
            return


async def filter_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يحذف الصور/الفيديوهات لو ميديا الجروب مقفولة"""
    message = update.message
    if not message:
        return

    chat_id = update.effective_chat.id
    user_id = message.from_user.id

    if await is_admin(update, context, user_id):
        return

    if await enforce_channel_subscription(update, context):
        return

    # فلترة الروابط والكلمات الموجودة في الكابشن أيضًا
    caption = message.caption
    if caption:
        text_lower = caption.lower()

        if DELETE_LINKS and ("http://" in text_lower or "https://" in text_lower or "t.me/" in text_lower):
            try:
                await message.delete()
                await context.bot.send_message(
                    chat_id,
                    f"🚫 يا {message.from_user.first_name}، ممنوع إرسال روابط في الجروب.",
                )
            except Exception as e:
                logger.warning(f"مقدرتش أحذف رسالة الميديا ذات الرابط: {e}")
            return

        for word in BANNED_WORDS:
            if word.lower() in text_lower:
                try:
                    await message.delete()
                    await context.bot.send_message(
                        chat_id,
                        f"🚫 يا {message.from_user.first_name}، الرسالة اتحذفت لمخالفتها القوانين.",
                    )
                except Exception as e:
                    logger.warning(f"مقدرتش أحذف الميديا ذات الكلمة الممنوعة: {e}")
                return

    if not is_media_blocked(chat_id):
        return

    try:
        await message.delete()
        await context.bot.send_message(
            chat_id,
            f"🚫 يا {message.from_user.first_name}، إرسال الصور والفيديوهات ممنوع حاليًا في الجروب.",
        )
    except Exception as e:
        logger.warning(f"مقدرتش أحذف رسالة الميديا: {e}")


# ================== تشغيل البوت ==================

def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # أوامر عامة
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("points", cmd_points))
    app.add_handler(CommandHandler("top", cmd_top))

    # أوامر الأدمن
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("unwarn", cmd_unwarn))
    app.add_handler(CommandHandler("pin", cmd_pin))
    app.add_handler(CommandHandler("unpin", cmd_unpin))
    app.add_handler(CommandHandler("blockmedia", cmd_blockmedia))
    app.add_handler(CommandHandler("allowmedia", cmd_allowmedia))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("unschedule", cmd_unschedule))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("setchannel", cmd_setchannel))
    app.add_handler(CommandHandler("removechannel", cmd_removechannel))

    # إعادة تحميل الرسائل المجدولة المحفوظة من قبل (لو البوت كان اتقفل واشتغل تاني)
    for schedule_id, chat_id, hour, minute, text in get_scheduled_messages():
        register_schedule_job(app, schedule_id, chat_id, hour, minute, text)

    # ترحيب بالأعضاء الجدد
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))

    # فلترة الرسائل النصية العادية
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, filter_messages))

    # فلترة الصور والفيديوهات والملفات
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL, filter_media)
    )

    logger.info("البوت شغال...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
