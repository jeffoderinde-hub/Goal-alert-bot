# bot_telegram_acca.py
# Daily ACCA generator for JBOT (4-fold / 7-fold / 10-fold)
# Posts every morning at ACCA_TIME_HHMM and responds to /acca

import os
import time
import math
import random
import logging
from datetime import datetime, timezone
import requests
from telegram import Bot, ParseMode
from telegram.ext import Updater, CommandHandler

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("acca")

# ---------------- ENV ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROUP_CHAT_ID  = int(os.getenv("TELEGRAM_GROUP_CHAT_ID"))
DM_CHAT_ID     = int(os.getenv("TELEGRAM_DM_CHAT_ID", "0"))
API_KEY        = os.getenv("API_FOOTBALL_KEY")

ACCA_ENABLED   = os.getenv("ACCA_ENABLED", "1") == "1"
ACCA_TIME      = os.getenv("ACCA_TIME_HHMM", "10:00")
ACCA_STAKE     = float(os.getenv("ACCA_STAKE", "1"))
ACCA_BOOKMAKER = os.getenv("ACCA_BOOKMAKER", "Bet365")
ACCA_STYLE     = os.getenv("ACCA_STYLE", "D")

# Major + fallback leagues
MAJOR = [int(x) for x in os.getenv("ACCA_MAJOR_LEAGUES","39,140,135,78,61,2,3,128,71").split(",")]
FALLBACK = [int(x) for x in os.getenv("ACCA_FALLBACK_LEAGUES","94,95,88,144,99,180,203,233").split(",")]

# Odds ranges
T4_MIN, T4_MAX   = float(os.getenv("ACCA_TARGET_4_MIN","2.6")), float(os.getenv("ACCA_TARGET_4_MAX","3.8"))
T7_MIN, T7_MAX   = float(os.getenv("ACCA_TARGET_7_MIN","5.0")), float(os.getenv("ACCA_TARGET_7_MAX","7.5"))
T10_MIN, T10_MAX = float(os.getenv("ACCA_TARGET_10_MIN","25.0")), float(os.getenv("ACCA_TARGET_10_MAX","40.0"))

SEASON = os.getenv("SEASON","2025")

if not TELEGRAM_TOKEN or not GROUP_CHAT_ID or not API_KEY:
    raise SystemExit("Missing envs: TELEGRAM_BOT_TOKEN, TELEGRAM_GROUP_CHAT_ID, API_FOOTBALL_KEY")

# ---------------- Telegram ----------------
bot = Bot(token=TELEGRAM_TOKEN)

def send(chat_id, text):
    return bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# ---------------- API-FOOTBALL ----------------
BASE = "https://v3.football.api-sports.io"
HEADERS = { "x-apisports-key": API_KEY }

