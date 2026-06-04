# 🚀 CryptoBot Live Dashboard

Real-time crypto signal bot with live web dashboard.  
Binance data → Auto signals → Win/Loss tracking → Web URL

---

## 📁 Files
```
cryptobot/
├── app.py              ← Main bot + Flask API
├── templates/
│   └── dashboard.html  ← Live dashboard UI
├── requirements.txt
├── Procfile
└── render.yaml
```

---

## 🌐 Deploy to Render (FREE — No Laptop Needed)

### Step 1 — GitHub
1. Go to **github.com** → New repository → name it `cryptobot`
2. Upload all these files (drag & drop)
3. Click **Commit changes**

### Step 2 — Render
1. Go to **render.com** → Sign up free
2. Click **New +** → **Web Service**
3. Connect your GitHub → select `cryptobot` repo
4. Settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT --timeout 120`
   - **Instance Type:** Free
5. Click **Create Web Service**
6. Wait ~2 minutes → you get a URL like `https://cryptobot-xxxx.onrender.com`

### Step 3 — Use It
- Open your URL in browser
- Click **⚡ SCAN NOW** to trigger first scan
- Auto-scans every **5 minutes** automatically
- Dashboard auto-refreshes every **30 seconds**

---

## 📊 What You See

| Section | What it shows |
|---------|--------------|
| **Stats row** | Balance, ROI, Win Rate, Open trades |
| **Open Trades** | Current positions with levels |
| **History** | All signals ever generated |
| **Closed** | Completed trades with P&L |

---

## ⚙️ Config (in app.py)

```python
STARTING_BALANCE   = 100.0   # Simulated USD
MIN_SCORE          = 6       # Min score to take signal
MIN_RR             = 1.8     # Min risk:reward
MAX_OPEN_TRADES    = 5       # Max concurrent
```

---

## ⚠️ Notes
- This is **simulated trading** — no real money moves
- Uses **Binance public API** (no API key needed)
- Render free tier **sleeps after 15min inactivity** — first load may be slow
- To keep it awake, use **UptimeRobot** (free) to ping your URL every 10 min
