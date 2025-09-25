# bot_telegram_goal_alert.py
# v2.1 — window-accurate alerts + pending->success/failed edits + success ping + no getUpdates conflict

import os
import time
import math
import logging
from datetime import datetime, timezone, timedelta
import requests
from telegram import Bot, ParseMode

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jbot")

# ---------------- ENV ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = int(os.getenv("TELEGRAM_GROUP_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID"))
API_KEY        = os.getenv("API_FOOTBALL_KEY")

# Core knobs
POLL_SECS       = int(os.getenv("POLL_SECS", 15))
LOOKAHEAD_MIN   = int(os.getenv("LOOKAHEAD_MIN", 12))
GOAL_THRESHOLD  = float(os.getenv("GOAL_THRESHOLD", 0.60))
ROLLING_SECONDS = int(os.getenv("ROLLING_SECONDS", 900))   # last ~15m for events
COOLDOWN_SECS   = int(os.getenv("COOLDOWN_SECS", 240))     # per-fixture cool-down

# Windows (inclusive)
W1_START = int(os.getenv("GOAL_WINDOW_1H_START", 18))
W1_END   = int(os.getenv("GOAL_WINDOW_1H_END", 25))
W2_START = int(os.getenv("GOAL_WINDOW_2H_START", 65))
W2_END   = int(os.getenv("GOAL_WINDOW_2H_END", 72))

GRACE_SECS = int(os.getenv("GOAL_CHECK_GRACE_SECS", 30))

STARTUP_MSG  = os.getenv("STARTUP_MESSAGE_ENABLED", "0") == "1"
HEARTBEAT    = os.getenv("HEARTBEAT_ENABLED", "0") == "1"
SUCCESS_PING = os.getenv("SUCCESS_PING_ENABLED", "1") == "1"  # <— extra ping on success

if not TELEGRAM_TOKEN or not CHAT_ID or not API_KEY:
    raise SystemExit("Missing envs: TELEGRAM_BOT_TOKEN, TELEGRAM_GROUP_CHAT_ID, API_FOOTBALL_KEY")

# ---------------- Telegram (no getUpdates) ----------------
bot = Bot(token=TELEGRAM_TOKEN)

def send_text(chat_id, text):
    return bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)

def edit_text(chat_id, message_id, old_text, new_text):
    if (old_text or "").strip() != (new_text or "").strip():
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=new_text, parse_mode=ParseMode.MARKDOWN)
        return new_text
    return old_text

# ---------------- Helpers ----------------
def now_utc(): return datetime.now(timezone.utc)

def minutes_and_half(elapsed_total: int):
    if elapsed_total is None:
        return None, None
    if elapsed_total <= 45:
        return 1, elapsed_total
    return 2, elapsed_total

def in_window(half, half_min):
    if half == 1:
        return W1_START <= half_min <= W1_END
    if half == 2:
        return W2_START <= half_min <= W2_END
    return False

def suggest_market(half, home, away):
    if half == 1:
        return "Over 0.5 First-Half Goals" if (home + away == 0) else "Over 1.5 Match Goals"
    # second half
    if home + away <= 1:   return "Over 1.5 Match Goals"
    if home + away == 2:   return "Over 2.5 Match Goals"
    return "Over 3.5 Match Goals"

def pressure_index(shots, sot, corners):
    return round(shots*1.0 + sot*2.0 + corners*1.5, 1)

def prob_from_counts(shots, sot, corners):
    x = 0.18*shots + 0.42*sot + 0.25*corners
    p = 1.0/(1.0 + math.exp(-x + 3.0))  # smoothed logistic
    return round(p, 2)

def option_d_text(home, away, minute_str, score_str, prob, pi, shots10, sot10, corners10, market, status_icon="⏳", status_word="Pending"):
    lines = [
        "🧠 *JBOT GOAL ALERT*",
        "",
        f"*Match:* {home} vs {away}",
        f"🕒 *Time:* {minute_str}",
        f"🔢 *Score:* {score_str}",
        "",
        f"*Probability:* {int(prob*100)}% (next ~{LOOKAHEAD_MIN} minutes)",
        f"*Pressure Index:* {pi}",
        "",
        "*Form (Last 10 Minutes):*",
        f"• Shots: {shots10}",
        f"• Shots on Target: {sot10}",
        f"• Corners: {corners10}",
        "",
        f"✅ *Recommended Bet:* {market}",
        "",
        f"{status_icon} *Status:* {status_word}",
    ]
    return "\n".join(lines)