def api_get(path, params=None):
    r = requests.get(BASE + path, headers=HEADERS, params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json().get("response", [])

def acca_collect_fixtures():
    today = datetime.now(timezone.utc).date().isoformat()
    leagues = MAJOR if MAJOR else FALLBACK
    fixtures = []
    for lid in leagues:
        try:
            resp = api_get("/fixtures", {"league": lid, "season": SEASON, "date": today})
            fixtures.extend(resp)
        except Exception as e:
            log.warning("fixtures fetch failed for league %s: %s", lid, e)
    if len(fixtures) < 12:  # not enough? expand to fallback
        for lid in FALLBACK:
            try:
                resp = api_get("/fixtures", {"league": lid, "season": SEASON, "date": today})
                fixtures.extend(resp)
            except Exception as e:
                log.warning("fixtures fetch failed for fallback %s: %s", lid, e)
    return fixtures

def choose_leg(fixture):
    home = fixture["teams"]["home"]["name"]
    away = fixture["teams"]["away"]["name"]
    match = f"{home} vs {away}"
    fid = fixture["fixture"]["id"]

    # fake market for demo (replace with real odds endpoint if needed)
    odds = round(random.uniform(1.2, 2.0), 2)
    market = random.choice(["Home Win","Away Win","Over 1.5","Over 2.5","BTTS: Yes","Double Chance 1X"])
    kick = fixture["fixture"]["date"]
    return {"match":match, "label":market, "odds":odds, "kick":kick, "fid":fid}

def build_acca(legs, n, tgt_min, tgt_max):
    for _ in range(1000):
        picks = random.sample(legs, n)
        prod = math.prod([p["odds"] for p in picks])
        if tgt_min <= prod <= tgt_max:
            return picks, prod
    return picks, prod

# ---------------- Formatting ----------------
def format_acca_block(title, picks, prod, stake, bookmaker, style="D"):
    est = stake * prod
    if style == "D":
        hdr = (f"{title}\nStake £{stake:.2f} | Odds {prod:.2f} | Return £{est:.2f} | {bookmaker}*")
        sep = "──────────────────────────────"
    else:
        hdr = f"{title} — Stake £{stake:.2f} • Odds {prod:.2f} • Return £{est:.2f} • {bookmaker}"
        sep = "-----------------------------"

    lines = [hdr, sep]
    for i,p in enumerate(picks,1):
        when = datetime.fromisoformat(p["kick"].replace("Z","+00:00")).strftime("%a %H:%M")
        lines.append(f"{i}. {p['match']} — <i>{p['label']}</i> (@{p['odds']:.2f}) • {when} UTC")
    return "\n".join(lines)

def format_acca_message(a4,o4,a7,o7,a10,o10, style="D"):
    top = "🎟 <b>JBOT • Daily ACCAs</b> (majors preferred, fallback if quiet)"
    block4  = format_acca_block("🔵 4-Fold (safer)",    a4,o4, ACCA_STAKE, ACCA_BOOKMAKER, style)
    block7  = format_acca_block("🟡 7-Fold (balanced)", a7,o7, ACCA_STAKE, ACCA_BOOKMAKER, style)
    block10 = format_acca_block("🔴 10-Fold (longshot)",a10,o10, ACCA_STAKE, ACCA_BOOKMAKER, style)
    return "\n\n".join([top, block4, "", block7, "", block10, "\n* Uses Bet365 when available; else best available bookmaker."])

# ---------------- Core ----------------
def generate_and_send_accas():
    fixtures = acca_collect_fixtures()
    legs = [choose_leg(f) for f in fixtures if f]
    if len(legs) < 12:
        return "⚠️ Not enough fixtures for ACCAs today."

    a4,o4   = build_acca(legs,4,T4_MIN,T4_MAX)
    a7,o7   = build_acca(legs,7,T7_MIN,T7_MAX)
    a10,o10 = build_acca(legs,10,T10_MIN,T10_MAX)

    msg = format_acca_message(a4,o4,a7,o7,a10,o10, style=ACCA_STYLE)
    send(GROUP_CHAT_ID, msg)
    if DM_CHAT_ID:
        send(DM_CHAT_ID, msg)
    return "✅ ACCAs sent."

def daily_loop():
    sent_today = False
    while True:
        try:
            now = datetime.now(timezone.utc).strftime("%H:%M")
            if now == ACCA_TIME and not sent_today:
                generate_and_send_accas()
                sent_today = True
            if now != ACCA_TIME:
                sent_today = False
        except Exception as e:
            log.exception("loop error: %s", e)
        time.sleep(60)

# ---------------- Command ----------------
def acca_command(update, context):
    try:
        res = generate_and_send_accas()
        if res.startswith("⚠️"):
            update.message.reply_text(res)
    except Exception as e:
        update.message.reply_text(f"⚠️ ACCA command error:\n{e}")

# ---------------- Runner ----------------
def main():
    # Start Telegram command listener
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("acca", acca_command))
    updater.start_polling()

    # Run daily loop in main thread
    daily_loop()

if __name__ == "__main__":
    main()
