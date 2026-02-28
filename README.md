# Whale Tracker

Track Polymarket holdings and live orderbook quotes for specified users on any binary (UP/DOWN) market. Built for monitoring whale positions with real-time bid/ask feeds.

## Disclaimer

This project is for **educational and visualization purposes only**. I do not trade on Polymarket or any prediction markets. I build tools like this for learning and data visualization. I am not financially liable for any use of this software. Use at your own risk. This is not financial advice.

## Features

- **Per-user holdings**: UP and DOWN shares with average entry price for each tracked user
- **Live market quotes**: Server-side WebSocket connection to Polymarket CLOB for real-time best bid/ask
- **Username resolution**: Enter Polymarket handles (`@username`) or full profile URLs; proxy wallets are resolved automatically
- **Polling UI**: Configurable polling interval; no browser WebSocket required

## Tech Stack

- **FastAPI** — Web server and REST API
- **httpx** — Async HTTP client for Polymarket APIs
- **websocket-client** — CLOB market WebSocket for orderbook streaming

## Prerequisites

- Python 3.10+
- Network access to Polymarket APIs

## Installation

1. Clone or download this repository.

2. Create and activate a virtual environment (recommended):

   ```bash
   python -m venv venv
   # Windows
   venv\Scripts\activate
   # macOS/Linux
   source venv/bin/activate
   ```

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   pip install websocket-client
   ```

## Usage

Start the server:

```bash
uvicorn main:app --reload
```

Then open **http://127.0.0.1:8000** in your browser.

### UI Controls

| Input | Description |
|-------|-------------|
| **Usernames** | Comma- or newline-separated Polymarket handles (e.g. `@user1`, `@user2`) |
| **Max users (N)** | Maximum number of users to fetch (1–50) |
| **Poll interval** | Seconds between API polls (1–15) |
| **Market slug** | Polymarket market slug (e.g. `bitcoin-up-or-down-january-28-4am-et`) |

Click **Connect** to start polling. The UI shows per-user holdings (UP/DOWN shares, avg entry) and market-wide bid/ask for each token.

## API

### `GET /`

Returns the main HTML/JS polling UI.

### `GET /api/holdings`

Returns holdings and quotes for the given market and users.

**Query parameters**

| Param | Required | Description |
|-------|----------|-------------|
| `slug` | Yes | Polymarket market slug |
| `handles` | No | Comma-separated usernames to track |

**Example**

```
GET /api/holdings?slug=bitcoin-up-or-down-january-28-4am-et&handles=@user1,@user2
```

**Response**

```json
{
  "market": {
    "slug": "...",
    "conditionId": "...",
    "title": "...",
    "upTokenId": "...",
    "downTokenId": "..."
  },
  "proxy_wallets": { "@user1": "0x...", "@user2": "0x..." },
  "holdings": {
    "@user1": {
      "UP": { "shares": 100.0, "avgPrice": 0.55 },
      "DOWN": { "shares": 0.0, "avgPrice": 0.0 }
    }
  },
  "quotes": {
    "UP": { "bid": 0.54, "ask": 0.56, "ts": 1234567890.0 },
    "DOWN": { "bid": 0.44, "ask": 0.46, "ts": 1234567890.0 }
  }
}
```

## Data Sources

- **Gamma API** — Market metadata, slug lookup, profile search
- **Data API** — User positions per market
- **CLOB WebSocket** — Real-time orderbook (best bid/ask)

## License

MIT