# ---------------- API-FOOTBALL ----------------
BASE = "https://v3.football.api-sports.io"
HEADERS = { "x-apisports-key": API_KEY }

def api_get(path, params=None):
    r = requests.get(BASE + path, headers=HEADERS, params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json().get("response", [])

def get_live_fixtures():
    return api_get("/fixtures", {"live":"all"})

def get_fixture_events(fixture_id):
    return api_get("/fixtures/events", {"fixture": fixture_id})

# ---------------- State ----------------
last_sent_at   = {}   # fixture_id -> datetime (cooldown)
pending_alerts = {}   # fixture_id -> dict (see below)
last_heartbeat_minute = None

# ---------------- Startup / Heartbeat ----------------
def maybe_startup():
    if STARTUP_MSG:
        try:
            send_text(CHAT_ID, "🚀 THE BOT IS UP AND RUNNING! YOU READY? 🔥")
        except Exception as e:
            log.warning("Startup message failed: %s", e)

def maybe_heartbeat():
    global last_heartbeat_minute
    if not HEARTBEAT: return
    m = now_utc().minute
    if m == last_heartbeat_minute:  # already this minute
        return
    if m == 0:
        last_heartbeat_minute = m
        try:
            send_text(CHAT_ID, "👀 Monitoring pressure spikes. Vibes check, squad?")
        except Exception as e:
            log.warning("Heartbeat failed: %s", e)

# ---------------- Reconcile Pending ----------------
def format_status_line(s):
    return {"Pending":"⏳ *Status:* Pending", "Success":"✅ *Status:* Success", "Failed":"❌ *Status:* Failed"}[s]

def reconcile_alerts(live_by_id):
    to_remove = []

    for fx_id, rec in list(pending_alerts.items()):
        chat_id    = rec["chat_id"]
        msg_id     = rec["msg_id"]
        old_text   = rec["text_last"]
        status     = rec["status"]
        window_end = rec["window_end"]
        score0     = rec["score_at_send"]
        home       = rec.get("home")
        away       = rec.get("away")
        league     = rec.get("league")
        half       = rec.get("half")
        now        = now_utc()

        data = live_by_id.get(fx_id)

        # Fixture ended/vanished -> fail
        if data is None:
            if status == "Pending":
                new_text = old_text.replace(format_status_line("Pending"), format_status_line("Failed"))
                rec["text_last"] = edit_text(chat_id, msg_id, old_text, new_text)
                rec["status"] = "Failed"
            to_remove.append(fx_id)
            continue

        # Current score
        h = int(data["goals"]["home"] or 0)
        a = int(data["goals"]["away"] or 0)
        current = f"{h}-{a}"

        # Success: score changed within window
        if current != score0 and now <= window_end and status == "Pending":
            new_text = old_text.replace(format_status_line("Pending"), format_status_line("Success"))
            rec["text_last"] = edit_text(chat_id, msg_id, old_text, new_text)
            rec["status"] = "Success"

            if SUCCESS_PING:
                minute_note = "1H window" if half == 1 else "2H window"
                try:
                    bot.send_message(
                        chat_id=chat_id,
                        parse_mode=ParseMode.MARKDOWN,
                        text=(
                            "✅ *GOAL LANDED*\n"
                            f"*Match:* {home} vs {away}\n"
                            f"*League:* {league}\n"
                            f"*Window:* {minute_note}\n"
                            f"*New Score:* {current}"
                        )
                    )
                except Exception as e:
                    log.warning("success ping failed: %s", e)

            to_remove.append(fx_id)
            continue

        # Failed: window expired without goal
        if now > window_end and status == "Pending":
            new_text = old_text.replace(format_status_line("Pending"), format_status_line("Failed"))
            rec["text_last"] = edit_text(chat_id, msg_id, old_text, new_text)
            rec["status"] = "Failed"
            to_remove.append(fx_id)

    for fx_id in to_remove:
        pending_alerts.pop(fx_id, None)

# ---------------- Main scan ----------------
def process_live():
    fixtures = get_live_fixtures()
    live_by_id = {}

    # Build lookup for reconcile step
    for fx in fixtures:
        fid = fx["fixture"]["id"]
        live_by_id[fid] = {
            "goals": fx.get("goals", {}),
            "fixture": fx.get("fixture", {}),
            "teams": fx.get("teams", {}),
            "league": fx.get("league", {}),
            "status": fx.get("fixture", {}).get("status", {}),
        }

    # Update outstanding alerts first
    reconcile_alerts(live_by_id)

    # Evaluate NEW alerts
    for fx in fixtures:
        fixture = fx["fixture"]
        fid = fixture["id"]
        status = fixture["status"] or {}
        elapsed = status.get("elapsed")

        # cooldown per fixture
        if fid in last_sent_at and (now_utc() - last_sent_at[fid]).total_seconds() < COOLDOWN_SECS:
            continue

        half, half_min = minutes_and_half(elapsed)
        if not half or not half_min or not in_window(half, half_min):
            continue

        # Pull events for candidate
        try:
            events = get_fixture_events(fid)
        except Exception as e:
            log.warning("events fetch failed for %s: %s", fid, e)
            continue

        # Count last-~ROLLING_SECONDS (use elapsed minutes)
        now_min = elapsed or 0
        shots10 = sot10 = corners10 = 0
        for ev in events:
            t = ev.get("time", {})
            m = t.get("elapsed")
            if m is None: 
                continue
            if (now_min - m) <= (ROLLING_SECONDS // 60):
                etype  = (ev.get("type") or "").lower()
                detail = (ev.get("detail") or "").lower()
                if etype == "shot":
                    shots10 += 1
                    if "target" in detail:
                        sot10 += 1
                if etype == "corner":
                    corners10 += 1

        prob = prob_from_counts(shots10, sot10, corners10)
        if prob < GOAL_THRESHOLD:
            continue

        # Build message
        home_name = fx["teams"]["home"]["name"]
        away_name = fx["teams"]["away"]["name"]
        h = int(fx["goals"]["home"] or 0)
        a = int(fx["goals"]["away"] or 0)
        score_str  = f"{h}-{a}"
        minute_str = f"{'First' if half==1 else 'Second'} Half ({half_min}′)"
        pi = pressure_index(shots10, sot10, corners10)
        market = suggest_market(half, h, a)

        text = option_d_text(
            home=home_name, away=away_name,
            minute_str=minute_str,
            score_str=score_str,
            prob=prob, pi=pi,
            shots10=shots10, sot10=sot10, corners10=corners10,
            market=market,
            status_icon="⏳", status_word="Pending"
        )

        try:
            msg = send_text(CHAT_ID, text)
        except Exception as e:
            log.warning("send failed for %s: %s", fid, e)
            continue

        window_end_dt = now_utc() + timedelta(minutes=LOOKAHEAD_MIN) + timedelta(seconds=GRACE_SECS)
        pending_alerts[fid] = {
            "msg_id": msg.message_id,
            "chat_id": CHAT_ID,
            "sent_at": now_utc(),
            "window_end": window_end_dt,
            "score_at_send": score_str,
            "text_last": text,
            "status": "Pending",
            # store extra for success ping
            "home": home_name,
            "away": away_name,
            "league": fx["league"]["name"],
            "half": half,
        }
        last_sent_at[fid] = now_utc()
        log.info("Alert sent for fixture %s", fid)

# ---------------- Runner ----------------
def main():
    if STARTUP_MSG:
        try: send_text(CHAT_ID, "🚀 THE BOT IS UP AND RUNNING! YOU READY? 🔥")
        except Exception as e: log.warning("Startup message failed: %s", e)

    while True:
        try:
            if HEARTBEAT:
                m = now_utc().minute
                global last_heartbeat_minute
                if last_heartbeat_minute != m and m == 0:
                    last_heartbeat_minute = m
                    try: send_text(CHAT_ID, "👀 Monitoring pressure spikes. Vibes check, squad?")
                    except Exception as e: log.warning("Heartbeat failed: %s", e)

            process_live()
        except Exception as e:
            log.exception("loop error: %s", e)
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    main()
