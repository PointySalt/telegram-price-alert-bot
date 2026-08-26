import asyncio
from collections import Counter
import datetime
import logging
import os
import threading
from dotenv import load_dotenv
from flask import Flask
import pytz
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
    """Initializes database schema with AUTOINCREMENT ID."""
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
    """Inserts a new alert into the database."""
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
    """Clears all alerts from the database."""
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


# --- 3. HELPER & WORKER FUNCTIONS ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def fetch_stock_price_sync(ticker: str):
    """Worker function to fetch current price using yfinance."""
    try:
        stock = yf.Ticker(ticker)
        data = stock.history(period="1d", interval="1m")
        if not data.empty:
            return ticker, data["Close"].iloc[-1]
    except Exception as e:
        logging.error(f"Error fetching price for {ticker}: {e}")
    return ticker, None


async def fetch_stock_price_async(ticker: str):
    """Executes yfinance fetch in a non-blocking thread executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fetch_stock_price_sync, ticker)


def is_valid_ticker_sync(ticker: str) -> bool:
    """Ticker validation with retries to prevent cold-start failures."""
    for attempt in range(2):
        try:
            stock = yf.Ticker(ticker)
            price = stock.fast_info.get("lastPrice")
            if price is not None and not list(map(str, [price])) == ["nan"]:
                return True

            hist = stock.history(period="1d")
            if not hist.empty:
                return True
        except Exception as e:
            logging.error(
                f"Validation attempt {attempt + 1} failed for {ticker}: {e}"
            )

        if attempt == 0:
            asyncio.run(asyncio.sleep(0.5))

    return False


def get_indices_text() -> str:
    """Helper to format Sensex, Nifty 50, and Nifty Midcap 150 current levels."""
    indices = {
        "Nifty 50": "^NSEI",
        "Sensex": "^BSESN",
        "Nifty Midcap 150": "NIFTY_MIDCAP_150.NS",
    }

    msg = "Market Indices Overview:\n\n"

    for name, ticker in indices.items():
        try:
            stock = yf.Ticker(ticker)
            data = stock.history(period="2d")

            if len(data) >= 2:
                prev_close = data["Close"].iloc[-2]
                curr_price = data["Close"].iloc[-1]
                change = curr_price - prev_close
                pct_change = (change / prev_close) * 100

                sign = "+" if change >= 0 else ""
                msg += f"- {name}:\n"
                msg += f"  Price: {curr_price:,.2f}\n"
                msg += (
                    f"  Change: {sign}{change:,.2f} ({sign}{pct_change:.2f}%)\n\n"
                )
            elif len(data) == 1:
                curr_price = data["Close"].iloc[-1]
                msg += f"- {name}: {curr_price:,.2f}\n\n"
            else:
                msg += f"- {name}: Data unavailable\n\n"
        except Exception as e:
            logging.error(f"Error fetching index {name}: {e}")
            msg += f"- {name}: Failed to load\n\n"

    return msg


# --- 4. TELEGRAM BOT COMMAND HANDLERS ---


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Welcome to Stock Price Alert Bot!\n\n"
        "Commands:\n"
        "- /start - Show this help menu\n"
        "- /alert <TICKER> <PRICE> - Set a new price alert\n"
        "- /list - View all active alerts with IDs\n"
        "- /top - View top 3 most tracked stocks in database\n"
        "- /index - View Sensex, Nifty 50, and Midcap 150 live levels & day change\n"
        "- /movers - View day's top 3 gainers and losers\n"
        "- /delete <ID or TICKER> - Cancel alert(s)\n"
        "- /delete ALL - Clear all active alerts"
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
            await update.message.reply_text(
                f"Invalid ticker: {ticker}.\nPlease check the symbol and exchange suffix (.NS for NSE, .BO for BSE)."
            )
            return

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


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = get_all_alerts()
    if not alerts:
        await update.message.reply_text("No active alerts currently tracked.")
        return

    ticker_counts = Counter([row[1] for row in alerts])
    top_3 = ticker_counts.most_common(3)

    msg = "Top 3 Tracked Stocks:\n\n"
    for rank, (ticker, count) in enumerate(top_3, start=1):
        msg += f"{rank}. {ticker}: {count} active alert(s)\n"

    await update.message.reply_text(msg)


async def indices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching market indices data...")
    loop = asyncio.get_running_loop()
    msg = await loop.run_in_executor(None, get_indices_text)
    await update.message.reply_text(msg)


async def market_movers_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text("Fetching Nifty 50 market movers...")

    nifty50_tickers = [
        "RELIANCE.NS",
        "TCS.NS",
        "HDFCBANK.NS",
        "ICICIBANK.NS",
        "INFY.NS",
        "BHARTIARTL.NS",
        "ITC.NS",
        "SBIN.NS",
        "LTIM.NS",
        "LT.NS",
        "HINDUNILVR.NS",
        "AXISBANK.NS",
        "KOTAKBANK.NS",
        "M&M.NS",
        "TATAMOTORS.NS",
        "SUNPHARMA.NS",
        "NTPC.NS",
        "POWERGRID.NS",
        "TITAN.NS",
        "BAJFINANCE.NS",
    ]

    async def get_stock_change(ticker):
        loop = asyncio.get_running_loop()

        def fetch():
            try:
                stock = yf.Ticker(ticker)
                hist = stock.history(period="2d")
                if len(hist) >= 2:
                    prev = hist["Close"].iloc[-2]
                    curr = hist["Close"].iloc[-1]
                    pct = ((curr - prev) / prev) * 100
                    clean_name = ticker.replace(".NS", "")
                    return clean_name, curr, pct
            except Exception:
                pass
            return None

        return await loop.run_in_executor(None, fetch)

    tasks = [get_stock_change(t) for t in nifty50_tickers]
    results = await asyncio.gather(*tasks)

    valid_results = [r for r in results if r is not None]

    if not valid_results:
        await update.message.reply_text(
            "Unable to fetch market movers right now."
        )
        return

    valid_results.sort(key=lambda x: x[2], reverse=True)

    top_gainers = valid_results[:3]
    top_losers = valid_results[-3:][::-1]

    msg = "Day's Top Market Movers (Nifty 50):\n\n"

    msg += "Top 3 Gainers:\n"
    for name, price, pct in top_gainers:
        msg += f"- {name}: Rs. {price:.2f} (+{pct:.2f}%)\n"

    msg += "\nTop 3 Losers:\n"
    for name, price, pct in top_losers:
        msg += f"- {name}: Rs. {price:.2f} ({pct:.2f}%)\n"

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

    if arg.isdigit():
        alert_id = int(arg)
        delete_alert_by_id(alert_id)
        await update.message.reply_text(f"Alert ID {alert_id} deleted.")
        return

    ticker = arg
    if not (ticker.endswith(".NS") or ticker.endswith(".BO")):
        ticker += ".NS"

    delete_alerts_by_ticker(ticker)
    await update.message.reply_text(f"All alerts for {ticker} removed.")


# --- 5. BACKGROUND JOBS ---


async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    alerts = get_all_alerts()
    if not alerts:
        return

    unique_tickers = list({row[1] for row in alerts})

    tasks = [fetch_stock_price_async(ticker) for ticker in unique_tickers]
    results = await asyncio.gather(*tasks)

    current_prices = {
        ticker: price for ticker, price in results if price is not None
    }

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
                delete_alert_by_id(alert_id)


async def send_daily_index_job(context: ContextTypes.DEFAULT_TYPE):
    """Auto-broadcasts market index closing summary at 3:40 PM IST Mon-Fri."""
    loop = asyncio.get_running_loop()
    msg = await loop.run_in_executor(None, get_indices_text)
    msg = "Market Closing Overview:\n\n" + msg.replace(
        "Market Indices Overview:\n\n", ""
    )
    await context.bot.send_message(chat_id=CHAT_ID, text=msg)


# --- 6. MAIN APPLICATION ---


def main():
    init_db()
    keep_alive()

    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("alert", alert_command))
    bot_app.add_handler(CommandHandler("list", list_command))
    bot_app.add_handler(CommandHandler("top", top_command))
    bot_app.add_handler(CommandHandler("index", indices_command))
    bot_app.add_handler(CommandHandler("movers", market_movers_command))
    bot_app.add_handler(CommandHandler("delete", delete_command))

    # Job Queue
    job_queue = bot_app.job_queue

    # 1. Check stock alerts every 10 seconds in parallel
    job_queue.run_repeating(check_prices, interval=10, first=5)

    # 2. Automated Daily Market Close Index Post (3:40 PM IST Mon-Fri)
    ist = pytz.timezone("Asia/Kolkata")
    close_time = datetime.time(hour=15, minute=40, second=0, tzinfo=ist)

    job_queue.run_daily(
        send_daily_index_job, time=close_time, days=(1, 2, 3, 4, 5)
    )

    print("Bot started successfully...")
    bot_app.run_polling()


if __name__ == "__main__":
    main()
    
