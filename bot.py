import asyncio
import logging
import os
import sqlite3
import threading
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import yfinance as yf

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise ValueError("Missing BOT_TOKEN or CHAT_ID environment variables!")

# --- 1. SQLITE DATABASE SETUP ---
DB_NAME = "alerts.db"


def init_db():
    """Creates the alerts table if it doesn't already exist."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            ticker TEXT PRIMARY KEY,
            target_price REAL
        )
    """
    )
    conn.commit()
    conn.close()


def save_alert(ticker: str, price: float):
    """Saves or updates an alert in the database."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO alerts (ticker, target_price) VALUES (?, ?)",
        (ticker, price),
    )
    conn.commit()
    conn.close()


def get_all_alerts() -> dict:
    """Retrieves all active alerts from the database."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT ticker, target_price FROM alerts")
    rows = cursor.fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


def delete_alert_db(ticker: str):
    """Deletes a single alert from the database."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM alerts WHERE ticker = ?", (ticker,))
    conn.commit()
    conn.close()


def clear_all_alerts_db():
    """Clears all alerts from the database."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM alerts")
    conn.commit()
    conn.close()


# --- 2. FLASK KEEP-ALIVE SERVER ---
app = Flask(__name__)


@app.route("/")
def home():
    return "Stock Alert Bot is active!"


def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = threading.Thread(target=run_web_server)
    t.daemon = True
    t.start()


# --- 3. TELEGRAM BOT LOGIC ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def fetch_stock_price_sync(ticker: str):
    try:
        stock = yf.Ticker(ticker)
        data = stock.history(period="1d", interval="1m")
        if not data.empty:
            return ticker, data["Close"].iloc[-1]
    except Exception as e:
        logging.error(f"Error fetching price for {ticker}: {e}")
    return ticker, None


async def fetch_stock_price_async(ticker: str):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fetch_stock_price_sync, ticker)


def is_valid_ticker_sync(ticker: str) -> bool:
    try:
        stock = yf.Ticker(ticker)
        price = stock.fast_info.get("lastPrice")
        if price is not None:
            return True
        hist = stock.history(period="1d")
        return not hist.empty
    except Exception as e:
        logging.error(f"Error validating ticker {ticker}: {e}")
        return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Welcome to Stock Price Alert Bot!\n\n"
        "Commands:\n"
        "- /start - Show this help menu\n"
        "- /alert <TICKER> <PRICE> - Set a new price alert\n"
        "- /list - View all active alerts\n"
        "- /delete <TICKER> - Cancel an alert\n"
        "- /delete ALL - Clear all alerts\n\n"
        "Examples:\n"
        "- /alert RELIANCE 2900\n"
        "- /delete RELIANCE.NS"
    )
    await update.message.reply_text(msg)


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ticker = context.args[0].upper()
        target_price = float(context.args[1])

        if not (ticker.endswith(".NS") or ticker.endswith(".BO")):
            ticker += ".NS"

        await update.message.reply_text(f"Checking ticker {ticker}...")

        loop = asyncio.get_running_loop()
        valid = await loop.run_in_executor(
            None, is_valid_ticker_sync, ticker
        )

        if not valid:
            await update.message.reply_text(f"Invalid ticker: {ticker}.")
            return

        # Save directly to database
        save_alert(ticker, target_price)
        await update.message.reply_text(
            f"Alert set for {ticker} at Rs. {target_price:.2f}."
        )
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /alert <TICKER> <PRICE>")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = get_all_alerts()
    if not alerts:
        await update.message.reply_text("You have no active stock alerts.")
        return

    msg = "Your Active Price Alerts:\n\n"
    for ticker, price in alerts.items():
        msg += f"- {ticker}: Rs. {price:.2f}\n"

    await update.message.reply_text(msg)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete <TICKER> or /delete ALL")
        return

    target = context.args[0].upper()

    if target == "ALL":
        clear_all_alerts_db()
        await update.message.reply_text("All active alerts cleared!")
        return

    if not (target.endswith(".NS") or target.endswith(".BO")):
        target += ".NS"

    alerts = get_all_alerts()
    if target in alerts:
        delete_alert_db(target)
        await update.message.reply_text(f"Alert for {target} removed.")
    else:
        await update.message.reply_text(f"No active alert found for {target}.")


async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    # Load fresh active alerts from database
    alerts = get_all_alerts()
    if not alerts:
        return

    tasks = [fetch_stock_price_async(ticker) for ticker in list(alerts.keys())]
    results = await asyncio.gather(*tasks)

    for ticker, current_price in results:
        if current_price is not None and ticker in alerts:
            target_price = alerts[ticker]
            if current_price >= target_price:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=(
                        f"PRICE ALERT!\n\n"
                        f"{ticker} reached target!\n"
                        f"Current Price: Rs. {current_price:.2f}\n"
                        f"Target Price: Rs. {target_price:.2f}"
                    ),
                )
                # Remove triggered alert from database
                delete_alert_db(ticker)


def main():
    # Initialize SQLite database table
    init_db()

    keep_alive()

    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()

    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("alert", alert_command))
    bot_app.add_handler(CommandHandler("list", list_command))
    bot_app.add_handler(CommandHandler("delete", delete_command))

    job_queue = bot_app.job_queue
    job_queue.run_repeating(check_prices, interval=10, first=5)

    print("Bot started with SQLite persistent storage...")
    bot_app.run_polling()


if __name__ == "__main__":
    main()
