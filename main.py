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
<html>
<head>
  <meta charset="utf-8" />
  <title>Polymarket Holdings + Quotes</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root{
      --bg:#0b1020; --text:rgba(255,255,255,.92); --muted:rgba(255,255,255,.68);
      --border:rgba(255,255,255,.12); --card:rgba(255,255,255,.06); --card2:rgba(255,255,255,.09);
      --good:#41d18b; --bad:#ff6b6b; --warn:#ffd166;
    }
    body{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;
      background:radial-gradient(1200px 800px at 20% 10%, rgba(110,168,255,0.25), transparent 60%),
               radial-gradient(1200px 800px at 90% 40%, rgba(65,209,139,0.18), transparent 60%),
               var(--bg);color:var(--text);}
    .wrap{max-width:1180px;margin:0 auto;padding:22px;}
    h1{margin:0 0 6px 0;font-size:22px;}
    .sub{color:var(--muted);margin-bottom:18px;}
    .grid{display:grid;grid-template-columns:1.05fr 1.95fr;gap:14px;}
    @media (max-width:980px){.grid{grid-template-columns:1fr;}}
    .card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:14px;
      box-shadow:0 10px 30px rgba(0,0,0,0.25);backdrop-filter: blur(10px);}
    .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
    label{display:block;color:var(--muted);font-size:12px;margin-bottom:6px;}
    textarea,input{width:100%;background:rgba(255,255,255,0.06);color:var(--text);border:1px solid var(--border);
      border-radius:12px;padding:10px 10px;outline:none;}
    textarea{min-height:90px;resize:vertical;}
    .btn{background:linear-gradient(135deg, rgba(110,168,255,0.92), rgba(65,209,139,0.84));
      color:#071022;border:0;border-radius:12px;padding:10px 12px;font-weight:900;cursor:pointer;}
    .btn2{background:rgba(255,255,255,0.08);color:var(--text);border:1px solid var(--border);
      border-radius:12px;padding:10px 12px;font-weight:800;cursor:pointer;}
    .pill{padding:4px 10px;border-radius:999px;border:1px solid var(--border);color:var(--muted);font-size:12px;}
    .statusDot{display:inline-block;width:10px;height:10px;border-radius:999px;background:var(--warn);
      box-shadow:0 0 0 4px rgba(255,209,102,0.15);}
    .ok{background:var(--good);box-shadow:0 0 0 4px rgba(65,209,139,0.15);}
    .bad{background:var(--bad);box-shadow:0 0 0 4px rgba(255,107,107,0.12);}
    .divider{height:1px;background:rgba(255,255,255,0.10);margin:12px 0;}
    .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono";}
    .small{font-size:12px;} .muted{color:var(--muted);}
    .holdGrid{display:grid;grid-template-columns:repeat(2, 1fr);gap:10px;margin-top:12px;}
    .holdCard{background:var(--card2);border:1px solid var(--border);border-radius:14px;padding:12px;}
    .k{color:var(--muted);font-size:12px;}
    .v{font-weight:900;font-size:18px;margin-top:4px;}
    .kv{display:flex;justify-content:space-between;gap:10px;margin-top:6px;}
    .kv .left{color:var(--muted);font-size:12px;}
    .kv .right{font-weight:800;}
    .bar{height:10px;border-radius:999px;border:1px solid rgba(255,255,255,0.12);
      background:rgba(255,255,255,0.05);overflow:hidden;}
    .fillUp{height:100%;background:rgba(65,209,139,0.90);}
    .fillDown{height:100%;background:rgba(255,107,107,0.90);}
    .errorBox{display:none;margin-top:10px;background:rgba(255,107,107,0.12);border:1px solid rgba(255,107,107,0.25);
      border-radius:14px;padding:10px;}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Polymarket Holdings + Quotes</h1>
    <div class="sub">Polling UI + server-side orderbook WS for best bid/ask. Also shows users’ avg entry prices.</div>

    <div class="grid">
      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <div class="row">
            <span id="dot" class="statusDot"></span>
            <span class="pill" id="status">idle</span>
          </div>
          <div class="row">
            <button class="btn2" id="stopBtn">Stop</button>
            <button class="btn" id="connectBtn">Connect</button>
          </div>
        </div>

        <div class="divider"></div>

        <label>Usernames</label>
        <textarea id="handles">@k9Q2mX4L8A7ZP3R,@0x8dxd</textarea>

        <div class="row">
          <div style="flex:1">
            <label>Max users (N)</label>
            <input id="maxUsers" type="number" min="1" max="50" value="5" />
          </div>
          <div style="flex:1">
            <label>Poll interval (seconds)</label>
            <input id="pollS" type="number" min="1" max="15" value="2" />
          </div>
        </div>

        <div class="divider"></div>

        <label>Market slug</label>
        <input id="marketSlug" placeholder="bitcoin-up-or-down-january-28-4am-et" />

        <div class="small muted" id="marketResolved" style="margin-top:8px;"></div>

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
            <div style="font-weight:900; font-size:16px;">Holdings</div>
            <div class="small muted">Shares + avg entry + market bid/ask.</div>
          </div>
          <div class="row">
            <span class="pill" id="countPill">0 users</span>
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

  function renderMarketResolved() {
    const el = document.getElementById("marketResolved");
    if (!marketInfo || !marketInfo.conditionId) { el.textContent = ""; return; }
    el.innerHTML = `
      Tracking: <b>${marketInfo.title}</b><br>
      <span class="mono muted">conditionId=${marketInfo.conditionId.slice(0,12)}…</span>
      <div class="small muted" style="margin-top:6px;">
        <span class="mono">UP bid/ask:</span> <b>${fmt(quotes.UP.bid)}</b> / <b>${fmt(quotes.UP.ask)}</b>
        &nbsp; • &nbsp;
        <span class="mono">DOWN bid/ask:</span> <b>${fmt(quotes.DOWN.bid)}</b> / <b>${fmt(quotes.DOWN.ask)}</b>
      </div>`;
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
    document.getElementById("countPill").textContent = `${handles.length} users`;
    grid.innerHTML = "";

    if (!handles.length) {
      grid.innerHTML = `<div class="small muted">No data yet.</div>`;
      return;
    }

    let maxShares = 1.0;
    for (const h of handles) {
      maxShares = Math.max(maxShares, holdings[h]?.UP?.shares || 0, holdings[h]?.DOWN?.shares || 0);
    }

    for (const h of handles) {
      const upS = holdings[h]?.UP?.shares || 0;
      const dnS = holdings[h]?.DOWN?.shares || 0;
      const upAvg = holdings[h]?.UP?.avgPrice || 0;
      const dnAvg = holdings[h]?.DOWN?.avgPrice || 0;

      const upPct = Math.max(0, Math.min(100, (upS / maxShares) * 100));
      const dnPct = Math.max(0, Math.min(100, (dnS / maxShares) * 100));

      const div = document.createElement("div");
      div.className = "holdCard";
      div.innerHTML = `
        <div class="row" style="justify-content: space-between;">
          <div style="font-weight:900;" class="mono">@${h}</div>
          <div class="pill mono">${(resolved[h] || "").slice(0,10)}…</div>
        </div>

        <div style="margin-top:10px;">
          <div class="k">UP</div>
          <div class="v mono">${fmt(upS)} shares</div>
          <div class="bar"><div class="fillUp" style="width:${upPct}%"></div></div>
          <div class="kv"><div class="left">Avg entry</div><div class="right mono">${fmt(upAvg)}</div></div>
          <div class="kv"><div class="left">Market bid/ask</div><div class="right mono">${fmt(quotes.UP.bid)} / ${fmt(quotes.UP.ask)}</div></div>
        </div>

        <div style="margin-top:14px;">
          <div class="k">DOWN</div>
          <div class="v mono">${fmt(dnS)} shares</div>
          <div class="bar"><div class="fillDown" style="width:${dnPct}%"></div></div>
          <div class="kv"><div class="left">Avg entry</div><div class="right mono">${fmt(dnAvg)}</div></div>
          <div class="kv"><div class="left">Market bid/ask</div><div class="right mono">${fmt(quotes.DOWN.bid)} / ${fmt(quotes.DOWN.ask)}</div></div>
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
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);

      marketInfo = data.market;
      resolved = data.proxy_wallets || {};
      holdings = data.holdings || {};
      quotes = data.quotes || quotes;

      renderMarketResolved();
      renderResolveBox();
      renderHoldings();

      if (timer) {
        clearInterval(timer);
        timer = setInterval(fetchOnce, Math.max(1, pollS) * 1000);
      }
    } catch (e) {
      setStatus("bad", "error");
      showError(String(e));
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

        return {
            "market": {
                "slug": slug,
                "conditionId": condition_id,
                "title": title,
                "upTokenId": up_token,
                "downTokenId": dn_token,
            },
            "proxy_wallets": proxy_wallets,
            "holdings": holdings_payload,
            "quotes": {
                "UP": {"bid": float(up_q.get("bid", 0.0)), "ask": float(up_q.get("ask", 0.0)), "ts": float(up_q.get("ts", 0.0))},
                "DOWN": {"bid": float(dn_q.get("bid", 0.0)), "ask": float(dn_q.get("ask", 0.0)), "ts": float(dn_q.get("ts", 0.0))},
            },
        }
