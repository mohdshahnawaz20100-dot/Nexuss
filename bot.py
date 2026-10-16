import asyncio
import logging
import os
import sqlite3
from telegram import Update, ChatPermissions
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ChatMemberHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

# ================== DATABASE ==================
DB = "members.db"

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS members (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def save_member(user):
    if user.is_bot:
        return
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        INSERT INTO members (user_id, first_name, username)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name=excluded.first_name,
            username=excluded.username,
            last_seen=CURRENT_TIMESTAMP
    """, (user.id, user.first_name, user.username))
    conn.commit()
    conn.close()

def get_all_members():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT user_id, first_name FROM members")
    rows = c.fetchall()
    conn.close()
    return rows


# ============ TAG ALL (1000+) ============
async def tag_all_members(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    members = get_all_members()
    if not members:
        await context.bot.send_message(chat_id, "❌ Koi member database me nahi hai. Pehle log message bhejein.")
        return

    mentions = [f"[{name}](tg://user?id={uid})" for uid, name in members]

    # 5 mentions per message
    batch_size = 5
    total = len(mentions)

    for i in range(0, total, batch_size):
        batch = mentions[i:i + batch_size]
        text = f"📢 **Attention** ({i+1}-{min(i+batch_size, total)}/{total}):\n" + " ".join(batch)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Batch error: {e}")
        await asyncio.sleep(1.2)


async def tagall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    members = get_all_members()
    await update.message.reply_text(
        f"⏳ Tagging shuru... Total: {len(members)} members\n"
        f"(~{len(members)//5} messages, {len(members)//5} seconds lagenge)"
    )
    asyncio.create_task(tag_all_members(context, update.effective_chat.id))


# ============ TIME SET ============
async def settime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) != 1:
            raise ValueError
        hh, mm = map(int, context.args[0].split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError

        chat_id = update.effective_chat.id
        for job in scheduler.get_jobs():
            if job.id == f"tag_{chat_id}":
                job.remove()

        scheduler.add_job(
            tag_all_members,
            trigger="cron",
            hour=hh, minute=mm,
            args=[context, chat_id],
            id=f"tag_{chat_id}",
            replace_existing=True
        )
        await update.message.reply_text(
            f"✅ Daily tag set: **{hh:02d}:{mm:02d}** IST",
            parse_mode="Markdown"
        )
    except Exception:
        await update.message.reply_text("❌ Usage: `/settime 09:00`", parse_mode="Markdown")


async def removetime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    removed = False
    for job in scheduler.get_jobs():
        if job.id == f"tag_{chat_id}":
            job.remove()
            removed = True
    await update.message.reply_text("✅ Removed" if removed else "❌ Koi time set nahi tha")


# ============ LINK DETECT → MUTE ============
async def link_watcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    # Member save karo
    if msg.from_user:
        save_member(msg.from_user)

    entities = msg.entities or []
    has_link = any(e.type in ("url", "text_link") for e in entities)

    if has_link or "http" in msg.text.lower() or "t.me/" in msg.text.lower():
        try:
            await msg.delete()
            await context.bot.restrict_chat_member(
                chat_id=update.effective_chat.id,
                user_id=msg.from_user.id,
                permissions=ChatPermissions(can_send_messages=False)
            )
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"🔇 {msg.from_user.first_name} ko link bhejne par mute kiya."
            )
        except Exception as e:
            logging.error(f"Mute error: {e}")


# ============ UNMUTE ============
async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Reply karke `/unmute` likho")
        return
    user_id = update.message.reply_to_message.from_user.id
    await context.bot.restrict_chat_member(
        chat_id=update.effective_chat.id,
        user_id=user_id,
        permissions=ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
    )
    await update.message.reply_text("🔊 Unmute ho gaya")


# ============ NEW MEMBER AUTO-MUTE ============
async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if cm.new_chat_member.status == "member" and cm.old_chat_member.status in ("left", "kicked"):
        save_member(cm.new_chat_member.user)
        try:
            await context.bot.restrict_chat_member(
                chat_id=cm.chat.id,
                user_id=cm.new_chat_member.user.id,
                permissions=ChatPermissions(can_send_messages=False)
            )
            await context.bot.send_message(
                chat_id=cm.chat.id,
                text=f"🔇 {cm.new_chat_member.user.first_name} temporarily muted. Admin verify kare."
            )
        except Exception as e:
            logging.error(f"Join mute error: {e}")


# ============ MEMBER COUNTER ============
async def count_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    members = get_all_members()
    await update.message.reply_text(f"👥 Database me {len(members)} members hain")


# ============ START ============
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Bot Commands:**\n\n"
        "`/tagall` - Sabko tag karo\n"
        "`/settime 09:00` - Daily tag time\n"
        "`/removetime` - Time hatao\n"
        "`/unmute` - Reply karke unmute\n"
        "`/count` - Total members\n",
        parse_mode="Markdown"
    )


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("tagall", tagall_cmd))
    app.add_handler(CommandHandler("settime", settime_cmd))
    app.add_handler(CommandHandler("removetime", removetime_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("count", count_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, link_watcher))
    app.add_handler(ChatMemberHandler(new_member, ChatMemberHandler.CHAT_MEMBER))

    scheduler.start()
    print("✅ Bot chal raha hai...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
