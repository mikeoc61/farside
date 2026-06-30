# farside-btc

Command-line fetcher for U.S. spot **Bitcoin ETF net flows**, scraped from
[Farside Investors](https://farside.co.uk/btc/). Emits a terminal briefing by
default, or structured JSON for piping into other tools.

It handles the parts that make scraping Farside annoying: TLS/JA3 fingerprinting
(via Chrome impersonation), schema-tolerant table parsing, on-disk caching, and
graceful stale-fallback when the upstream fetch fails.

---

## Features

- **Single-file, zero-config.** One Python script with inline [PEP 723](https://peps.python.org/pep-0723/)
  dependencies — run it directly with `uv` and nothing to install.
- **Bot-mitigation aware.** Uses `curl_cffi` with `impersonate="chrome"` to pass
  TLS fingerprint checks; falls back to a warmed `requests` session.
- **Schema-tolerant parser.** Locates the flow table by detecting date rows and
  maps columns by header name (`IBIT`, `FBTC`, `ARKB`, `GBTC`, `Total`) rather
  than fixed positions, so column reordering won't silently break it.
- **Derived metrics.** Rolling N-day net flow, inflow/outflow streak length, and
  an IBIT-share "conviction vs. breadth" classifier.
- **Caching + stale-fallback.** Caches the last good payload to
  `~/.openclaw/cache/farside_btc.json`; on fetch failure it returns the cached
  payload flagged `stale` instead of crashing.

---

## Install

### Option A — uv (recommended)

Requires [uv](https://docs.astral.sh/uv/) to be installed (one-time):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

The script declares its own Python dependencies inline ([PEP 723](https://peps.python.org/pep-0723/)),
so there's nothing else to install — uv resolves them into an ephemeral
environment on first run. The shebang (`#!/usr/bin/env -S uv run --script`)
routes execution through uv, so you can run the file directly:

```bash
chmod +x farside_btc.py   # once, if the exec bit was lost
./farside_btc.py
```

…or invoke uv explicitly (no exec bit needed):

```bash
uv run farside_btc.py
```

### Option B — pip

```bash
pip install -r requirements.txt
python3 farside_btc.py
```

Requires Python ≥ 3.11.

---

## Usage

```bash
# Human-readable briefing block
./farside_btc.py

# Full structured detail
./farside_btc.py --json
```

### Example — default output

```
BTC ETF flows (Farside, as of 26 Jun 2026):
  latest: -444.5m total | -444.5m IBIT
  5d net: -1719.0m total | -1131.5m IBIT
  streak: 3d outflow
```

### Example — `--json`

```json
{
  "fetched_at": "2026-06-29T00:00:00+00:00",
  "stale": false,
  "summary": {
    "as_of": "26 Jun 2026",
    "age_days": 3,
    "pending_today": false,
    "latest_total": -444.5,
    "latest_ibit": -444.5,
    "window": 5,
    "window_total": -1719.0,
    "window_ibit": -1131.5,
    "streak_days": 3,
    "streak_sign": "outflow"
  },
  "rows": [ /* last `window` reported days, per-ETF */ ],
  "line": "ETF Flows: -444.5M (26 Jun) | 5d -1.72B | IBIT -1.13B (66%) | 3d outflow — conviction distribution"
}
```

All flow values are in **US$ millions**. Negative = net outflow.

---

## Output schema

`--json` returns a single object:

| Field         | Type            | Notes                                                        |
| ------------- | --------------- | ------------------------------------------------------------ |
| `fetched_at`  | ISO-8601 string | UTC time of the fetch                                        |
| `stale`       | bool            | `true` if served from cache after a fetch failure           |
| `error`       | string          | Present only when `stale` — the underlying exception         |
| `summary`     | object          | Derived metrics (see below)                                  |
| `rows`        | array           | Last `window` reported days, each with per-ETF flows         |
| `line`        | string          | One-line briefing with conviction/breadth tag                |

`summary` fields:

| Field           | Notes                                                              |
| --------------- | ----------------------------------------------------------------- |
| `as_of`         | Date of the latest *reported* day                                 |
| `age_days`      | Days since `as_of` (UTC)                                           |
| `pending_today` | `true` if the newest row exists but has no flows reported yet     |
| `latest_total`  | Most recent day's total net flow (US$m)                           |
| `latest_ibit`   | Most recent day's IBIT net flow (US$m)                            |
| `window`        | Rolling window length (default 5)                                 |
| `window_total`  | Net total flow over the window                                    |
| `window_ibit`   | Net IBIT flow over the window                                     |
| `streak_days`   | Consecutive same-sign total-flow days                             |
| `streak_sign`   | `inflow` \| `outflow` \| `flat`                                   |

The `line` classifier tags window flow as *conviction* vs *broad* based on
whether IBIT accounts for ≥60% of the same-signed window total.

---

## How it works

1. **Fetch** (`fetch_html`) — `curl_cffi` Chrome impersonation; falls back to a
   `requests.Session` that first warms `farside.co.uk/` to pick up cookies.
2. **Parse** (`parse_table`) — finds the first table whose rows start with a
   `D MMM YYYY` date, builds a header→column-index map, and reads per-ETF flows.
3. **Summarize** (`summarize`) — computes window nets, streak, and age.
4. **Cache** (`save_cache` / `load_cache`) — writes the payload; on any fetch or
   parse error, `get_flows` returns the last cached payload flagged `stale`.

---

## Caching

Last good payload is written to:

```
~/.openclaw/cache/farside_btc.json
```

Delete it to force a clean state. The cache is what backs stale-fallback when
Farside is unreachable or changes its layout.

---

## Scheduling (optional)

Run it on a cron for a daily flow snapshot:

```cron
# 7:00 AM local, weekdays
0 7 * * 1-5  /path/to/farside_btc.py >> ~/btc_flows.log 2>&1
```

---

## Disclaimer

Data is scraped from Farside Investors and provided **as is**, with no guarantee
of accuracy, completeness, or timeliness. Farside generates its table
automatically and disclaims liability for errors. This tool is for
informational purposes only and is **not** investment advice. Respect Farside's
terms of use and don't hammer their site.

---

## License

[MIT](LICENSE) © 2026 Michael OConnor
