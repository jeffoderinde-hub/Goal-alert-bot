import os
import logging
import telegram
from telegram.ext import Updater, CommandHandler
from datetime import datetime

# Setup logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID")  # Group chat
DM_CHAT_ID = os.getenv("TELEGRAM_DM_CHAT_ID")        # Personal DM
API_KEY = os.getenv("API_FOOTBALL_KEY")              # API key for football odds

# Example ACCA config
ACCA_TIME = os.getenv("ACCA_TIME_HHMM", "1000")  # default 10:00
ACCA_BOOKMAKER = os.getenv("ACCA_BOOKMAKER", "Bet365")

# Telegram Bot init
bot = telegram.Bot(token=TOKEN)

def start(update, context):
    """Send a welcome message when the bot starts."""
    update.message.reply_text("✅ ACCA bot is live and ready to build your bets!")

def acca(update, context):
    """Generate daily ACCAs (stub logic for now)."""
    now = datetime.now().strftime("%H:%M")
    msg = (
        f"🎯 *Daily ACCA Suggestions* ({now})\n\n"
        f"⚽ 4-Fold → ~£3 returns\n"
        f"⚽ 7-Fold → ~£5 returns\n"
        f"⚽ 10-Fold → ~£30 returns\n\n"
        f"Bookmaker: {ACCA_BOOKMAKER}"
    )
    bot.send_message(chat_id=GROUP_CHAT_ID, text=msg, parse_mode="Markdown")

def error(update, context):
    """Log Errors caused by Updates."""
    logger.warning('Update "%s" caused error "%s"', update, context.error)

def main():
    """Start the bot."""
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Commands
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("acca", acca))

    # Errors
    dp.add_error_handler(error)

    # Start polling
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
