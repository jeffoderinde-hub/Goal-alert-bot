# bot_telegram_goal_alert.py
import os
import re
import logging
from datetime import datetime
from random import choice
from threading import Thread

from flask import Flask, jsonify
from telegram import Bot, Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("goal-alert-bot")

# ---------- Env ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID")  # e.g. -4731356113
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "1") == "1"
HEARTBEAT_INTERVAL_MIN = int(os.getenv("HEARTBEAT_INTERVAL_MIN", "180"))

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise RuntimeError("Missing env vars: TELEGRAM_BOT_TOKEN and/or TELEGRAM_GROUP_CHAT_ID.")

# ---------- Markdown V2 escaping ----------
_MD2_PATTERN = re.compile(r'([_*\[\]()~`>#+\-=|{}.!])')
def esc(s: str) -> str:
    return _MD2_PATTERN.sub(r'\\\1', str(s))

# ---------- Message builders ----------
def build_option_d_alert(
    home: str,
    away: str,
    minute: int,
    score: str,
    prob_pct: int,
    pressure_index: float,
    last10_shots: int,
    last10_sot: int,
    last10_corners: int,
    recommended: str,
    status: str = "Pending",
) -> str:
    title = esc("🧠 JBOT GOAL ALERT")
    mline = esc(f"Match: {home} vs {away}")
    tline = esc(f"Time: Second Half ({minute}’)" if minute >= 45 else f"Time: First Half ({minute}’)")
    sline = esc(f"Score: {score}")
    pline = esc(f"Probability: {prob_pct}% (next ~12 minutes)")
    piline = esc(f"Pressure Index: {pressure_index}")
    form_title = esc("Form (Last 10 Minutes):")
    shots = esc(f"• Shots: {last10_shots}")
    sots = esc(f"• Shots on Target: {last10_sot}")
    corners = esc(f"• Corners: {last10_corners}")
    rline = esc(f"✅ Recommended Bet: {recommended}")
    status_line = esc(f"📌 Status: {status}")

    header = "*{}*".format(esc("JBOT GOAL ALERT").replace("\\ ", " "))
    text = (
        f"{title}\n\n"
        f"{header}\n\n"
        f"{mline}\n"
        f"{tline}\n"
        f"{sline}\n\n"
        f"{pline}\n"
        f"{piline}\n\n"
        f"{form_title}\n"
        f"{shots}\n"
        f"{sots}\n"
        f"{corners}\n\n"
        f"{rline}\n\n"
        f"{status_line}"
    )
    return text

def build_heartbeat_message() -> str:
    phrases = [
        "🟢 Systems green\\. Monitoring matches worldwide\\.",
        "🛰️ Link stable\\. Tracking pressure spikes and shots\\.",
        "🧭 Scanners active\\. Pinging live fixtures\\.",
        "📡 Telemetry nominal\\. Next goal models running\\.",
    ]
    now = esc(datetime.utcnow().strftime("%H:%M UTC"))
    return f"{choice(phrases)} \\| {now}"

# ---------- Telegram handlers ----------
def cmd_start(update: Update, context: CallbackContext) -> None:
    update.message.reply_text(
        esc("👋 Goal Alert Bot is ready to send updates!"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

def cmd_testalert(update: Update, context: CallbackContext) -> None:
    sample = build_option_d_alert(
        home="Cruz Azul",
        away="Club Queretaro",
        minute=66,
        score="1–2",
        prob_pct=65,
        pressure_index=10.0,
        last10_shots=0,
        last10_sot=0,
        last10_corners=0,
        recommended="Over 3.5 goals",
        status="Pending ⏳",
    )
    update.message.reply_text(sample, parse_mode=ParseMode.MARKDOWN_V2)

# ---------- Lifecycle helpers ----------
def notify_start(bot: Bot) -> None:
    try:
        bot.send_message(
            chat_id=CHAT_ID,
            text=esc("✅ Goal Alert Bot is live and monitoring matches!"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        logger.error(f"Startup notify failed: {e}")

def heartbeat_job(context: CallbackContext) -> None:
    try:
        context.bot.send_message(
            chat_id=CHAT_ID,
            text=build_heartbeat_message(),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        logger.warning(f"Heartbeat send failed: {e}")

# ---------- Example alert trigger you can call from your logic ----------
def send_alert(bot: Bot, **kwargs) -> None:
    try:
        text = build_option_d_alert(**kwargs)
        bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error sending alert: {e}")

# ---------- Tiny Flask web server (keeps Render Web Service port open) ----------
app = Flask(__name__)

@app.route("/")
def root():
    return "OK", 200

@app.route("/healthz")
def health():
    return jsonify(status="ok", time=datetime.utcnow().isoformat()), 200

def run_web():
    # Render injects PORT. Default to 10000 locally.
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ---------- Main ----------
def main():
    # Start the tiny web server in the background so Render sees an open port
    Thread(target=run_web, daemon=True).start()

    # Telegram polling bot
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("testalert", cmd_testalert))

    # IMPORTANT: drop any queued updates to prevent "terminated by other getUpdates request"
    updater.start_polling(drop_pending_updates=True)

    logger.info("Env OK. Starting bot...")
    notify_start(updater.bot)

    if HEARTBEAT_ENABLED and HEARTBEAT_INTERVAL_MIN > 0:
        updater.job_queue.run_repeating(
            heartbeat_job,
            interval=HEARTBEAT_INTERVAL_MIN * 60,
            first=HEARTBEAT_INTERVAL_MIN * 60,
            name="heartbeat_job",
        )

    updater.idle()

if __name__ == "__main__":
    main()
