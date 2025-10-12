# bot_telegram_goal_alert.py
import os
import re
import logging
from datetime import datetime
from random import choice

from telegram import Bot, Update
from telegram import ParseMode
from telegram.error import Conflict, Unauthorized, BadRequest
from telegram.ext import Updater, CommandHandler, CallbackContext

# ───────────────────────── Logging ─────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("goal-alert-bot")

# ───────────────────────── Envs (aligned to your Render keys) ─────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")                # e.g. 8389…:AA…
CHAT_ID        = os.getenv("TELEGRAM_GROUP_CHAT_ID")            # e.g. -4731356113  (string is fine)
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")                # available for your data fetcher

# optional/quality-of-life flags you showed in Render
HEARTBEAT_ENABLED      = os.getenv("HEARTBEAT_ENABLED", "1") == "1"
HEARTBEAT_INTERVAL_MIN = int(os.getenv("HEARTBEAT_INTERVAL_MIN", "180"))
SUCCESS_PING_ENABLED   = os.getenv("SUCCESS_PING_ENABLED", "1") == "1"
POLL_SECS              = float(os.getenv("POLL_SECS", "12"))

# extra model controls you had in envs (not used by this file, but we log them so it's clear)
_IGNORED_KEYS = {
    "GOAL_ALERTS_ENABLED": os.getenv("GOAL_ALERTS_ENABLED"),
    "GOAL_THRESHOLD": os.getenv("GOAL_THRESHOLD"),
    "GOAL_WINDOW_1H_START": os.getenv("GOAL_WINDOW_1H_START"),
    "GOAL_WINDOW_1H_END": os.getenv("GOAL_WINDOW_1H_END"),
    "GOAL_WINDOW_2H_START": os.getenv("GOAL_WINDOW_2H_START"),
    "GOAL_WINDOW_2H_END": os.getenv("GOAL_WINDOW_2H_END"),
    "LOOKAHEAD_MIN": os.getenv("LOOKAHEAD_MIN"),
    "ROLLING_SECONDS": os.getenv("ROLLING_SECONDS"),
    "PREDICTIVE_ENABLED": os.getenv("PREDICTIVE_ENABLED"),
    "SEASON": os.getenv("SEASON"),
    "COOLDOWN_SECS": os.getenv("COOLDOWN_SECS"),
}

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise RuntimeError(
        "Missing env vars: TELEGRAM_BOT_TOKEN and/or TELEGRAM_GROUP_CHAT_ID."
    )

logger.info("Loaded TELEGRAM_GROUP_CHAT_ID=%s", CHAT_ID)
logger.info("Heartbeat: enabled=%s, every %s min", HEARTBEAT_ENABLED, HEARTBEAT_INTERVAL_MIN)
logger.info("Polling interval (POLL_SECS)=%s", POLL_SECS)
for k, v in _IGNORED_KEYS.items():
    if v is not None:
        logger.info("Env present (ignored by Telegram bot code): %s=%s", k, v)

# ───────────────────────── MarkdownV2 escaping ─────────────────────────
_MD2_PATTERN = re.compile(r'([_*\[\]()~`>#+\-=|{}.!])')
def esc(s: str) -> str:
    return _MD2_PATTERN.sub(r'\\\1', str(s))

# ───────────────────────── Message builders ─────────────────────────
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

# ───────────────────────── Handlers ─────────────────────────
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

# ───────────────────────── Lifecycle helpers ─────────────────────────
def notify_start(bot: Bot) -> None:
    if not SUCCESS_PING_ENABLED:
        logger.info("Startup notify suppressed (SUCCESS_PING_ENABLED=0).")
        return
    try:
        bot.send_message(
            chat_id=CHAT_ID,
            text=esc("✅ Goal Alert Bot is live and monitoring matches!"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        logger.info("Startup notify sent.")
    except BadRequest as e:
        # Typical cause: replying to a deleted message (we never set reply_to here now).
        logger.error("Startup notify failed (BadRequest): %s", e)
    except Exception as e:
        logger.error("Startup notify failed: %s", e)

def heartbeat_job(context: CallbackContext) -> None:
    try:
        context.bot.send_message(
            chat_id=CHAT_ID,
            text=build_heartbeat_message(),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        logger.warning("Heartbeat send failed: %s", e)

# ───────────────────────── Alert entrypoint you can call from your logic ─────────────────────────
def send_alert(bot: Bot, **kwargs) -> None:
    try:
        text = build_option_d_alert(**kwargs)
        bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error("Error sending alert: %s", e)

# ───────────────────────── Main ─────────────────────────
def main():
    """
    Uses polling (no webhook), with poll interval taken from POLL_SECS.
    Handles common Telegram API errors with helpful logs.
    """
    try:
        updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
        dp = updater.dispatcher

        # Commands
        dp.add_handler(CommandHandler("start", cmd_start))
        dp.add_handler(CommandHandler("testalert", cmd_testalert))

        # Start polling; pass the env-based poll interval
        updater.start_polling(poll_interval=POLL_SECS)

        # Announce we are live if enabled
        notify_start(updater.bot)

        # Optional heartbeat
        if HEARTBEAT_ENABLED and HEARTBEAT_INTERVAL_MIN > 0:
            updater.job_queue.run_repeating(
                heartbeat_job,
                interval=HEARTBEAT_INTERVAL_MIN * 60,
                first=HEARTBEAT_INTERVAL_MIN * 60,
                name="heartbeat_job",
            )

        updater.idle()

    except Conflict as e:
        # Most common cause: another process is calling getUpdates with the same token.
        logger.error(
            "Conflict from Telegram (another getUpdates in progress). "
            "Make sure no other service/container/process is running with this TELEGRAM_BOT_TOKEN. "
            "Details: %s", e
        )
        raise
    except Unauthorized as e:
        logger.error(
            "Unauthorized (bad token or token revoked). Double-check TELEGRAM_BOT_TOKEN. Details: %s", e
        )
        raise
    except Exception as e:
        logger.exception("Fatal error starting bot: %s", e)
        raise

if __name__ == "__main__":
    main()
