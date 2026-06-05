import os, time, json, threading
from datetime import datetime, timedelta
import calendar
from flask import Flask, render_template, jsonify, request
import pandas as pd
import requests
import tempfile

app = Flask(__name__)

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = "8905811864:AAEaEzjyirk1dJivfvtQWtumL3mXXCh5-SQ"
TELEGRAM_CHAT_ID   = "1450144996"

STARTING_BALANCE     = 100.0
MIN_SCORE            = 6
MIN_RR               = 1.8
MAX_OPEN_TRADES      = 5
MAX_SIGNALS_PER_COIN = 3
COOLDOWN_BARS        = 4
DATA_LIMIT           = 700
LK_OFFSET_SEC        = 5 * 3600 + 30 * 60
SKIP_UTC_START       = 0
SKIP_UTC_END         = 6

SCORE_ALLOC_PCT = {6: 5.0, 7: 8.0, 8: 12.0, 9: 18.0, 10: 22.0}
MAX_ALLOC_PCT   = 0.25
MIN_TRADE_USD   = 2.0

WATCH_LIST = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","LINKUSDT","AVAXUSDT","DOTUSDT","MATICUSDT",
    "LTCUSDT","UNIUSDT","NEARUSDT","APTUSDT","INJUSDT",
    "OPUSDT","ARBUSDT","SUIUSDT","TIAUSDT","FETUSDT"
]
BLOCKED_COINS = ["TRXUSDT","DOGEUSDT"]

# ── Global state ───────────────────────────────────────────
state = {
    "balance":      STARTING_BALANCE,
    "starting":     STARTING_BALANCE,
    "signals":      [],
    "running":      False,
    "progress":     "",
    "progress_pct": 0,
    "wins": 0, "losses": 0, "bes": 0,
    "last_error":   None,
    "run_start_date": "",
    "run_end_date":   "",
}
state_lock = threading.Lock()

# ══════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════
def date_to_ms(date_str, end_of_day=False):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    if end_of_day:
        utc_sec = calendar.timegm((d.year,d.month,d.day,23,59,59)) - LK_OFFSET_SEC
    else:
        utc_sec = calendar.timegm((d.year,d.month,d.day,0,0,0)) - LK_OFFSET_SEC
    return utc_sec * 1000

def ms_to_lk(ms):
    if ms is None: return "—"
    return datetime.utcfromtimestamp(ms/1000 + LK_OFFSET_SEC).strftime('%d %b %Y %I:%M %p')

def get_date_range(start_str, end_str):
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end   = datetime.strptime(end_str,   "%Y-%m-%d")
    dates = []
    cur = start
    while cur <= end:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates

# ══════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════
def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in [message[i:i+4000] for i in range(0, len(message), 4000)]:
        try:
            r = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "Markdown"
            }, timeout=10)
            if r.status_code != 200:
                requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk}, timeout=10)
        except Exception as e:
            print(f"Alert error: {e}")
        time.sleep(0.3)

