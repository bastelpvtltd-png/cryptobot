import os, time, json, threading, calendar
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify
import pandas as pd
import requests

app = Flask(__name__)

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
STARTING_BALANCE     = 100.0
MIN_SCORE            = 6
MIN_RR               = 1.8
MAX_OPEN_TRADES      = 5
MAX_SIGNALS_PER_COIN = 2
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

# ── State ──────────────────────────────────────────────────
state = {
    "balance":       STARTING_BALANCE,
    "starting":      STARTING_BALANCE,
    "open_trades":   [],
    "closed_trades": [],
    "signals":       [],
    "last_scan":     None,
    "scanning":      False,
    "wins": 0, "losses": 0, "bes": 0,
    "last_error":    None,
}
state_lock = threading.Lock()

# ══════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════
def ms_to_lk(ms):
    if ms is None: return "—"
    return datetime.utcfromtimestamp(ms/1000 + LK_OFFSET_SEC).strftime('%d %b %Y %I:%M %p')

def now_lk():
    return datetime.utcnow() + timedelta(seconds=LK_OFFSET_SEC)

def available_balance():
    locked = sum(t.get('allocated_usd', 0) for t in state["open_trades"])
    return max(0.0, state["balance"] - locked)

def get_allocation(score):
    pct   = SCORE_ALLOC_PCT.get(min(max(score, 6), 10), 5.0)
    avail = available_balance()
    mx    = state["balance"] * MAX_ALLOC_PCT
    return round(min(avail * (pct / 100.0), mx), 4), pct

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
    except Exception as e:
        return None

# ══════════════════════════════════════════════════════════
# HTF TREND
# ══════════════════════════════════════════════════════════
def get_htf_trend(symbol, end_ms=None):
    scores = {"BULL": 0, "BEAR": 0}
    for tf, lim, w in [("4h", 100, 2), ("1d", 50, 1)]:
        df = download_data(symbol, interval=tf, limit=lim, end_ms=end_ms)
        if df is None or len(df) < 50: continue
        c   = df['close']
        e20 = c.ewm(span=20, adjust=False).mean()
        e50 = c.ewm(span=50, adjust=False).mean()
        lc  = c.iloc[-1]
        if lc > e20.iloc[-1] > e50.iloc[-1]:   scores["BULL"] += w
        elif lc < e20.iloc[-1] < e50.iloc[-1]: scores["BEAR"] += w
        time.sleep(0.08)
    if scores["BULL"] >= 2: return "BULL", min(scores["BULL"], 3)
    if scores["BEAR"] >= 2: return "BEAR", min(scores["BEAR"], 3)
    return "NEUTRAL", 0

# ══════════════════════════════════════════════════════════
# MARKET STRUCTURE
# ══════════════════════════════════════════════════════════
def get_market_structure(df, lookback=30):
    highs = df['high'].iloc[-lookback:].values
    lows  = df['low'].iloc[-lookback:].values
    sh, sl = [], []
    for i in range(2, len(highs) - 2):
        if highs[i] == max(highs[i-2], highs[i-1], highs[i], highs[i+1], highs[i+2]): sh.append(highs[i])
        if lows[i]  == min(lows[i-2],  lows[i-1],  lows[i],  lows[i+1],  lows[i+2]):  sl.append(lows[i])
    if len(sh) < 2 or len(sl) < 2: return "NEUTRAL"
    if sh[-1] > sh[-2] and sl[-1] > sl[-2]: return "BULL"
    if sh[-1] < sh[-2] and sl[-1] < sl[-2]: return "BEAR"
    return "NEUTRAL"

