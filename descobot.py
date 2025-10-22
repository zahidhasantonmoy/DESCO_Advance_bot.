import asyncio
import logging
import os
from datetime import time as dt_time
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import requests
import urllib3
import sqlite3
from time import sleep

# ---------------- Configuration ----------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Use env var if available, else default token
BOT_TOKEN = os.getenv("BOT_TOKEN", "8258968161:AAHFL2uEIjJJ3I5xNSn66248UaQHRr-Prl0")

DAILY_TIME = dt_time(hour=21, minute=30, tzinfo=ZoneInfo("Asia/Dhaka"))
DB_FILE = "desco_bot_users.db"
INFO_URL = "https://prepaid.desco.org.bd/api/tkdes/customer/getCustomerInfo"
BALANCE_URL = "https://prepaid.desco.org.bd/api/tkdes/customer/getBalance"


# ---------------- SQLite Helpers ----------------
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                account_no TEXT,
                meter_no TEXT,
                threshold REAL DEFAULT 100.0,
                last_balance REAL
            )
        """)
        conn.commit()
    logger.info("SQLite database ready at %s", DB_FILE)


def add_or_update_user(chat_id, account_no, meter_no, threshold=100.0):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (chat_id, account_no, meter_no, threshold)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET 
                account_no=excluded.account_no, 
                meter_no=excluded.meter_no, 
                threshold=excluded.threshold
        """, (chat_id, account_no, meter_no, threshold))
        conn.commit()


def get_user(chat_id):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT chat_id, account_no, meter_no, threshold, last_balance FROM users WHERE chat_id=?", (chat_id,))
        return cur.fetchone()


def update_last_balance(chat_id, balance):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET last_balance=? WHERE chat_id=?", (balance, chat_id))
        conn.commit()


def get_all_users():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT chat_id, account_no, meter_no, threshold, last_balance FROM users")
        return cur.fetchall()