def send_telegram_photo(photo_path, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    caption = caption[:1020]
    try:
        with open(photo_path, 'rb') as photo:
            r = requests.post(url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"},
                files={"photo": photo}, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"Chart send error: {e}"); return False

# ══════════════════════════════════════════════════════════
# DATA
# ══════════════════════════════════════════════════════════
def download_data(symbol, interval="1h", limit=700, end_ms=None):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        if end_ms: url += f"&endTime={end_ms}"
        r = requests.get(url, timeout=10)
        if r.status_code != 200: return None
        df = pd.DataFrame(r.json(), columns=[
            'Open_time','open','high','low','close','volume',
            'Close_time','qav','num_trades','taker_base','taker_quote','ignore'])
        for c in ['open','high','low','close','volume']:
            df[c] = df[c].astype(float)
        df['Open_time']  = df['Open_time'].astype(int)
        df['Close_time'] = df['Close_time'].astype(int)
        return df.reset_index(drop=True)
    except: return None

# ══════════════════════════════════════════════════════════
# HTF TREND
# ══════════════════════════════════════════════════════════
def get_htf_trend(symbol, end_ms=None):
    scores = {"BULL":0,"BEAR":0}
    for tf,lim,w in [("4h",100,2),("1d",50,1)]:
        df = download_data(symbol, interval=tf, limit=lim, end_ms=end_ms)
        if df is None or len(df)<50: continue
        c=df['close']; e20=c.ewm(span=20,adjust=False).mean(); e50=c.ewm(span=50,adjust=False).mean()
        lc=c.iloc[-1]
        if lc>e20.iloc[-1]>e50.iloc[-1]:   scores["BULL"]+=w
        elif lc<e20.iloc[-1]<e50.iloc[-1]: scores["BEAR"]+=w
        time.sleep(0.08)
    if scores["BULL"]>=2: return "BULL",min(scores["BULL"],3)
    if scores["BEAR"]>=2: return "BEAR",min(scores["BEAR"],3)
    return "NEUTRAL",0

# ══════════════════════════════════════════════════════════
# MARKET STRUCTURE
# ══════════════════════════════════════════════════════════
def get_market_structure(df, lookback=30):
    highs=df['high'].iloc[-lookback:].values; lows=df['low'].iloc[-lookback:].values
    sh,sl=[],[]
    for i in range(2,len(highs)-2):
        if highs[i]==max(highs[i-2],highs[i-1],highs[i],highs[i+1],highs[i+2]): sh.append(highs[i])
        if lows[i] ==min(lows[i-2], lows[i-1], lows[i], lows[i+1], lows[i+2]):  sl.append(lows[i])
    if len(sh)<2 or len(sl)<2: return "NEUTRAL"
    if sh[-1]>sh[-2] and sl[-1]>sl[-2]: return "BULL"
    if sh[-1]<sh[-2] and sl[-1]<sl[-2]: return "BEAR"
    return "NEUTRAL"

# ══════════════════════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════════════════════
def compute_indicators(df):
    d=df.copy()
    d['EMA8']  =d['close'].ewm(span=8,  adjust=False).mean()
    d['EMA21'] =d['close'].ewm(span=21, adjust=False).mean()
    d['EMA55'] =d['close'].ewm(span=55, adjust=False).mean()
    d['EMA200']=d['close'].ewm(span=200,adjust=False).mean()
    delta=d['close'].diff()
    gain=delta.where(delta>0,0.0).rolling(14).mean()
    loss=(-delta.where(delta<0,0.0)).rolling(14).mean()
    d['RSI']=100-(100/(1+gain/(loss+1e-9)))
    d['RSI_DIV_BULL']=False; d['RSI_DIV_BEAR']=False
    for i in range(5,len(d)):
        pl=d['low'].iloc[i-5:i+1];  rl=d['RSI'].iloc[i-5:i+1]
        ph=d['high'].iloc[i-5:i+1]; rh=d['RSI'].iloc[i-5:i+1]
        if pl.iloc[-1]<pl.min()*1.001 and rl.iloc[-1]>rl.iloc[:-1].min()*1.01:
            d.at[d.index[i],'RSI_DIV_BULL']=True
        if ph.iloc[-1]>ph.max()*0.999 and rh.iloc[-1]<rh.iloc[:-1].max()*0.99:
            d.at[d.index[i],'RSI_DIV_BEAR']=True
    e12=d['close'].ewm(span=12,adjust=False).mean(); e26=d['close'].ewm(span=26,adjust=False).mean()
    d['MACD']=e12-e26; d['MACDS']=d['MACD'].ewm(span=9,adjust=False).mean()
    d['MACD_HIST']=d['MACD']-d['MACDS']
    d['BB_MA']=d['close'].rolling(20).mean(); d['BB_STD']=d['close'].rolling(20).std()
    d['BB_UP']=d['BB_MA']+2*d['BB_STD']; d['BB_LO']=d['BB_MA']-2*d['BB_STD']
    d['BB_PCT']=(d['close']-d['BB_LO'])/(d['BB_UP']-d['BB_LO']+1e-9)
    d['BB_WIDTH']=(d['BB_UP']-d['BB_LO'])/(d['BB_MA']+1e-9)
    d['VOL_MA20']=d['volume'].rolling(20).mean()
    d['OBV']=(d['volume']*d['close'].diff().apply(lambda x:1 if x>0 else -1 if x<0 else 0)).cumsum()
    hl=d['high']-d['low']; hpc=(d['high']-d['close'].shift(1)).abs(); lpc=(d['low']-d['close'].shift(1)).abs()
    d['ATR']=pd.concat([hl,hpc,lpc],axis=1).max(axis=1).rolling(14).mean()
    rsi_min=d['RSI'].rolling(14).min(); rsi_max=d['RSI'].rolling(14).max()
    d['STOCHRSI']=(d['RSI']-rsi_min)/((rsi_max-rsi_min)+1e-9)
    pdm=d['high'].diff().clip(lower=0); mdm=(-d['low'].diff()).clip(lower=0)
    pdm2=pdm.where(pdm>mdm,0); mdm2=mdm.where(mdm>pdm,0)
    d['PLUS_DI'] =100*(pdm2.rolling(14).mean()/(d['ATR']+1e-9))
    d['MINUS_DI']=100*(mdm2.rolling(14).mean()/(d['ATR']+1e-9))
    dx=100*(d['PLUS_DI']-d['MINUS_DI']).abs()/(d['PLUS_DI']+d['MINUS_DI']+1e-9)
    d['ADX']=dx.rolling(14).mean()
    hl2=(d['high']+d['low'])/2; ub=hl2+3.0*d['ATR']; lb=hl2-3.0*d['ATR']
    st_dir=[1]*len(d); st_val=[0.0]*len(d)
    for i in range(1,len(d)):
        fub=ub.iloc[i] if ub.iloc[i]<ub.iloc[i-1] or d['close'].iloc[i-1]>ub.iloc[i-1] else ub.iloc[i-1]
        flb=lb.iloc[i] if lb.iloc[i]>lb.iloc[i-1] or d['close'].iloc[i-1]<lb.iloc[i-1] else lb.iloc[i-1]
        if st_val[i-1]==ub.iloc[i-1]:
            st_dir[i]=-1 if d['close'].iloc[i]<=fub else 1
            st_val[i]=fub if d['close'].iloc[i]<=fub else flb
        else:
            st_dir[i]=1 if d['close'].iloc[i]>=flb else -1
            st_val[i]=flb if d['close'].iloc[i]>=flb else fub
    d['ST_DIR']=st_dir; d['ST_VAL']=st_val
    typ=(d['high']+d['low']+d['close'])/3
    d['VWAP']=(typ*d['volume']).rolling(24).sum()/d['volume'].rolling(24).sum()
    d['ROC5']=d['close'].pct_change(5)*100
    d['TENKAN']=(d['high'].rolling(9).max()+d['low'].rolling(9).min())/2
    d['KIJUN'] =(d['high'].rolling(26).max()+d['low'].rolling(26).min())/2
    return d

# ══════════════════════════════════════════════════════════
# CANDLE PATTERNS
# ══════════════════════════════════════════════════════════
def detect_patterns(df,i):
    c=df.iloc[i]; p=df.iloc[i-1]; p2=df.iloc[i-2] if i>=2 else p
    pats=[]; tr=c['high']-c['low']
    if tr<1e-9: return pats
    body=abs(c['close']-c['open']); uw=c['high']-max(c['open'],c['close']); lw=min(c['open'],c['close'])-c['low']
    if (p['close']<p['open'] and c['close']>c['open'] and
            c['open']<min(p['open'],p['close']) and c['close']>max(p['open'],p['close'])): pats.append("BULL_ENGULF")
    if (p['close']>p['open'] and c['close']<c['open'] and
            c['open']>max(p['open'],p['close']) and c['close']<min(p['open'],p['close'])): pats.append("BEAR_ENGULF")
    if lw>tr*0.55 and body<tr*0.35 and uw<tr*0.2: pats.append("BULL_PIN")
    if uw>tr*0.55 and body<tr*0.35 and lw<tr*0.2: pats.append("BEAR_PIN")
    if (p2['close']<p2['open'] and abs(p['close']-p['open'])<(p['high']-p['low'])*0.3 and
            c['close']>c['open'] and c['close']>(p2['open']+p2['close'])/2): pats.append("MORNING_STAR")
    if (p2['close']>p2['open'] and abs(p['close']-p['open'])<(p['high']-p['low'])*0.3 and
            c['close']<c['open'] and c['close']<(p2['open']+p2['close'])/2): pats.append("EVENING_STAR")
    if i>=3:
        if (all(df.iloc[k]['close']>df.iloc[k]['open'] for k in range(i-2,i+1)) and
                df.iloc[i-1]['open']>df.iloc[i-2]['close']*0.995): pats.append("THREE_SOLDIERS")
        if (all(df.iloc[k]['close']<df.iloc[k]['open'] for k in range(i-2,i+1)) and
                df.iloc[i-1]['open']<df.iloc[i-2]['close']*1.005): pats.append("THREE_CROWS")
    if c['high']<=p['high'] and c['low']>=p['low']: pats.append("INSIDE_BAR")
    if body<tr*0.1: pats.append("DOJI")
    return pats

def find_key_levels(df,lookback=100):
    levels=[]; src=df.tail(lookback).reset_index(drop=True)
    for i in range(2,len(src)-2):
        h=src['high'].iloc[i]; l=src['low'].iloc[i]
        if h==src['high'].iloc[i-2:i+3].max(): levels.append(h)
        if l==src['low'].iloc[i-2:i+3].min():  levels.append(l)
    levels.sort(); zones=[]; i=0
    while i<len(levels):
        cluster=[levels[i]]; j=i+1
        while j<len(levels) and (levels[j]-levels[i])/(levels[i]+1e-9)<0.005:
            cluster.append(levels[j]); j+=1
        if len(cluster)>=2: zones.append(sum(cluster)/len(cluster))
        i=j if j>i else i+1
    return zones

# ══════════════════════════════════════════════════════════
# SCORING ENGINE
# ══════════════════════════════════════════════════════════
def score_signal(df,i,direction,htf_trend,htf_strength,key_levels):
    c=df.iloc[i]; prev=df.iloc[i-1] if i>0 else df.iloc[i]
    score=0; reasons=[]
    utc_h=(int(df.iloc[i]['Open_time'])//1000//3600)%24
    if SKIP_UTC_START<=utc_h<SKIP_UTC_END: return 0,["Dead session"]
    if direction=="LONG" and htf_trend=="BULL":
        score+=htf_strength; reasons.append(f"HTF BULL +{htf_strength}")
    elif direction=="SHORT" and htf_trend=="BEAR":
        score+=htf_strength; reasons.append(f"HTF BEAR +{htf_strength}")
    else: return 0,["HTF counter-trend"]
    adx=float(c.get('ADX',0) or 0)
    if adx<18: return 0,[f"ADX {adx:.1f} flat"]
    if adx>=30:   score+=3; reasons.append(f"ADX {adx:.1f} very strong")
    elif adx>=25: score+=2; reasons.append(f"ADX {adx:.1f} strong")
    else:         score+=1; reasons.append(f"ADX {adx:.1f} trending")
    st=int(c.get('ST_DIR',0) or 0)
    if direction=="LONG"  and st!=1:  return 0,["ST bearish"]
    if direction=="SHORT" and st!=-1: return 0,["ST bullish"]
    score+=1; reasons.append("Supertrend ✓")
    cl=float(c['close']); prev_cl=float(prev['close'])
    e8=float(c['EMA8']); e21=float(c['EMA21']); e55=float(c['EMA55']); e200=float(c['EMA200'])
    if direction=="LONG":
        if cl>e8>e21>e55>e200:   score+=4; reasons.append("EMA perfect bull")
        elif cl>e21>e55>e200:    score+=3; reasons.append("EMA full bull")
        elif cl>e21>e55:         score+=2; reasons.append("EMA 21>55")
        elif cl>e21:             score+=1; reasons.append("Above EMA21")
        elif cl<e55:             score-=1
        if prev_cl<e8 and cl>e8: score+=1; reasons.append("EMA8 reclaim ↑")
    else:
        if cl<e8<e21<e55<e200:   score+=4; reasons.append("EMA perfect bear")
        elif cl<e21<e55<e200:    score+=3; reasons.append("EMA full bear")
        elif cl<e21<e55:         score+=2; reasons.append("EMA 21<55")
        elif cl<e21:             score+=1; reasons.append("Below EMA21")
        elif cl>e55:             score-=1
        if prev_cl>e8 and cl<e8: score+=1; reasons.append("EMA8 reclaim ↓")
    tenkan=float(c.get('TENKAN',0) or 0); kijun=float(c.get('KIJUN',0) or 0)
    p_tenkan=float(prev.get('TENKAN',0) or 0); p_kijun=float(prev.get('KIJUN',0) or 0)
    if tenkan>0 and kijun>0:
        if direction=="LONG" and tenkan>kijun and cl>kijun:
            if p_tenkan<=p_kijun: score+=2; reasons.append("Ichimoku TK cross ↑")
            else: score+=1; reasons.append("Ichimoku bull")
        elif direction=="SHORT" and tenkan<kijun and cl<kijun:
            if p_tenkan>=p_kijun: score+=2; reasons.append("Ichimoku TK cross ↓")
            else: score+=1; reasons.append("Ichimoku bear")
    pdi=float(c.get('PLUS_DI',0) or 0); mdi=float(c.get('MINUS_DI',0) or 0)
    if direction=="LONG"  and pdi>mdi: score+=1; reasons.append(f"+DI{pdi:.0f}>{mdi:.0f}")
    elif direction=="SHORT" and mdi>pdi: score+=1; reasons.append(f"-DI{mdi:.0f}>{pdi:.0f}")
    ms=get_market_structure(df.iloc[:i+1],lookback=25)
    if direction=="LONG"  and ms=="BULL": score+=1; reasons.append("Structure HH+HL")
    elif direction=="SHORT" and ms=="BEAR": score+=1; reasons.append("Structure LH+LL")
    vwap=float(c.get('VWAP',0) or 0)
    if vwap>0:
        if direction=="LONG"  and cl>vwap: score+=1; reasons.append("Above VWAP")
        elif direction=="SHORT" and cl<vwap: score+=1; reasons.append("Below VWAP")
        vg=abs(cl-vwap)/vwap
        if vg>0.005 and ((direction=="LONG" and cl>vwap) or (direction=="SHORT" and cl<vwap)):
            score+=1; reasons.append(f"VWAP gap {vg*100:.1f}%")
    rsi=float(c['RSI'])
    if direction=="LONG":
        if rsi>80: return 0,[f"RSI {rsi:.0f} OB"]
        if rsi>72: score-=1
        if 40<=rsi<=65:   score+=2; reasons.append(f"RSI {rsi:.0f} ideal")
        elif 30<=rsi<40:  score+=2; reasons.append(f"RSI {rsi:.0f} OS bounce")
        elif rsi<30:      score+=1; reasons.append(f"RSI {rsi:.0f} deep OS")
        if bool(c.get('RSI_DIV_BULL',False)): score+=2; reasons.append("RSI bull div ⚡")
    else:
        if rsi<20: return 0,[f"RSI {rsi:.0f} OS"]
        if rsi<28: score-=1
        if 35<=rsi<=60:   score+=2; reasons.append(f"RSI {rsi:.0f} ideal")
        elif 60<rsi<=70:  score+=2; reasons.append(f"RSI {rsi:.0f} OB reject")
        elif rsi>70:      score+=1; reasons.append(f"RSI {rsi:.0f} deep OB")
        if bool(c.get('RSI_DIV_BEAR',False)): score+=2; reasons.append("RSI bear div ⚡")
    macd=float(c.get('MACD',0) or 0); macds=float(c.get('MACDS',0) or 0)
    hist=float(c.get('MACD_HIST',0) or 0); p_hist=float(prev.get('MACD_HIST',0) or 0)
    pmacd=float(prev.get('MACD',0) or 0); pmacds=float(prev.get('MACDS',0) or 0)
    if direction=="LONG":
        if macd>macds and pmacd<=pmacds: score+=2; reasons.append("MACD cross ↑")
        elif macd>macds: score+=1; reasons.append("MACD bull")
        if hist>0 and hist>p_hist: score+=1; reasons.append("MACD hist ↑")
    else:
        if macd<macds and pmacd>=pmacds: score+=2; reasons.append("MACD cross ↓")
        elif macd<macds: score+=1; reasons.append("MACD bear")
        if hist<0 and hist<p_hist: score+=1; reasons.append("MACD hist ↓")
    bbma=float(c.get('BB_MA',cl) or cl); bbup=float(c.get('BB_UP',cl) or cl)
    bblo=float(c.get('BB_LO',cl) or cl); bbpct=float(c.get('BB_PCT',0.5) or 0.5)
    bbw=float(c.get('BB_WIDTH',0) or 0)
    if bbma>0 and bbw<0.012: return 0,["BB squeeze"]
    if direction=="LONG":
        if cl<=bblo*1.005:   score+=2; reasons.append("BB lower bounce")
        elif bbpct<0.3:       score+=1; reasons.append("BB lower half")
        prev_bbw=float(prev.get('BB_WIDTH',bbw) or bbw)
        if bbw>prev_bbw*1.1: score+=1; reasons.append("BB expanding")
    else:
        if cl>=bbup*0.995:   score+=2; reasons.append("BB upper reject")
        elif bbpct>0.7:       score+=1; reasons.append("BB upper half")
        prev_bbw=float(prev.get('BB_WIDTH',bbw) or bbw)
        if bbw>prev_bbw*1.1: score+=1; reasons.append("BB expanding")
    if i>=5:
        obv_now=float(c.get('OBV',0) or 0); obv_prev=float(df.iloc[i-5].get('OBV',obv_now) or obv_now)
        if direction=="LONG"  and obv_now>obv_prev: score+=1; reasons.append("OBV rising")
        elif direction=="SHORT" and obv_now<obv_prev: score+=1; reasons.append("OBV falling")
    vm=float(c.get('VOL_MA20',0) or 0); vr=c['volume']/vm if vm>0 else 1.0
    if vr>2.0:   score+=3; reasons.append(f"Vol {vr:.1f}x HUGE")
    elif vr>1.5: score+=2; reasons.append(f"Vol {vr:.1f}x spike")
    elif vr>1.2: score+=1; reasons.append(f"Vol {vr:.1f}x above avg")
    for lvl in key_levels:
        dp=abs(cl-lvl)/(cl+1e-9)
        if dp<0.006:   score+=3; reasons.append(f"At S/R ${lvl:.4f}"); break
        elif dp<0.012: score+=2; reasons.append(f"Near S/R ${lvl:.4f}"); break
        elif dp<0.02:  score+=1; reasons.append(f"Close S/R ${lvl:.4f}"); break
    pats=detect_patterns(df,i)
    if direction=="LONG":
        if   "BULL_ENGULF"    in pats: score+=3; reasons.append("Bull Engulf 🕯")
        elif "MORNING_STAR"   in pats: score+=3; reasons.append("Morning Star 🌟")
        elif "THREE_SOLDIERS" in pats: score+=2; reasons.append("3 Soldiers")
        elif "BULL_PIN"       in pats: score+=2; reasons.append("Bull Pin Bar")
        if "INSIDE_BAR" in pats and cl>float(prev['high']): score+=1; reasons.append("IB break ↑")
    else:
        if   "BEAR_ENGULF"  in pats: score+=3; reasons.append("Bear Engulf 🕯")
        elif "EVENING_STAR" in pats: score+=3; reasons.append("Evening Star 🌟")
        elif "THREE_CROWS"  in pats: score+=2; reasons.append("3 Crows")
        elif "BEAR_PIN"     in pats: score+=2; reasons.append("Bear Pin Bar")
        if "INSIDE_BAR" in pats and cl<float(prev['low']): score+=1; reasons.append("IB break ↓")
    roc5=float(c.get('ROC5',0) or 0)
    if direction=="LONG"  and roc5>1.5: score+=1; reasons.append(f"ROC5 {roc5:.1f}%")
    elif direction=="SHORT" and roc5<-1.5: score+=1; reasons.append(f"ROC5 {roc5:.1f}%")
    stoch=float(c.get('STOCHRSI',0.5) or 0.5)
    if direction=="LONG"  and stoch>0.92: return 0,["StochRSI OB"]
    if direction=="SHORT" and stoch<0.08: return 0,["StochRSI OS"]
    return max(score,0),reasons

# ══════════════════════════════════════════════════════════
# SL / TP
# ══════════════════════════════════════════════════════════
def calculate_levels(df,i,direction,entry):
    c=df.iloc[i]; atr=float(c['ATR']) if not pd.isna(c['ATR']) else entry*0.015
    lb=max(0,i-20); rl=df['low'].iloc[lb:i]; rh=df['high'].iloc[lb:i]
    if direction=="LONG":
        atr_sl=entry-atr*1.8; sw_sl=float(rl.min())*0.998 if len(rl)>0 else atr_sl
        sl=min(atr_sl,sw_sl); sl=min(sl,entry*0.985); sl=max(sl,entry*0.970)
        risk=entry-sl; tp1=entry+risk*2.0; tp2=entry+risk*3.5
    else:
        atr_sl=entry+atr*1.8; sw_sl=float(rh.max())*1.002 if len(rh)>0 else atr_sl
        sl=max(atr_sl,sw_sl); sl=max(sl,entry*1.015); sl=min(sl,entry*1.030)
        risk=sl-entry; tp1=entry-risk*2.0; tp2=entry-risk*3.5
    sl_pct=abs(entry-sl)/entry*100; tp1_pct=abs(tp1-entry)/entry*100
    rr=tp1_pct/sl_pct if sl_pct>0 else 0
    return sl,tp1,tp2,sl_pct,tp1_pct,rr

# ══════════════════════════════════════════════════════════
# OUTCOME CHECKER (historical)
# ══════════════════════════════════════════════════════════
def check_outcome(df_full,signal_idx,direction,entry,tp1,tp2,sl):
    future=df_full.iloc[signal_idx+1:signal_idx+73]
    tp1_hit=False
    for i,(_,row) in enumerate(future.iterrows()):
        h=float(row['high']); l=float(row['low']); ms=int(row['Open_time'])
        if direction=="LONG":
            if l<=sl:
                if tp1_hit: return "BREAKEVEN",(tp1-entry)/entry*100*0.5+(sl-entry)/entry*100*0.5,i+1,ms
                return "SL_HIT",(sl-entry)/entry*100,i+1,ms
            if not tp1_hit and h>=tp1: tp1_hit=True
            if tp1_hit and h>=tp2:
                return "TP2_HIT",((tp1-entry)/entry*100)*0.5+((tp2-entry)/entry*100)*0.5,i+1,ms
        else:
            if h>=sl:
                if tp1_hit: return "BREAKEVEN",(entry-tp1)/entry*100*0.5+(entry-sl)/entry*100*0.5,i+1,ms
                return "SL_HIT",(entry-sl)/entry*100,i+1,ms
            if not tp1_hit and l<=tp1: tp1_hit=True
            if tp1_hit and l<=tp2:
                return "TP2_HIT",((entry-tp1)/entry*100)*0.5+((entry-tp2)/entry*100)*0.5,i+1,ms
    if len(future)==0: return "NO_DATA",0.0,0,None
    last=float(future.iloc[-1]['close']); lms=int(future.iloc[-1]['Open_time'])
    pct=(last-entry)/entry*100 if direction=="LONG" else (entry-last)/entry*100
    return "STILL_OPEN",pct,len(future),lms

def calc_pnl_usd(outcome,result_pct,alloc_usd,entry,tp1,tp2,sl,direction):
    if outcome=="TP2_HIT":
        tp1_pct=abs(tp1-entry)/entry*100; tp2_pct=abs(tp2-entry)/entry*100
        return round(alloc_usd*((tp1_pct/100)*0.5+(tp2_pct/100)*0.5),4)
    elif outcome=="SL_HIT":
        return round(-alloc_usd*(abs(sl-entry)/entry*100)/100,4)
    elif outcome=="BREAKEVEN":
        return round(alloc_usd*(abs(tp1-entry)/entry*100)/100*0.5,4)
    else:
        return round(alloc_usd*(result_pct/100),4)

# ══════════════════════════════════════════════════════════
# CHART BUILDER
# ══════════════════════════════════════════════════════════
def build_chart(coin,df_raw,signal_idx,entry,tp1,tp2,sl,
                direction,score,outcome,pnl_pct,reasons,
                alloc_usd,bal_before,bal_after):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        end_idx=signal_idx+1; start_idx=max(0,end_idx-60)
        chart_df=df_raw.iloc[start_idx:end_idx].copy()
        if len(chart_df)<5: return None

        fig,ax=plt.subplots(figsize=(14,7),facecolor='#0D1117')
        ax.set_facecolor('#0D1117')

        for spine in ax.spines.values(): spine.set_color('#30363D')
        ax.tick_params(colors='#8B949E'); ax.yaxis.label.set_color('#E6EDF3')
        ax.grid(color='#21262D',linestyle='--',linewidth=0.4,alpha=0.7)

        xs=range(len(chart_df))
        for idx,((_,row),x) in enumerate(zip(chart_df.iterrows(),xs)):
            o,h,l,c2=row['open'],row['high'],row['low'],row['close']
            col='#26a69a' if c2>=o else '#ef5350'
            ax.plot([x,x],[l,h],color=col,linewidth=0.8)
            ax.add_patch(mpatches.FancyBboxPatch(
                (x-0.3,min(o,c2)),0.6,max(abs(c2-o),0.0001*(h-l+1e-9)),
                boxstyle="square,pad=0",facecolor=col,edgecolor=col))

        n=len(chart_df)
        for price,color,label,ls in [
            (entry,'#2196F3','E','--'),(tp1,'#4CAF50','T1','-'),
            (tp2,'#1B5E20','T2','-'),(sl,'#F44336','SL','-')]:
            ax.axhline(y=price,color=color,linestyle=ls,linewidth=1.5,alpha=0.9)
            ax.annotate(f' {label}',xy=(n-1,price),xycoords=('data','data'),
                        fontsize=9,color=color,fontweight='bold',va='center')

        if outcome=="TP2_HIT" or (outcome=="STILL_OPEN" and pnl_pct>0):
            rc='#4CAF50'; rl=f"✅ WIN +{pnl_pct:.2f}%"
        elif outcome=="BREAKEVEN":
            rc='#FF9800'; rl=f"🟡 BE {pnl_pct:+.2f}%"
        elif outcome=="STILL_OPEN":
            rc='#2196F3'; rl=f"🔵 OPEN {pnl_pct:+.2f}%"
        else:
            rc='#F44336'; rl=f"❌ LOSS {pnl_pct:.2f}%"

        dc='#4CAF50' if direction=="LONG" else '#F44336'
        ax.set_title(f"  {coin} 1H {direction}  Score:{score}  Alloc:${alloc_usd:.2f}",
                     fontsize=13,fontweight='bold',color=dc,pad=10,loc='left')
        ax.text(0.99,0.97,rl,transform=ax.transAxes,fontsize=12,fontweight='bold',
                color=rc,ha='right',va='top',
                bbox=dict(boxstyle='round,pad=0.4',facecolor='#0D1117',edgecolor=rc,linewidth=2))
        bal_color='#4CAF50' if bal_after>=bal_before else '#F44336'
        ax.text(0.99,0.08,f"${bal_before:.2f} → ${bal_after:.2f}  ({bal_after-bal_before:+.2f})",
                transform=ax.transAxes,fontsize=9,fontweight='bold',color=bal_color,ha='right',va='bottom',
                bbox=dict(boxstyle='round,pad=0.4',facecolor='#0D1117',edgecolor=bal_color,linewidth=1.5))
        ax.text(0.01,0.04,f"E:${entry:.4f}  TP1:${tp1:.4f}  TP2:${tp2:.4f}  SL:${sl:.4f}",
                transform=ax.transAxes,fontsize=8,family='monospace',color='#E6EDF3',va='bottom',
                bbox=dict(boxstyle='round,pad=0.4',facecolor='#161B22',edgecolor='#30363D'))
        ax.text(0.01,0.97," | ".join(reasons[:5]),transform=ax.transAxes,fontsize=8,
                color='#8B949E',va='top',
                bbox=dict(boxstyle='round,pad=0.3',facecolor='#161B22',edgecolor='#30363D',alpha=0.85))

        fname=os.path.join(tempfile.gettempdir(),f"{coin}_{direction}_{signal_idx}.png")
        fig.savefig(fname,dpi=120,facecolor='#0D1117',bbox_inches='tight')
        plt.close(fig)
        return fname if os.path.exists(fname) and os.path.getsize(fname)>1000 else None
    except Exception as e:
        print(f"Chart error: {e}"); return None

# ══════════════════════════════════════════════════════════
# BALANCE MANAGER
# ══════════════════════════════════════════════════════════
class BalanceManager:
    def __init__(self, starting):
        self.balance=starting; self.starting=starting
        self.open_trades=[]; self.wins=self.losses=self.bes=0

    def available(self):
        locked=sum(t['allocated_usd'] for t in self.open_trades)
        return max(0.0,self.balance-locked)

    def get_allocation(self,score):
        pct=SCORE_ALLOC_PCT.get(min(max(score,6),10),5.0)
        avail=self.available(); max_usd=self.balance*MAX_ALLOC_PCT
        alloc=min(avail*(pct/100.0),max_usd)
        return round(alloc,4),pct

    def open_trade(self,trade): self.open_trades.append(trade)

    def close_trade(self,trade_id,pnl_usd,outcome):
        for i,t in enumerate(self.open_trades):
            if t['trade_id']==trade_id: self.open_trades.pop(i); break
        old=self.balance; self.balance=round(self.balance+pnl_usd,4)
        if outcome=="TP2_HIT" or (outcome=="STILL_OPEN" and pnl_usd>0): self.wins+=1
        elif outcome=="BREAKEVEN": self.bes+=1
        else: self.losses+=1
        return old,self.balance

    def can_open(self):
        return len(self.open_trades)<MAX_OPEN_TRADES and self.available()>=MIN_TRADE_USD

# ══════════════════════════════════════════════════════════
# MAIN BACKTEST RUNNER
# ══════════════════════════════════════════════════════════
def run_backtest_thread(start_date, end_date):
    with state_lock:
        state["running"]=True; state["signals"]=[]
        state["wins"]=0; state["losses"]=0; state["bes"]=0
        state["balance"]=STARTING_BALANCE; state["starting"]=STARTING_BALANCE
        state["last_error"]=None
        state["run_start_date"]=start_date; state["run_end_date"]=end_date

    try:
        date_range=get_date_range(start_date,end_date)
        active=[c for c in WATCH_LIST if c not in BLOCKED_COINS]
        bm=BalanceManager(STARTING_BALANCE)
        all_results=[]

        send_telegram_alert(
            f"🔬 *BACKTEST STARTED* 🇱🇰\n\n"
            f"📅 Range: `{start_date}` → `{end_date}` ({len(date_range)} days)\n"
            f"💵 Balance: `${STARTING_BALANCE:.2f}` → dynamic\n"
            f"📐 Score: `{MIN_SCORE}+` | R:R `{MIN_RR}+`\n"
            f"🔄 Max trades: `{MAX_OPEN_TRADES}`\n\n"
            f"Score 6→5% | 7→8% | 8→12% | 9→18% | 10→22%\n"
            f"━━━━━━━━━━━━━━━━━━━━━"
        )

        for d_idx,date_str in enumerate(date_range):
            with state_lock:
                state["progress"]=f"Day {d_idx+1}/{len(date_range)} — {date_str}"
                state["progress_pct"]=int((d_idx/len(date_range))*100)

            start_ms=date_to_ms(date_str,end_of_day=False)
            end_ms=date_to_ms(date_str,end_of_day=True)
            day_results=[]

            send_telegram_alert(
                f"📅 *Day {d_idx+1}/{len(date_range)} — {date_str}*\n"
                f"💵 Balance: `${bm.balance:.2f}` | Available: `${bm.available():.2f}`"
            )

            for coin in active:
                if not bm.can_open(): break
                try:
                    htf_trend,htf_str=get_htf_trend(coin,end_ms=end_ms)
                    if htf_trend=="NEUTRAL": continue

                    df_full=download_data(coin,interval="1h",limit=DATA_LIMIT,
                                         end_ms=end_ms+72*3600_000)
                    if df_full is None or len(df_full)<100: continue

                    df_ind=compute_indicators(df_full)
                    key_lvls=find_key_levels(df_ind)

                    mask=(df_full['Open_time']>=start_ms)&(df_full['Open_time']<=end_ms)
                    targets=df_full.index[mask].tolist()
                    if not targets: continue

                    last_sig={"LONG":-99,"SHORT":-99}
                    coin_count=0
                    directions=["LONG"] if htf_trend=="BULL" else ["SHORT"]

                    for i in targets:
                        if i<100: continue
                        if coin_count>=MAX_SIGNALS_PER_COIN: break
                        if not bm.can_open(): break
                        c=df_ind.iloc[i]
                        if any(pd.isna(c.get(k,float('nan'))) for k in ['ATR','EMA200','STOCHRSI','ADX']): continue

                        for direction in directions:
                            if i-last_sig[direction]<COOLDOWN_BARS: continue
                            score,reasons=score_signal(df_ind,i,direction,htf_trend,htf_str,key_lvls)
                            if score<MIN_SCORE: continue
                            entry=float(c['close'])
                            sl,tp1,tp2,sl_pct,tp1_pct,rr=calculate_levels(df_ind,i,direction,entry)
                            if rr<MIN_RR: continue

                            alloc_usd,alloc_pct=bm.get_allocation(score)
                            if alloc_usd<MIN_TRADE_USD: continue

                            outcome,result_pct,candles,result_ms=check_outcome(
                                df_full,i,direction,entry,tp1,tp2,sl)
                            pnl_usd=calc_pnl_usd(outcome,result_pct,alloc_usd,entry,tp1,tp2,sl,direction)

                            trade_id=f"{coin}_{direction}_{i}"
                            bm.open_trade({"trade_id":trade_id,"allocated_usd":alloc_usd})
                            bal_before,new_bal=bm.close_trade(trade_id,pnl_usd,outcome)

                            last_sig[direction]=i; coin_count+=1

                            sig_lk=ms_to_lk(int(df_full.iloc[i]['Open_time']))
                            result_lk=ms_to_lk(result_ms)
                            is_win=outcome=="TP2_HIT" or (outcome=="STILL_OPEN" and pnl_usd>0)
                            is_be=outcome=="BREAKEVEN"
                            em="✅" if is_win else "🟡" if is_be else "🔵" if outcome=="STILL_OPEN" else "❌"

                            rl=(f"✅ WIN  +{result_pct:.2f}% (+${pnl_usd:.2f})"  if is_win else
                                f"🟡 BE   {result_pct:+.2f}% (+${pnl_usd:.2f})"  if is_be  else
                                f"🔵 OPEN {result_pct:+.2f}% (${pnl_usd:+.2f})"  if outcome=="STILL_OPEN" else
                                f"❌ LOSS {result_pct:.2f}% (-${abs(pnl_usd):.2f})")

                            alloc_pct_of_bal=(alloc_usd/bal_before*100) if bal_before>0 else 0

                            msg=(
                                f"{'📈' if direction=='LONG' else '📉'} *{direction} {coin}* {em}\n"
                                f"📅 {sig_lk}\n"
                                f"Score `{score}` | HTF `{htf_trend}` | RR `1:{rr:.1f}`\n\n"
                                f"💵 Entry `${entry:.4f}` | SL `${sl:.4f}`\n"
                                f"🎯 TP1 `${tp1:.4f}` | TP2 `${tp2:.4f}`\n\n"
                                f"━━ 💰 ALLOCATION ━━\n"
                                f"Score {score} → {alloc_pct:.0f}% of available\n"
                                f"Allocated: `${alloc_usd:.2f}` ({alloc_pct_of_bal:.1f}% of balance)\n\n"
                                f"{rl}\n"
                                f"⏰ {result_lk} ({candles}h)\n\n"
                                f"━━ 📊 BALANCE ━━\n"
                                f"Before: `${bal_before:.2f}`\n"
                                f"P&L: `${pnl_usd:+.2f}`\n"
                                f"*After: `${new_bal:.2f}`* 💵\n\n"
                                f"_{' | '.join(reasons[:5])}_"
                            )

                            chart_path=build_chart(coin,df_ind,i,entry,tp1,tp2,sl,
                                direction,score,outcome,result_pct,reasons,
                                alloc_usd,bal_before,new_bal)

                            if chart_path and os.path.exists(chart_path):
                                sent=send_telegram_photo(chart_path,msg)
                                try: os.remove(chart_path)
                                except: pass
                                if not sent: send_telegram_alert(msg)
                            else:
                                send_telegram_alert(msg)

                            rec={
                                "date":date_str,"coin":coin,"direction":direction,
                                "htf":htf_trend,"sig_time":sig_lk,"result_time":result_lk,
                                "score":score,"alloc_usd":alloc_usd,"alloc_pct":alloc_pct,
                                "bal_before":bal_before,"bal_after":new_bal,
                                "entry":entry,"outcome":outcome,"result_pct":result_pct,
                                "pnl_usd":pnl_usd,"candles":candles,"rr":rr,
                                "reasons":reasons,"is_win":is_win,"is_be":is_be,
                            }
                            day_results.append(rec); all_results.append(rec)
                            with state_lock:
                                state["signals"].insert(0,rec)
                                state["balance"]=new_bal
                                if is_win: state["wins"]+=1
                                elif is_be: state["bes"]+=1
                                else: state["losses"]+=1

                            time.sleep(0.8)

                except Exception as e:
                    print(f"Coin error {coin}: {e}"); continue
                time.sleep(0.4)

            # Daily summary
            if day_results:
                wins=[r for r in day_results if r['is_win']]
                bes=[r for r in day_results if r['is_be']]
                losses=[r for r in day_results if not r['is_win'] and not r['is_be']]
                day_pnl=sum(r['pnl_usd'] for r in day_results)
                grade="🟢" if day_pnl>0 else "🔴" if day_pnl<0 else "🟡"
                lines=[f"{'✅' if r['is_win'] else '🟡' if r['is_be'] else '❌'} `{r['coin']}` "
                       f"{r['direction']} | ${r['alloc_usd']:.2f} → {r['pnl_usd']:+.2f} | `${r['bal_after']:.2f}`"
                       for r in day_results]
                send_telegram_alert(
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{grade} *DAILY — {date_str}*\n"
                    f"Trades:`{len(day_results)}` ✅`{len(wins)}` 🟡`{len(bes)}` ❌`{len(losses)}`\n"
                    f"Day P/L: *${day_pnl:+.2f}* | Balance: *`${bm.balance:.2f}`*\n\n"
                    +"\n".join(lines)
                )
            else:
                send_telegram_alert(f"📅 *{date_str}* — No signals\nBalance: `${bm.balance:.2f}`")

            time.sleep(1.0)

        # Final report
        _send_final(all_results,bm,date_range)

    except Exception as e:
        with state_lock: state["last_error"]=str(e)
        print(f"Backtest error: {e}")
        send_telegram_alert(f"❌ *Backtest error:* {e}")
    finally:
        with state_lock:
            state["running"]=False; state["progress"]="Done"
            state["progress_pct"]=100

def _send_final(all_results,bm,date_range):
    if not all_results:
        send_telegram_alert("⚠️ *No signals found.*"); return
    wins=[r for r in all_results if r['is_win']]
    bes=[r for r in all_results if r['is_be']]
    losses=[r for r in all_results if not r['is_win'] and not r['is_be']]
    total=len(all_results)
    wr=round(len(wins)/total*100,1) if total>0 else 0
    t_pnl=sum(r['pnl_usd'] for r in all_results)
    roi=(bm.balance-bm.starting)/bm.starting*100
    avg_win=sum(r['pnl_usd'] for r in wins)/len(wins) if wins else 0
    avg_loss=sum(r['pnl_usd'] for r in losses)/len(losses) if losses else 0
    avg_rr=sum(r['rr'] for r in all_results)/total if total>0 else 0
    expectancy=(wr/100*avg_win)+((1-wr/100)*avg_loss)
    best=max(all_results,key=lambda x:x['pnl_usd'])
    worst=min(all_results,key=lambda x:x['pnl_usd'])
    grade="🏆 LIVE READY" if wr>=55 and t_pnl>0 else "⚠️ NEEDS TUNING" if wr>=45 else "❌ NOT READY"
    send_telegram_alert(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *FINAL REPORT*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💵 Start: `${bm.starting:.2f}` → *`${bm.balance:.2f}`*\n"
        f"📈 P/L: *${t_pnl:+.2f}* | ROI: *{roi:+.2f}%*\n\n"
        f"Total: `{total}` | ✅`{len(wins)}` 🟡`{len(bes)}` ❌`{len(losses)}`\n"
        f"🏆 Win Rate: *{wr:.1f}%*\n"
        f"📐 Avg R:R: `1:{avg_rr:.1f}`\n"
        f"🎯 Expectancy: `${expectancy:.2f}` per trade\n\n"
        f"💚 Avg Win: `+${avg_win:.2f}`\n"
        f"💔 Avg Loss: `-${abs(avg_loss):.2f}`\n\n"
        f"🥇 Best: `{best['coin']}` *+${best['pnl_usd']:.2f}*\n"
        f"💀 Worst: `{worst['coin']}` *${worst['pnl_usd']:+.2f}*\n\n"
        f"*{grade}*"
    )

# ══════════════════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════════════════
@app.route('/')
def index(): return render_template('dashboard.html')

@app.route('/api/state')
def api_state():
    with state_lock:
        total=state["wins"]+state["losses"]+state["bes"]
        wr=round(state["wins"]/total*100,1) if total>0 else 0
        roi=round((state["balance"]-state["starting"])/state["starting"]*100,2)
        return jsonify({
            "balance":      round(state["balance"],4),
            "starting":     state["starting"],
            "pnl":          round(state["balance"]-state["starting"],4),
            "roi":          roi,
            "wins":         state["wins"],
            "losses":       state["losses"],
            "bes":          state["bes"],
            "win_rate":     wr,
            "total_signals":len(state["signals"]),
            "running":      state["running"],
            "progress":     state["progress"],
            "progress_pct": state["progress_pct"],
            "last_error":   state["last_error"],
            "start_date":   state["run_start_date"],
            "end_date":     state["run_end_date"],
        })

@app.route('/api/signals')
def api_signals(): 
    with state_lock: return jsonify(state["signals"][:200])

@app.route('/api/run', methods=['POST'])
def api_run():
    with state_lock:
        if state["running"]: return jsonify({"error":"Already running"}),400
    data=request.json
    start=data.get("start_date","")
    end=data.get("end_date","")
    if not start or not end: return jsonify({"error":"Need start_date and end_date"}),400
    t=threading.Thread(target=run_backtest_thread,args=(start,end),daemon=True)
    t.start()
    return jsonify({"status":"started","start":start,"end":end})

if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port,debug=False)
