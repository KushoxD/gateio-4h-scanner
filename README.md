# Gate.io 4H MACD Scanner

Scans every Gate.io spot **USDT** market after each 4-hour candle **close**
(00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC) and sends a Telegram alert for
every pair that satisfies all of these on that **closed** bar:

| # | Condition |
|---|-----------|
| 1 | Quote asset is `USDT` and `trade_status == "tradable"` |
| 2 | Base asset market cap ≥ `MIN_MCAP` (default $10,000,000) |
| 3 | Not a leveraged / ETF token (`3L`, `5L`, `3S`, `5S`, `4L`, `4S`) |
| 4 | MACD(12,26,9) golden cross: `MACD[-2] <= Signal[-2]` **and** `MACD[-1] > Signal[-1]` |
| 5 | The cross happens **above zero**: `MACD[-1] > 0` |
| 6 | `Close[-1] > EMA(20)[-1]` |

Market data comes exclusively from the **Gate.io public v4 REST API** — no API
key, no TradingView, no third-party charting.

## The still-forming candle is never used

This is the single most important correctness property, and it is enforced twice:

1. **By timestamp.** The bar currently forming opens at
   `floor(now / 14400) * 14400`. Every candle with a timestamp at or after that
   is discarded, so the newest bar the scanner sees always opened one full
   interval earlier.
2. **By Gate's own flag.** Newer `/spot/candlesticks` responses append a
   `window_closed` field; any candle explicitly marked `"false"` is dropped even
   if its timestamp would have passed step 1.

On top of that, `STRICT_BAR_TS=1` (the default) requires the newest surviving
candle to match the expected bar timestamp exactly. If Gate is lagging for a
pair, that pair is skipped for this cycle rather than evaluated on stale data.

Indicators are computed so that the value at bar `i` depends only on closes
`0..i` — there is no look-ahead. `tests/test_indicators.py` asserts this
directly by comparing streamed prefixes against a batch computation.

## Layout

```
app/
  main.py         entry point: RUN_ONCE or the internal 4H scheduler
  scanner.py      the scan job - universe, filters, evaluation, dispatch
  gate.py         Gate.io v4 REST client (rate limit, concurrency, retries)
  indicators.py   EMA + MACD from closes, NumPy, no look-ahead
  mcap.py         market caps from Gate, with a CoinGecko fallback
  notify.py       Telegram delivery and message formatting
  store.py        SQLite: dedupe ledger + hit/scan history for the dashboard
  dashboard.py    the dashboard page (self-contained HTML/CSS/JS)
  config.py       every env var, parsed and validated once at boot
  health.py       HTTP server: dashboard, /api/state, /health
  timefmt.py      DISPLAY_TZ handling for messages, page and logs
tests/            unit tests + a full end-to-end test against a fake Gate API
Dockerfile        worker image, non-root, no exposed port
railway.toml      Railway build/deploy config
Procfile          `worker: python -m app.main`
```

## Market cap: read this before deploying

Gate's `/spot/currencies` endpoint **does not reliably expose a market cap
field**. The scanner therefore defaults to `MCAP_SOURCE=auto`:

- read any usable `market_cap` Gate does return, then
- fill every remaining symbol from CoinGecko's public `/coins/markets`
  (top `COINGECKO_PAGES × 250` coins, default 2,000).

A base asset with no market cap from either source is **skipped**, as specified.
Set `MCAP_SOURCE=gate` to use Gate only (expect almost everything to be
filtered out), or `MCAP_SOURCE=coingecko` to skip Gate's field entirely.

CoinGecko tickers are not unique. When several coins share a symbol the
**largest** market cap wins, which biases the `MIN_MCAP` screen toward
inclusion rather than silently dropping a legitimate large-cap coin. If you
want stricter matching, set a `COINGECKO_API_KEY` and raise `COINGECKO_PAGES`,
or pin the universe with `ONLY_PAIRS`.

## Leveraged-token exclusion

The default `LEVERAGED_REGEX` is `.+(?:3|4|5)(?:L|S)$`, which matches Gate's own
naming (`BTC3L`, `ETH5S`, `XRP4L`) and requires a real base prefix.

`BULL`, `BEAR`, `UP` and `DOWN` are **not** in the default on purpose: those
suffixes also end legitimate tickers such as **JUP** and **SYRUP**, and Gate
does not use that naming scheme. If you list a market that needs them, widen
the pattern with a minimum prefix length instead:

```
LEVERAGED_REGEX=.+(?:3|4|5)(?:L|S)$|.{3,}(?:BULL|BEAR|UP|DOWN)$
```

## Timezones

Every time a human reads — the Telegram message, the dashboard, the logs — is
rendered in `DISPLAY_TZ`, which defaults to `Asia/Kuala_Lumpur` (**GMT+8**).

This is **display only**. Scanning is always anchored to Gate.io's UTC candle
boundaries, because that is how the exchange closes its bars. Since GMT+8 is a
whole-hour offset, the same six closes simply read as clean local hours:

| UTC | GMT+8 |
|---|---|
| 00:00 | 08:00 |
| 04:00 | 12:00 |
| 08:00 | 16:00 |
| 12:00 | 20:00 |
| 16:00 | 00:00 (next day) |
| 20:00 | 04:00 (next day) |

Set `DISPLAY_TZ=UTC` to go back to UTC, or any IANA name (`America/New_York`,
`Europe/London`). Zones with DST and half-hour offsets are handled — the offset
is resolved at the instant being displayed, not at process start. An unknown
name logs a warning and falls back to UTC rather than crashing.

`requirements.txt` includes `tzdata` so zone lookups work in the slim image,
which does not ship a system timezone database.

## Scheduling

Railway has no native cron on every plan, so the worker schedules itself:

- On boot it scans the last closed bar immediately (`SCAN_ON_START=1`);
  deduplication makes a restart loop harmless.
- It then sleeps until the next interval close **+ `SCAN_BUFFER_SECONDS`**
  (default 90s) so Gate has settled the bar, scans, and repeats.
- Each loop logs a heartbeat with the next scan time and the dedupe row count.
- `SIGTERM` / `SIGINT` interrupt the sleep, so redeploys shut down promptly.

Set `RUN_ONCE=1` to perform exactly one scan and exit — use that if you later
attach a Railway Cron job (`0 */4 * * *`) or any external scheduler.

## Deduplication

`(pair, bar_ts)` is claimed in SQLite with `INSERT OR IGNORE` before the
message is sent, so the same pair can never be alerted twice for the same
candle even if two scans overlap. If delivery fails the claim is **released**
so the next scan retries it.

### Retention

Everything stored — the dedupe ledger, the hit detail behind the dashboard, and
the scan history — is kept for `RETENTION_DAYS`, which defaults to **3**.
Pruning runs at startup and after every scan, so the database stays roughly
18 bars deep and does not grow without bound. Set `RETENTION_DAYS=0` to keep
everything forever.

Three days is far more than dedupe needs (a 4H bar stops being relevant after
four hours); it exists so the dashboard has recent history to show.

The database lives at `$DATA_DIR/scanner.db`. If `DATA_DIR` is not writable
(no volume attached), the scanner logs a warning and falls back to
`/tmp/gate-scanner`, accepting that dedupe resets on restart.

## Deploy on Railway

The scanner is a **background worker**, not a web service. Do not generate a
domain for it.

1. **Push this repository to GitHub.**

2. **Create the service.** In Railway: *New Project → Deploy from GitHub repo*
   and pick this repo. `railway.toml` already selects the Dockerfile builder
   and `python -m app.main` as the start command, so no build settings are
   needed. If the repo lives in a subdirectory, set *Settings → Root Directory*
   to that folder.

