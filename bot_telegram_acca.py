import os
import time
import random
import logging
from datetime import datetime, timedelta
from typing import List, Dict

import requests
from telegram import Bot, ParseMode

# ---------------------------
# Config from environment
# ---------------------------
TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()        # group to post ACCAs
API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
SEASON  = os.getenv("SEASON", "2024")
ACCA_TIME_HHMM = os.getenv("ACCA_ALERT_TIME", "10:00")     # e.g. "10:00" (UTC/server time)
SEND_ON_START = os.getenv("ACCA_SEND_ON_START", "false").lower() == "true"

# Optional preferences (safe defaults)
TARGET_RETURNS_4  = float(os.getenv("TARGET_RETURNS_4", 3))    # ~£3 return
TARGET_RETURNS_7  = float(os.getenv("TARGET_RETURNS_7", 5))    # ~£5 return
TARGET_RETURNS_10 = float(os.getenv("TARGET_RETURNS_10", 30))  # ~£30 return
STAKE_PER_ACCA    = float(os.getenv("ACCA_STAKE", 1))          # £1 per slip

# If you want to constrain leagues, comma separated e.g. "39,140,78"
LEAGUE_IDS = [x.strip() for x in os.getenv("ACCA_LEAGUE_IDS", "").split(",") if x.strip()]

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("acca-bot")

if not TOKEN or not CHAT_ID or not API_KEY:
    raise SystemExit("Missing required env vars: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, API_FOOTBALL_KEY")

bot = Bot(TOKEN)

# ---------------------------
# Very light odds stub
# (You can replace this with your real odds feed.)
# ---------------------------
def fetch_candidates_for_today() -> List[Dict]:
    """
    Returns a list of candidate selections with naive 'odds' & market.
    In production, replace with real odds (Bet365 feed/API) and real filters.
    """
    # Demo list across markets we support (1X2 + BTTS + Over)
    sample = [
        {"league":"Premier League","match":"Arsenal vs Brentford","market":"1X2","pick":"Arsenal","odds":1.45},
        {"league":"La Liga","match":"Barcelona vs Osasuna","market":"Over 2.5","pick":"Over 2.5","odds":1.72},
        {"league":"Serie A","match":"Inter vs Udinese","market":"BTTS","pick":"Yes","odds":1.90},
        {"league":"Bundesliga","match":"Leverkusen vs Mainz","market":"1X2","pick":"Leverkusen","odds":1.33},
        {"league":"Ligue 1","match":"PSG vs Reims","market":"Over 2.5","pick":"Over 2.5","odds":1.55},
        {"league":"Eredivisie","match":"PSV vs Heracles","market":"BTTS","pick":"Yes","odds":1.85},
        {"league":"Championship","match":"Leeds vs Preston","market":"1X2","pick":"Leeds","odds":1.70},
        {"league":"Portugal","match":"Benfica vs Rio Ave","market":"Over 2.5","pick":"Over 2.5","odds":1.62},
        {"league":"Belgium","match":"Genk vs Eupen","market":"1X2","pick":"Genk","odds":1.50},
        {"league":"Turkey","match":"Galatasaray vs Rizespor","market":"BTTS","pick":"No","odds":1.75},
        {"league":"Scotland","match":"Celtic vs St. Mirren","market":"Over 2.5","pick":"Over 2.5","odds":1.57},
        {"league":"MLS","match":"LAFC vs Austin","market":"BTTS","pick":"Yes","odds":1.80},
        {"league":"Brazil A","match":"Flamengo vs Goias","market":"1X2","pick":"Flamengo","odds":1.52},
        {"league":"Brazil B","match":"Avai vs Mirassol","market":"Over 2.5","pick":"Over 2.5","odds":1.95},
        {"league":"Argentina","match":"River vs Sarmiento","market":"1X2","pick":"River Plate","odds":1.48},
    ]
    random.shuffle(sample)
    return sample

