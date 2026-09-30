"""
Nifty50 F&O Screener — Backend (Upstox API)
=============================================
Ye backend Upstox API se live data khinch ke:
 - Sector ranking (Nifty Metal, Bank, IT, FMCG, etc.)
 - Option chain (OI, Volume, Greeks, IV)
 - VWAP + Opening Range (9:15-9:20) breakout
 - Entry/Exit signal checklist
calculate karta hai aur REST API se serve karta hai.

SETUP (README.md dekhein full steps ke liye):
 1. Upstox Developer Console pe app banao: https://developer.upstox.com
 2. CLIENT_ID, CLIENT_SECRET, REDIRECT_URI .env mein daalo
 3. pip install -r requirements.txt
 4. uvicorn main:app --reload
"""

import os
import math
import hashlib
from datetime import datetime, time as dtime, date
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Nifty50 F&O Screener API")

# Frontend se cross-origin requests allow karne ke liye
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # production mein apne frontend domain tak limit karo
    allow_methods=["*"],
    allow_headers=["*"],
)

UPSTOX_BASE = "https://api.upstox.com/v2"
CLIENT_ID = os.environ.get("UPSTOX_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("UPSTOX_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("UPSTOX_REDIRECT_URI", "")

# Access token ko simple in-memory store mein rakh rahe hain (production mein DB use karo)
_token_store: dict = {"access_token": None}

# Expiry list cache — symbol -> {"expiries": [...], "fetched_at": datetime}
# Isse baar baar Upstox API hit nahi karni padti, 6 ghante tak cache valid rehta hai
_expiry_cache: dict = {}

# Nifty50 F&O sector mapping (illustrative — poori list NSE se lekar expand karo)
SECTOR_STOCKS = {
    "Nifty Metal": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL"],
    "Nifty Bank": ["HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "SBIN"],
    "Nifty IT": ["TCS", "INFY", "HCLTECH", "WIPRO", "TECHM"],
    "Nifty FMCG": ["ITC", "HINDUNILVR", "NESTLEIND", "BRITANNIA", "TATACONSUM"],
    "Nifty Auto": ["MARUTI", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "EICHERMOT"],
    "Nifty Pharma": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP"],
    "Nifty Oil & Gas": ["RELIANCE", "ONGC", "BPCL", "IOC", "GAIL"],
}


# ---------------------------------------------------------------------------
# AUTH — Upstox OAuth2 flow
# ---------------------------------------------------------------------------

@app.get("/auth/login")
def login():
    url = (
        f"https://api.upstox.com/v2/login/authorization/dialog"
        f"?response_type=code&client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}"
    )
    return {"login_url": url}


@app.get("/auth/callback")
async def auth_callback(code: str):
    """Upstox login ke baad ye redirect URI hit hoga is 'code' ke saath."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{UPSTOX_BASE}/login/authorization/token",
            data={
                "code": code,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    data = resp.json()
    if "access_token" not in data:
        raise HTTPException(status_code=400, detail=data)
    _token_store["access_token"] = data["access_token"]
    return {"status": "logged_in"}


def _auth_headers():
    token = _token_store.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Pehle /auth/login se login karo")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ---------------------------------------------------------------------------
# SECTOR RANKING
# ---------------------------------------------------------------------------

@app.get("/sectors/ranking")
async def sector_ranking():
    """
    Har sector ke representative stocks ka average % change nikal ke rank karta hai.
    Real NSE sector-index endpoint available ho to usse directly use karo — ye
    stock-average se approximate karta hai jab tak sector-index feed na ho.
    """
    headers = _auth_headers()
    results = []

    async with httpx.AsyncClient() as client:
        for sector, symbols in SECTOR_STOCKS.items():
            changes = []
            for sym in symbols:
                instrument_key = f"NSE_EQ|{sym}"  # Upstox instrument key format
                r = await client.get(
                    f"{UPSTOX_BASE}/market-quote/quotes",
                    params={"instrument_key": instrument_key},
                    headers=headers,
                )
                d = r.json().get("data", {})
                quote_data = next(iter(d.values()), None)
                if quote_data:
                    pct = quote_data.get("net_change_percentage", 0)
                    changes.append(pct)
            avg_change = sum(changes) / len(changes) if changes else 0
            results.append({"sector": sector, "change_pct": round(avg_change, 2)})

    results.sort(key=lambda x: x["change_pct"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1
        r["tag"] = "Most Bullish" if i == 0 else ("Most Bearish" if i == len(results) - 1 else None)
    return {"sectors": results}


# ---------------------------------------------------------------------------
# EXPIRY — auto-select nearest expiry (expiry-to-expiry rollover)
# ---------------------------------------------------------------------------

def _instrument_key(symbol: str) -> str:
    """Index vs stock ke liye Upstox instrument key format alag hota hai."""
    if symbol.upper() in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        return f"NSE_INDEX|{symbol.upper()}"
    return f"NSE_EQ|{symbol.upper()}"


async def _fetch_expiries(symbol: str, client: httpx.AsyncClient, headers: dict) -> list:
    """Upstox 'option/contract' endpoint se ek symbol ki saari available expiries laata hai."""
    r = await client.get(
        f"{UPSTOX_BASE}/option/contract",
        params={"instrument_key": _instrument_key(symbol)},
        headers=headers,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    data = r.json().get("data", [])
    # Contracts list mein har entry ke paas "expiry" field hoti hai (YYYY-MM-DD)
    expiries = sorted({item["expiry"] for item in data if item.get("expiry")})
    return expiries


def _pick_nearest(expiries: list) -> Optional[str]:
    """
    Aaj ki date se sabse nazdeek, abhi expire NA hui ho aisi expiry chunta hai.
    Expiry day guzarne ke baad ye automatically agli expiry pick kar leta hai —
    yahi "expiry-to-expiry rollover" hai.
    """
    if not expiries:
        return None
    today = date.today()
    upcoming = [
        e for e in expiries
        if datetime.strptime(e, "%Y-%m-%d").date() >= today
    ]
    return min(upcoming) if upcoming else max(expiries)


async def _get_nearest_expiry(symbol: str) -> dict:
    """Cache check karke, zaroorat pe fresh expiry list fetch karke nearest expiry deta hai."""
    headers = _auth_headers()
    cache_key = symbol.upper()
    cached = _expiry_cache.get(cache_key)

    # 6 ghante tak cache valid — isse rollover din bhi zyada purana cache use nahi hota
    if cached and (datetime.now() - cached["fetched_at"]).total_seconds() < 6 * 3600:
        expiries = cached["expiries"]
    else:
        async with httpx.AsyncClient() as client:
            expiries = await _fetch_expiries(cache_key, client, headers)
        _expiry_cache[cache_key] = {"expiries": expiries, "fetched_at": datetime.now()}

    nearest = _pick_nearest(expiries)
    if not nearest:
        raise HTTPException(status_code=404, detail=f"{symbol} ke liye koi expiry nahi mili")

    return {"symbol": cache_key, "nearest_expiry": nearest, "all_expiries": expiries}


@app.get("/expiry/{symbol}")
async def nearest_expiry(symbol: str):
    """
    Symbol ke liye available expiries fetch karke sabse nearest (auto-select)
    expiry return karta hai. Weekly index (NIFTY/BANKNIFTY) ho ya monthly stock
    (RELIANCE/TATASTEEL) — dono ke liye same logic kaam karta hai, kyunki hum
    Upstox se hi actual available dates maangte hain, khud calculate nahi karte.
    """
    return await _get_nearest_expiry(symbol)


@app.get("/option-chain/{symbol}")
async def option_chain(
    symbol: str,
    expiry: Optional[str] = Query(None, description="YYYY-MM-DD — na diya to nearest expiry auto-select hogi"),
):
    """
    Upstox option-chain endpoint se OI, Volume, LTP, Greeks (IV se derive) leta hai.
    Agar 'expiry' param nahi diya, to automatically nearest (current active) expiry
    use hoti hai — expiry khatam hone ke baad ye khud agli expiry pe switch ho jaata hai.
    """
    headers = _auth_headers()
    instrument_key = _instrument_key(symbol)

    if not expiry:
        expiry_info = await _get_nearest_expiry(symbol)
        expiry = expiry_info["nearest_expiry"]

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{UPSTOX_BASE}/option/chain",
            params={"instrument_key": instrument_key, "expiry_date": expiry},
            headers=headers,
        )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    result = r.json()
    result["_expiry_used"] = expiry  # frontend ko pata rahe konsi expiry use hui
    return result


# ---------------------------------------------------------------------------
# VWAP + OPENING RANGE (9:15-9:20)
# ---------------------------------------------------------------------------

class ORBResult(BaseModel):
    symbol: str
    range_high: float
    range_low: float
    vwap: float
    breakout: Optional[str] = None  # "above" | "below" | None
    breakout_time: Optional[str] = None


@app.get("/orb/{symbol}", response_model=ORBResult)
async def opening_range(symbol: str):
    """
    Intraday 1-min candles leke:
      - 9:15-9:20 ka High/Low = Opening Range
      - Poore din ka VWAP
      - Range breakout status
    """
    headers = _auth_headers()
    instrument_key = _instrument_key(symbol)

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{UPSTOX_BASE}/historical-candle/intraday/{instrument_key}/1minute",
            headers=headers,
        )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    candles = r.json().get("data", {}).get("candles", [])
    # Upstox format: [timestamp, open, high, low, close, volume, oi]
    orb_candles = []
    cum_pv, cum_vol = 0.0, 0.0
    breakout, breakout_time = None, None
    range_high, range_low = None, None

    for c in reversed(candles):  # Upstox usually returns latest-first
        ts = datetime.fromisoformat(c[0])
        o, h, l, close, vol = c[1], c[2], c[3], c[4], c[5]
        typical = (h + l + close) / 3
        cum_pv += typical * vol
        cum_vol += vol

        if dtime(9, 15) <= ts.time() <= dtime(9, 20):
            orb_candles.append((h, l))
        elif range_high is not None and breakout is None:
            if close > range_high:
                breakout, breakout_time = "above", ts.strftime("%H:%M")
            elif close < range_low:
                breakout, breakout_time = "below", ts.strftime("%H:%M")

        if orb_candles and range_high is None:
            range_high = max(x[0] for x in orb_candles)
            range_low = min(x[1] for x in orb_candles)

    vwap = cum_pv / cum_vol if cum_vol else 0.0

    return ORBResult(
        symbol=symbol,
        range_high=range_high or 0,
        range_low=range_low or 0,
        vwap=round(vwap, 2),
        breakout=breakout,
        breakout_time=breakout_time,
    )


# ---------------------------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------------------------

@app.get("/")
def health():
    return {"status": "ok", "logged_in": bool(_token_store.get("access_token"))}


# ============================================================
# FYERS API — alag broker support
# ============================================================

FYERS_APP_ID = os.environ.get("FYERS_APP_ID", "")
FYERS_SECRET_KEY = os.environ.get("FYERS_SECRET_KEY", "")
FYERS_REDIRECT_URI = os.environ.get("FYERS_REDIRECT_URI", "")

_fyers_token_store: dict = {"access_token": None}


@app.get("/auth/fyers/login")
def fyers_login():
    """Is URL ko browser mein kholo — Fyers login page khulega."""
    encoded_redirect = quote(FYERS_REDIRECT_URI, safe="")
    url = (
        f"https://api.fyers.in/api/v3/generate-authcode"
        f"?client_id={FYERS_APP_ID}"
        f"&redirect_uri={encoded_redirect}"
        f"&response_type=code&state=sample_state"
    )
    return {"login_url": url}


@app.get("/auth/fyers/callback")
async def fyers_callback(auth_code: str = Query(None), code: str = Query(None)):
    """Fyers login ke baad ye redirect URI hit hoga."""
    final_code = auth_code or code
    app_id_hash = hashlib.sha256(
        f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()
    ).hexdigest()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.fyers.in/api/v3/validate-authcode",
            json={
                "grant_type": "authorization_code",
                "appIdHash": app_id_hash,
                "code": final_code,
            },
        )
    data = resp.json()
    if "access_token" not in data:
        raise HTTPException(status_code=400, detail=data)

    _fyers_token_store["access_token"] = data["access_token"]
    return {"status": "Fyers login successful"}


@app.get("/auth/fyers/status")
def fyers_status():
    return {"status": "ok", "logged_in": _fyers_token_store["access_token"] is not None}