3. **Set the variables** under *Variables*:

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | from [@BotFather](https://t.me/BotFather) |
   | `TELEGRAM_CHAT_ID` | your chat / channel id (negative for groups) |
   | `MIN_MCAP` | `10000000` |
   | `MIN_QUOTE_VOLUME_24H` | `100000` |
   | `LOG_LEVEL` | `INFO` |
   | `DATA_DIR` | `/data` |

   Every other knob in `.env.example` is optional and has a working default.

4. **Attach a volume** so dedupe survives restarts: *Settings → Volumes → Add
   Volume*, mount path `/data`. Without one the scanner still runs, but it may
   re-alert the current bar after a redeploy.

5. **Deploy and watch the logs.** You should see the startup banner, then
   `Universe: N pairs -> USDT quote ... (scanning M)` and a heartbeat line
   giving the next scan time.

### Optional: Railway Cron instead of the internal loop

If your plan offers Cron, set `RUN_ONCE=1`, leave the service with no replicas,
and configure the cron schedule `2 */4 * * *` (two minutes after each close).
The process performs one scan and exits with code 0.

### Running as a non-root user

The image runs as root so that the `/data` volume — which Railway mounts
root-owned — stays writable. A non-root process would fall back to
`/tmp/gate-scanner` and quietly lose dedupe across restarts. If your policy
requires a non-root container, add to the Dockerfile:

```dockerfile
RUN useradd --create-home --uid 10001 scanner && chown -R scanner:scanner /app /data
USER scanner
```

…and confirm in the logs that the store reports `/data/scanner.db` rather than
the `/tmp` fallback.

### The dashboard

When `PORT` is present (Railway injects it) or `ENABLE_HEALTH_SERVER=1`, a
daemon thread serves a web dashboard on `0.0.0.0:$PORT`. Generate a Railway
domain for the service to reach it.

| Route | What it returns |
|---|---|
| `/` | Dashboard: every hit with close/EMA/MACD/signal/mcap/volume, whether it was sent, the filter funnel, and scan history |
| `/api/state` | The same data as JSON |
| `/health` | Liveness probe: uptime, scans completed, last bar, last error |

The page is self-contained (no CDN, no build step), follows the viewer's
light/dark preference, works at phone width, and refreshes every 30s. It reads
from the same SQLite database on the volume, so history survives restarts.
Scanning always stays on the main thread — the server only reads.

Note the dashboard is **public** once a domain is generated. It exposes hits
and scan statistics, never the bot token or chat id.

## Getting your Telegram chat id

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Send any message to the bot (or add it to your group and post there).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read
   `result[].message.chat.id`. Group and channel ids are negative.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # fill in the Telegram values

# one scan, nothing sent
RUN_ONCE=1 DRY_RUN=1 DATA_DIR=./data LOG_LEVEL=DEBUG python -m app.main

# one scan against a couple of pairs only
RUN_ONCE=1 DRY_RUN=1 DATA_DIR=./data ONLY_PAIRS=BTC_USDT,ETH_USDT python -m app.main
```

With Docker:

```bash
docker build -t gate-scanner .
docker run --rm --env-file .env -e RUN_ONCE=1 -e DATA_DIR=/tmp gate-scanner
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite covers the EMA/MACD math (including an explicit no-look-ahead check),
bar-boundary arithmetic, forming-candle exclusion, leveraged-token matching,
both candlestick response shapes, dedupe semantics, and a full end-to-end scan
against a local stand-in for the Gate.io and Telegram APIs — universe filtering,
429 backoff, alert formatting and duplicate suppression included.

## Configuration reference

See `.env.example` — every variable is listed there with its default and a
comment. The ones you are most likely to touch:

| Variable | Default | Meaning |
|---|---|---|
| `MIN_MCAP` | `10000000` | Minimum base-asset market cap in USD |
| `MIN_QUOTE_VOLUME_24H` | `100000` | Minimum 24h quote volume from `/spot/tickers` |
| `ENABLE_VOLUME_FILTER` | `1` | Set `0` to ignore the volume filter |
| `MACD_FAST` / `MACD_SLOW` / `MACD_SIGNAL` | `12` / `26` / `9` | MACD lengths |
| `EMA_LEN` | `20` | EMA length for condition 6 |
| `INTERVAL` | `4h` | Any Gate interval (`1h`, `4h`, `1d`, …) |
| `SCAN_BUFFER_SECONDS` | `90` | Delay after close before scanning |
| `CONCURRENCY` | `8` | Parallel Gate requests |
| `REQUEST_INTERVAL` | `0.06` | Minimum seconds between Gate requests |
| `MAX_ALERTS_PER_SCAN` | `40` | Guard against an alert storm |
| `DRY_RUN` | `0` | Compute and log alerts without sending |
| `RETENTION_DAYS` | `3` | Days of results to keep; `0` keeps everything |
| `DISPLAY_TZ` | `Asia/Kuala_Lumpur` | Timezone for displayed times (GMT+8); does not move the schedule |

## Notes and limits

- Gate's public spot endpoints need no API key. The client caps concurrency,
  spaces requests with a shared rate limiter, and retries `408/429/5xx` with
  jittered exponential backoff that honours `Retry-After`.
- EMA seeding follows the TA-Lib / pandas-ta convention: the first EMA value is
  the SMA of the first `length` closes. With `CANDLE_LIMIT=80` the warm-up
  difference against other seeding choices is negligible by the newest bar.
- A pair with fewer than `MACD_SLOW + MACD_SIGNAL + 10` closed bars is skipped;
  newly listed coins simply do not qualify yet.
- Alerts are sorted by 24h quote volume before `MAX_ALERTS_PER_SCAN` is applied,
  so the most liquid hits survive the cap.