# ══════════════════════════════════════════════════════════
# INDICATORS (exact same as backtest)
# ══════════════════════════════════════════════════════════
def compute_indicators(df):
    d = df.copy()
    d['EMA8']   = d['close'].ewm(span=8,   adjust=False).mean()
    d['EMA21']  = d['close'].ewm(span=21,  adjust=False).mean()
    d['EMA55']  = d['close'].ewm(span=55,  adjust=False).mean()
    d['EMA200'] = d['close'].ewm(span=200, adjust=False).mean()

    delta = d['close'].diff()
    gain  = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss  = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    d['RSI'] = 100 - (100 / (1 + gain / (loss + 1e-9)))

    d['RSI_DIV_BULL'] = False
    d['RSI_DIV_BEAR'] = False
    for i in range(5, len(d)):
        p_lows  = d['low'].iloc[i-5:i+1];  r_lows  = d['RSI'].iloc[i-5:i+1]
        p_highs = d['high'].iloc[i-5:i+1]; r_highs = d['RSI'].iloc[i-5:i+1]
        if p_lows.iloc[-1] < p_lows.min() * 1.001 and r_lows.iloc[-1] > r_lows.iloc[:-1].min() * 1.01:
            d.at[d.index[i], 'RSI_DIV_BULL'] = True
        if p_highs.iloc[-1] > p_highs.max() * 0.999 and r_highs.iloc[-1] < r_highs.iloc[:-1].max() * 0.99:
            d.at[d.index[i], 'RSI_DIV_BEAR'] = True

    e12 = d['close'].ewm(span=12, adjust=False).mean()
    e26 = d['close'].ewm(span=26, adjust=False).mean()
    d['MACD']      = e12 - e26
    d['MACDS']     = d['MACD'].ewm(span=9, adjust=False).mean()
    d['MACD_HIST'] = d['MACD'] - d['MACDS']

    d['BB_MA']    = d['close'].rolling(20).mean()
    d['BB_STD']   = d['close'].rolling(20).std()
    d['BB_UP']    = d['BB_MA'] + 2 * d['BB_STD']
    d['BB_LO']    = d['BB_MA'] - 2 * d['BB_STD']
    d['BB_PCT']   = (d['close'] - d['BB_LO']) / (d['BB_UP'] - d['BB_LO'] + 1e-9)
    d['BB_WIDTH'] = (d['BB_UP'] - d['BB_LO']) / (d['BB_MA'] + 1e-9)

    d['VOL_MA20'] = d['volume'].rolling(20).mean()
    d['OBV']      = (d['volume'] * d['close'].diff().apply(
                      lambda x: 1 if x > 0 else -1 if x < 0 else 0)).cumsum()

    hl  = d['high'] - d['low']
    hpc = (d['high'] - d['close'].shift(1)).abs()
    lpc = (d['low']  - d['close'].shift(1)).abs()
    d['ATR'] = pd.concat([hl, hpc, lpc], axis=1).max(axis=1).rolling(14).mean()

    rsi_min = d['RSI'].rolling(14).min()
    rsi_max = d['RSI'].rolling(14).max()
    d['STOCHRSI'] = (d['RSI'] - rsi_min) / ((rsi_max - rsi_min) + 1e-9)

    pdm = d['high'].diff().clip(lower=0)
    mdm = (-d['low'].diff()).clip(lower=0)
    pdm = pdm.where(pdm > mdm, 0)
    mdm = mdm.where(mdm > pdm, 0)
    d['PLUS_DI']  = 100 * (pdm.rolling(14).mean() / (d['ATR'] + 1e-9))
    d['MINUS_DI'] = 100 * (mdm.rolling(14).mean() / (d['ATR'] + 1e-9))
    dx = 100 * (d['PLUS_DI'] - d['MINUS_DI']).abs() / (d['PLUS_DI'] + d['MINUS_DI'] + 1e-9)
    d['ADX'] = dx.rolling(14).mean()

    hl2 = (d['high'] + d['low']) / 2
    ub = hl2 + 3.0 * d['ATR']
    lb = hl2 - 3.0 * d['ATR']
    st_dir = [1] * len(d); st_val = [0.0] * len(d)
    for i in range(1, len(d)):
        fub = ub.iloc[i] if ub.iloc[i] < ub.iloc[i-1] or d['close'].iloc[i-1] > ub.iloc[i-1] else ub.iloc[i-1]
        flb = lb.iloc[i] if lb.iloc[i] > lb.iloc[i-1] or d['close'].iloc[i-1] < lb.iloc[i-1] else lb.iloc[i-1]
        if st_val[i-1] == ub.iloc[i-1]:
            st_dir[i] = -1 if d['close'].iloc[i] <= fub else 1
            st_val[i] = fub if d['close'].iloc[i] <= fub else flb
        else:
            st_dir[i] = 1 if d['close'].iloc[i] >= flb else -1
            st_val[i] = flb if d['close'].iloc[i] >= flb else fub
    d['ST_DIR'] = st_dir
    d['ST_VAL'] = st_val

    typ = (d['high'] + d['low'] + d['close']) / 3
    d['VWAP']   = (typ * d['volume']).rolling(24).sum() / d['volume'].rolling(24).sum()
    d['ROC5']   = d['close'].pct_change(5) * 100
    d['TENKAN'] = (d['high'].rolling(9).max()  + d['low'].rolling(9).min())  / 2
    d['KIJUN']  = (d['high'].rolling(26).max() + d['low'].rolling(26).min()) / 2
    return d

