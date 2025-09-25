# -*- coding: utf-8 -*-
import os, time, math, traceback, collections
from datetime import datetime, timedelta, timezone

import requests
from telegram import Bot, ParseMode
from telegram.ext import Updater, CommandHandler

# ======================
# TELEGRAM BASICS
# ======================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_DM_CHAT_ID = os.getenv("TELEGRAM_DM_CHAT_ID")  # your private id

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN/TELEGRAM_TOKEN or TELEGRAM_GROUP_CHAT_ID/TELEGRAM_CHAT_ID")
TELEGRAM_CHAT_ID = int(TELEGRAM_CHAT_ID)

bot = Bot(token=TELEGRAM_TOKEN)

def send(text, parse_mode="HTML"):
    try:
        bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=True
        )
    except Exception as e:
        print("Telegram send error:", e)

def edit_message(msg_id, new_text, parse_mode="HTML"):
    try:
        bot.edit_message_text(
            chat_id=TELEGRAM_CHAT_ID,
            message_id=msg_id,
            text=new_text,
            parse_mode=parse_mode,
            disable_web_page_preview=True
        )
    except Exception as e:
        print("Telegram edit error:", e)

# ======================
# API-FOOTBALL CLIENT
# ======================
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
if not API_FOOTBALL_KEY:
    raise RuntimeError("Missing API_FOOTBALL_KEY")

AF_BASE = "https://v3.football.api-sports.io"
AF_HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}
SEASON = os.getenv("SEASON", str(datetime.utcnow().year))

session = requests.Session()
session.headers.update(AF_HEADERS)

