import logging
import psycopg
import sqlite3
import requests
import urllib3
from datetime import time as dt_time
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import os
import asyncio

# ---------------- Configuration ----------------
BOT_TOKEN = "8258968161:AAHFL2uEIjJJ3I5xNSn66248UaQHRr-Prl0"  # Replace with your new token from BotFather
DATABASE_URL = os.getenv("postgresql://postgres:yOJhoVdkGMQIVELPoFCpqrAHqmgwxUym@postgres.railway.internal:5432/railway")  # For PostgreSQL on Railway
USE_SQLITE = not DATABASE_URL  # Fallback to SQLite if DATABASE_URL not set
if USE_SQLITE:
    DB_FILE = os.path.join(os.path.expanduser("~"), "desco_bot_users.db")
DAILY_TIME = dt_time(hour=21, minute=30, tzinfo=ZoneInfo("Asia/Dhaka"))  # 9:30 PM for testing
INFO_URL = "https://prepaid.desco.org.bd/api/tkdes/customer/getCustomerInfo"
BALANCE_URL = "https://prepaid.desco.org.bd/api/tkdes/customer/getBalance"

# Disable insecure HTTPS warnings (not recommended for production)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- Database helpers ----------------
def init_db():
    if USE_SQLITE:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                account_no TEXT,
                meter_no TEXT,
                threshold REAL DEFAULT 100.0,
                last_balance REAL
            )
            """
        )
    else:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        chat_id BIGINT PRIMARY KEY,
                        account_no TEXT,
                        meter_no TEXT,
                        threshold FLOAT DEFAULT 100.0,
                        last_balance FLOAT
                    )
                    """
                )
            conn.commit()

def add_or_update_user(chat_id: int, account_no: str, meter_no: str, threshold: float = 100.0):
    if USE_SQLITE:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (chat_id, account_no, meter_no, threshold)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET account_no=excluded.account_no, meter_no=excluded.meter_no, threshold=excluded.threshold
            """,
            (chat_id, account_no, meter_no, threshold),
        )
    else:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (chat_id, account_no, meter_no, threshold)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (chat_id) DO UPDATE
                    SET account_no = EXCLUDED.account_no, meter_no = EXCLUDED.meter_no, threshold = EXCLUDED.threshold
                    """,
                    (chat_id, account_no, meter_no, threshold),
                )
            conn.commit()

def remove_user(chat_id: int):
    if USE_SQLITE:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
    else:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE chat_id = %s", (chat_id,))
            conn.commit()

def get_all_users():
    if USE_SQLITE:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT chat_id, account_no, meter_no, threshold, last_balance FROM users")
    else:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT chat_id, account_no, meter_no, threshold, last_balance FROM users")
            rows = cur.fetchall()
        return rows
    rows = cur.fetchall()
    conn.close()
    return rows

def get_user(chat_id: int):
    if USE_SQLITE:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT chat_id, account_no, meter_no, threshold, last_balance FROM users WHERE chat_id = ?", (chat_id,))
    else:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT chat_id, account_no, meter_no, threshold, last_balance FROM users WHERE chat_id = %s", (chat_id,))
                row = cur.fetchone()
            return row
    row = cur.fetchone()
    conn.close()
    return row

def update_last_balance(chat_id: int, balance: float):
    if USE_SQLITE:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("UPDATE users SET last_balance = ? WHERE chat_id = ?", (balance, chat_id))
    else:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET last_balance = %s WHERE chat_id = %s", (balance, chat_id))
            conn.commit()

def set_threshold(chat_id: int, threshold: float):
    if USE_SQLITE:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("UPDATE users SET threshold = ? WHERE chat_id = ?", (threshold, chat_id))
    else:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET threshold = %s WHERE chat_id = %s", (threshold, chat_id))
            conn.commit()

# ---------------- DESCO API helpers ----------------
def fetch_balance(account_no: str, meter_no: str):
    try:
        params = {"accountNo": account_no, "meterNo": meter_no}
        resp = requests.get(BALANCE_URL, params=params, timeout=10, verify=False)
        resp.raise_for_status()
        j = resp.json()
        if j.get("code") == 200 and "data" in j:
            return j["data"]
        return None
    except Exception as e:
        logger.error("Error fetching balance: %s", e)
        return None