# ══════════════════════════════════════════════════════════
# CANDLE PATTERNS (exact same as backtest)
# ══════════════════════════════════════════════════════════
def detect_patterns(df, i):
    c  = df.iloc[i]; p = df.iloc[i-1]; p2 = df.iloc[i-2] if i >= 2 else p
    pats = []; tr = c['high'] - c['low']
    if tr < 1e-9: return pats
    body = abs(c['close'] - c['open'])
    uw   = c['high'] - max(c['open'], c['close'])
    lw   = min(c['open'], c['close']) - c['low']

    if (p['close'] < p['open'] and c['close'] > c['open'] and
            c['open'] < min(p['open'], p['close']) and c['close'] > max(p['open'], p['close'])):
        pats.append("BULL_ENGULF")
    if (p['close'] > p['open'] and c['close'] < c['open'] and
            c['open'] > max(p['open'], p['close']) and c['close'] < min(p['open'], p['close'])):
        pats.append("BEAR_ENGULF")
    if lw > tr * 0.55 and body < tr * 0.35 and uw < tr * 0.2: pats.append("BULL_PIN")
    if uw > tr * 0.55 and body < tr * 0.35 and lw < tr * 0.2: pats.append("BEAR_PIN")
    if (p2['close'] < p2['open'] and abs(p['close'] - p['open']) < (p['high'] - p['low']) * 0.3 and
            c['close'] > c['open'] and c['close'] > (p2['open'] + p2['close']) / 2):
        pats.append("MORNING_STAR")
    if (p2['close'] > p2['open'] and abs(p['close'] - p['open']) < (p['high'] - p['low']) * 0.3 and
            c['close'] < c['open'] and c['close'] < (p2['open'] + p2['close']) / 2):
        pats.append("EVENING_STAR")
    if i >= 3:
        if (all(df.iloc[k]['close'] > df.iloc[k]['open'] for k in range(i-2, i+1)) and
                df.iloc[i-1]['open'] > df.iloc[i-2]['close'] * 0.995):
            pats.append("THREE_SOLDIERS")
        if (all(df.iloc[k]['close'] < df.iloc[k]['open'] for k in range(i-2, i+1)) and
                df.iloc[i-1]['open'] < df.iloc[i-2]['close'] * 1.005):
            pats.append("THREE_CROWS")
    if c['high'] <= p['high'] and c['low'] >= p['low']: pats.append("INSIDE_BAR")
    if body < tr * 0.1: pats.append("DOJI")
    return pats

def find_key_levels(df, lookback=100):
    levels = []
    src = df.tail(lookback).reset_index(drop=True)
    for i in range(2, len(src) - 2):
        h = src['high'].iloc[i]; l = src['low'].iloc[i]
        if h == src['high'].iloc[i-2:i+3].max(): levels.append(h)
        if l == src['low'].iloc[i-2:i+3].min():  levels.append(l)
    levels.sort(); zones = []; i = 0
    while i < len(levels):
        cluster = [levels[i]]; j = i + 1
        while j < len(levels) and (levels[j] - levels[i]) / (levels[i] + 1e-9) < 0.005:
            cluster.append(levels[j]); j += 1
        if len(cluster) >= 2: zones.append(sum(cluster) / len(cluster))
        i = j if j > i else i + 1
    return zones

