# IG API Reference

## Base URLs

| Environment    | URL                                    |
| -------------- | -------------------------------------- |
| Demo (sandbox) | `https://demo-api.ig.com/gateway/deal` |
| Production     | `https://api.ig.com/gateway/deal`      |

The URL is derived automatically from `IG_ENV` in `.env`.

______________________________________________________________________

## Authentication

### OAuth v3 (used by this bot)

```
POST /session   (Version: 3)
Headers: X-IG-API-KEY: <api_key>
Body:    {"identifier": "<username>", "password": "<password>"}

Response:
{
  "oauthToken": {
    "access_token":  "702f6580-...",
    "refresh_token": "a9cec2d7-...",
    "token_type":    "Bearer",
    "expires_in":    "60"
  }
}
```

- **Access token** — valid ~60 seconds; refreshed automatically by `IGSession`
- **Refresh token** — expires ~10 minutes after the access token
- Refresh endpoint: `POST /session/refresh-token` with `{"refresh_token": "..."}`
- Tokens are stored **in memory only** — never written to disk or DB

Implementation: [src/core/api/session.py](../src/core/api/session.py)

### Legacy CST / X-SECURITY-TOKEN (not used)

```
POST /session   (Version: 1 or 2)
Headers: X-IG-API-KEY: <api_key>
Response headers: CST + X-SECURITY-TOKEN
```

Tokens valid 6h, extendable to 72h. This is the approach the old PHP bot used.

______________________________________________________________________

## Standard request headers

```
Content-Type:   application/json; charset=UTF-8
Accept:         application/json; charset=UTF-8
X-IG-API-KEY:   <api_key>
Authorization:  Bearer <access_token>
IG-ACCOUNT-ID:  <account_id>
Version:        <endpoint_version>
```

______________________________________________________________________

## Endpoints

### Session

| Method | Endpoint                 | Version | Description                 |
| ------ | ------------------------ | ------- | --------------------------- |
| POST   | `/session`               | 3       | OAuth login                 |
| POST   | `/session/refresh-token` | 1       | Refresh OAuth access token  |
| GET    | `/session/encryptionKey` | 1       | Encryption key for password |

### Account

| Method | Endpoint                | Version | Description                     |
| ------ | ----------------------- | ------- | ------------------------------- |
| GET    | `/accounts`             | 1       | List accounts                   |
| GET    | `/accounts/preferences` | 1       | Account preferences             |
| GET    | `/history/activity`     | 3       | Activity history (FIQL filters) |
| GET    | `/history/transactions` | 2       | Transaction history             |

### Positions

| Method | Endpoint                    | Version | Description       |
| ------ | --------------------------- | ------- | ----------------- |
| GET    | `/positions`                | 2       | Open positions    |
| GET    | `/positions/{dealId}`       | 2       | Position details  |
| POST   | `/positions/otc`            | 2       | Open a position   |
| PUT    | `/positions/otc/{dealId}`   | 2       | Update stop/limit |
| DELETE | `/positions/otc/{dealId}`   | 1       | Close a position  |
| GET    | `/confirms/{dealReference}` | 1       | Deal confirmation |

### Working orders

| Method | Endpoint                       | Version | Description    |
| ------ | ------------------------------ | ------- | -------------- |
| GET    | `/working-orders`              | 2       | Pending orders |
| POST   | `/working-orders/otc`          | 2       | Create order   |
| PUT    | `/working-orders/otc/{dealId}` | 2       | Update order   |
| DELETE | `/working-orders/otc/{dealId}` | 1       | Delete order   |

### Markets & prices

| Method | Endpoint                                    | Version | Description                    |
| ------ | ------------------------------------------- | ------- | ------------------------------ |
| GET    | `/markets/{epic}`                           | 3       | Market details + dealing rules |
| GET    | `/markets?searchTerm={term}`                | 1       | Search markets                 |
| GET    | `/markets?epics=E1,E2`                      | 2       | Batch market details           |
| GET    | `/prices/{epic}/{resolution}/{numPoints}`   | 1       | Last N candles                 |
| GET    | `/prices/{epic}/{resolution}/{start}/{end}` | 1       | Candles by date range          |
| GET    | `/categories`                               | 1       | Market category tree           |

### Watchlists

| Method | Endpoint                  | Version | Description                |
| ------ | ------------------------- | ------- | -------------------------- |
| GET    | `/watchlists`             | 1       | All watchlists             |
| GET    | `/watchlists/{id}`        | 1       | Watchlist details          |
| POST   | `/watchlists/{id}/{epic}` | 1       | Add epic to watchlist      |
| DELETE | `/watchlists/{id}/{epic}` | 1       | Remove epic from watchlist |

### Client sentiment

| Method | Endpoint                       | Version | Description      |
| ------ | ------------------------------ | ------- | ---------------- |
| GET    | `/client-sentiment/{marketId}` | 1       | Long/short ratio |

______________________________________________________________________

## Rate limits

IG enforces per-account rate limits. All calls are routed through `APIQueue` + `APIGuard`:

- **30 requests/minute** (the IG per-account limit; also half the per-API-key limit)
- **20 requests/second** (burst ceiling — the per-minute cap always bites first)
- Quota blocks trigger automatic wait + resume

See [src/core/api_guard.py](../src/core/api_guard.py).

______________________________________________________________________

## Streaming API (Lightstreamer)

IG provides a real-time data feed via [Lightstreamer](https://lightstreamer.com):

- **Live candles** — OHLC + volume at configurable resolution (default: 1 minute)
- **Position updates** — real-time position changes
- **Deal confirmations** — instant order fill notifications

The streaming endpoint and credentials are obtained from the OAuth login response.
Hard limit: **40 subscriptions per connection** (`STREAMING_MAX_EPICS`).

Implementation: [src/feed/streaming.py](../src/feed/streaming.py)

______________________________________________________________________

## IG error codes (common)

| Code                                     | Meaning                               |
| ---------------------------------------- | ------------------------------------- |
| `error.security.account-token-invalid`   | Access token expired — refresh needed |
| `error.security.client-token-invalid`    | Bad API key                           |
| `error.confirms.deal-unavailable`        | Deal not confirmed yet — retry        |
| `error.service.marketdata.epic.notfound` | Epic does not exist                   |
| `IG-TOO-MANY-REQUESTS`                   | Rate limit hit — back off             |

______________________________________________________________________

## Resources

- [IG API Guide](https://labs.ig.com/rest-trading-api-guide.html)
- [IG API Reference](https://labs.ig.com/rest-trading-api-reference.html)
- [IG API Companion (interactive)](https://labs.ig.com/companion/api-rest-companion-release/index.html)
- [IG Streaming API Guide](https://labs.ig.com/streaming-api-guide.html)
