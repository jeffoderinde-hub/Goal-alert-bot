# bot_telegram_goal_alert.py
# PTB 13.x compatible. Uses polling + JobQueue timers.
import os, time, threading, math, logging, random
from datetime import datetime, timedelta, timezone
import requests

from telegram import Bot, ParseMode
from telegram.utils.helpers import escape_markdown
from telegram.ext import Updater, CallbackContext

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("JBOT")

# ---------- Env (with safe defaults) ----------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GROUP_ID = int(os.getenv("TELEGRAM_GROUP_CHAT_ID", "0"))

API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
SEASON = os.getenv("SEASON", "2025").strip()

GOAL_ALERTS_ENABLED = os.getenv("GOAL_ALERTS_ENABLED", "1") == "1"
GOAL_THRESHOLD = float(os.getenv("GOAL_THRESHOLD", "0.60"))
LOOKAHEAD_MIN = int(os.getenv("LOOKAHEAD_MIN", "12"))
ROLLING_SECONDS = int(os.getenv("ROLLING_SECONDS", "900"))   # 15m
POLL_SECS = int(os.getenv("POLL_SECS", "12"))
COOLDOWN_SECS = int(os.getenv("COOLDOWN_SECS", "240"))
GOAL_CHECK_GRACE_SECS = int(os.getenv("GOAL_CHECK_GRACE_SECS", "30"))

# Windows (inclusive)
W1S = int(os.getenv("GOAL_WINDOW_1H_START", "18"))
W1E = int(os.getenv("GOAL_WINDOW_1H_END", "25"))
W2S = int(os.getenv("GOAL_WINDOW_2H_START", "65"))
W2E = int(os.getenv("GOAL_WINDOW_2H_END", "72"))

# Heartbeat & pings
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "1") == "1"
HEARTBEAT_INTERVAL_MIN = int(os.getenv("HEARTBEAT_INTERVAL_MIN", "180"))
SUCCESS_PING_ENABLED = os.getenv("SUCCESS_PING_ENABLED", "1") == "1"
DM_CHAT_ID = int(os.getenv("TELEGRAM_DM_CHAT_ID", "0"))  # optional

# ---------- API-FOOTBALL helpers ----------
AF_BASE = "https://v3.football.api-sports.io"

def af_headers():
    return {"x-apisports-key": API_KEY}

