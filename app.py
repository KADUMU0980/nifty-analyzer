"""
Upstox Option Chain Local Proxy Server
=======================================
Run this on your machine → it fetches Upstox data (bypassing CORS)
and serves it to your dashboard at http://localhost:5000

SETUP (one time):
  pip install flask flask-cors requests

CONFIGURE:
  Set your Upstox daily access token:
    Windows:  set UPSTOX_ACCESS_TOKEN=your_token_here
    Mac/Linux: export UPSTOX_ACCESS_TOKEN=your_token_here
  OR paste it directly into the UPSTOX_ACCESS_TOKEN variable below.

  Get your token from: https://developer.upstox.com → login → access token

RUN:
  python app.py

Then open index.html in your browser.
"""

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
import requests
import os
import time
from datetime import datetime

app = Flask(__name__)
CORS(app)

@app.route('/')
@app.route('/index.html')
def serve_index():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base_dir, 'index.html')

# ── Upstox config ─────────────────────────────────────────────────────
# Set via environment variable (recommended) OR paste token directly here
UPSTOX_ACCESS_TOKEN = os.environ.get('UPSTOX_ACCESS_TOKEN', "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI1UkNHVTMiLCJqdGkiOiI2YTE1MTBhNTM3NDhjODUzMDFhZmMyMGMiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc3OTc2NTQxMywiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzc5ODMyODAwfQ.PmY6ZZUFI300sEv4PqTmdXBHhPEl6xav1tL5n6m5zhg")

UPSTOX_BASE        = 'https://api.upstox.com/v2'
NIFTY_KEY          = 'NSE_INDEX|Nifty 50'
VIX_KEY            = 'NSE_INDEX|India VIX'

def upstox_headers():
    return {
        'Authorization': f'Bearer {UPSTOX_ACCESS_TOKEN}',
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }

# ── Cache to avoid hammering Upstox ──────────────────────────────────
cache = {}
CACHE_TTL = 60  # seconds

def get_cached(key, fetch_fn):
    now = time.time()
    if key in cache and now - cache[key]['ts'] < CACHE_TTL:
        return cache[key]['data']
    data = fetch_fn()
    if data:
        cache[key] = {'data': data, 'ts': now}
    return data

