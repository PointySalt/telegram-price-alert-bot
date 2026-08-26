import asyncio
import logging
import os
import threading
from dotenv import load_dotenv
from flask import Flask
import sqlitecloud
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import yfinance as yf

# Load environment variables
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
SQLITE_CLOUD_URL = os.environ.get("SQLITE_CLOUD_URL")

if not BOT_TOKEN or not CHAT_ID or not SQLITE_CLOUD_URL:
    raise ValueError(
        "Missing BOT_TOKEN, CHAT_ID, or SQLITE_CLOUD_URL environment variable!"
    )

# --- 1. CLOUD DATABASE FUNCTIONS ---


def get_db_connection():
    """Establishes connection to SQLite Cloud."""
    return sqlitecloud.connect(SQLITE_CLOUD_URL)


def init_db():
    """Initializes table with an AUTOINCREMENT ID to allow multiple alerts for the same stock."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            target_price REAL NOT NULL
        )
    """
    )
    conn.commit()
    conn.close()


def save_alert(ticker: str, price: float):
    """Inserts a new alert into the cloud database."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO alerts (ticker, target_price) VALUES (?, ?)",
        (ticker, price),
    )
    conn.commit()
    conn.close()


def get_all_alerts() -> list:
    """Retrieves all active alerts [(id, ticker, target_price), ...]."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, ticker, target_price FROM alerts")
    rows = cursor.fetchall()
    conn.close()
    return rows


def delete_alert_by_id(alert_id: int):
    """Deletes a specific alert by ID."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    conn.commit()
    conn.close()


def delete_alerts_by_ticker(ticker: str):
    """Deletes all alerts matching a ticker."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM alerts WHERE ticker = ?", (ticker,))
    conn.commit()
    conn.close()


def clear_all_alerts_db():
    """Clears all alerts from the cloud database."""
    conn = get_db_connection()
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
        "- /list - View all active alerts with IDs\n"
        "- /delete <ID or TICKER> - Cancel a specific alert ID or all alerts for a ticker\n"
        "- /delete ALL - Clear all active alerts\n\n"
        "Examples:\n"
        "- /alert RELIANCE 2900\n"
        "- /alert RELIANCE 3000\n"
        "- /delete 2 (deletes alert #2)\n"
        "- /delete RELIANCE.NS (deletes all RELIANCE alerts)"
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

        # Save to SQLite Cloud
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
    for alert_id, ticker, price in alerts:
        msg += f"ID {alert_id}: {ticker} -> Rs. {price:.2f}\n"

    msg += "\nTo remove an alert, use /delete <ID> or /delete <TICKER>."
    await update.message.reply_text(msg)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /delete <ID>, /delete <TICKER>, or /delete ALL"
        )
        return

    arg = context.args[0].upper()

    if arg == "ALL":
        clear_all_alerts_db()
        await update.message.reply_text("All active alerts cleared!")
        return

    # Delete by numeric ID
    if arg.isdigit():
        alert_id = int(arg)
        delete_alert_by_id(alert_id)
        await update.message.reply_text(f"Alert ID {alert_id} deleted.")
        return

    # Delete by ticker
    ticker = arg
    if not (ticker.endswith(".NS") or ticker.endswith(".BO")):
        ticker += ".NS"

    delete_alerts_by_ticker(ticker)
    await update.message.reply_text(f"All alerts for {ticker} removed.")


async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    alerts = get_all_alerts()
    if not alerts:
        return

    # Extract unique tickers to query prices in parallel
    unique_tickers = list({row[1] for row in alerts})

    tasks = [fetch_stock_price_async(ticker) for ticker in unique_tickers]
    results = await asyncio.gather(*tasks)

    # Convert results into a price dictionary: { "RELIANCE.NS": 2905.0 }
    current_prices = {
        ticker: price for ticker, price in results if price is not None
    }

    # Evaluate each active alert against current price
    for alert_id, ticker, target_price in alerts:
        if ticker in current_prices:
            current_price = current_prices[ticker]
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
                # Remove triggered alert by ID from cloud database
                delete_alert_by_id(alert_id)


def main():
    init_db()
    keep_alive()

    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()

    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("alert", alert_command))
    bot_app.add_handler(CommandHandler("list", list_command))
    bot_app.add_handler(CommandHandler("delete", delete_command))

    job_queue = bot_app.job_queue
    job_queue.run_repeating(check_prices, interval=10, first=5)

    print("Bot started with SQLite Cloud storage and multi-alert support...")
    bot_app.run_polling()


if __name__ == "__main__":
    main()
