import logging
import os
import threading
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import yfinance as yf

# Load local .env file if available
load_dotenv()

# Fetch credentials from environment variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Ensure required credentials exist before continuing
if not BOT_TOKEN or not CHAT_ID:
    raise ValueError(
        "Missing environment variables! Please set BOT_TOKEN and CHAT_ID."
    )

# --- 1. KEEP-ALIVE WEB SERVER FOR FREE HOSTING ---
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


# --- 2. BOT LOGIC & DICTIONARY STORAGE ---
# Dictionary to store active alerts: { ticker: target_price }
alerts = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage:
    /alert RELIANCE 2900
    /alert TATAMOTORS.NS 950
    /alert TCS.BO 4100
    """
    try:
        ticker = context.args[0].upper()
        target_price = float(context.args[1])

        # Append default NSE suffix (.NS) if exchange suffix is omitted
        if not (ticker.endswith(".NS") or ticker.endswith(".BO")):
            ticker += ".NS"

        alerts[ticker] = target_price
        await update.message.reply_text(
            f"✅ Alert set for **{ticker}** at **₹{target_price}**.\n"
            "I will notify you when it reaches or crosses this target!",
            parse_mode="Markdown",
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Invalid format!\n\n"
            "Usage: `/alert <TICKER> <TARGET_PRICE>`\n"
            "NSE Example: `/alert RELIANCE 2900`\n"
            "BSE Example: `/alert TCS.BO 4100`",
            parse_mode="Markdown",
        )


async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    """Runs every 60 seconds to check stock prices against target alerts."""
    triggered = []

    for ticker, target_price in list(alerts.items()):
        try:
            stock = yf.Ticker(ticker)
            data = stock.history(period="1d", interval="1m")
            if data.empty:
                continue

            current_price = data["Close"].iloc[-1]

            # Trigger alert if current price reaches or exceeds target price
            if current_price >= target_price:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"🚨 **PRICE ALERT!** 🚨\n\n"
                    f"**{ticker}** reached your target!\n"
                    f"📈 **Current Price:** ₹{current_price:.2f}\n"
                    f"🎯 **Target Price:** ₹{target_price:.2f}",
                    parse_mode="Markdown",
                )
                triggered.append(ticker)
        except Exception as e:
            logging.error(f"Error fetching price for {ticker}: {e}")

    # Remove triggered alerts
    for ticker in triggered:
        del alerts[ticker]


def main():
    # Start the Flask web server thread
    keep_alive()

    # Initialize Telegram Bot
    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("alert", alert_command))

    # Schedule price checks every 60 seconds
    job_queue = bot_app.job_queue
    job_queue.run_repeating(check_prices, interval=60, first=10)

    print("Stock Alert Bot is running...")
    bot_app.run_polling()


if __name__ == "__main__":
    main()
