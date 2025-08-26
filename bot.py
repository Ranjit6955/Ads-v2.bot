import os
import time
import logging
import threading
from typing import Optional
from dotenv import load_dotenv

import requests
from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ParseMode,
    Bot,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# -----------------------
# Load env (local use)
# -----------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BLOCK_ID = os.getenv("BLOCK_ID")  # Accept full like "ird-12345" or numeric "12345"
PORT = int(os.environ.get("PORT", 5000))  # Render sets PORT environment variable

if not BOT_TOKEN:
    raise RuntimeError("❌ Missing BOT_TOKEN env var. Set it in .env (local) or Render Dashboard (production).")
if not BLOCK_ID:
    raise RuntimeError("❌ Missing BLOCK_ID env var. Set it in .env (local) or Render Dashboard (production).")

# If someone put only digits (e.g. "12345"), auto-prefix with 'ird-' (common AdsGram prefix).
# This is a convenience — if your block uses other prefix (rwd- etc.) do not use only digits.
if BLOCK_ID.isdigit():
    logging.info("BLOCK_ID is digits-only, auto-prepending 'ird-'.")
    BLOCK_ID = f"ird-{BLOCK_ID}"

AD_API_TEMPLATE = "https://adsgram.ai/api/blocks/{blockid}/start?telegram_id={tgid}"

# -----------------------
# Logging
# -----------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("adsgram-bot")

# -----------------------
# Flask app for Reward callback
# -----------------------
flask_app = Flask(__name__)
telegram_bot = Bot(BOT_TOKEN)  # synchronous bot used by Flask thread

@flask_app.route("/", methods=["GET"])
def home():
    return "AdsGram Telegram bot is running", 200

# AdsGram requires Reward URL containing [userId] placeholder.
# AdsGram will replace [userId] with the user's Telegram ID and call:
#    https://your-app.onrender.com/reward/123456789
# We keep the callback silent (no amount shown). We log it and reply 200.
@flask_app.route("/reward/<int:user_id>", methods=["GET", "POST"])
def reward(user_id: int):
    try:
        # AdsGram may send GET or POST and may include JSON or query params.
        data = {}
        if request.method == "POST":
            try:
                data = request.get_json(force=True, silent=True) or {}
            except Exception:
                data = {}
        # Also merge query params if any:
        data.update(request.args.to_dict())

        logger.info("Reward callback received for user_id=%s; payload=%s", user_id, data)

        # Silent handling: do not show amounts to the user. If you want to update DB, add code here.
        # Example: update DB / Firebase here.

        # Optionally notify user (you asked not to show amount; we keep it silent).
        # If you'd like a silent confirmation message, you can send a short non-amount message:
        # telegram_bot.send_message(user_id, "✅ Reward processed", disable_notification=True)

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.exception("Reward handler error: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500

def run_flask():
    # Bind host 0.0.0.0 so Render can reach it. Use provided PORT.
    flask_app.run(host="0.0.0.0", port=PORT)

# -----------------------
# Telegram bot (async)
# -----------------------
COOLDOWN_SECONDS = 6
_last_ad_at: dict[int, float] = {}  # user_id -> timestamp

def cooldown_ok(user_id: int) -> bool:
    now = time.time()
    last = _last_ad_at.get(user_id, 0)
    if now - last >= COOLDOWN_SECONDS:
        _last_ad_at[user_id] = now
        return True
    return False

def fetch_adsgram_ad(tgid: int) -> Optional[dict]:
    """Call AdsGram and return parsed JSON dict or None."""
    url = AD_API_TEMPLATE.format(blockid=BLOCK_ID, tgid=tgid)
    logger.info("Requesting AdsGram: %s", url)
    try:
        resp = requests.get(url, timeout=10)
    except Exception as e:
        logger.exception("HTTP request to AdsGram failed: %s", e)
        return None

    logger.info("AdsGram HTTP status: %s", resp.status_code)
    raw = (resp.text or "").strip()
    logger.info("AdsGram raw response (first 1000 chars): %s", raw[:1000] or "(empty)")

    if resp.status_code != 200:
        logger.warning("AdsGram non-200 response: %s", resp.status_code)
        return None

    try:
        data = resp.json()
        if not isinstance(data, dict):
            logger.warning("AdsGram returned non-dict JSON: %s", type(data))
            return None
        return data
    except ValueError:
        # Not JSON (empty or HTML). Return None so caller can show an error message.
        logger.warning("AdsGram response is not JSON.")
        return None

# --- Telegram command handlers ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🎯 Show Ads", callback_data="show_ads")]]
    await update.message.reply_text(
        "👋 Welcome! Click the button to view a sponsored ad.", reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def ads_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow /ads command as alternative to button."""
    # Reuse button handler logic by constructing a fake callback-like flow:
    class _Fake:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user

    fake = _Fake(update.message)
    await handle_show_ads(fake, context)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # Pass the callback query object as reply_target
    await handle_show_ads(query, context)

async def handle_show_ads(reply_source, context: ContextTypes.DEFAULT_TYPE):
    """
    reply_source should have:
      - from_user.id
      - message (object with reply methods) OR implement edit_message_text
    For CallbackQuery -> reply_source is CallbackQuery (has edit_message_text),
    For /ads command -> we passed a fake with .message.
    """
    user_id = reply_source.from_user.id
    # If invoked as a message (not callback) reply_target will be message
    reply_target = getattr(reply_source, "message", None) or reply_source

    if not cooldown_ok(user_id):
        await reply_target.reply_text("⏳ Please wait a few seconds before requesting another ad.")
        return

    ad = fetch_adsgram_ad(user_id)
    if not ad:
        # If AdsGram returned nothing or non-json, inform user and suggest checking later
        await reply_target.reply_text("⚠️ No ad available right now (or invalid response from ad server). Try again later.")
        return

    # Build buttons from AdsGram fields (be defensive)
    buttons = []
    click_url = ad.get("click_url") or ad.get("url") or ad.get("link")
    button_name = ad.get("button_name") or "Open"
    if click_url:
        buttons.append([InlineKeyboardButton(button_name, url=click_url)])

    # Reward button (if provided)
    reward_name = ad.get("button_reward_name")
    reward_url = ad.get("reward_url") or ad.get("rewardUrl")
    if reward_name and reward_url:
        buttons.append([InlineKeyboardButton(reward_name, url=reward_url)])

    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

    # Some AdsGram responses include text_html and image_url
    text_html = ad.get("text_html") or ad.get("text") or "Sponsored"
    image_url = ad.get("image_url") or ad.get("imageUrl")

    try:
        if image_url:
            # Send photo with caption (HTML allowed)
            # If reply_target supports edit_message_text (callback), use reply_target.message.reply_photo
            # Most straightforward: send a new message.
            await context.bot.send_photo(
                chat_id=user_id,
                photo=image_url,
                caption=text_html,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                protect_content=True,
            )
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text=text_html,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                protect_content=True,
            )
    except Exception as e:
        logger.exception("Failed to send ad message: %s", e)
        await reply_target.reply_text("❌ Failed to display ad. Please try again later.")

# -----------------------
# Application entrypoint
# -----------------------
def main():
    # Start Flask thread first (so Render sees open port)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Started Flask thread on port %s", PORT)

    # Build telegram app (async)
    app = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ads", ads_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Starting Telegram bot polling...")
    app.run_polling()

if __name__ == "__main__":
    main()