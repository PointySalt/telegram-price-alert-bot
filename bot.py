import logging
import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import yfinance as yf

# --- 1. WEB SERVER FOR FREE HOSTING KEEP-ALIVE ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Stock Alert Bot is running!"

def run_web_server():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_web_server)
    t.daemon = True
    t.start()

# --- 2. TELEGRAM BOT CONFIGURATION ---
# Replace with your actual credentials
BOT_TOKEN = "8818739500:AAG8tKDgtrT8DVaEaGX0A5ATTFDPN74MJNk"
CHAT_ID = "5220887722"

# Dictionary to store active alerts: { ticker: target_price }
alerts = {}

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command usage: /alert RELIANCE 2900  OR  /alert TCS.BO 4100"""
    try:
        ticker = context.args[0].upper()
        target_price = float(context.args[1])
        
        # Default to NSE (.NS) if no exchange suffix is supplied
        if not (ticker.endswith(".NS") or ticker.endswith(".BO")):
            ticker += ".NS"
            
        alerts[ticker] = target_price
        await update.message.reply_text(
            f"✅ Alert set for {ticker} at ₹{target_price}.\nI will ping you when it hits target!"
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Invalid format!\nUse: /alert TICKER PRICE\n"
            "NSE Example: /alert RELIANCE 2900\n"
            "BSE Example: /alert TCS.BO 4100"
        )

async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    """Runs every 60 seconds to evaluate prices against targets."""
    triggered = []
    
    for ticker, target_price in list(alerts.items()):
        try:
            stock = yf.Ticker(ticker)
            data = stock.history(period="1d", interval="1m")
            if data.empty:
                continue
                
            current_price = data['Close'].iloc[-1]
            
            if current_price >= target_price:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"🚨 PRICE ALERT! 🚨\n\n{ticker} hit your target!\n"
                         f"Current Price: ₹{current_price:.2f}\n"
                         f"Target Price: ₹{target_price:.2f}"
                )
                triggered.append(ticker)
        except Exception as e:
            print(f"Error reading price for {ticker}: {e}")

    for ticker in triggered:
        del alerts[ticker]

def main():
    # Start the web server background thread
    keep_alive()
    
    # Initialize the Telegram Bot
    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("alert", alert_command))
    
    # Schedule price checks every 60 seconds
    job_queue = bot_app.job_queue
    job_queue.run_repeating(check_prices, interval=60, first=10)
    
    print("Bot is up and running...")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
