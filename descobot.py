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

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Suppress SSL warnings for DESCO API
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------- Configuration ----------------
BOT_TOKEN = "8258968161:AAHFL2uEIjJJ3I5xNSn66248UaQHRr-Prl0"  # Hardcoded for testing; use env var for production
DAILY_TIME = dt_time(hour=21, minute=30, tzinfo=ZoneInfo("Asia/Dhaka"))
DB_FILE = "desco_bot_users.db"  # Local SQLite database file
INFO_URL = "https://prepaid.desco.org.bd/api/tkdes/customer/getCustomerInfo"
BALANCE_URL = "https://prepaid.desco.org.bd/api/tkdes/customer/getBalance"

# ---------------- SQLite Database helpers ----------------
def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
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
            conn.commit()
            logger.info("SQLite database initialized at %s", DB_FILE)
    except sqlite3.Error as e:
        logger.error("SQLite initialization failed: %s", e)
        raise

def add_or_update_user(chat_id: int, account_no: str, meter_no: str, threshold: float = 100.0):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (chat_id, account_no, meter_no, threshold)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET 
            account_no=excluded.account_no, 
            meter_no=excluded.meter_no, 
            threshold=excluded.threshold
            """,
            (chat_id, account_no, meter_no, threshold),
        )
        conn.commit()
    logger.info("User %s registered with account_no %s and meter_no %s", chat_id, account_no, meter_no)

def remove_user(chat_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        conn.commit()
    logger.info("Removed user %s", chat_id)

def get_all_users():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT chat_id, account_no, meter_no, threshold, last_balance FROM users")
        return cur.fetchall()

def get_user(chat_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT chat_id, account_no, meter_no, threshold, last_balance FROM users WHERE chat_id = ?", (chat_id,))
        return cur.fetchone()

def update_last_balance(chat_id: int, balance: float):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET last_balance = ? WHERE chat_id = ?", (balance, chat_id))
        conn.commit()
    logger.info("Updated last_balance for user %s to %s", chat_id, balance)

def set_threshold(chat_id: int, threshold: float):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET threshold = ? WHERE chat_id = ?", (threshold, chat_id))
        conn.commit()
    logger.info("User %s set threshold to %s", chat_id, threshold)

# ---------------- DESCO API helpers ----------------
def fetch_balance(account_no: str, meter_no: str, retries=3, delay=2):
    for attempt in range(retries):
        try:
            params = {"accountNo": account_no, "meterNo": meter_no}
            resp = requests.get(BALANCE_URL, params=params, timeout=10, verify=False)
            resp.raise_for_status()
            j = resp.json()
            if j.get("code") == 200 and "data" in j:
                return j["data"]
            logger.warning("Invalid response from balance API: %s", j)
            return None
        except requests.exceptions.RequestException as e:
            logger.error("Attempt %d/%d: Error fetching balance: %s", attempt + 1, retries, e)
            if attempt < retries - 1:
                sleep(delay)
            continue
    logger.error("Failed to fetch balance after %d retries", retries)
    return None

def fetch_customer_info(account_no: str, meter_no: str, retries=3, delay=2):
    for attempt in range(retries):
        try:
            params = {"accountNo": account_no, "meterNo": meter_no}
            resp = requests.get(INFO_URL, params=params, timeout=10, verify=False)
            resp.raise_for_status()
            j = resp.json()
            if j.get("code") == 200 and "data" in j:
                return j["data"]
            logger.warning("Invalid response from customer info API: %s", j)
            return None
        except requests.exceptions.RequestException as e:
            logger.error("Attempt %d/%d: Error fetching customer info: %s", attempt + 1, retries, e)
            if attempt < retries - 1:
                sleep(delay)
            continue
    logger.error("Failed to fetch customer info after %d retries", retries)
    return None

# ---------------- Telegram command handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to the DESCO Bot! Please provide your Account Number (e.g., 123456789)."
    )
    context.user_data["expect"] = "account"

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    logger.info("User %s sent text: %s", chat_id, text)
    expect = context.user_data.get("expect")
    
    if expect == "account":
        if not text.isalnum() or len(text) < 5:
            await update.message.reply_text("⚠️ Invalid Account Number. Please provide a valid account number.")
            return
        context.user_data["account_no"] = text
        context.user_data["expect"] = "meter"
        await update.message.reply_text("✅ Received. Now send your Meter Number (e.g., 661120136562)")
        return
    elif expect == "meter":
        if not text.isdigit() or len(text) != 12:
            await update.message.reply_text("⚠️ Invalid Meter Number. It should be a 12-digit number.")
            context.user_data.pop("expect", None)
            context.user_data.pop("account_no", None)
            return
        account_no = context.user_data.get("account_no")
        meter_no = text
        await update.message.reply_text("Please wait — verifying your details...")
        info = fetch_customer_info(account_no, meter_no)
        if info is None:
            await update.message.reply_text(
                "⚠️ Invalid Account/Meter Number or API issue. Please try again.\n"
                "You can start over with /start."
            )
            context.user_data.pop("expect", None)
            context.user_data.pop("account_no", None)
            return
        add_or_update_user(chat_id, account_no, meter_no)
        context.user_data.pop("expect", None)
        context.user_data.pop("account_no", None)
        await update.message.reply_text(
            "✅ Registration complete!\n"
            f"You will receive daily balance updates at {DAILY_TIME.strftime('%H:%M')} (Dhaka time).\n\n"
            "Commands: /status, /setthreshold, /stop, /help"
        )
        return
    
    if text.lower() in ("status", "স্ট্যাটাস"):
        await cmd_status(update, context)
        return
    
    await update.message.reply_text(
        "I didn't understand. Type /help for assistance.\n"
        "To register, use /start."
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user(chat_id)
    if not user:
        await update.message.reply_text("You are not registered. Use /start to register.")
        return
    _, account_no, meter_no, threshold, last_balance = user
    await update.message.reply_text("Please wait — fetching balance from DESCO...")
    bal = fetch_balance(account_no, meter_no)
    if not bal:
        await update.message.reply_text("Sorry, could not fetch data from the server. Try again later.")
        return
    balance = bal.get("balance", 0)
    consumption = bal.get("currentMonthConsumption", "N/A")
    reading_time = bal.get("readingTime", "N/A")
    update_last_balance(chat_id, balance)
    msg = (
        f"💡 Your DESCO balance: ৳{balance}\n"
        f"🔋 This month's consumption: {consumption} kWh\n"
        f"🕒 Reading time: {reading_time}\n"
        f"⚙️ Threshold: ৳{threshold}"
    )
    if balance <= threshold:
        msg += "\n\n⚠️ Warning: Your balance is below the threshold. Please recharge."
    await update.message.reply_text(msg)

async def cmd_setthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /setthreshold <amount>\nExample: /setthreshold 150")
        return
    try:
        val = float(args[0])
        if val < 0:
            await update.message.reply_text("Threshold cannot be negative.")
            return
        set_threshold(chat_id, val)
        await update.message.reply_text(f"✅ Threshold set to: ৳{val}")
    except ValueError:
        await update.message.reply_text("Invalid amount. Please enter a number.")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user(chat_id)
    if not user:
        await update.message.reply_text("You are not registered.")
        return
    remove_user(chat_id)
    await update.message.reply_text("Your registration has been canceled. No more messages will be sent.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🔹 Commands:\n"
        "/start - Register with your account and meter number\n"
        "/status - Check your current balance\n"
        "/setthreshold <amount> - Set balance threshold for alerts\n"
        "/stop - Cancel registration\n"
        "/help - Show this help message\n\n"
        "During registration, first send your Account Number, then your Meter Number."
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
                continue
            balance = bal.get("balance", 0)
            consumption = bal.get("currentMonthConsumption", "N/A")
            reading_time = bal.get("readingTime", "N/A")
            msg = (
                f"💡 Your DESCO balance: ৳{balance}\n"
                f"🔋 This month's consumption: {consumption} kWh\n"
                f"🕒 Reading time: {reading_time}\n"
            )
            if balance <= threshold:
                msg += f"\n⚠️ Warning: Balance is below your threshold (৳{threshold:.2f}). Please recharge!"
            await context.bot.send_message(chat_id=chat_id, text=msg)
            update_last_balance(chat_id, balance)
        except Exception as e:
            logger.error("Error processing user %s: %s", chat_id, e)

# ---------------- Main ----------------
async def main():
    """Start the bot."""
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("setthreshold", cmd_setthreshold))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Schedule daily job only if not already scheduled
    job_queue = app.job_queue
    if not job_queue.get_jobs_by_name("daily_job"):
        job_queue.run_daily(daily_job, time=DAILY_TIME, name="daily_job")

    logger.info("Bot started. Polling...")
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Fix for potential event loop issues
    try:
        import nest_asyncio
        nest_asyncio.apply()
    except ImportError:
        logger.warning("nest_asyncio not installed; install if needed for nested loops")
    
    # Run on PC
    asyncio.run(main())