def af_get(path, params=None):
    r = requests.get(f"{AF_BASE}{path}", headers=af_headers(), params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def get_live_fixtures():
    """Return list of live fixtures with minimal info."""
    data = af_get("/fixtures", {"live": "all"})
    return data.get("response", [])

def get_fixture_stats_last_15min(fixture_id):
    """
    Approximate “pressure” from recent stats.
    API-FOOTBALL doesn’t give exact per-minute shot logs here,
    so we pull the team stats object and use what’s available.
    """
    # Stats endpoint
    data = af_get("/fixtures/statistics", {"fixture": fixture_id})
    # default zeros
    shots, sot, corners = 0, 0, 0

    for side in data.get("response", []):
        stats = side.get("statistics", [])
        for row in stats:
            t = (row.get("type") or "").lower()
            v = row.get("value") or 0
            if t == "total shots":
                shots += int(v)
            elif t == "shots on goal" or t == "shots on target":
                sot += int(v)
            elif t == "corners":
                corners += int(v)

    # Crude rolling approximation: weight last 10m out of total game tempo.
    # (If you later wire exact in-play events, swap this for a true rolling window.)
    # We’ll scale to a 0–100 “Pressure Index”.
    pi = min(100.0, shots * 2.0 + sot * 6.0 + corners * 3.0)
    # Fake a “last 10m” slice by damping:
    last10_shots = max(0, int(round(shots * 0.25)))
    last10_sot = max(0, int(round(sot * 0.35)))
    last10_corners = max(0, int(round(corners * 0.35)))
    return last10_shots, last10_sot, last10_corners, round(pi, 1)

def predict_prob(next_min, shots, sot, corners, pi, minute, score_tuple):
    """
    Simple heuristic probability (0..1) for a goal in next_min minutes.
    Tuned to be smooth & monotonic; adjust as you like.
    """
    (hg, ag) = score_tuple
    base = 0.28  # live baseline
    load = 0.02 * shots + 0.05 * sot + 0.015 * corners + (pi/100.0)*0.25
    # Game state tweaks
    if minute >= 80:
        load += 0.08
    if (hg + ag) >= 3:
        load += 0.04
    # compress to (0,1)
    p_per_min = max(0.01, min(0.9, base + load))
    p_window = 1 - (1 - p_per_min) ** (next_min/2.0)  # sublinear
    return max(0.0, min(1.0, p_window))

def half_and_minute(status_short, elapsed):
    """
    Try to determine half and minute from API fields.
    """
    # API-FOOTBALL gives elapsed total; we map rough halves.
    m = int(elapsed or 0)
    if m <= 45:
        return "First Half", m
    elif m <= 90:
        return "Second Half", m - 45
    else:
        # extra time – treat as 2H with minute cap
        return "Second Half", min(90, m) - 45

def suggest_market(score_home, score_away, half_label):
    """Generate the human-sounding bet line."""
    total = score_home + score_away
    if half_label == "First Half":
        if total == 0:
            return "Over 0.5 goals (1H)"
        else:
            return "Over 1.5 goals (FT)"
    else:
        # 2H
        if total == 0:
            return "Over 0.5 goals (FT)"
        elif total == 1:
            return "Over 1.5 goals (FT)"
        elif total == 2:
            return "Over 2.5 goals (FT)"
        else:
            return "Next Goal in 2H"

# ---------- State ----------
last_alert_ts = {}            # fixture_id -> unix
pending = {}                  # fixture_id -> dict(message_id, expire_at, sent_at)
recent_outcomes = {}          # fixture_id -> last known score to catch goals

LIVE_VIBES = [
    "👀 Monitoring pressure spikes. Vibes check, squad?",
    "⚙️ Systems green. Anyone got a hot pick right now?",
    "🧪 Live and locked in. Want me to be stricter or looser today?",
]

def within_windows(half_label, min_in_half):
    if half_label == "First Half":
        return W1S <= min_in_half <= W1E
    return W2S <= min_in_half <= W2E

# ---------- Formatting (Option D) ----------
def build_option_d(
    home, away, half_label, minute_in_half, score, prob, pi,
    last10, suggestion, status_text
):
    shots, sot, corners = last10
    lines = [
        "🧠 *JBOT GOAL ALERT*",
        "",
        f"*Match:* {escape_markdown(home)} vs {escape_markdown(away)}",
        f"🕒 *Time:* {escape_markdown(half_label)} ({minute_in_half}′)",
        f"🔢 *Score:* {score[0]}–{score[1]}",
        "",
        f"*Probability:* {int(round(prob*100))}% (next ~{LOOKAHEAD_MIN} minutes)",
        f"*Pressure Index:* {pi}",
        "",
        "*Form (Last 10 Minutes):*",
        f"• Shots: {shots}",
        f"• Shots on Target: {sot}",
        f"• Corners: {corners}",
        "",
        f"✅ *Recommended Bet:* {escape_markdown(suggestion)}",
        f"📌 *Status:* {status_text}",
    ]
    return "\n".join(lines)

# ---------- Telegram ----------
bot = Bot(BOT_TOKEN)

def send_group(text):
    if GROUP_ID == 0:
        return None
    return bot.send_message(
        chat_id=GROUP_ID, text=text, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True
    )

def edit_group(message_id, text):
    bot.edit_message_text(
        chat_id=GROUP_ID, message_id=message_id,
        text=text, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True
    )

def dm(text):
    if DM_CHAT_ID:
        bot.send_message(chat_id=DM_CHAT_ID, text=text)

# ---------- Heartbeat ----------
def heartbeat_job(context: CallbackContext):
    vibe = random.choice(LIVE_VIBES)
    try:
        send_group(vibe)
    except Exception as e:
        log.warning(f"heartbeat send failed: {e}")

# ---------- Startup ping ----------
def startup_ping():
    try:
        send_group("🚀 *THE BOT IS UP AND RUNNING!* _YOU READY?_ 🔥")
    except Exception as e:
        log.warning(f"startup message failed: {e}")

# ---------- Core loop ----------
def polling_loop():
    global pending, last_alert_ts, recent_outcomes
    log.info("Polling loop started.")
    while True:
        t0 = time.time()
        try:
            if not GOAL_ALERTS_ENABLED:
                time.sleep(POLL_SECS); continue

            fixtures = get_live_fixtures()
            now = datetime.now(timezone.utc)

            # 1) evaluate each live fixture
            for f in fixtures:
                fix = f.get("fixture", {})
                teams = f.get("teams", {})
                goals = f.get("goals", {})
                status = fix.get("status", {}) or {}
                short = status.get("short", "")
                elapsed = status.get("elapsed", 0) or 0

                fid = int(fix.get("id"))
                home = teams.get("home", {}).get("name", "Home")
                away = teams.get("away", {}).get("name", "Away")
                hg = int(goals.get("home") or 0)
                ag = int(goals.get("away") or 0)
                recent_outcomes.setdefault(fid, (hg, ag))

                # Determine half + minute-in-half
                half_label, m_in_half = half_and_minute(short, elapsed)

                # Track outcome edges (goal just scored)
                prev_score = recent_outcomes.get(fid, (hg, ag))
                if (hg, ag) != prev_score:
                    # if a goal just happened, resolve any pending
                    for pfid, info in list(pending.items()):
                        if pfid == fid:
                            try:
                                # success if within the pending window
                                if datetime.now(timezone.utc) <= info["expire_at"]:
                                    # mark success
                                    msg_id = info["message_id"]
                                    text = build_option_d(
                                        home, away, half_label, m_in_half, (hg, ag),
                                        prob=info["prob"], pi=info["pi"],
                                        last10=info["last10"],
                                        suggestion=info["suggestion"],
                                        status_text="✅ *Success*",
                                    )
                                    edit_group(msg_id, text)
                                    if SUCCESS_PING_ENABLED:
                                        send_group(f"✅ *Success:* {home} vs {away} — next goal landed.")
                                pending.pop(pfid, None)
                            except Exception as e:
                                log.warning(f"edit success failed: {e}")
                    recent_outcomes[fid] = (hg, ag)

                # Only alert inside chosen windows
                if not within_windows(half_label, m_in_half):
                    # If a pending alert expired with no goal -> mark failed
                    for pfid, info in list(pending.items()):
                        if pfid == fid and datetime.now(timezone.utc) > info["expire_at"] + timedelta(seconds=GOAL_CHECK_GRACE_SECS):
                            try:
                                msg_id = info["message_id"]
                                text = build_option_d(
                                    home, away, half_label, m_in_half, (hg, ag),
                                    prob=info["prob"], pi=info["pi"],
                                    last10=info["last10"],
                                    suggestion=info["suggestion"],
                                    status_text="❌ *Failed*",
                                )
                                edit_group(msg_id, text)
                            except Exception as e:
                                log.warning(f"edit fail failed: {e}")
                            pending.pop(pfid, None)
                    continue

                # cooldown per fixture
                if (time.time() - last_alert_ts.get(fid, 0)) < COOLDOWN_SECS:
                    continue

                # pull crude “last 10” stats + PI
                s, so, c, pi = get_fixture_stats_last_15min(fid)

                # probability in the next LOOKAHEAD_MIN minutes
                prob = predict_prob(LOOKAHEAD_MIN, s, so, c, pi, m_in_half, (hg, ag))

                if prob >= GOAL_THRESHOLD:
                    suggestion = suggest_market(hg, ag, half_label)
                    text = build_option_d(
                        home, away, half_label, m_in_half,
                        (hg, ag), prob, pi, (s, so, c),
                        suggestion, status_text="*Pending* ⏳"
                    )
                    try:
                        msg = send_group(text)
                        expire_at = datetime.now(timezone.utc) + timedelta(minutes=LOOKAHEAD_MIN)
                        pending[fid] = {
                            "message_id": msg.message_id if msg else None,
                            "expire_at": expire_at,
                            "prob": prob,
                            "pi": pi,
                            "last10": (s, so, c),
                            "suggestion": suggestion,
                        }
                        last_alert_ts[fid] = time.time()
                    except Exception as e:
                        log.warning(f"send alert failed: {e}")

            # 2) sweep expired pendings (no goal)
            for fid, info in list(pending.items()):
                if datetime.now(timezone.utc) > info["expire_at"] + timedelta(seconds=GOAL_CHECK_GRACE_SECS):
                    # find the fixture to get latest score/minute for edit
                    try:
                        # best-effort edit to failed
                        msg_id = info["message_id"]
                        text = "📌 *Status:* ❌ *Failed*"
                        if msg_id:
                            bot.edit_message_text(chat_id=GROUP_ID, message_id=msg_id,
                                                  text=text, parse_mode=ParseMode.MARKDOWN_V2)
                    except Exception as e:
                        log.debug(f"late fail edit: {e}")
                    pending.pop(fid, None)

        except Exception as e:
            log.exception(f"polling error: {e}")

        # pacing
        dt = time.time() - t0
        sleep_for = max(1.0, POLL_SECS - dt)
        time.sleep(sleep_for)

# ---------- Main ----------
def main():
    if not BOT_TOKEN or GROUP_ID == 0 or not API_KEY:
        log.error("Missing required env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_GROUP_CHAT_ID, API_FOOTBALL_KEY")
        return

    # Updater just for JobQueue timers (we don’t attach handlers here)
    updater = Updater(BOT_TOKEN, use_context=True)

    # Startup ping
    startup_ping()

    # Heartbeat (optional)
    if HEARTBEAT_ENABLED and HEARTBEAT_INTERVAL_MIN > 0:
        updater.job_queue.run_repeating(
            heartbeat_job, interval=HEARTBEAT_INTERVAL_MIN * 60, first=60
        )

    # Start job queue thread
    updater.start_polling(clean=True)  # keeps the job queue alive

    # Spin polling loop in its own thread
    t = threading.Thread(target=polling_loop, daemon=True)
    t.start()

    # idle forever
    updater.idle()

if __name__ == "__main__":
    main()