def build_acca(candidates: List[Dict], legs: int, target_return: float) -> List[Dict]:
    """
    Pick 'legs' selections aiming roughly at given returns for £1 stake.
    This is a simple heuristic (choose mid odds to hit target ballpark).
    """
    # Try a few mixes and choose product closest to target_return
    best, best_gap = None, 1e9
    for _ in range(500):
        picks = random.sample(candidates, k=min(legs, len(candidates)))
        prod = 1.0
        for p in picks:
            prod *= p["odds"]
        gap = abs(prod - target_return)
        if gap < best_gap:
            best, best_gap = picks, gap
    return best or candidates[:legs]

def fmt_price(x: float) -> str:
    return f"{x:.2f}"

def render_acca_card(title: str, picks: List[Dict], stake: float) -> str:
    product = 1.0
    for p in picks:
        product *= p["odds"]
    est = stake * product

    lines = []
    lines.append("🏷️ *" + title + "*")
    lines.append("────────────────────────")
    for i, p in enumerate(picks, 1):
        emoji = "⚽" if p["market"].lower() != "1x2" else "🏟️"
        lines.append(f"{i}. {emoji} *{p['match']}*")
        lines.append(f"   └ {p['league']} • {p['market']}: *{p['pick']}* • Odds: *{fmt_price(p['odds'])}*")
    lines.append("────────────────────────")
    lines.append(f"💷 Stake: *£{fmt_price(stake)}*")
    lines.append(f"📈 Est. return: *£{fmt_price(est)}*  (Acca price {fmt_price(product)})")
    lines.append("✅ Good luck! Bet responsibly.")
    return "\n".join(lines)

def send_accas_now():
    try:
        cands = fetch_candidates_for_today()
        if len(cands) < 10:
            log.warning("Only %d candidates available; slips may be repetitive.", len(cands))

        acca4  = build_acca(cands, 4,  TARGET_RETURNS_4)
        acca7  = build_acca(cands, 7,  TARGET_RETURNS_7)
        acca10 = build_acca(cands, 10, TARGET_RETURNS_10)

        header = "🎯 *JBOT Daily ACCAs*  \n" \
                 f"🕙 {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC  \n" \
                 "Mix of 1X2, BTTS & Overs from bigger leagues.\n"

        bot.send_message(chat_id=CHAT_ID, text=header, parse_mode=ParseMode.MARKDOWN)

        bot.send_message(chat_id=CHAT_ID,
                         text=render_acca_card("4-Fold (≈£3 return)", acca4, STAKE_PER_ACCA),
                         parse_mode=ParseMode.MARKDOWN)
        bot.send_message(chat_id=CHAT_ID,
                         text=render_acca_card("7-Fold (≈£5 return)", acca7, STAKE_PER_ACCA),
                         parse_mode=ParseMode.MARKDOWN)
        bot.send_message(chat_id=CHAT_ID,
                         text=render_acca_card("10-Fold (≈£30 return)", acca10, STAKE_PER_ACCA),
                         parse_mode=ParseMode.MARKDOWN)

        log.info("ACCA messages sent.")
    except Exception as e:
        log.exception("Failed to send ACCAs: %s", e)

def seconds_until_next_hhmm(hhmm: str) -> int:
    """Return seconds until the next occurrence of HH:MM (24h) in server time."""
    now = datetime.now()
    hour, minute = map(int, hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return int((target - now).total_seconds())

def main_loop():
    # Optional: fire once at start for quick check
    if SEND_ON_START:
        log.info("SEND_ON_START=true → sending ACCAs immediately.")
        send_accas_now()

    while True:
        wait_s = seconds_until_next_hhmm(ACCA_TIME_HHMM)
        log.info("Sleeping %s seconds until next ACCA slot (%s).", wait_s, ACCA_TIME_HHMM)
        # Sleep in chunks so Render “free” plans don’t think we’ve died
        slept = 0
        chunk = 60
        while slept < wait_s:
            time.sleep(min(chunk, wait_s - slept))
            slept += min(chunk, wait_s - slept)
        send_accas_now()
        # small gap to avoid double-send if server clock drifts
        time.sleep(5)

if __name__ == "__main__":
    log.info("ACCA bot (no-polling) starting…")
    main_loop()
