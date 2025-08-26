import os
import logging
import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# Load local .env (only used locally, not on Render)
load_dotenv()

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
BLOCK_ID = os.getenv("BLOCK_ID")

if not BOT_TOKEN:
    raise RuntimeError("❌ Missing BOT_TOKEN env var. Set it in .env (local) or Render Dashboard (production).")
if not BLOCK_ID:
    raise RuntimeError("❌ Missing BLOCK_ID env var. Set it in .env (local) or Render Dashboard (production).")

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🎯 Show Ads", callback_data="show_ads")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Welcome! Click below to view an ad:", reply_markup=reply_markup
    )


# Callback handler
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "show_ads":
        try:
            url = f"https://adsgram.ai/api/blocks/{BLOCK_ID}/start?telegram_id={query.from_user.id}"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                if "url" in data:
                    await query.edit_message_text(
                        text=f"🔗 [Click here to view the ad]({data['url']})",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                else:
                    await query.edit_message_text("⚠️ No ads available right now.")
            else:
                logger.error(f"Bad response from AdsGram: {response.status_code} {response.text}")
                await query.edit_message_text("❌ Failed to fetch ads. (API error)")

        except Exception as e:
            logger.error(f"Error fetching ad: {e}")
            await query.edit_message_text("❌ Failed to fetch ads.")


def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.run_polling()


if __name__ == "__main__":
    main()