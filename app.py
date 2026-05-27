"""
Upstox Option Chain Proxy Server
=======================================
Proxies Upstox API requests (bypassing CORS) for the Nifty OI dashboard.

The access token can be provided in two ways:
  1. Via the frontend — user pastes their token in the website popup,
     which is sent as an X-Upstox-Token header on each request.
  2. Via environment variable UPSTOX_ACCESS_TOKEN (fallback).

Get your token from: https://developer.upstox.com → login → access token
Tokens expire daily at midnight IST.
"""

from flask import Flask, jsonify, send_from_directory, request
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
UPSTOX_BASE = 'https://api.upstox.com/v2'
NIFTY_KEY   = 'NSE_INDEX|Nifty 50'
VIX_KEY     = 'NSE_INDEX|India VIX'

def get_token():
    """Get the Upstox access token from the request header or env var."""
    # Priority: request header > environment variable
    header_token = request.headers.get('X-Upstox-Token', '').strip()
    if header_token:
        return header_token
    return os.environ.get('UPSTOX_ACCESS_TOKEN', '').strip()

def upstox_headers(token=None):
    t = token or get_token()
    return {
        'Authorization': f'Bearer {t}',
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
def get_nearest_expiry(token):
    """Fetch nearest weekly/monthly expiry from Upstox"""
    try:
        url = f"{UPSTOX_BASE}/option/contract"
        r = requests.get(url, headers=upstox_headers(token),
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

def get_spot_price(token):
    """Fetch Nifty 50 last price from Upstox quotes"""
    try:
        url = f"{UPSTOX_BASE}/market-quote/quotes"
        r = requests.get(url, headers=upstox_headers(token),
                         params={'instrument_key': NIFTY_KEY}, timeout=8)
        if r.status_code == 200:
            quotes = r.json().get('data', {})
            nifty  = quotes.get(NIFTY_KEY, {})
            return nifty.get('last_price', 0)
    except Exception as e:
        print(f"Spot price fetch error: {e}")
    return 0

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
    token = get_token()
    if not token:
        return jsonify({
            "status": "error",
            "message": "No access token provided",
            "debug": {
                "hint": "Please enter your Upstox access token in the popup. Get one from developer.upstox.com"
            }
        }), 401

    def fetch():
        try:
            expiry = get_nearest_expiry(token)
            if not expiry:
                print("❌ Could not determine nearest expiry")
                return None

            print(f"📅 Fetching option chain for expiry: {expiry}")
            url = f"{UPSTOX_BASE}/option/chain"
            r   = requests.get(url, headers=upstox_headers(token),
                                params={'instrument_key': NIFTY_KEY,
                                        'expiry_date':    expiry},
                                timeout=15)

            if r.status_code == 401:
                print("🔑 Upstox 401 — access token expired or invalid")
                return None
            if r.status_code == 200:
                result = r.json()
                if result.get('status') == 'success':
                    spot = get_spot_price(token)
                    return convert_to_nse_format(result.get('data', []), spot)
            print(f"Upstox error {r.status_code}: {r.text[:300]}")
        except Exception as e:
            print(f"Option chain fetch error: {e}")
        return None

    data = get_cached('option_chain', fetch)
    if data:
        return jsonify({"status": "ok", "source": "upstox_live", "data": data})
    return jsonify({
        "status": "error",
        "message": "Upstox fetch failed — token may be expired or market is closed",
        "debug": {
            "hint": "Upstox tokens expire daily at midnight IST. Get a fresh token from developer.upstox.com"
        }
    }), 500

# ── VIX + Nifty quote endpoint ─────────────────────────────────────────
@app.route('/api/vix')
def vix():
    token = get_token()
    if not token:
        return jsonify({"status": "error", "message": "No token"}), 401

    def fetch():
        try:
            url = f"{UPSTOX_BASE}/market-quote/quotes"
            r   = requests.get(url, headers=upstox_headers(token),
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
    return jsonify({
        "status":  "running",
        "proxy":   "Upstox Option Chain Proxy v3.0",
        "note":    "Token is now provided by the user via the website popup"
    })

if __name__ == '__main__':
    print("\n🚀 Upstox Option Chain Proxy Server v3.0")
    print("=" * 45)
    print("✅ Token is now entered by the user on the website")
    print("   (or set UPSTOX_ACCESS_TOKEN env var as fallback)")
    print("\n✅ Server starting at http://localhost:5000")
    print("📊 Open index.html in your browser")
    print("⏹  Press Ctrl+C to stop\n")
    app.run(host='0.0.0.0', port=5000, debug=False)