def af_get(path, params=None, timeout=20):
    r = session.get(f"{AF_BASE}{path}", params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ======================
# NEXT GOAL ENGINE
# ======================
POLL_SECS       = int(os.getenv("POLL_SECS", "15"))
GOAL_THRESHOLD  = float(os.getenv("GOAL_THRESHOLD", "0.60"))
COOLDOWN_SECS   = int(os.getenv("COOLDOWN_SECS", "240"))
ROLLING_SECONDS = int(os.getenv("ROLLING_SECONDS", "900"))
LOOKAHEAD_MIN   = int(os.getenv("LOOKAHEAD_MIN", "12"))

class NGState:
    def __init__(self):
        self.last_totals = None
        self.roll = collections.deque()
        self.last_prob = 0.0
        self.active_msg_id = None
        self.alert_window_end_min = None
        self.alert_start_min = None
        self.alert_score = None
        self.alert_status = None
        self.last_score_seen = None

    def add_delta(self, ts, ds, dsot, dcor, dred):
        self.roll.append((ts, ds, dsot, dcor, dred))
        cutoff = ts - ROLLING_SECONDS
        while self.roll and self.roll[0][0] < cutoff:
            self.roll.popleft()

    def sums(self):
        s = sot = c = r = 0
        for _, a, b, d, e in self.roll:
            s += a; sot += b; c += d; r += e
        return s, sot, c, r

def get_live_fixtures():
    js = af_get("/fixtures", {"live": "all"})
    return js.get("response", [])

def get_fixture_stats(fid):
    js = af_get("/fixtures/statistics", {"fixture": fid})
    return js.get("response", [])

def extract_totals(stats_json):
    out = {"shots":0, "sot":0, "corners":0, "reds":0}
    for side in stats_json:
        for it in side.get("statistics", []):
            name = (it.get("type") or "").lower()
            val  = it.get("value")
            v = val if isinstance(val, (int, float)) else 0
            if name in ("total shots", "shots total"): out["shots"] += v
            elif name in ("shots on target", "shots on goal"): out["sot"] += v
            elif name in ("corner kicks", "corners"): out["corners"] += v
            elif name == "red cards": out["reds"] += v
    return out

def pressure_index(s, sot, cor, reds):
    pi = 1.0*s + 2.2*sot + 1.2*cor
    if reds > 0: pi += 10.0
    return max(0.0, min(25.0, pi))

def goal_probability(pi, minute):
    base = 1 - math.exp(-0.11 * pi)
    window_boost = 0.10 if (20 <= minute <= 25 or 65 <= minute <= 70) else 0.0
    horizon_boost = max(0, min(0.10, (LOOKAHEAD_MIN - 10) * 0.01))
    return max(0.0, min(0.98, base + window_boost + horizon_boost))

def pick_over_line(score, minute):
    h, a = [int(x) for x in score.split("-")]
    total = h + a
    if minute < 45:
        return "Over 0.5 goals (First Half)" if total == 0 else "Over 1.5 goals"
    return "Over 1.5 goals" if total <= 1 else "Over 2.5 goals"

def format_goal_card(match, minute, score, prob, pi, shots10, sot10, cor10, status):
    half = "First Half" if minute < 45 else "Second Half"
    status_emoji = "⏳" if status == "Pending" else ("✅" if status == "Success" else "❌")
    return (
        "🎟️ <b>JBOT GOAL ALERT</b>\n\n"
        f"<b>Match:</b> {match}\n"
        f"⏱️ <b>Time:</b> {half} ({minute}′)\n"
        f"🔢 <b>Score:</b> {score}\n\n"
        f"<b>Probability:</b> {int(prob*100)}% (next ~{LOOKAHEAD_MIN} minutes)\n"
        f"<b>Pressure Index:</b> {pi:.1f}\n\n"
        "<b>Form (Last 10 Minutes):</b>\n"
        f"• Shots: {shots10}\n"
        f"• Shots on Target: {sot10}\n"
        f"• Corners: {cor10}\n\n"
        f"✅ <b>Recommended Bet:</b> {pick_over_line(score, minute)}\n\n"
        f"📌 <b>Status:</b> {status} {status_emoji}"
    )

ng_states = {}

def next_goal_loop_cycle():
    now_ts = datetime.now(timezone.utc).timestamp()
    fixtures = get_live_fixtures()

    for f in fixtures:
        fid = f["fixture"]["id"]
        st  = ng_states.get(fid) or NGState(); ng_states[fid] = st

        minute = int(f["fixture"]["status"].get("elapsed") or 0)
        status_short = f["fixture"]["status"].get("short", "")
        home = f["teams"]["home"]["name"]; away = f["teams"]["away"]["name"]
        hg = f["goals"]["home"] or 0; ag = f["goals"]["away"] or 0
        score = f"{hg}-{ag}"
        st.last_score_seen = score

        if status_short in ("1H","2H","ET","LIVE","INP") or minute > 0:
            totals = extract_totals(get_fixture_stats(fid))
            if st.last_totals is None:
                st.last_totals = totals
                continue
            ds   = max(0, totals["shots"]   - st.last_totals["shots"])
            dsot = max(0, totals["sot"]     - st.last_totals["sot"])
            dcor = max(0, totals["corners"] - st.last_totals["corners"])
            dred = max(0, totals["reds"]    - st.last_totals["reds"])
            st.last_totals = totals
            st.add_delta(now_ts, ds, dsot, dcor, dred)
            rs, rsot, rcor, rred = st.sums()
            pi = pressure_index(rs, rsot, rcor, rred)
            p  = goal_probability(pi, minute)

            if (st.active_msg_id is None) and (p >= GOAL_THRESHOLD):
                st.alert_start_min = minute
                st.alert_window_end_min = min(90, minute + LOOKAHEAD_MIN)
                st.alert_score = score
                st.alert_status = "Pending"
                text = format_goal_card(
                    f"{home} vs {away}", minute, score, p, pi, int(rs), int(rsot), int(rcor), st.alert_status
                )
                msg = bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                st.active_msg_id = msg.message_id

            if st.active_msg_id is not None:
                if st.alert_status == "Pending" and score != st.alert_score:
                    st.alert_status = "Success"
                if st.alert_status == "Pending" and minute >= (st.alert_window_end_min or minute):
                    st.alert_status = "Failed"
                text = format_goal_card(
                    f"{home} vs {away}", minute, score, p, pi, int(rs), int(rsot), int(rcor), st.alert_status
                )
                edit_message(st.active_msg_id, text)
                if st.alert_status in ("Success","Failed"):
                    if (now_ts - st.roll[-1][0]) >= COOLDOWN_SECS:
                        st.active_msg_id = None
                        st.alert_window_end_min = None
                        st.alert_start_min = None
                        st.alert_score = None
                        st.alert_status = None
                        st.last_prob = 0.0
            st.last_prob = p

# ======================
# DAILY ACCAS
# ======================
ACCA_ENABLED      = os.getenv("ACCA_ENABLED","1") == "1"
ACCA_TIME_HHMM    = os.getenv("ACCA_TIME_HHMM","10:00")
ACCA_STAKE        = float(os.getenv("ACCA_STAKE","1"))
ACCA_BOOKMAKER    = os.getenv("ACCA_BOOKMAKER","Bet365")
ACCA_STYLE        = os.getenv("ACCA_STYLE","D")
ACCA_MIN_FIXTURES = int(os.getenv("ACCA_MIN_FIXTURES","18"))
ACCA_MARKETS = [m.strip() for m in os.getenv(
    "ACCA_MARKETS","1X2,OVER_1_5,DOUBLE_CHANCE,BTTS_YES"
).split(",") if m.strip()]

ACCA_MAJOR_LEAGUES   = [int(x) for x in os.getenv(
    "ACCA_MAJOR_LEAGUES","39,140,135,78,61,2,3,128,71"
).split(",") if x.strip()]
ACCA_FALLBACK_LEAGUES = [int(x) for x in os.getenv(
    "ACCA_FALLBACK_LEAGUES","94,95,88,144,99,180,203,233"
).split(",") if x.strip()]

T4_MIN, T4_MAX  = 2.6, 3.8
T7_MIN, T7_MAX  = 5.0, 7.5
T10_MIN, T10_MAX= 25.0, 40.0

def acca_collect_fixtures():
    fx = []
    for lid in ACCA_MAJOR_LEAGUES:
        fx += af_get("/fixtures", {"league": lid, "season": SEASON, "from": str(datetime.utcnow().date()), "to": str((datetime.utcnow()+timedelta(days=2)).date())}).get("response", [])
    if len(fx) < ACCA_MIN_FIXTURES:
        for lid in ACCA_FALLBACK_LEAGUES:
            fx += af_get("/fixtures", {"league": lid, "season": SEASON, "from": str(datetime.utcnow().date()), "to": str((datetime.utcnow()+timedelta(days=2)).date())}).get("response", [])
    return [f for f in fx if f["fixture"]["status"]["short"] in ("NS","TBD","PST")]

def leg_label(mkt, sel, teams):
    if mkt == "1X2":
        if sel == "Home": return f'{teams["home"]["name"]} to Win'
        if sel == "Away": return f'{teams["away"]["name"]} to Win'
        return "Draw"
    if mkt == "OVER_1_5":     return "Over 1.5 Goals"
    if mkt == "DOUBLE_CHANCE": return f"Double Chance {sel}"
    if mkt == "BTTS_YES":     return "BTTS: Yes"
    return None

# Dummy odds fetch (replace with real /odds in production)
def choose_leg(f):
    return {
        "fixture": f["fixture"]["id"],
        "label": "Over 1.5 Goals",
        "odds": 1.30,
        "match": f'{f["teams"]["home"]["name"]} vs {f["teams"]["away"]["name"]}',
        "kick": f["fixture"]["date"],
        "bookmaker": ACCA_BOOKMAKER
    }

def build_acca(pool, legs, tmin, tmax):
    picks = pool[:legs]
    prod = 1.0
    for p in picks: prod *= p["odds"]
    return picks, prod

def format_acca_block(title, picks, prod, stake, bookmaker, style="D", colour=None):
    est = stake * prod
    badge = {"blue":"🔵","yellow":"🟡","red":"🔴"}.get(colour,"🏷")
    head = f"{badge} <b>{title}</b>\nStake £{stake:.2f} | Odds {prod:.2f} | Return £{est:.2f} | {bookmaker}*"
    lines = [head,"─────────────────────────────"]
    for i,p in enumerate(picks,1):
        when = datetime.fromisoformat(p["kick"].replace("Z","+00:00")).strftime("%a %H:%M")
        lines.append(f"{i}. {p['match']} — <i>{p['label']}</i> (@{p['odds']:.2f}) • {when} UTC")
    lines.append("\n<i>* Uses Bet365 when available; else best available bookmaker.</i>")
    return "\n".join(lines)

def format_acca_message(a4,o4,a7,o7,a10,o10):
    top = "🧠 <b>JBOT • Daily ACCAs</b>"
    b4  = format_acca_block("4-Fold (safer)",a4,o4,ACCA_STAKE,ACCA_BOOKMAKER,colour="blue")
    b7  = format_acca_block("7-Fold (balanced)",a7,o7,ACCA_STAKE,ACCA_BOOKMAKER,colour="yellow")
    b10 = format_acca_block("10-Fold (longshot)",a10,o10,ACCA_STAKE,ACCA_BOOKMAKER,colour="red")
    return "\n\n".join([top,b4,"",b7,"",b10])

def send_daily_accas():
    fixtures = acca_collect_fixtures()
    legs = [choose_leg(f) for f in fixtures if choose_leg(f)]
    if len(legs) < 8:
        send("⚠️ Not enough priced fixtures for ACCAs today.")
        return
    a4,o4   = build_acca(legs,4,T4_MIN,T4_MAX)
    a7,o7   = build_acca(legs,7,T7_MIN,T7_MAX)
    a10,o10 = build_acca(legs,10,T10_MIN,T10_MAX)
    msg = format_acca_message(a4,o4,a7,o7,a10,o10)
    send(msg,parse_mode="HTML")
    if TELEGRAM_DM_CHAT_ID:
        bot.send_message(chat_id=int(TELEGRAM_DM_CHAT_ID),text=msg,parse_mode=ParseMode.HTML,disable_web_page_preview=True)

def acca_command(update, context):
    send_daily_accas()

# ======================
# MAIN LOOP
# ======================
def main():
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("acca", acca_command))
    updater.start_polling()

    last_acca_date = None
    while True:
        try:
            now = datetime.now()
            if ACCA_ENABLED:
                hhmm = now.strftime("%H:%M")
                if hhmm == ACCA_TIME_HHMM and last_acca_date != now.date():
                    last_acca_date = now.date()
                    send_daily_accas()
            next_goal_loop_cycle()
        except Exception as e:
            print("Loop error:", e)
            traceback.print_exc()
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    main()