def fetch_customer_info(account_no: str, meter_no: str):
    try:
        params = {"accountNo": account_no, "meterNo": meter_no}
        resp = requests.get(INFO_URL, params=params, timeout=10, verify=False)
        resp.raise_for_status()
        j = resp.json()
        if j.get("code") == 200 and "data" in j:
            return j["data"]
        return None
    except Exception as e:
        logger.error("Error fetching customer info: %s", e)
        return None

# ---------------- Telegram command handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info("User %s started registration", chat_id)
    user = get_user(chat_id)
    if user:
        await update.message.reply_text(
            "👋 আপনি ইতোমধ্যেই রেজিস্টারেড।\n\n"
            "👉 /status দিয়ে বর্তমান ব্যালেন্স দেখুন\n"
            "👉 /setthreshold <amount> দিয়ে থ্রেশহোল্ড সেট করুন (উদাহরণ: /setthreshold 150)\n"
            "👉 /stop দিয়ে রেজিস্ট্রেশন বাতিল করুন"
        )
        return
    context.user_data["expect"] = "account"
    await update.message.reply_text(
        "👋 স্বাগতম!\nঅনুগ্রহ করে আপনার DESCO Account Number পাঠান (যেমন: 14039719)\n\n"
        "আপনি তথ্য পাঠালে আমি সেটি সেভ করে রাখবো এবং প্রতিদিন ব্যালেন্স পাঠাব।"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    logger.info("User %s sent text: %s", chat_id, text)
    expect = context.user_data.get("expect")
    if expect == "account":
        context.user_data["account_no"] = text
        context.user_data["expect"] = "meter"
        await update.message.reply_text("✅ পাওয়া গেছে। এখন আপনার Meter Number পাঠান (যেমন: 661120136562)")
        return
    elif expect == "meter":
        account_no = context.user_data.get("account_no")
        meter_no = text
        await update.message.reply_text("অনুগ্রহ করে একটু অপেক্ষা করুন — আপনার তথ্য যাচাই করা হচ্ছে...")
        info = fetch_customer_info(account_no, meter_no)
        if info is None:
            await update.message.reply_text(
                "⚠️ ঠিকঠাক Account/Meter মিললো না অথবা API তে সমস্যা। আবার চেষ্টা করুন।\n"
                "আপনি /start দিয়ে নতুন করে শুরু করতে পারেন।"
            )
            context.user_data.pop("expect", None)
            context.user_data.pop("account_no", None)
            return
        add_or_update_user(chat_id, account_no, meter_no)
        context.user_data.pop("expect", None)
        context.user_data.pop("account_no", None)
        await update.message.reply_text(
            "✅ রেজিস্ট্রেশন সম্পন্ন হয়েছে!\n"
            "আপনি প্রতিদিন " + DAILY_TIME.strftime("%H:%M") + " (Dhaka সময়) ব্যালেন্স পাবেন।\n\n"
            "কমান্ডসমূহ: /status, /setthreshold, /stop, /help"
        )
        return
    if text.lower() in ("status", "স্ট্যাটাস"):
        await cmd_status(update, context)
        return
    await update.message.reply_text(
        "আমি বুঝতে পারিনি। সাহায্যের জন্য /help টাইপ করুন।\n"
        "নতুন রেজিস্ট্রেশন করতে /start ব্যবহার করুন।"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info("User %s requested status", chat_id)
    user = get_user(chat_id)
    if not user:
        await update.message.reply_text("আপনি রেজিস্টার করেননি। রেজিস্ট্রেশন করতে /start টাইপ করুন।")
        return
    _, account_no, meter_no, threshold, last_balance = user
    await update.message.reply_text("অপেক্ষা করুন — DESCO থেকে ব্যালেন্স নেওয়া হচ্ছে...")
    bal = fetch_balance(account_no, meter_no)
    if not bal:
        await update.message.reply_text("দুঃখিত, সার্ভার থেকে তথ্য পাওয়া যায়নি। পরে চেষ্টা করুন।")
        return
    balance = bal.get("balance", 0)
    consumption = bal.get("currentMonthConsumption", "N/A")
    reading_time = bal.get("readingTime", "N/A")
    update_last_balance(chat_id, balance)
    msg = (
        f"💡 আপনার DESCO ব্যালেন্স: ৳{balance}\n"
        f"🔋 এই মাসের ব্যবহার: {consumption} kWh\n"
        f"🕒 রিডিং সময়: {reading_time}\n"
        f"⚙️ থ্রেশহোল্ড: ৳{threshold}"
    )
    if balance <= threshold:
        msg += "\n\n⚠️ সতর্কতা: আপনার ব্যালেন্স থ্রেশহোল্ডের নিচে আছে। অনুগ্রহ করে রিচার্জ করুন।"
    await update.message.reply_text(msg)

async def cmd_setthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info("User %s requested setthreshold: %s", chat_id, context.args)
    args = context.args
    if not args:
        await update.message.reply_text("ব্যবহার: /setthreshold <amount>\nউদাহরণ: /setthreshold 150")
        return
    try:
        val = float(args[0])
        set_threshold(chat_id, val)
        await update.message.reply_text(f"✅ থ্রেশহোল্ড সেট করা হয়েছে: ৳{val}")
    except ValueError:
        await update.message.reply_text("ভুল পরিমাপ। অনুগ্রহ করে একটি সংখ্যা লিখুন।")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info("User %s requested stop", chat_id)
    user = get_user(chat_id)
    if not user:
        await update.message.reply_text("আপনি আগে থেকে রেজিস্টারড নন।")
        return
    remove_user(chat_id)
    await update.message.reply_text("আপনার রেজিস্ট্রেশন বাতিল করা হয়েছে। আর মেসেজ পাঠানো হবে না।")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("User %s requested help", update.effective_chat.id)
    help_text = (
        "🔹 কমান্ডসমূহ:\n"
        "/start - রেজিস্টার করতে\n"
        "/status - এখনকার ব্যালান্স দেখাবে\n"
        "/setthreshold <amount> - ব্যালান্স থ্রেশহোল্ড সেট করবে\n"
        "/stop - রেজিস্ট্রেশন বন্ধ করবে\n"
        "/help - সাহায্য\n\n"
        "রেজিস্ট্রেশনের সময় প্রথমে Account No পাঠাবে, এরপর Meter No।"
    )
    await update.message.reply_text(help_text)

# ---------------- Daily job ----------------
async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running daily job to check balances...")
    users = get_all_users()
    for chat_id, account_no, meter_no, threshold, _ in users:
        try:
            bal = fetch_balance(account_no, meter_no)
            if not bal:
                logger.warning("No balance for %s/%s", account_no, meter_no)
                continue
            balance = bal.get("balance", 0)
            consumption = bal.get("currentMonthConsumption", "N/A")
            reading_time = bal.get("readingTime", "N/A")
            msg = (
                f"💡 আপনার DESCO ব্যালেন্স: ৳{balance}\n"
                f"🔋 এই মাসের ব্যবহার: {consumption} kWh\n"
                f"🕒 রিডিং সময়: {reading_time}\n"
            )
            if balance <= threshold:
                msg += f"\n⚠️ সতর্কতা: ব্যালেন্স আপনার থ্রেশহোল্ড (৳{threshold:.2f}) এর নিচে আছে। দ্রুত রিচার্জ করুন!"
            await context.bot.send_message(chat_id=chat_id, text=msg)
            update_last_balance(chat_id, balance)
        except Exception as e:
            logger.error("Error processing user %s: %s", chat_id, e)

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
    app.job_queue.run_daily(daily_job, time=DAILY_TIME)
    logger.info("Bot started. Polling...")
    try:
        await app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error("Error running application: %s", e)
        raise
    finally:
        try:
            await app.stop()
            await app.shutdown()
        except Exception as e:
            logger.error("Error during shutdown: %s", e)

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            logger.warning("Event loop is already running. Scheduling main task.")
            loop.create_task(main())
        else:
            try:
                loop.run_until_complete(main())
            finally:
                loop.run_until_complete(loop.shutdown_asyncgens())
                if not loop.is_closed():
                    loop.close()
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        raise