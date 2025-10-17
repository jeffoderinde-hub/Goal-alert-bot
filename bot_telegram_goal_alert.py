# bot_telegram_goal_alert.py
# Goal Alert Bot — with conflict catcher
# Uses your Render environment variables (as shown in your screenshots)

import os
import re
import time
import logging
from datetime import datetime
from random import choice
from telegram import Bot, Update, ParseMode
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    Updater,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    Filters,
)

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("goal-alert-bot")

# ─────────────────────────────────────────────────────────────
# Environment
# ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")
SEASON = os.getenv("SEASON", "2024")

GOAL_ALERTS_ENABLED = os.getenv("GOAL_ALERTS_ENABLED", "1") == "1"
GOAL_THRESHOLD = float(os.getenv("GOAL_THRESHOLD", "0.60"))
LOOKAHEAD_MIN = int(os.getenv("LOOKAHEAD_MIN", "12"))
ROLLING_SECONDS = int(os.getenv("ROLLING_SECONDS", "900"))
POLL_SECS = int(os.getenv("POLL_SECS", "12"))
COOLDOWN_SECS = int(os.getenv("COOLDOWN_SECS", "240"))
GOAL_CHECK_GRACE_SECS = int(os.getenv("GOAL_CHECK_GRACE_SECS", "30"))
GOAL_WINDOW_1H_START = int(os.getenv("GOAL_WINDOW_1H_START", "18"))
GOAL_WINDOW_1H_END = int(os.getenv("GOAL_WINDOW_1H_END", "25"))
GOAL_WINDOW_2H_START = int(os.getenv("GOAL_WINDOW_2H_START", "65"))
GOAL_WINDOW_2H_END = int(os.getenv("GOAL_WINDOW_2H_END", "72"))
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "1") == "1"
HEARTBEAT_INTERVAL_MIN = int(os.getenv("HEARTBEAT_INTERVAL_MIN", "180"))

# ─────────────────────────────────────────────────────────────
# MarkdownV2 escaping
# ─────────────────────────────────────────────────────────────
_MD2_PATTERN = re.compile(r'([_*\[\]()~`>#+\-=|{}.!])')
def esc(s: str) -> str:
    return _MD2_PATTERN.sub(r'\\\1', str(s))

# ─────────────────────────────────────────────────────────────
# Message builders
# ─────────────────────────────────────────────────────────────
def build_option_d_alert(home, away, minute, score, prob_pct,
                         pressure_index, last10_shots, last10_sot,
                         last10_corners, recommended, status="Pending"):
    title = esc("🧠 JBOT GOAL ALERT")
    header = "*{}*".format(esc("JBOT GOAL ALERT").replace("\\ ", " "))
    mline = esc(f"Match: {home} vs {away}")
    tline = esc(f"Time: Second Half ({minute}’)" if minute >= 45 else f"Time: First Half ({minute}’)")
    sline = esc(f"Score: {score}")
    pline = esc(f"Probability: {prob_pct}% (next ~{LOOKAHEAD_MIN} minutes)")
    piline = esc(f"Pressure Index: {pressure_index}")
    form_title = esc("Form (Last 10 Minutes):")
    shots = esc(f"• Shots: {last10_shots}")
    sots = esc(f"• Shots on Target: {last10_sot}")
    corners = esc(f"• Corners: {last10_corners}")
    rline = esc(f"✅ Recommended Bet: {recommended}")
    status_line = esc(f"📌 Status: {status}")
    return (
        f"{title}\n\n{header}\n\n{mline}\n{tline}\n{sline}\n\n"
        f"{pline}\n{piline}\n\n{form_title}\n{shots}\n{sots}\n{corners}\n\n"
        f"{rline}\n\n{status_line}"
    )

def build_heartbeat_message() -> str:
    phrases = [
        "🟢 Systems green\\. Monitoring matches worldwide\\.",
        "🛰️ Link stable\\. Tracking pressure spikes and shots\\.",
        "📡 Telemetry nominal\\. Next goal models running\\.",
        "🧭 Scanners active\\. Pinging live fixtures\\.",
    ]
    now = esc(datetime.utcnow().strftime("%H:%M UTC"))
    return f"{choice(phrases)} \\| {now}"

# ─────────────────────────────────────────────────────────────
# Command Handlers
# ─────────────────────────────────────────────────────────────
def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text(
        esc("👋 Goal Alert Bot is ready to send updates!"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

def cmd_testalert(update: Update, context: CallbackContext):
    sample = build_option_d_alert(
        home="Cruz Azul", away="Querétaro", minute=66,
        score="1–2", prob_pct=65, pressure_index=10.0,
        last10_shots=3, last10_sot=1, last10_corners=2,
        recommended="Over 3.5 goals", status="Pending ⏳"
    )
    update.message.reply_text(sample, parse_mode=ParseMode.MARKDOWN_V2)

# ─────────────────────────────────────────────────────────────
# Notify + Heartbeat
# ─────────────────────────────────────────────────────────────
def notify_start(bot: Bot):
    try:
        bot.send_message(chat_id=CHAT_ID,
                         text=esc("✅ Goal Alert Bot is live and monitoring matches!"),
                         parse_mode=ParseMode.MARKDOWN_V2)
        log.info("Startup notify sent.")
    except Exception as e:
        log.error(f"Startup notify failed: {e}")

def heartbeat_job(context: CallbackContext):
    try:
        context.bot.send_message(
            chat_id=CHAT_ID,
            text=build_heartbeat_message(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_notification=True,
        )
    except Exception as e:
        log.warning(f"Heartbeat send failed: {e}")

# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_GROUP_CHAT_ID")

    log.info("Env OK. Starting bot...")
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("testalert", cmd_testalert))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, lambda u, c: None))

    try:
        updater.start_polling(clean=True)
        notify_start(updater.bot)

        if HEARTBEAT_ENABLED:
            updater.job_queue.run_repeating(
                heartbeat_job,
                interval=HEARTBEAT_INTERVAL_MIN * 60,
                first=HEARTBEAT_INTERVAL_MIN * 60,
                name="heartbeat_job",
            )

        updater.idle()

    except Conflict:
        log.warning("⚠️ Another instance of this bot is already polling. Exiting gracefully.")
        # Exit quietly to avoid “Conflict: terminated by other getUpdates request”
        return
    except Exception as e:
        log.error(f"Fatal error in main(): {e}")

if __name__ == "__main__":
    main()
