# bot_telegram_goal_alert.py
# JBOT: goal-likely alerts (Option D), status tracking, hourly heartbeat, optional GOAL! alerts

import os, time, math, requests, collections, random
from datetime import datetime, timezone, timedelta

# Optional .env support (safe if missing)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from telegram import Bot

# ============== ENV VARS (case-sensitive) ==============
API_KEY = os.getenv("API_FOOTBALL_KEY")                            # REQUIRED
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")  # REQUIRED
CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")  # REQUIRED

# Feature toggles
PREDICTIVE_ENABLED   = os.getenv("PREDICTIVE_ENABLED", "1")  # 1/0
GOAL_ALERTS_ENABLED  = os.getenv("GOAL_ALERTS_ENABLED", "1") # 1/0
HEARTBEAT_ENABLED    = os.getenv("HEARTBEAT_ENABLED", "1")   # 1/0
HEARTBEAT_INTERVAL_MIN = int(os.getenv("HEARTBEAT_INTERVAL_MIN", "60"))

# Tunables
GOAL_THRESHOLD = float(os.getenv("GOAL_THRESHOLD", "0.70"))
POLL_SECS      = int(os.getenv("POLL_SECS", "30"))
COOLDOWN_SECS  = int(os.getenv("COOLDOWN_SECS", "360"))     # anti-spam per window
ROLLING_SECONDS = int(os.getenv("ROLLING_SECONDS", "600"))  # ~last 10 minutes
LOOKAHEAD_MIN   = int(os.getenv("LOOKAHEAD_MIN", "12"))

# Minute windows (absolute match clock)
FIRST_WINDOW  = (20, 25)   # First Half
SECOND_WINDOW = (65, 70)   # Second Half

# Sanity checks
if not BOT_TOKEN: raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
if not CHAT_ID:   raise SystemExit("Missing TELEGRAM_GROUP_CHAT_ID")
if not API_KEY:   raise SystemExit("Missing API_FOOTBALL_KEY")

CHAT_ID = int(CHAT_ID)
bot = Bot(token=BOT_TOKEN)

# ============== HEARTBEAT ==============
_HEARTBEAT_LINES = [
    "🚀 THE BOT IS UP AND RUNNING! YOU READY? 🔥",
    "🟢 I’m alive and watching the games — how’s everyone feeling?",
    "⚙️ Systems green. Anyone got a hot pick right now?",
    "👀 Monitoring pressure spikes. Vibes check, squad?",
    "⚡ Live and locked in. Want me to be stricter or looser today?",
    "🔥 I’m on it. Who needs a goal right now?"
]

def next_top_of_hour_plus_jitter(minutes_jitter=2):
    now = datetime.now(timezone.utc)
    nxt = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    j = random.randint(-minutes_jitter, minutes_jitter)
    return (nxt + timedelta(minutes=j)).timestamp()

def send(text: str):
    try:
        bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as e:
        print("Send error:", e, flush=True)

def send_startup():
    send("🚀 THE BOT IS UP AND RUNNING! YOU READY? 🔥")

def send_heartbeat():
    send(random.choice(_HEARTBEAT_LINES))

# ============== API-FOOTBALL helpers ==============
API_HOST = "https://v3.football.api-sports.io"
session = requests.Session()
session.headers.update({"x-apisports-key": API_KEY})

def get_live_fixtures():
    r = session.get(f"{API_HOST}/fixtures?live=all", timeout=20)
    r.raise_for_status()
    return r.json().get("response", [])

def get_stats(fid):
    r = session.get(f"{API_HOST}/fixtures/statistics?fixture={fid}", timeout=20)
    r.raise_for_status()
    return r.json().get("response", [])

def extract_totals(stats_json):
    # cumulative across both teams
    m = {"shots":0,"sot":0,"corners":0,"reds":0}
    for side in stats_json:
        for it in side.get("statistics", []):
            name = (it.get("type") or "").lower()
            val = it.get("value"); v = val if isinstance(val,(int,float)) else 0
            if name in ("shots on target","shots on goal"): m["sot"] += v
            elif name in ("total shots","shots total"):     m["shots"] += v
            elif name in ("corner kicks","corners"):        m["corners"] += v
            elif name == "red cards":                       m["reds"] += v
    return m

