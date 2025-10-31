# bot_telegram_goal_alert.py
import os
import re
import logging
import threading
from datetime import datetime
from random import choice

import requests
from flask import Flask, jsonify

from telegram import Bot, Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext
from apscheduler.schedulers.background import BackgroundScheduler

# -------------------- Logging & ENV --------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("goal-alert-bot")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID")  # e.g. "-1001234567890" or "-4731356113"
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")

HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "1") == "1"
HEARTBEAT_INTERVAL_MIN = int(os.getenv("HEARTBEAT_INTERVAL_MIN", "180"))

GOAL_ALERTS_ENABLED = os.getenv("GOAL_ALERTS_ENABLED", "1") == "1"
POLL_SECS = int(os.getenv("POLL_SECS", "60"))

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise RuntimeError(
        "Missing env vars: TELEGRAM_BOT_TOKEN and/or TELEGRAM_GROUP_CHAT_ID."
    )

# -------------------- MarkdownV2 escaping --------------------
_MD2_PATTERN = re.compile(r'([_*\[\]()~`>#+\-=|{}.!])')

def esc(s: str) -> str:
    return _MD2_PATTERN.sub(r'\\\1', str(s))

# -------------------- Message builders --------------------
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
    status: str = "Pending"
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

# -------------------- Telegram commands --------------------
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

# -------------------- Bot lifecycle helpers --------------------
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

# -------------------- Live fixtures polling --------------------
def _fetch_live_fixtures():
    """Call API-Football live fixtures and return list of fixture dicts."""
    if not API_FOOTBALL_KEY:
        logger.warning("No API_FOOTBALL_KEY set; skipping poll.")
        return []
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    url = "https://v3.football.api-sports.io/fixtures?live=all"
    try:
        r = requests.get(url, headers=headers, timeout=12)
        r.raise_for_status()
        j = r.json()
        resp = j.get("response", [])
        return resp if isinstance(resp, list) else []
    except Exception as e:
        logger.error(f"Live fixtures fetch failed: {e}")
        return []

def goal_check_job(bot: Bot):
    """Runs every POLL_SECS; simple demo trigger. Replace with your logic."""
    if not GOAL_ALERTS_ENABLED:
        logger.debug("Goal alerts disabled; skipping cycle.")
        return

    fixtures = _fetch_live_fixtures()
    logger.debug(f"Polled live fixtures: {len(fixtures)} found")

    for fx in fixtures:
        try:
            minute = (fx["fixture"]["status"]["elapsed"] or 0) or 0
            home = fx["teams"]["home"]["name"]
            away = fx["teams"]["away"]["name"]
            sh = fx["goals"]["home"] or 0
            sa = fx["goals"]["away"] or 0

            # --- Example trigger (late-goal chase). Tune as you like. ---
            if 65 <= minute <= 80 and (sh + sa) <= 2:
                logger.info(f"Trigger: {home} vs {away} @ {minute}’ {sh}-{sa}")
                text = build_option_d_alert(
                    home=home,
                    away=away,
                    minute=minute,
                    score=f"{sh}-{sa}",
                    prob_pct=70,
                    pressure_index=9.5,
                    last10_shots=5,
                    last10_sot=3,
                    last10_corners=2,
                    recommended="Over 2.5 goals",
                    status="Live ⚽",
                )
                bot.send_message(
                    chat_id=CHAT_ID,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
        except Exception as e:
            logger.warning(f"Fixture parse/alert failed: {e}")

def start_goal_polling(bot: Bot):
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        goal_check_job,
        "interval",
        seconds=POLL_SECS,
        args=[bot],
        id="goal_check_job",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=15,
    )
    if HEARTBEAT_ENABLED and HEARTBEAT_INTERVAL_MIN > 0:
        scheduler.add_job(
            heartbeat_job,
            "interval",
            seconds=HEARTBEAT_INTERVAL_MIN * 60,
            id="heartbeat_job",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=15,
            kwargs={"context": CallbackContext(Bot(TELEGRAM_TOKEN))}
        )
    scheduler.start()
    logger.info(f"Started goal polling every {POLL_SECS}s")

# -------------------- Minimal Flask health server --------------------
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def root():
    return jsonify(ok=True, service="jbot")

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)

# -------------------- Main --------------------
def main():
    # Start Flask in a side thread so Render Web Service binds a port
    threading.Thread(target=run_flask, daemon=True).start()

    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("testalert", cmd_testalert))

    updater.start_polling()  # avoid clean=True to skip deprecation noise
    logger.info("Env OK. Starting bot...")
    notify_start(updater.bot)

    # Start our polling/heartbeat jobs
    start_goal_polling(updater.bot)

    updater.idle()

if __name__ == "__main__":
    main()