# ── Helpers ───────────────────────────────────────────────────────────
def get_nearest_expiry():
    """Fetch nearest weekly/monthly expiry from Upstox"""
    try:
        url = f"{UPSTOX_BASE}/option/contract"
        r = requests.get(url, headers=upstox_headers(),
                         params={'instrument_key': NIFTY_KEY}, timeout=10)
        if r.status_code == 200:
            data = r.json().get('data', [])
            expiries = sorted(set(d['expiry'] for d in data if d.get('expiry')))
            today = datetime.now().strftime('%Y-%m-%d')
            future = sorted([e for e in expiries if e >= today])
            return future[0] if future else None
        print(f"Expiry list error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"Expiry fetch error: {e}")
    return None

def get_spot_price():
    """Fetch Nifty 50 last price from Upstox quotes"""
    try:
        url = f"{UPSTOX_BASE}/market-quote/quotes"
        r = requests.get(url, headers=upstox_headers(),
                         params={'instrument_key': NIFTY_KEY}, timeout=8)
        if r.status_code == 200:
            quotes = r.json().get('data', {})
            nifty  = quotes.get(NIFTY_KEY, {})
            return nifty.get('last_price', 24750)
    except Exception as e:
        print(f"Spot price fetch error: {e}")
    return 24750

def convert_to_nse_format(upstox_records, spot_price):
    """
    Convert Upstox option chain list → NSE-compatible records dict.
    Frontend processNSEData() expects:
      data.records.underlyingValue
      data.records.data  → [{strikePrice, CE:{openInterest, changeinOpenInterest,
                              totalTradedVolume, impliedVolatility, lastPrice, change}, PE:{...}}]
      data.records.timestamp
    """
    rows = []
    for item in upstox_records:
        strike = item.get('strike_price', 0)
        row    = {'strikePrice': strike}

        ce_raw = item.get('call_options', {})
        if ce_raw:
            md = ce_raw.get('market_data', {})
            row['CE'] = {
                'strikePrice':          strike,
                'openInterest':         md.get('oi', 0),
                'changeinOpenInterest': md.get('change_oi', 0),
                'totalTradedVolume':    md.get('volume', 0),
                'impliedVolatility':    round(md.get('iv', 0), 2),
                'lastPrice':            md.get('ltp', 0),
                'change':               md.get('net_change', 0),
            }

        pe_raw = item.get('put_options', {})
        if pe_raw:
            md = pe_raw.get('market_data', {})
            row['PE'] = {
                'strikePrice':          strike,
                'openInterest':         md.get('oi', 0),
                'changeinOpenInterest': md.get('change_oi', 0),
                'totalTradedVolume':    md.get('volume', 0),
                'impliedVolatility':    round(md.get('iv', 0), 2),
                'lastPrice':            md.get('ltp', 0),
                'change':               md.get('net_change', 0),
            }

        if 'CE' in row or 'PE' in row:
            rows.append(row)

    return {
        'records': {
            'underlyingValue': spot_price,
            'data':            rows,
            'timestamp':       datetime.now().strftime('%d-%b-%Y %H:%M:%S'),
        }
    }

# ── Option Chain endpoint ──────────────────────────────────────────────
@app.route('/api/option-chain')
def option_chain():
    def fetch():
        try:
            expiry = get_nearest_expiry()
            if not expiry:
                print("❌ Could not determine nearest expiry")
                return None

            print(f"📅 Fetching option chain for expiry: {expiry}")
            url = f"{UPSTOX_BASE}/option/chain"
            r   = requests.get(url, headers=upstox_headers(),
                                params={'instrument_key': NIFTY_KEY,
                                        'expiry_date':    expiry},
                                timeout=15)

            if r.status_code == 401:
                print("🔑 Upstox 401 — access token expired or invalid")
                return None
            if r.status_code == 200:
                result = r.json()
                if result.get('status') == 'success':
                    spot = get_spot_price()
                    return convert_to_nse_format(result.get('data', []), spot)
            print(f"Upstox error {r.status_code}: {r.text[:300]}")
        except Exception as e:
            print(f"Option chain fetch error: {e}")
        return None

    data = get_cached('option_chain', fetch)
    if data:
        return jsonify({"status": "ok", "source": "upstox_live", "data": data})
    # Check if token looks like it might be the expired hardcoded one
    is_env = os.environ.get('UPSTOX_ACCESS_TOKEN') is not None
    return jsonify({
        "status": "error",
        "message": "Upstox fetch failed — check token / market hours",
        "debug": {
            "token_source": "env_var" if is_env else "hardcoded_fallback (likely expired)",
            "hint": "Upstox tokens expire daily at midnight. Get a fresh token from developer.upstox.com and set UPSTOX_ACCESS_TOKEN env var."
        }
    }), 500

# ── VIX + Nifty quote endpoint ─────────────────────────────────────────
@app.route('/api/vix')
def vix():
    def fetch():
        try:
            url = f"{UPSTOX_BASE}/market-quote/quotes"
            r   = requests.get(url, headers=upstox_headers(),
                                params={'instrument_key': f"{NIFTY_KEY},{VIX_KEY}"},
                                timeout=10)
            if r.status_code == 200:
                quotes = r.json().get('data', {})

                nq = quotes.get(NIFTY_KEY, {})
                vq = quotes.get(VIX_KEY,   {})

                nifty_last  = nq.get('last_price', 0)
                nifty_chg   = nq.get('net_change',  0)
                prev_close  = nifty_last - nifty_chg
                pct_change  = (nifty_chg / prev_close * 100) if prev_close else 0

                return {
                    'nifty': {
                        'lastPrice': nifty_last,
                        'change':    round(nifty_chg,  2),
                        'pChange':   round(pct_change, 2),
                    },
                    'vix': {
                        'lastPrice': vq.get('last_price', 0),
                    }
                }
            print(f"VIX quote error {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"VIX fetch error: {e}")
        return None

    data = get_cached('vix', fetch)
    if data:
        return jsonify({"status": "ok", "data": data})
    return jsonify({"status": "error"}), 500

# ── Health check ───────────────────────────────────────────────────────
@app.route('/health')
def health():
    token_ok = UPSTOX_ACCESS_TOKEN != 'YOUR_UPSTOX_ACCESS_TOKEN_HERE'
    return jsonify({
        "status":  "running",
        "proxy":   "Upstox Option Chain Proxy v2.0",
        "token":   "configured" if token_ok else "MISSING — set UPSTOX_ACCESS_TOKEN"
    })

if __name__ == '__main__':
    print("\n🚀 Upstox Option Chain Proxy Server")
    print("=" * 45)
    if UPSTOX_ACCESS_TOKEN == 'YOUR_UPSTOX_ACCESS_TOKEN_HERE':
        print("⚠️  WARNING: Access token not set!")
        print("   Mac/Linux: export UPSTOX_ACCESS_TOKEN=your_token")
        print("   Windows:   set UPSTOX_ACCESS_TOKEN=your_token")
        print("   Get token: https://developer.upstox.com")
    else:
        print("✅ Upstox access token loaded")
    print("\n✅ Server starting at http://localhost:5000")
    print("📊 Open index.html in your browser")
    print("⏹  Press Ctrl+C to stop\n")
    app.run(host='0.0.0.0', port=5000, debug=False)
