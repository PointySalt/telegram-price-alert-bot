import logging
import os
import threading
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import yfinance as yf

# Load local environment variables from .env if present
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise ValueError("Missing BOT_TOKEN or CHAT_ID environment variables!")

# --- FLASK SERVER (Prevents Free Hosting Sleep) ---
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


# --- TELEGRAM BOT LOGIC ---
# Active alerts storage: { ticker: target_price }
alerts = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def is_valid_ticker(ticker: str) -> bool:
    """Verifies if the ticker exists and returns price data on Yahoo Finance."""
    try:
        stock = yf.Ticker(ticker)
        # Check fast_info first, then fallback to recent history
        price = stock.fast_info.get("lastPrice")
        if price is not None:
            return True

        hist = stock.history(period="1d")
        return not hist.empty
    except Exception as e:
        logging.error(f"Error validating ticker {ticker}: {e}")
        return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays welcome message and available commands."""
    msg = (
        "Welcome to Stock Price Alert Bot!\n\n"
        "I monitor Indian stocks (NSE/BSE) and notify you when targets are hit.\n\n"
        "Commands:\n"
        "- /start - Show this help menu\n"
        "- /alert <TICKER> <PRICE> - Set a new price alert\n"
        "- /list - View all active alerts\n"
        "- /delete <TICKER> - Cancel an alert for a specific ticker\n"
        "- /delete ALL - Clear all active alerts\n\n"
        "Examples:\n"
        "- Set alert: /alert RELIANCE 2900 or /alert TCS.BO 4100\n"
        "- Delete alert: /delete RELIANCE.NS or /delete ALL"
    )
    await update.message.reply_text(msg)


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validates ticker existence and sets a price alert."""
    try:
        ticker = context.args[0].upper()
        target_price = float(context.args[1])

        if not (ticker.endswith(".NS") or ticker.endswith(".BO")):
            ticker += ".NS"

        # Notify user that validation is in progress
        await update.message.reply_text(f"Checking ticker {ticker}...")

        # Validate if stock exists
        if not is_valid_ticker(ticker):
            await update.message.reply_text(
                f"Invalid ticker: {ticker}.\nPlease check the symbol and exchange suffix (.NS for NSE, .BO for BSE)."
            )
            return

        alerts[ticker] = target_price
        await update.message.reply_text(
            f"Alert set for {ticker} at Rs. {target_price:.2f}."
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Invalid Format!\nUsage: /alert <TICKER> <PRICE>\n"
            "Example: /alert RELIANCE 2900"
        )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all current active price alerts."""
    if not alerts:
        await update.message.reply_text("You have no active stock alerts.")
        return

    msg = "Your Active Price Alerts:\n\n"
    for ticker, price in alerts.items():
        msg += f"- {ticker}: Rs. {price:.2f}\n"

    msg += "\nUse /delete <TICKER> or /delete ALL to manage alerts."
    await update.message.reply_text(msg)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes an individual alert or clears all alerts."""
    if not context.args:
        await update.message.reply_text(
            "Missing argument!\n"
            "Usage: /delete <TICKER> or /delete ALL\n"
            "Example: /delete RELIANCE.NS"
        )
        return

    target = context.args[0].upper()

    # Clear all alerts
    if target == "ALL":
        alerts.clear()
        await update.message.reply_text("All active alerts cleared!")
        return

    # Append default NSE suffix if missing
    if not (target.endswith(".NS") or target.endswith(".BO")):
        target += ".NS"

    # Delete individual alert
    if target in alerts:
        del alerts[target]
        await update.message.reply_text(f"Alert for {target} removed.")
    else:
        await update.message.reply_text(
            f"No active alert found for {target}.\nUse /list to check active tickers."
        )


async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    """Checks active alerts against current market price every 60s."""
    triggered = []
    for ticker, target_price in list(alerts.items()):
        try:
            stock = yf.Ticker(ticker)
            data = stock.history(period="1d", interval="1m")
            if data.empty:
                continue

            current_price = data["Close"].iloc[-1]
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
                triggered.append(ticker)
        except Exception as e:
            logging.error(f"Error checking {ticker}: {e}")

    for ticker in triggered:
        del alerts[ticker]


def main():
    keep_alive()

    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Command Handlers
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("alert", alert_command))
    bot_app.add_handler(CommandHandler("list", list_command))
    bot_app.add_handler(CommandHandler("delete", delete_command))

    # Price checking job every 60s
    job_queue = bot_app.job_queue
    job_queue.run_repeating(check_prices, interval=60, first=10)

    print("Bot started successfully...")
    bot_app.run_polling()


if __name__ == "__main__":
    main()
