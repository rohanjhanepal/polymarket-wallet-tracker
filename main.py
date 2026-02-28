# polymarket_holdings_polling_quotes.py
# FastAPI + HTML/JS polling UI (NO browser WS)
# + Server-side Polymarket CLOB "market" websocket to stream best bid/ask for UP/DOWN token ids
#
# Shows per tracked user:
#   - UP shares + avg entry price (avgPrice)
#   - DOWN shares + avg entry price (avgPrice)
# And market-wide:
#   - UP bid/ask (top-of-book)
#   - DOWN bid/ask (top-of-book)

from __future__ import annotations

import json
import re
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from websocket import WebSocketApp

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
CLOB_WSS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

USER_AGENT = "pm-holdings+quotes/1.0"
HTTP_TIMEOUT = 15.0

app = FastAPI()


@app.exception_handler(Exception)
async def json_exception_handler(request, exc: Exception):
    """Ensure API always returns JSON, never HTML, to avoid JSON.parse errors in frontend."""
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "detail": "Internal server error"},
    )


# ----------------------------
# Helpers
# ----------------------------
def normalize_handle(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    m = re.search(r"polymarket\.com/@([A-Za-z0-9_\-\.]+)", s)
    if m:
        return m.group(1)
    if s.startswith("@"):
        s = s[1:]
    return s.strip()


def normalize_slug(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    s = re.sub(r"^https?://", "", s)
    if "/" in s:
        s = s.split("/")[-1]
    return s.strip()


def safe_float(x: Any) -> float:
    try:
        return float(x if x is not None else 0.0)
    except Exception:
        return 0.0


def _parse_maybe_json_list(x: Any) -> List[Any]:
    """
    Gamma sometimes returns arrays as JSON-encoded strings:
      outcomes='["Up","Down"]'
      clobTokenIds='["123","456"]'
    This function returns a Python list in all cases.
    """
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        s = x.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                v = json.loads(s)
                return v if isinstance(v, list) else []
            except Exception:
                return []
        return []
    return []


async def http_get_json(client: httpx.AsyncClient, url: str, params: Dict[str, Any] | None = None) -> Any:
    r = await client.get(url, params=params or {}, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ----------------------------
# Gamma API (market + user profile)
# ----------------------------
async def gamma_market_by_slug(client: httpx.AsyncClient, slug: str) -> Optional[dict]:
    """
    Normalize multiple Gamma response shapes into a single "market dict".
    """
    slug = normalize_slug(slug)
    if not slug:
        return None

    # 1) Dedicated endpoint
    try:
        obj = await http_get_json(client, f"{GAMMA_BASE}/markets/slug/{slug}", {})
        if isinstance(obj, dict):
            # sometimes nested
            if "markets" in obj and isinstance(obj["markets"], list) and obj["markets"]:
                return obj["markets"][0]
            # typical market dict
            if obj.get("conditionId") or obj.get("question") or obj.get("title"):
                return obj
    except Exception:
        pass

    # 2) Fallback endpoint
    data = await http_get_json(client, f"{GAMMA_BASE}/markets", {"slug": slug})

    if isinstance(data, list) and data:
        first = data[0]
        # sometimes list of events-like objects that contain markets
        if isinstance(first, dict) and "markets" in first and isinstance(first["markets"], list) and first["markets"]:
            return first["markets"][0]
        return first if isinstance(first, dict) else None

    if isinstance(data, dict):
        if "markets" in data and isinstance(data["markets"], list) and data["markets"]:
            return data["markets"][0]

    return None


async def gamma_resolve_proxy_wallet(client: httpx.AsyncClient, handle: str) -> Optional[str]:
    """
    Resolve a username to proxyWallet via Gamma /public-search.
    """
    handle = normalize_handle(handle)
    if not handle:
        return None

    url = f"{GAMMA_BASE}/public-search"
    params = {
        "q": handle,
        "search_profiles": "true",
        "search_tags": "false",
        "cache": "true",
        "limit_per_type": 10,
        "page": 1,
        "optimized": "true",
    }

    data = await http_get_json(client, url, params)
    profiles = data.get("profiles") or []
    if not profiles:
        return None

    hl = handle.lower()
    best = None
    for p in profiles:
        # depending on response, may have username/name/pseudonym
        name = (p.get("name") or "").strip().lower()
        pseud = (p.get("pseudonym") or "").strip().lower()
        username = (p.get("username") or "").strip().lower()
        if hl in {name, pseud, username}:
            best = p
            break

    prof = best or profiles[0]
    w = (prof.get("proxyWallet") or "").strip()
    return w or None


def map_up_down_token_ids(market: dict) -> Tuple[Optional[str], Optional[str]]:
    """
    Map UP/DOWN outcomes to their clobTokenIds.

    Handles:
      - outcomes as list OR JSON-string
      - clobTokenIds as list OR JSON-string
      - fallback to binary order token_ids[0], token_ids[1]
    """
    outcomes = _parse_maybe_json_list(market.get("outcomes"))
    token_ids = _parse_maybe_json_list(
        market.get("clobTokenIds")
        or market.get("clobTokenIDs")
        or market.get("clob_token_ids")
    )

    if not token_ids or len(token_ids) < 2:
        return None, None

    up_id = None
    dn_id = None

    if outcomes and len(outcomes) == len(token_ids):
        # strict mapping for exact labels first (Up/Down or Yes/No)
        for o, tid in zip(outcomes, token_ids):
            ol = str(o).strip().lower()
            if ol in ("up", "yes", "true"):
                up_id = str(tid)
            elif ol in ("down", "no", "false"):
                dn_id = str(tid)

        # heuristic mapping
        if up_id is None or dn_id is None:
            for o, tid in zip(outcomes, token_ids):
                ol = str(o).strip().lower()
                if "up" in ol:
                    up_id = up_id or str(tid)
                elif "down" in ol:
                    dn_id = dn_id or str(tid)

    # final fallback: assume first two are the pair
    up_id = up_id or str(token_ids[0])
    dn_id = dn_id or str(token_ids[1])

    return up_id, dn_id


# ----------------------------
# Data API (/positions)
# ----------------------------
async def data_api_positions(client: httpx.AsyncClient, proxy_wallet: str, condition_id: str) -> List[dict]:
    """
    Fetch positions for a user. Some filters work, some don’t, so we try a few.
    """
    url = f"{DATA_BASE}/positions"

    attempts = [
        {"user": proxy_wallet, "limit": 200, "offset": 0, "market": condition_id},
        {"user": proxy_wallet, "limit": 200, "offset": 0, "conditionId": condition_id},
        {"user": proxy_wallet, "limit": 200, "offset": 0, "condition_id": condition_id},
        {"user": proxy_wallet, "limit": 200, "offset": 0},  # fallback: filter locally
    ]

    last_rows: List[dict] = []
    for params in attempts:
        rows = await http_get_json(client, url, params)
        rows = rows if isinstance(rows, list) else []
        last_rows = rows

        if not rows:
            continue

        if "market" not in params and "conditionId" not in params and "condition_id" not in params:
            rows = [r for r in rows if str(r.get("conditionId") or r.get("condition_id") or "") == condition_id]
        return rows

    return [r for r in last_rows if str(r.get("conditionId") or r.get("condition_id") or "") == condition_id]


def extract_up_down_avgprice(positions: List[dict], condition_id: str) -> Dict[str, Dict[str, float]]:
    """
    Uses avgPrice from /positions to show average entry price for each side.
    Weighted by shares in case multiple rows exist.
    """
    out = {
        "UP": {"shares": 0.0, "avgPrice": 0.0},
        "DOWN": {"shares": 0.0, "avgPrice": 0.0},
    }
    wsum = {"UP": 0.0, "DOWN": 0.0}

    for p in positions:
        cid = str(p.get("conditionId") or p.get("condition_id") or "")
        if cid and cid != condition_id:
            continue

        outcome = str(p.get("outcome") or p.get("label") or p.get("name") or "").strip().lower()
        shares = safe_float(p.get("size")) or safe_float(p.get("shares")) or safe_float(p.get("quantity"))
        avgp = safe_float(p.get("avgPrice"))

        side = "UP" if "up" in outcome else ("DOWN" if "down" in outcome else None)
        if not side:
            continue

        out[side]["shares"] += shares
        if shares > 0 and avgp > 0:
            out[side]["avgPrice"] += avgp * shares
            wsum[side] += shares

    for side in ("UP", "DOWN"):
        out[side]["avgPrice"] = out[side]["avgPrice"] / wsum[side] if wsum[side] > 0 else 0.0

    return out


# ----------------------------
# CLOB Market WS Manager (best bid/ask)
# ----------------------------
class OrderbookWSManager:
    """
    Maintains a single WS connection to the market channel and subscribes to asset_ids.
    Caches best bid/ask by token_id.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.asset_ids: List[str] = []
        self.best: Dict[str, Dict[str, float]] = {}
        self.ws: Optional[WebSocketApp] = None
        self.thread: Optional[threading.Thread] = None
        self.connected = False
        self.stop_flag = False
        self._last_connect_attempt = 0.0

    def ensure_running(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return

        self.stop_flag = False
        self._spawn_ws()

    def _spawn_ws(self):
        # basic backoff to avoid tight reconnect loops
        now = time.time()
        if now - self._last_connect_attempt < 1.0:
            time.sleep(1.0)
        self._last_connect_attempt = time.time()

        self.ws = WebSocketApp(
            CLOB_WSS_MARKET,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        def run():
            while not self.stop_flag:
                try:
                    self.ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception:
                    pass
                self.connected = False
                if self.stop_flag:
                    break
                time.sleep(1.0)  # reconnect pause

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def set_assets(self, asset_ids: List[str]):
        asset_ids = [str(a) for a in asset_ids if a]
        if len(asset_ids) < 2:
            return

        self.ensure_running()
        with self.lock:
            if asset_ids == self.asset_ids:
                return
            self.asset_ids = asset_ids
            self.best = {aid: {"bid": 0.0, "ask": 0.0, "ts": 0.0} for aid in asset_ids}

        # if already connected, subscribe immediately
        if self.connected and self.ws:
            self._send_subscribe(asset_ids)

    def _send_subscribe(self, asset_ids: List[str]):
        try:
            # docs show: type="market" and assets_ids=[...]
            self.ws.send(json.dumps({"type": "market", "assets_ids": asset_ids}))
        except Exception:
            pass

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        with self.lock:
            return {k: dict(v) for k, v in self.best.items()}

    # WS callbacks
    def _on_open(self, ws: WebSocketApp):
        self.connected = True
        with self.lock:
            ids = list(self.asset_ids)
        if ids:
            self._send_subscribe(ids)

    def _on_close(self, ws: WebSocketApp, status_code, msg):
        self.connected = False

    def _on_error(self, ws: WebSocketApp, error):
        self.connected = False

    def _on_message(self, ws: WebSocketApp, message: str):
        try:
            data = json.loads(message)
        except Exception:
            return

        et = data.get("event_type")

        # book: level2 updates (top item in bids/asks is best)
        if et == "book":
            asset_id = str(data.get("asset_id") or "")
            bids = data.get("bids") or []
            asks = data.get("asks") or []

            best_bid = safe_float(bids[0].get("price")) if bids else 0.0
            best_ask = safe_float(asks[0].get("price")) if asks else 0.0

            with self.lock:
                if asset_id in self.best:
                    self.best[asset_id]["bid"] = best_bid
                    self.best[asset_id]["ask"] = best_ask
                    self.best[asset_id]["ts"] = time.time()

        # Some deployments send best_bid_ask events too
        elif et == "best_bid_ask":
            asset_id = str(data.get("asset_id") or "")
            best_bid = safe_float(data.get("best_bid"))
            best_ask = safe_float(data.get("best_ask"))
            with self.lock:
                if asset_id in self.best:
                    self.best[asset_id]["bid"] = best_bid
                    self.best[asset_id]["ask"] = best_ask
                    self.best[asset_id]["ts"] = time.time()


OB = OrderbookWSManager()


# ----------------------------
# UI HTML
# ----------------------------
INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Whale Tracker - Polymarket Wallet Holdings</title>
  <style>
    *{box-sizing:border-box;}
    html,body{margin:0;padding:0;min-height:100vh;background:#f1f5f9;color:#1e293b;font-family:system-ui,sans-serif;font-size:15px;}
    .wrap{max-width:1400px;margin:0 auto;padding:20px;}
    h1{margin:0 0 8px 0;font-size:1.5rem;font-weight:700;color:#0f172a;}
    .sub{color:#64748b;font-size:0.9rem;margin-bottom:20px;}
    .grid{display:grid;gap:16px;}
    @media(min-width:1024px){.grid{grid-template-columns:360px 1fr;}}
    .card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,0.1);}
    .card-header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:16px;}
    .card-title{font-weight:700;font-size:1rem;color:#0f172a;}
    .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
    label{display:block;color:#64748b;font-size:0.75rem;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px;}
    textarea,input{width:100%;background:#f8fafc;color:#1e293b;border:1px solid #cbd5e1;border-radius:8px;padding:10px 12px;font-size:0.9rem;outline:none;}
    textarea:focus,input:focus{border-color:#6366f1;box-shadow:0 0 0 2px rgba(99,102,241,0.2);}
    textarea{min-height:80px;resize:vertical;}
    .input-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
    .btn{background:#6366f1;color:#fff;border:0;border-radius:8px;padding:12px 20px;font-weight:600;cursor:pointer;font-size:0.9rem;}
    .btn:hover{background:#4f46e5;}
    .btn-ghost{background:#f1f5f9;color:#334155;border:1px solid #cbd5e1;}
    .btn-ghost:hover{background:#e2e8f0;}
    .statusDot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#f59e0b;flex-shrink:0;}
    .statusDot.ok{background:#22c55e;}
    .statusDot.bad{background:#ef4444;}
    .pill{padding:6px 12px;border-radius:6px;background:#f1f5f9;border:1px solid #e2e8f0;color:#64748b;font-size:0.75rem;font-weight:600;}
    .divider{height:1px;background:#e2e8f0;margin:16px 0;}
    .mono{font-family:ui-monospace,monospace;}
    .small{font-size:0.8rem;}
    .muted{color:#64748b;}
    .errorBox{display:none;margin-top:12px;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:12px;color:#b91c1c;}
    .errorBox .title{font-weight:700;margin-bottom:4px;}
    .quotes-bar{display:flex;flex-wrap:wrap;gap:16px;margin-top:12px;padding:12px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0;}
    .quote-item{display:flex;align-items:center;gap:8px;}
    .quote-item .side{font-size:0.7rem;font-weight:700;text-transform:uppercase;}
    .quote-item.up .side{color:#22c55e;}
    .quote-item.down .side{color:#ef4444;}
    .quote-item .bid{color:#22c55e;font-weight:700;}
    .quote-item .ask{color:#ef4444;font-weight:700;}
    .holdGrid{display:grid;gap:12px;}
    @media(min-width:640px){.holdGrid{grid-template-columns:repeat(2,1fr);}}
    @media(min-width:1200px){.holdGrid{grid-template-columns:repeat(3,1fr);}}
    .holdCard{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:16px;}
    .holdCard .wallet{font-weight:700;font-size:0.95rem;margin-bottom:2px;color:#0f172a;}
    .holdCard .addr{font-size:0.7rem;color:#64748b;font-family:ui-monospace,monospace;}
    .holdCard .side-block{margin-top:12px;padding:10px;border-radius:8px;}
    .holdCard .side-block.up{background:#dcfce7;border:1px solid #86efac;}
    .holdCard .side-block.down{background:#fee2e2;border:1px solid #fca5a5;}
    .holdCard .side-label{font-size:0.7rem;font-weight:700;text-transform:uppercase;margin-bottom:4px;}
    .holdCard .side-block.up .side-label{color:#16a34a;}
    .holdCard .side-block.down .side-label{color:#dc2626;}
    .holdCard .shares{font-size:1.1rem;font-weight:700;font-family:ui-monospace,monospace;color:#0f172a;}
    .holdCard .meta{display:flex;justify-content:space-between;margin-top:6px;font-size:0.75rem;}
    .holdCard .meta .l{color:#64748b;}
    .holdCard .meta .r{font-weight:600;font-family:ui-monospace,monospace;}
    .holdCard .spread{font-size:0.7rem;color:#64748b;margin-top:4px;}
    .empty-state{text-align:center;padding:40px 20px;color:#64748b;font-size:0.9rem;}
    @media(max-width:767px){.btn,.btn-ghost{padding:14px;min-height:48px;} .input-row{grid-template-columns:1fr;}}
    .top-bar{display:flex;flex-wrap:wrap;gap:20px;align-items:center;padding:14px 18px;background:#fff;border:1px solid #e2e8f0;border-radius:10px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,0.08);}
    .top-bar .item{display:flex;align-items:center;gap:8px;}
    .top-bar .item .label{font-size:0.7rem;font-weight:700;text-transform:uppercase;color:#64748b;}
    .top-bar .item.up .label{color:#16a34a;}
    .top-bar .item.down .label{color:#dc2626;}
    .top-bar .item .prices{font-weight:700;font-family:ui-monospace,monospace;font-size:1rem;}
    .top-bar .item.up .prices .bid{color:#16a34a;}
    .top-bar .item.up .prices .ask{color:#dc2626;}
    .top-bar .item.down .prices .bid{color:#16a34a;}
    .top-bar .item.down .prices .ask{color:#dc2626;}
    .top-bar .time-left{margin-left:auto;font-weight:700;font-family:ui-monospace,monospace;font-size:1rem;color:#0f172a;}
    .top-bar .time-left.ended{color:#dc2626;}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Whale Tracker</h1>
    <div class="sub">Track Polymarket wallet holdings and live orderbook for binary markets</div>

    <div id="topBar" class="top-bar" style="display:none;">
      <div class="item up"><span class="label">UP</span><span class="prices"><span class="bid" id="topUpBid">0.000</span> / <span class="ask" id="topUpAsk">0.000</span></span></div>
      <div class="item down"><span class="label">DOWN</span><span class="prices"><span class="bid" id="topDownBid">0.000</span> / <span class="ask" id="topDownAsk">0.000</span></span></div>
      <div class="time-left" id="timeLeft">—</div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <div class="row">
            <span id="dot" class="statusDot"></span>
            <span class="pill" id="status">idle</span>
          </div>
          <div class="row">
            <button class="btn-ghost" id="stopBtn">Stop</button>
            <button class="btn" id="connectBtn">Connect</button>
          </div>
        </div>

        <div class="divider"></div>

        <label>Usernames</label>
        <textarea id="handles" placeholder="@user1, @user2 or polymarket.com/@user"></textarea>

        <div class="input-row" style="margin-top:12px;">
          <div><label>Max users</label><input id="maxUsers" type="number" min="1" max="50" value="5" /></div>
          <div><label>Poll (sec)</label><input id="pollS" type="number" min="1" max="15" value="2" /></div>
        </div>

        <div class="divider"></div>

        <label>Market slug</label>
        <input id="marketSlug" placeholder="bitcoin-up-or-down-january-28-4am-et" />

        <div class="small muted" id="marketResolved" style="margin-top:8px;"></div>
        <div id="quotesBar" class="quotes-bar" style="display:none;"></div>

        <div class="errorBox" id="errorBox">
          <div style="font-weight:900;">Error</div>
          <div class="small" id="errorText"></div>
        </div>

        <div class="divider"></div>
        <div id="resolveBox" class="small muted"></div>
      </div>

      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <div>
            <div class="card-title">Wallet Holdings</div>
          </div>
          <div class="row">
            <span class="pill" id="countPill">0 wallets</span>
          </div>
        </div>

        <div class="holdGrid" id="holdGrid"></div>
      </div>
    </div>
  </div>

<script>
  let timer = null;
  let resolved = {};
  let marketInfo = null;
  let holdings = {};
  let quotes = {UP:{bid:0,ask:0}, DOWN:{bid:0,ask:0}};

  function setStatus(kind, text) {
    const dot = document.getElementById("dot");
    const status = document.getElementById("status");
    status.textContent = text;
    dot.classList.remove("ok","bad");
    if (kind === "ok") dot.classList.add("ok");
    if (kind === "bad") dot.classList.add("bad");
  }
  function showError(msg) {
    document.getElementById("errorText").textContent = msg;
    document.getElementById("errorBox").style.display = "block";
  }
  function clearError() {
    document.getElementById("errorText").textContent = "";
    document.getElementById("errorBox").style.display = "none";
  }
  function parseHandles(text) {
    const parts = text.split(/\n|,/).map(s => s.trim()).filter(Boolean);
    return parts.map(s => s.replace(/^@/, "").replace(/^https?:\/\/polymarket\.com\/@/i, ""));
  }
  function fmt(x) {
    if (x === null || x === undefined || isNaN(x)) return "0.000";
    return Number(x).toFixed(3);
  }

  function formatTimeLeft(endDateStr) {
    if (!endDateStr) return null;
    let end;
    try {
      end = new Date(endDateStr.replace("Z", "+00:00"));
    } catch (e) { return null; }
    const ms = end.getTime() - Date.now();
    if (ms <= 0) return { text: "Ended", ended: true };
    const totalMin = ms / 60000;
    const days = Math.floor(totalMin / 1440);
    const hours = Math.floor((totalMin % 1440) / 60);
    const mins = Math.floor(totalMin % 60);
    let parts = [];
    if (days > 0) parts.push(days + "d");
    if (hours > 0) parts.push(hours + "h");
    parts.push(mins + "m");
    return { text: parts.join(" "), ended: false };
  }

  let timeLeftInterval = null;
  function updateTopBar() {
    const tb = document.getElementById("topBar");
    if (!marketInfo) { tb.style.display = "none"; return; }
    tb.style.display = "flex";
    document.getElementById("topUpBid").textContent = fmt(quotes.UP?.bid);
    document.getElementById("topUpAsk").textContent = fmt(quotes.UP?.ask);
    document.getElementById("topDownBid").textContent = fmt(quotes.DOWN?.bid);
    document.getElementById("topDownAsk").textContent = fmt(quotes.DOWN?.ask);
    const tl = formatTimeLeft(marketInfo.endDate);
    const el = document.getElementById("timeLeft");
    if (tl) { el.textContent = tl.text; el.classList.toggle("ended", tl.ended); }
    else el.textContent = "—";
  }

  function renderMarketResolved() {
    const el = document.getElementById("marketResolved");
    const qb = document.getElementById("quotesBar");
    if (!marketInfo || !marketInfo.conditionId) {
      el.textContent = "";
      qb.style.display = "none";
      return;
    }
    el.innerHTML = `Tracking: <b>${marketInfo.title || "(unknown)"}</b>`;
    qb.style.display = "flex";
    qb.innerHTML = `
      <div class="quote-item up"><span class="side">UP</span> <span class="bid">${fmt(quotes.UP.bid)}</span> / <span class="ask">${fmt(quotes.UP.ask)}</span></div>
      <div class="quote-item down"><span class="side">DOWN</span> <span class="bid">${fmt(quotes.DOWN.bid)}</span> / <span class="ask">${fmt(quotes.DOWN.ask)}</span></div>
    `;
  }

  function renderResolveBox() {
    const box = document.getElementById("resolveBox");
    const entries = Object.entries(resolved);
    if (!entries.length) { box.innerHTML = ""; return; }
    box.innerHTML = `<div style="font-weight:900; margin-bottom:6px;">Resolved proxy wallets</div>` +
      entries.map(([h,w]) => `<div class="mono">@${h} → ${w}</div>`).join("");
  }

  function renderHoldings() {
    const grid = document.getElementById("holdGrid");
    const handles = Object.keys(holdings);
    document.getElementById("countPill").textContent = `${handles.length} wallets`;
    grid.innerHTML = "";

    if (!handles.length) {
      grid.innerHTML = `<div class="empty-state">No wallet data yet.</div>`;
      return;
    }

    for (const h of handles) {
      const upS = holdings[h]?.UP?.shares || 0;
      const dnS = holdings[h]?.DOWN?.shares || 0;
      const upAvg = holdings[h]?.UP?.avgPrice || 0;
      const dnAvg = holdings[h]?.DOWN?.avgPrice || 0;
      const walletShort = (resolved[h] || h).slice(0, 10) + "...";

      const div = document.createElement("div");
      div.className = "holdCard";
      div.innerHTML = `
        <div class="wallet">@${h}</div>
        <div class="addr">${walletShort}</div>
        <div class="side-block up">
          <div class="side-label">UP</div>
          <div class="shares">${fmt(upS)} shares</div>
          <div class="meta"><span class="l">Avg entry</span><span class="r">${fmt(upAvg)}</span></div>
          <div class="spread">Bid/Ask: ${fmt(quotes.UP.bid)} / ${fmt(quotes.UP.ask)}</div>
        </div>
        <div class="side-block down">
          <div class="side-label">DOWN</div>
          <div class="shares">${fmt(dnS)} shares</div>
          <div class="meta"><span class="l">Avg entry</span><span class="r">${fmt(dnAvg)}</span></div>
          <div class="spread">Bid/Ask: ${fmt(quotes.DOWN.bid)} / ${fmt(quotes.DOWN.ask)}</div>
        </div>
      `;
      grid.appendChild(div);
    }
  }

  async function fetchOnce() {
    clearError();
    const maxUsers = parseInt(document.getElementById("maxUsers").value || "5", 10);
    const pollS = parseFloat(document.getElementById("pollS").value || "2");
    const handles = parseHandles(document.getElementById("handles").value).slice(0, maxUsers);
    const marketSlug = (document.getElementById("marketSlug").value || "").trim();
    if (!marketSlug) {
      showError("Market slug is required.");
      stop();
      return;
    }

    const params = new URLSearchParams();
    params.set("slug", marketSlug);
    params.set("handles", handles.join(","));

    setStatus("ok", "polling...");

    try {
      const resp = await fetch(`/api/holdings?${params.toString()}`);
      const rawText = await resp.text();
      let data;
      try {
        data = rawText ? JSON.parse(rawText) : {};
      } catch (parseErr) {
        throw new Error("Invalid JSON response. Server may be down or returned HTML. Raw: " + (rawText || "").slice(0, 80) + "...");
      }
      if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);

      marketInfo = data.market;
      resolved = data.proxy_wallets || {};
      holdings = data.holdings || {};
      quotes = data.quotes || quotes;

      updateTopBar();
      renderMarketResolved();
      renderResolveBox();
      renderHoldings();

      if (!timeLeftInterval) {
        timeLeftInterval = setInterval(updateTopBar, 1000);
      }

      if (timer) {
        clearInterval(timer);
        timer = setInterval(fetchOnce, Math.max(1, pollS) * 1000);
      }
    } catch (e) {
      setStatus("bad", "error");
      showError(e instanceof Error ? e.message : String(e));
      stop();
    }
  }

  function connect() {
    stop();
    fetchOnce();
    const pollS = parseFloat(document.getElementById("pollS").value || "2");
    timer = setInterval(fetchOnce, Math.max(1, pollS) * 1000);
  }

  function stop() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    if (timeLeftInterval) {
      clearInterval(timeLeftInterval);
      timeLeftInterval = null;
    }
    setStatus("warn", "stopped");
  }

  document.getElementById("connectBtn").onclick = () => connect();
  document.getElementById("stopBtn").onclick = () => stop();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


@app.get("/api/holdings")
async def api_holdings(
    slug: str = Query(...),
    handles: str = Query("", description="comma-separated handles"),
):
    slug = normalize_slug(slug)
    handle_list = [normalize_handle(h) for h in (handles.split(",") if handles else [])]
    handle_list = [h for h in handle_list if h][:50]

    async with httpx.AsyncClient() as client:
        # Resolve market
        m = await gamma_market_by_slug(client, slug)
        if not m:
            return JSONResponse({"error": f"Could not resolve slug: {slug}"}, status_code=400)

        condition_id = (m.get("conditionId") or "").strip()
        title = (m.get("question") or m.get("title") or "").strip()
        if not condition_id:
            return JSONResponse({"error": "Resolved market but conditionId missing"}, status_code=400)

        up_token, dn_token = map_up_down_token_ids(m)
        if not up_token or not dn_token:
            # show debug keys for you to quickly see what Gamma returned
            return JSONResponse(
                {
                    "error": "Could not map UP/DOWN token IDs from Gamma market response",
                    "debug": {
                        "has_outcomes": "outcomes" in m,
                        "outcomes_type": str(type(m.get("outcomes"))),
                        "has_clobTokenIds": ("clobTokenIds" in m) or ("clob_token_ids" in m) or ("clobTokenIDs" in m),
                        "clobTokenIds_type": str(type(m.get("clobTokenIds") or m.get("clob_token_ids") or m.get("clobTokenIDs"))),
                    },
                },
                status_code=400,
            )

        # Start/retarget WS subscription to these tokens
        OB.set_assets([up_token, dn_token])

        # Resolve wallets
        proxy_wallets: Dict[str, str] = {}
        for h in handle_list:
            try:
                w = await gamma_resolve_proxy_wallet(client, h)
                if w:
                    proxy_wallets[h] = w
            except Exception:
                pass

        # Holdings + avg entry price
        holdings_payload: Dict[str, Dict[str, Dict[str, float]]] = {}
        for h, w in proxy_wallets.items():
            try:
                pos = await data_api_positions(client, w, condition_id)
                holdings_payload[h] = extract_up_down_avgprice(pos, condition_id)
            except Exception:
                holdings_payload[h] = {
                    "UP": {"shares": 0.0, "avgPrice": 0.0},
                    "DOWN": {"shares": 0.0, "avgPrice": 0.0},
                }

        # Quotes (bid/ask) from WS cache
        best = OB.snapshot()
        up_q = best.get(up_token, {"bid": 0.0, "ask": 0.0, "ts": 0.0})
        dn_q = best.get(dn_token, {"bid": 0.0, "ask": 0.0, "ts": 0.0})

        end_date = (m.get("endDate") or m.get("end_date") or "").strip()
        return {
            "market": {
                "slug": slug,
                "conditionId": condition_id,
                "title": title,
                "upTokenId": up_token,
                "downTokenId": dn_token,
                "endDate": end_date,
            },
            "proxy_wallets": proxy_wallets,
            "holdings": holdings_payload,
            "quotes": {
                "UP": {"bid": float(up_q.get("bid", 0.0)), "ask": float(up_q.get("ask", 0.0)), "ts": float(up_q.get("ts", 0.0))},
                "DOWN": {"bid": float(dn_q.get("bid", 0.0)), "ask": float(dn_q.get("ask", 0.0)), "ts": float(dn_q.get("ts", 0.0))},
            },
        }