# ============== Predictive model bits ==============
def logistic(a,b,c,d,PI,time_factor,strength_delta):
    z = a + b*PI + c*time_factor + d*strength_delta
    return 1.0/(1.0+math.exp(-z))

def pressure_index(ds,dsot,dcor,dred):
    PI = 1.0*ds + 2.2*dsot + 1.2*dcor
    if dred > 0: PI += 10.0
    return max(0.0, min(25.0, PI))

def in_window(elapsed):
    if elapsed is None: return None
    if FIRST_WINDOW[0] <= elapsed <= FIRST_WINDOW[1]: return "First Half"
    if SECOND_WINDOW[0] <= elapsed <= SECOND_WINDOW[1]: return "Second Half"
    return None

def over_line_text(h,a):
    total = (h or 0) + (a or 0)
    return f"Over {total + 0.5:.1f} goals"

# ============== Option D formatter (your choice) ==============
def format_alert_D(h, a, hg, ag, half_label, minute, prob, lookahead_min, shots, sot, corners, pi, pick_text, status):
    return (f"🎟️ JBOT GOAL ALERT\n\n"
            f"Match: {h} vs {a}\n"
            f"⏱️ Time: {half_label} ({minute}′)\n"
            f"🔢 Score: {hg}–{ag}\n\n"
            f"Probability: {prob:.0f}% (next ~{lookahead_min} minutes)\n"
            f"Pressure Index: {pi:.1f}\n\n"
            f"Form (Last 10 Minutes):\n"
            f"• Shots: {shots}\n"
            f"• Shots on Target: {sot}\n"
            f"• Corners: {corners}\n\n"
            f"✅ Recommended Bet: {pick_text}\n\n"
            f"📌 Status: {status}")

# ============== State: fixtures + alerts + scores ==============
class State:
    def __init__(self):
        self.last = None                 # last totals
        self.roll = collections.deque()  # (ts, ds, dsot, dcor, dred)
        self.last_alert_time = {"First Half":0.0, "Second Half":0.0}
        self.last_prob = 0.0

    def add(self, ds,dsot,dcor,dred, ts, horizon):
        self.roll.append((ts,ds,dsot,dcor,dred))
        cut = ts - horizon
        while self.roll and self.roll[0][0] < cut:
            self.roll.popleft()

    def sums(self):
        ds=dsot=dcor=dred=0
        for _,a,b,c,d in self.roll:
            ds += a; dsot += b; dcor += c; dred += d
        return ds, dsot, dcor, dred

# track score for GOAL! and for status
last_score = {}    # fid -> (hg,ag)
class TrackedAlert:
    def __init__(self, fixture_id, window, init_score):
        self.fixture_id = fixture_id
        self.window = window            # "First Half" or "Second Half"
        self.init_score = init_score    # (hg,ag) at alert time
        self.status = "Pending ⏳"
        self.done = False

alerts = {}  # (fid, window) -> TrackedAlert

