# bot_telegram_goal_alert.py
# Sends Telegram alerts in 20–25′ and 65–70′ if a goal looks likely (shots/SOT/corners pressure).
import os, time, math, requests, collections
from datetime import datetime, timezone
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
from telegram import Bot

API_HOST = "https://v3.football.api-sports.io"
API_KEY  = os.getenv("API_FOOTBALL_KEY")               # REQUIRED
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")     # REQUIRED
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID") # REQUIRED (e.g., -1002957942850)

FIRST_WINDOW  = (20, 25)      # 1st half
SECOND_WINDOW = (65, 70)      # 2nd half (match clock)
GOAL_THRESHOLD = float(os.getenv("GOAL_THRESHOLD", "0.70"))
POLL_SECS = int(os.getenv("POLL_SECS", "30"))
COOLDOWN_SECS = int(os.getenv("COOLDOWN_SECS", "360"))
ROLLING_SECONDS = int(os.getenv("ROLLING_SECONDS", "600"))
LOOKAHEAD_MIN = int(os.getenv("LOOKAHEAD_MIN", "12"))

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_GROUP_CHAT_ID")
if not API_KEY: raise SystemExit("Missing API_FOOTBALL_KEY")

bot = Bot(token=TELEGRAM_TOKEN); CHAT_ID = int(TELEGRAM_CHAT_ID)
session = requests.Session(); session.headers.update({"x-apisports-key": API_KEY})

def get_live_fixtures():
    r = session.get(f"{API_HOST}/fixtures?live=all", timeout=20); r.raise_for_status()
    return r.json().get("response", [])

def get_stats(fid):
    r = session.get(f"{API_HOST}/fixtures/statistics?fixture={fid}", timeout=20); r.raise_for_status()
    return r.json().get("response", [])

def extract_totals(stats_json):
    m = {"shots":0,"sot":0,"corners":0,"reds":0}
    for side in stats_json:
        for it in side.get("statistics", []):
            n = (it.get("type") or "").lower(); v = it.get("value"); v = v if isinstance(v,(int,float)) else 0
            if n in ("shots on target","shots on goal"): m["sot"] += v
            elif n in ("total shots","shots total"):     m["shots"] += v
            elif n in ("corner kicks","corners"):        m["corners"] += v
            elif n == "red cards":                       m["reds"] += v
    return m

def logistic(a,b,c,d,PI,time_factor,strength_delta):
    z=a+b*PI+c*time_factor+d*strength_delta; return 1/(1+math.exp(-z))

def pressure_index(ds,dsot,dcor,dred):
    PI=1.0*ds+2.2*dsot+1.2*dcor;  PI += 10.0 if dred>0 else 0
    return max(0.0,min(25.0,PI))

def in_window(elapsed):
    if elapsed is None: return None
    if FIRST_WINDOW[0] <= elapsed <= FIRST_WINDOW[1]: return "1H"
    if SECOND_WINDOW[0] <= elapsed <= SECOND_WINDOW[1]: return "2H"
    return None

def over_line_text(h,a): tot=(h or 0)+(a or 0); return f"Over {tot+0.5:.1f} goals"
def send(text): bot.send_message(chat_id=CHAT_ID,text=text)

class State:
    def __init__(self): self.last=None; self.roll=collections.deque(); self.last_alert={"1H":0.0,"2H":0.0}; self.last_p=0.0
    def add(self,ds,dsot,dcor,dred,ts,win): 
        self.roll.append((ts,ds,dsot,dcor,dred)); cutoff=ts-win
        while self.roll and self.roll[0][0]<cutoff: self.roll.popleft()
    def sums(self):
        ds=dsot=dcor=dred=0
        for _,a,b,c,d in self.roll: ds+=a; dsot+=b; dcor+=c; dred+=d
        return ds,dsot,dcor,dred

states={}

def main():
    send("✅ Goal-alert bot starting…")
    while True:
        now=datetime.now(timezone.utc).timestamp()
        try:
            for f in get_live_fixtures():
                fid=f["fixture"]["id"]; st=states.get(fid) or State(); states[fid]=st
                elapsed=f["fixture"]["status"].get("elapsed") or 0
                wnd=in_window(elapsed); 
                if not wnd: continue
                totals=extract_totals(get_stats(fid))
                if st.last is None: st.last=totals; continue
                ds=max(0,totals["shots"]-st.last["shots"])
                dsot=max(0,totals["sot"]-st.last["sot"])
                dcor=max(0,totals["corners"]-st.last["corners"])
                dred=max(0,totals["reds"]-st.last["reds"])
                st.last=totals
                st.add(ds,dsot,dcor,dred,now,ROLLING_SECONDS)
                rs,rsot,rcor,rred=st.sums()
                minutes_in_half = elapsed if elapsed<=45 else elapsed-45
                minutes_left = max(0.0,45-minutes_in_half)
                time_factor = min(1.0, minutes_left/45.0)
                PI=pressure_index(rs,rsot,rcor,rred)
                p=logistic(a=-2.0,b=0.22,c=0.8,d=0.0,PI=PI,time_factor=time_factor,strength_delta=0.0)
                cooldown_ok=(now-st.last_alert[wnd])>COOLDOWN_SECS
                jumped=(p-st.last_p)>=0.10; hot=p>=GOAL_THRESHOLD; micro=(rsot>=2 or rcor>=2)
                if hot and cooldown_ok and (jumped or micro):
                    h=f["teams"]["home"]["name"]; a=f["teams"]["away"]["name"]
                    hg=f["goals"]["home"] or 0; ag=f["goals"]["away"] or 0
                    send(f"⚠️ Goal likely soon ({wnd} window)\n{h} vs {a}\n"
                         f"Score: {hg}-{ag} | Suggest: {over_line_text(hg,ag)}\n"
                         f"Minute: {elapsed}′ | Prob(next ~{LOOKAHEAD_MIN}m): {p*100:.0f}%\n"
                         f"Last ~10m: shots={int(rs)}, SOT={int(rsot)}, corners={int(rcor)} | PI={PI:.1f}")
                    st.last_alert[wnd]=now
                st.last_p=p
        except Exception as e:
            try: send(f"Bot error: {e}")
            except: pass
        time.sleep(POLL_SECS)

if __name__=="__main__": main()