def remove_user(chat_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
        conn.commit()


def set_threshold(chat_id, threshold):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE users SET threshold=? WHERE chat_id=?", (threshold, chat_id))
        conn.commit()


# ---------------- DESCO API ----------------
def fetch_balance(account_no, meter_no):
    try:
        params = {"accountNo": account_no, "meterNo": meter_no}
        r = requests.get(BALANCE_URL, params=params, timeout=10, verify=False)
        j = r.json()
        if j.get("code") == 200:
            return j["data"]
    except Exception as e:
        logger.error("Balance fetch error: %s", e)
    return None


def fetch_customer_info(account_no, meter_no):
    try:
        params = {"accountNo": account_no, "meterNo": meter_no}
        r = requests.get(INFO_URL, params=params, timeout=10, verify=False)
        j = r.json()
        if j.get("code") == 200:
            return j["data"]
    except Exception as e:
        logger.error("Customer info error: %s", e)
    return None


# ---------------- Telegram Commands ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 স্বাগতম DESCO বটে! দয়া করে আপনার Account Number পাঠান (e.g. 123456789).")
    context.user_data["expect"] = "account"


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    expect = context.user_data.get("expect")

    if expect == "account":
        if not text.isalnum() or len(text) < 5:
            await update.message.reply_text("⚠️ ভুল Account Number! আবার চেষ্টা করুন।")
            return
        context.user_data["account_no"] = text
        context.user_data["expect"] = "meter"
        await update.message.reply_text("✅ ঠিক আছে! এখন আপনার Meter Number পাঠান (১২ digit).")
        return

    elif expect == "meter":
        if not text.isdigit() or len(text) != 12:
            await update.message.reply_text("⚠️ ভুল Meter Number! এটি ১২ digit হতে হবে।")
            return
        account_no = context.user_data.get("account_no")
        meter_no = text
        await update.message.reply_text("⏳ একটু অপেক্ষা করুন — তথ্য যাচাই করা হচ্ছে...")
        info = fetch_customer_info(account_no, meter_no)
        if info is None:
            await update.message.reply_text("⚠️ ভুল তথ্য বা সার্ভারে সমস্যা! আবার চেষ্টা করুন। /start লিখে নতুনভাবে শুরু করুন।")
            return
        add_or_update_user(chat_id, account_no, meter_no)
        await update.message.reply_text(
            "✅ রেজিস্ট্রেশন সম্পন্ন!\n"
            f"প্রতিদিন রাত {DAILY_TIME.strftime('%H:%M')} এ আপনি ব্যালেন্স আপডেট পাবেন।\n\n"
            "কমান্ডসমূহ:\n/status\n/setthreshold\n/stop\n/help"
        )
        context.user_data.clear()
        return

    if text.lower() in ("status", "স্ট্যাটাস"):
        await cmd_status(update, context)
        return

    await update.message.reply_text("❓ বুঝতে পারিনি। সাহায্যের জন্য /help লিখুন।")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user(chat_id)
    if not user:
        await update.message.reply_text("আপনি এখনো রেজিস্টার করেননি! /start লিখে শুরু করুন।")
        return
    _, account_no, meter_no, threshold, _ = user
    await update.message.reply_text("🔍 আপনার ব্যালেন্স চেক করা হচ্ছে...")
    data = fetch_balance(account_no, meter_no)
    if not data:
        await update.message.reply_text("⚠️ সার্ভার থেকে তথ্য আনা যাচ্ছে না। পরে চেষ্টা করুন।")
        return
    balance = data.get("balance", 0)
    usage = data.get("currentMonthConsumption", "N/A")
    time = data.get("readingTime", "N/A")
    update_last_balance(chat_id, balance)

    msg = f"💡 DESCO ব্যালেন্স: ৳{balance}\n🔋 মাসিক খরচ: {usage} kWh\n🕒 রিডিং টাইম: {time}\n⚙️ থ্রেশহোল্ড: ৳{threshold}"
    if balance <= threshold:
        msg += "\n\n⚠️ সতর্কতা: ব্যালেন্স কমে গেছে, দয়া করে রিচার্জ করুন।"
    await update.message.reply_text(msg)


async def cmd_setthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        await update.message.reply_text("ব্যবহার: /setthreshold <amount>\nউদাহরণ: /setthreshold 150")
        return
    try:
        val = float(args[0])
        set_threshold(chat_id, val)
        await update.message.reply_text(f"✅ থ্রেশহোল্ড সেট হয়েছে: ৳{val}")
    except ValueError:
        await update.message.reply_text("⚠️ দয়া করে সঠিক সংখ্যা দিন।")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    remove_user(chat_id)
    await update.message.reply_text("❌ আপনি সফলভাবে আনরেজিস্টার হয়েছেন। আর কোনো নোটিফিকেশন পাবেন না।")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔹 কমান্ড লিস্ট:\n"
        "/start - রেজিস্ট্রেশন শুরু করুন\n"
        "/status - ব্যালেন্স দেখুন\n"
        "/setthreshold <amount> - ব্যালেন্স থ্রেশহোল্ড সেট করুন\n"
        "/stop - রেজিস্ট্রেশন বন্ধ করুন\n"
        "/help - সাহায্য মেনু"
    )


# ---------------- Daily Job ----------------
async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    users = get_all_users()
    for chat_id, account_no, meter_no, threshold, _ in users:
        data = fetch_balance(account_no, meter_no)
        if not data:
            continue
        balance = data.get("balance", 0)
        usage = data.get("currentMonthConsumption", "N/A")
        time = data.get("readingTime", "N/A")
        msg = f"💡 ব্যালেন্স: ৳{balance}\n🔋 ব্যবহার: {usage} kWh\n🕒 সময়: {time}"
        if balance <= threshold:
            msg += f"\n⚠️ সতর্কতা: আপনার ব্যালেন্স ৳{threshold} এর নিচে। রিচার্জ প্রয়োজন!"
        await context.bot.send_message(chat_id=chat_id, text=msg)
        update_last_balance(chat_id, balance)


# ---------------- Main ----------------
async def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("setthreshold", cmd_setthreshold))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    job = app.job_queue
    job.run_daily(daily_job, time=DAILY_TIME, name="daily_job")

    logger.info("DESCO Bot started successfully!")
    await app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.run(main())