# ============== MAIN LOOP ==============
def main():
    send_startup()
    next_hb = next_top_of_hour_plus_jitter()

    states = {}

    while True:
        now_ts = datetime.now(timezone.utc).timestamp()

        # Hourly heartbeat
        if HEARTBEAT_ENABLED == "1" and now_ts >= next_hb:
            send_heartbeat()
            next_hb = next_top_of_hour_plus_jitter()

        try:
            fixtures = get_live_fixtures()
            for f in fixtures:
                fid = f["fixture"]["id"]
                st = states.get(fid) or State()
                states[fid] = st

                # live basics
                elapsed = f["fixture"]["status"].get("elapsed") or 0
                status_short = f["fixture"]["status"].get("short") or ""
                h = f["teams"]["home"]["name"]; a = f["teams"]["away"]["name"]
                hg = f["goals"]["home"] or 0;   ag = f["goals"]["away"] or 0
                last_score[fid] = (hg, ag)

                # ---------- Optional GOAL! alerts on score change ----------
                if GOAL_ALERTS_ENABLED == "1":
                    prev = last_score.get(("prev", fid))
                    if prev is None:
                        last_score[("prev", fid)] = (hg, ag)
                    else:
                        if (hg, ag) != prev:
                            minute = elapsed
                            who = "⚽ Goal!"
                            if (hg - prev[0]) > 0:
                                who = f"⚽ {h}"
                            elif (ag - prev[1]) > 0:
                                who = f"⚽ {a}"
                            send(f"{who} — {h} {hg}–{ag} {a}  •  {minute}′")
                            last_score[("prev", fid)] = (hg, ag)

                # ---------- Predictive window logic ----------
                wnd = in_window(elapsed)
                if PREDICTIVE_ENABLED == "1" and wnd:
                    totals = extract_totals(get_stats(fid))
                    if st.last is None:
                        st.last = totals
                        continue

                    ds   = max(0, totals["shots"]   - st.last["shots"])
                    dsot = max(0, totals["sot"]     - st.last["sot"])
                    dcor = max(0, totals["corners"] - st.last["corners"])
                    dred = max(0, totals["reds"]    - st.last["reds"])
                    st.last = totals

                    st.add(ds, dsot, dcor, dred, now_ts, ROLLING_SECONDS)
                    rs, rsot, rcor, rred = st.sums()

                    minutes_in_half = elapsed if elapsed <= 45 else elapsed - 45
                    minutes_left = max(0.0, 45 - minutes_in_half)
                    time_factor = min(1.0, minutes_left / 45.0)

                    PI = pressure_index(rs, rsot, rcor, rred)
                    p  = logistic(a=-2.0, b=0.22, c=0.8, d=0.0,
                                  PI=PI, time_factor=time_factor, strength_delta=0.0)

                    cooldown_ok = (now_ts - st.last_alert_time[wnd]) > COOLDOWN_SECS
                    jumped = (p - st.last_prob) >= 0.10
                    hot    = p >= GOAL_THRESHOLD
                    micro  = (rsot >= 2 or rcor >= 2)

                    if hot and cooldown_ok and (jumped or micro):
                        pick = over_line_text(hg, ag)
                        msg = format_alert_D(
                            h=h, a=a, hg=hg, ag=ag,
                            half_label=wnd, minute=elapsed,
                            prob=p*100, lookahead_min=LOOKAHEAD_MIN,
                            shots=int(rs), sot=int(rsot), corners=int(rcor), pi=PI,
                            pick_text=pick, status="Pending ⏳"
                        )
                        send(msg)
                        st.last_alert_time[wnd] = now_ts
                        alerts[(fid, wnd)] = TrackedAlert(fid, wnd, (hg, ag))

                    st.last_prob = p

                # ---------- Result tracking for outstanding alerts ----------
                for key, alert in list(alerts.items()):
                    if alert.done or key[0] != fid:
                        continue

                    # success: score increased vs initial
                    if (hg, ag) != alert.init_score and alert.status.startswith("Pending"):
                        alert.status = "Success ✅"
                        send(
                            "🎉 JBOT RESULT — SUCCESS ✅\n\n"
                            f"Match: {h} vs {a}\n"
                            f"Current Score: {hg}–{ag}\n"
                            "Goal scored within expected window!"
                        )
                        alert.done = True
                        alerts.pop(key, None)
                        continue

                    # failure: match ended without new goal
                    if status_short in ("FT", "AET", "PEN") and alert.status.startswith("Pending"):
                        alert.status = "Failed ❌"
                        send(
                            "❌ JBOT RESULT — FAILED\n\n"
                            f"Match: {h} vs {a}\n"
                            f"Final Score: {hg}–{ag}\n"
                            "No goal scored within expected window."
                        )
                        alert.done = True
                        alerts.pop(key, None)

        except Exception as e:
            print("Loop error:", e, flush=True)
            try: send(f"Bot error: {e}")
            except: pass

        time.sleep(POLL_SECS)

if __name__ == "__main__":
    main()