# ══════════════════════════════════════════════════════════
# SCORING ENGINE (exact same as backtest)
# ══════════════════════════════════════════════════════════
def score_signal(df, i, direction, htf_trend, htf_strength, key_levels):
    c = df.iloc[i]; prev = df.iloc[i-1] if i > 0 else df.iloc[i]
    score = 0; reasons = []

    utc_h = (int(df.iloc[i]['Open_time']) // 1000 // 3600) % 24
    if SKIP_UTC_START <= utc_h < SKIP_UTC_END:
        return 0, ["Dead session"]

    if direction == "LONG" and htf_trend == "BULL":
        score += htf_strength; reasons.append(f"HTF BULL +{htf_strength}")
    elif direction == "SHORT" and htf_trend == "BEAR":
        score += htf_strength; reasons.append(f"HTF BEAR +{htf_strength}")
    else:
        return 0, ["HTF counter-trend"]

    adx = float(c.get('ADX', 0) or 0)
    if adx < 18: return 0, [f"ADX {adx:.1f} flat"]
    if adx >= 30:   score += 3; reasons.append(f"ADX {adx:.1f} very strong")
    elif adx >= 25: score += 2; reasons.append(f"ADX {adx:.1f} strong")
    else:           score += 1; reasons.append(f"ADX {adx:.1f} trending")

    st = int(c.get('ST_DIR', 0) or 0)
    if direction == "LONG"  and st != 1:  return 0, ["ST bearish"]
    if direction == "SHORT" and st != -1: return 0, ["ST bullish"]
    score += 1; reasons.append("Supertrend ✓")

    cl = float(c['close']); prev_cl = float(prev['close'])
    e8 = float(c['EMA8']); e21 = float(c['EMA21'])
    e55 = float(c['EMA55']); e200 = float(c['EMA200'])

    if direction == "LONG":
        if cl > e8 > e21 > e55 > e200:   score += 4; reasons.append("EMA perfect bull")
        elif cl > e21 > e55 > e200:        score += 3; reasons.append("EMA full bull")
        elif cl > e21 > e55:               score += 2; reasons.append("EMA 21>55")
        elif cl > e21:                     score += 1; reasons.append("Above EMA21")
        elif cl < e55:                     score -= 1
        if prev_cl < e8 and cl > e8:       score += 1; reasons.append("EMA8 reclaim ↑")
    else:
        if cl < e8 < e21 < e55 < e200:   score += 4; reasons.append("EMA perfect bear")
        elif cl < e21 < e55 < e200:        score += 3; reasons.append("EMA full bear")
        elif cl < e21 < e55:               score += 2; reasons.append("EMA 21<55")
        elif cl < e21:                     score += 1; reasons.append("Below EMA21")
        elif cl > e55:                     score -= 1
        if prev_cl > e8 and cl < e8:       score += 1; reasons.append("EMA8 reclaim ↓")

    tenkan = float(c.get('TENKAN', 0) or 0); kijun = float(c.get('KIJUN', 0) or 0)
    p_tenkan = float(prev.get('TENKAN', 0) or 0); p_kijun = float(prev.get('KIJUN', 0) or 0)
    if tenkan > 0 and kijun > 0:
        if direction == "LONG" and tenkan > kijun and cl > kijun:
            if p_tenkan <= p_kijun: score += 2; reasons.append("Ichimoku TK cross ↑")
            else: score += 1; reasons.append("Ichimoku bull")
        elif direction == "SHORT" and tenkan < kijun and cl < kijun:
            if p_tenkan >= p_kijun: score += 2; reasons.append("Ichimoku TK cross ↓")
            else: score += 1; reasons.append("Ichimoku bear")

    pdi = float(c.get('PLUS_DI', 0) or 0); mdi = float(c.get('MINUS_DI', 0) or 0)
    if direction == "LONG"  and pdi > mdi: score += 1; reasons.append(f"+DI{pdi:.0f}>{mdi:.0f}")
    elif direction == "SHORT" and mdi > pdi: score += 1; reasons.append(f"-DI{mdi:.0f}>{pdi:.0f}")

    ms = get_market_structure(df.iloc[:i+1], lookback=25)
    if direction == "LONG"  and ms == "BULL": score += 1; reasons.append("Structure HH+HL")
    elif direction == "SHORT" and ms == "BEAR": score += 1; reasons.append("Structure LH+LL")

    vwap = float(c.get('VWAP', 0) or 0)
    if vwap > 0:
        if direction == "LONG"  and cl > vwap: score += 1; reasons.append("Above VWAP")
        elif direction == "SHORT" and cl < vwap: score += 1; reasons.append("Below VWAP")
        vg = abs(cl - vwap) / vwap
        if vg > 0.005 and ((direction == "LONG" and cl > vwap) or (direction == "SHORT" and cl < vwap)):
            score += 1; reasons.append(f"VWAP gap {vg*100:.1f}%")

    rsi = float(c['RSI'])
    if direction == "LONG":
        if rsi > 80: return 0, [f"RSI {rsi:.0f} OB"]
        if rsi > 72: score -= 1
        if 40 <= rsi <= 65:  score += 2; reasons.append(f"RSI {rsi:.0f} ideal")
        elif 30 <= rsi < 40: score += 2; reasons.append(f"RSI {rsi:.0f} OS bounce")
        elif rsi < 30:       score += 1; reasons.append(f"RSI {rsi:.0f} deep OS")
        if bool(c.get('RSI_DIV_BULL', False)): score += 2; reasons.append("RSI bull div ⚡")
    else:
        if rsi < 20: return 0, [f"RSI {rsi:.0f} OS"]
        if rsi < 28: score -= 1
        if 35 <= rsi <= 60:  score += 2; reasons.append(f"RSI {rsi:.0f} ideal")
        elif 60 < rsi <= 70: score += 2; reasons.append(f"RSI {rsi:.0f} OB reject")
        elif rsi > 70:       score += 1; reasons.append(f"RSI {rsi:.0f} deep OB")
        if bool(c.get('RSI_DIV_BEAR', False)): score += 2; reasons.append("RSI bear div ⚡")

    macd = float(c.get('MACD', 0) or 0); macds = float(c.get('MACDS', 0) or 0)
    hist = float(c.get('MACD_HIST', 0) or 0); p_hist = float(prev.get('MACD_HIST', 0) or 0)
    pmacd = float(prev.get('MACD', 0) or 0); pmacds = float(prev.get('MACDS', 0) or 0)
    if direction == "LONG":
        if macd > macds and pmacd <= pmacds: score += 2; reasons.append("MACD cross ↑")
        elif macd > macds: score += 1; reasons.append("MACD bull")
        if hist > 0 and hist > p_hist: score += 1; reasons.append("MACD hist ↑")
    else:
        if macd < macds and pmacd >= pmacds: score += 2; reasons.append("MACD cross ↓")
        elif macd < macds: score += 1; reasons.append("MACD bear")
        if hist < 0 and hist < p_hist: score += 1; reasons.append("MACD hist ↓")

    bbma = float(c.get('BB_MA', cl) or cl); bbup = float(c.get('BB_UP', cl) or cl)
    bblo = float(c.get('BB_LO', cl) or cl); bbpct = float(c.get('BB_PCT', 0.5) or 0.5)
    bbw  = float(c.get('BB_WIDTH', 0) or 0)
    if bbma > 0 and bbw < 0.012: return 0, ["BB squeeze"]
    if direction == "LONG":
        if cl <= bblo * 1.005:  score += 2; reasons.append("BB lower bounce")
        elif bbpct < 0.3:        score += 1; reasons.append("BB lower half")
        prev_bbw = float(prev.get('BB_WIDTH', bbw) or bbw)
        if bbw > prev_bbw * 1.1: score += 1; reasons.append("BB expanding")
    else:
        if cl >= bbup * 0.995:  score += 2; reasons.append("BB upper reject")
        elif bbpct > 0.7:        score += 1; reasons.append("BB upper half")
        prev_bbw = float(prev.get('BB_WIDTH', bbw) or bbw)
        if bbw > prev_bbw * 1.1: score += 1; reasons.append("BB expanding")

    if i >= 5:
        obv_now  = float(c.get('OBV', 0) or 0)
        obv_prev = float(df.iloc[i-5].get('OBV', obv_now) or obv_now)
        if direction == "LONG"  and obv_now > obv_prev: score += 1; reasons.append("OBV rising")
        elif direction == "SHORT" and obv_now < obv_prev: score += 1; reasons.append("OBV falling")

    vm = float(c.get('VOL_MA20', 0) or 0); vr = c['volume'] / vm if vm > 0 else 1.0
    if vr > 2.0:   score += 3; reasons.append(f"Vol {vr:.1f}x HUGE")
    elif vr > 1.5: score += 2; reasons.append(f"Vol {vr:.1f}x spike")
    elif vr > 1.2: score += 1; reasons.append(f"Vol {vr:.1f}x above avg")

    for lvl in key_levels:
        dp = abs(cl - lvl) / (cl + 1e-9)
        if dp < 0.006:   score += 3; reasons.append(f"At S/R ${lvl:.4f}"); break
        elif dp < 0.012: score += 2; reasons.append(f"Near S/R ${lvl:.4f}"); break
        elif dp < 0.02:  score += 1; reasons.append(f"Close S/R ${lvl:.4f}"); break

    pats = detect_patterns(df, i)
    if direction == "LONG":
        if   "BULL_ENGULF"    in pats: score += 3; reasons.append("Bull Engulf 🕯")
        elif "MORNING_STAR"   in pats: score += 3; reasons.append("Morning Star 🌟")
        elif "THREE_SOLDIERS" in pats: score += 2; reasons.append("3 Soldiers")
        elif "BULL_PIN"       in pats: score += 2; reasons.append("Bull Pin Bar")
        if "INSIDE_BAR" in pats and cl > float(prev['high']): score += 1; reasons.append("IB break ↑")
    else:
        if   "BEAR_ENGULF"  in pats: score += 3; reasons.append("Bear Engulf 🕯")
        elif "EVENING_STAR" in pats: score += 3; reasons.append("Evening Star 🌟")
        elif "THREE_CROWS"  in pats: score += 2; reasons.append("3 Crows")
        elif "BEAR_PIN"     in pats: score += 2; reasons.append("Bear Pin Bar")
        if "INSIDE_BAR" in pats and cl < float(prev['low']): score += 1; reasons.append("IB break ↓")

    roc5 = float(c.get('ROC5', 0) or 0)
    if direction == "LONG"  and roc5 > 1.5: score += 1; reasons.append(f"ROC5 {roc5:.1f}%")
    elif direction == "SHORT" and roc5 < -1.5: score += 1; reasons.append(f"ROC5 {roc5:.1f}%")

    stoch = float(c.get('STOCHRSI', 0.5) or 0.5)
    if direction == "LONG"  and stoch > 0.92: return 0, ["StochRSI OB"]
    if direction == "SHORT" and stoch < 0.08: return 0, ["StochRSI OS"]

    return max(score, 0), reasons

# ══════════════════════════════════════════════════════════
# SL / TP
# ══════════════════════════════════════════════════════════
def calculate_levels(df, i, direction, entry):
    c   = df.iloc[i]
    atr = float(c['ATR']) if not pd.isna(c['ATR']) else entry * 0.015
    lb  = max(0, i - 20); rl = df['low'].iloc[lb:i]; rh = df['high'].iloc[lb:i]
    if direction == "LONG":
        atr_sl = entry - atr * 1.8
        sw_sl  = float(rl.min()) * 0.998 if len(rl) > 0 else atr_sl
        sl = min(atr_sl, sw_sl); sl = min(sl, entry * 0.985); sl = max(sl, entry * 0.970)
        risk = entry - sl; tp1 = entry + risk * 2.0; tp2 = entry + risk * 3.5
    else:
        atr_sl = entry + atr * 1.8
        sw_sl  = float(rh.max()) * 1.002 if len(rh) > 0 else atr_sl
        sl = max(atr_sl, sw_sl); sl = max(sl, entry * 1.015); sl = min(sl, entry * 1.030)
        risk = sl - entry; tp1 = entry - risk * 2.0; tp2 = entry - risk * 3.5
    sl_pct  = abs(entry - sl) / entry * 100
    tp1_pct = abs(tp1 - entry) / entry * 100
    rr      = tp1_pct / sl_pct if sl_pct > 0 else 0
    return sl, tp1, tp2, sl_pct, tp1_pct, rr

# ══════════════════════════════════════════════════════════
# LIVE OUTCOME CHECK
# ══════════════════════════════════════════════════════════
def check_live_outcome(symbol, direction, entry, tp1, tp2, sl):
    try:
        df = download_data(symbol, interval="1h", limit=75)
        if df is None: return None
        tp1_hit = False
        for _, row in df.iterrows():
            h = float(row['high']); l = float(row['low']); ms = int(row['Open_time'])
            if direction == "LONG":
                if l <= sl:
                    return ("BREAKEVEN" if tp1_hit else "SL_HIT"), ms
                if not tp1_hit and h >= tp1: tp1_hit = True
                if tp1_hit and h >= tp2:     return "TP2_HIT", ms
            else:
                if h >= sl:
                    return ("BREAKEVEN" if tp1_hit else "SL_HIT"), ms
                if not tp1_hit and l <= tp1: tp1_hit = True
                if tp1_hit and l <= tp2:     return "TP2_HIT", ms
        return None
    except:
        return None

def calc_pnl_usd(outcome, alloc_usd, entry, tp1, tp2, sl, direction):
    tp1_pct = abs(tp1 - entry) / entry * 100
    tp2_pct = abs(tp2 - entry) / entry * 100
    sl_pct  = abs(sl  - entry) / entry * 100
    if outcome == "TP2_HIT":
        return round(alloc_usd * ((tp1_pct/100)*0.5 + (tp2_pct/100)*0.5), 4)
    elif outcome == "SL_HIT":
        return round(-alloc_usd * (sl_pct / 100), 4)
    elif outcome == "BREAKEVEN":
        return round(alloc_usd * (tp1_pct / 100) * 0.5, 4)
    return 0.0

# ══════════════════════════════════════════════════════════
# SCANNER
# ══════════════════════════════════════════════════════════
def scan_all_coins():
    with state_lock:
        if state["scanning"]: return
        state["scanning"] = True

    try:
        active = [c for c in WATCH_LIST if c not in BLOCKED_COINS]
        last_sig = {c: {"LONG": -99, "SHORT": -99} for c in active}

        for coin in active:
            try:
                with state_lock:
                    if len(state["open_trades"]) >= MAX_OPEN_TRADES: break

                htf_trend, htf_str = get_htf_trend(coin)
                if htf_trend == "NEUTRAL": continue

                df_raw = download_data(coin, interval="1h", limit=DATA_LIMIT)
                if df_raw is None or len(df_raw) < 150: continue

                df       = compute_indicators(df_raw)
                key_lvls = find_key_levels(df)

                i = len(df) - 2   # last completed candle
                if i < 100: continue
                c = df.iloc[i]
                if any(pd.isna(c.get(k, float('nan'))) for k in ['ATR','EMA200','STOCHRSI','ADX']):
                    continue

                directions = ["LONG"] if htf_trend == "BULL" else ["SHORT"]

                for direction in directions:
                    with state_lock:
                        already = any(
                            t['coin'] == coin and t['direction'] == direction
                            for t in state["open_trades"])
                    if already: continue
                    if i - last_sig[coin][direction] < COOLDOWN_BARS: continue

                    with state_lock:
                        if len(state["open_trades"]) >= MAX_OPEN_TRADES: break
                        avail = available_balance()

                    score, reasons = score_signal(df, i, direction, htf_trend, htf_str, key_lvls)
                    if score < MIN_SCORE: continue

                    entry = float(c['close'])
                    sl, tp1, tp2, sl_pct, tp1_pct, rr = calculate_levels(df, i, direction, entry)
                    if rr < MIN_RR: continue

                    with state_lock:
                        alloc_usd, alloc_pct = get_allocation(score)
                        if alloc_usd < MIN_TRADE_USD: continue

                        trade_id = f"{coin}_{direction}_{i}_{int(time.time())}"
                        sig_time = ms_to_lk(int(df.iloc[i]['Open_time']))

                        trade = {
                            "trade_id":      trade_id,
                            "coin":          coin,
                            "direction":     direction,
                            "htf":           htf_trend,
                            "score":         score,
                            "entry":         entry,
                            "sl":            sl,
                            "tp1":           tp1,
                            "tp2":           tp2,
                            "sl_pct":        round(sl_pct, 2),
                            "tp1_pct":       round(tp1_pct, 2),
                            "rr":            round(rr, 2),
                            "allocated_usd": alloc_usd,
                            "alloc_pct":     alloc_pct,
                            "sig_time":      sig_time,
                            "open_time":     now_lk().strftime('%d %b %Y %I:%M %p'),
                            "reasons":       reasons[:6],
                            "status":        "OPEN",
                            "pnl_usd":       0.0,
                            "outcome":       "OPEN",
                        }
                        state["open_trades"].append(trade)
                        state["signals"].insert(0, trade.copy())

                    last_sig[coin][direction] = i
                    print(f"  📡 {direction} {coin} score={score} alloc=${alloc_usd:.2f}")
                    time.sleep(0.3)

            except Exception as e:
                print(f"  Coin error {coin}: {e}")
                continue

        # ── Check open trades for outcomes ──
        with state_lock:
            open_copy = [t.copy() for t in state["open_trades"]]

        for trade in open_copy:
            result = check_live_outcome(
                trade['coin'], trade['direction'],
                trade['entry'], trade['tp1'], trade['tp2'], trade['sl'])
            if result is None: continue

            outcome, result_ms = result
            pnl = calc_pnl_usd(outcome, trade['allocated_usd'],
                                trade['entry'], trade['tp1'], trade['tp2'],
                                trade['sl'], trade['direction'])

            with state_lock:
                state["balance"] = round(state["balance"] + pnl, 4)
                if outcome == "TP2_HIT":    state["wins"]   += 1
                elif outcome == "BREAKEVEN": state["bes"]    += 1
                else:                        state["losses"] += 1

                closed = trade.copy()
                closed.update({
                    "outcome":    outcome,
                    "pnl_usd":    pnl,
                    "status":     outcome,
                    "close_time": ms_to_lk(result_ms),
                })
                state["closed_trades"].insert(0, closed)
                state["open_trades"] = [
                    t for t in state["open_trades"]
                    if t["trade_id"] != trade["trade_id"]]

                for s in state["signals"]:
                    if s["trade_id"] == trade["trade_id"]:
                        s.update({"outcome": outcome, "pnl_usd": pnl,
                                  "status": outcome, "close_time": ms_to_lk(result_ms)})
                        break

            print(f"  🔔 CLOSED {trade['coin']} {outcome} ${pnl:+.2f}")

    except Exception as e:
        with state_lock:
            state["last_error"] = str(e)
        print(f"Scanner error: {e}")
    finally:
        with state_lock:
            state["scanning"]  = False
            state["last_scan"] = now_lk().strftime('%d %b %Y %I:%M %p')

def background_loop():
    while True:
        try:
            scan_all_coins()
        except Exception as e:
            print(f"BG loop error: {e}")
        time.sleep(300)

# ══════════════════════════════════════════════════════════
# API ROUTES
# ══════════════════════════════════════════════════════════
@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/api/state')
def api_state():
    with state_lock:
        total = state["wins"] + state["losses"] + state["bes"]
        wr    = round(state["wins"] / total * 100, 1) if total > 0 else 0
        roi   = round((state["balance"] - state["starting"]) / state["starting"] * 100, 2)
        return jsonify({
            "balance":       round(state["balance"], 4),
            "starting":      state["starting"],
            "available":     round(available_balance(), 4),
            "pnl":           round(state["balance"] - state["starting"], 4),
            "roi":           roi,
            "wins":          state["wins"],
            "losses":        state["losses"],
            "bes":           state["bes"],
            "win_rate":      wr,
            "open_count":    len(state["open_trades"]),
            "total_signals": len(state["signals"]),
            "last_scan":     state["last_scan"],
            "scanning":      state["scanning"],
            "last_error":    state["last_error"],
        })

@app.route('/api/open')
def api_open():
    with state_lock:
        return jsonify(state["open_trades"][:20])

@app.route('/api/history')
def api_history():
    with state_lock:
        return jsonify(state["signals"][:100])

@app.route('/api/closed')
def api_closed():
    with state_lock:
        return jsonify(state["closed_trades"][:50])

@app.route('/api/scan_now')
def api_scan_now():
    t = threading.Thread(target=scan_all_coins, daemon=True)
    t.start()
    return jsonify({"status": "scanning started"})

# ══════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════
def start_background():
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    print("✅ Background scanner started (every 5 min)")

if __name__ == '__main__':
    start_background()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
