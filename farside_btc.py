#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["curl_cffi", "beautifulsoup4", "requests"]
# ///
"""Fetch and summarize U.S. spot Bitcoin ETF net flows from Farside Investors.

Scrapes the daily flow table at https://farside.co.uk/btc/, parses the per-ETF
net flows (US$ millions), and derives a few summary metrics: a rolling N-day
net, an inflow/outflow streak, and an IBIT-share "conviction vs. breadth" tag.

Design notes
------------
* Fetch uses ``curl_cffi`` with Chrome TLS impersonation to pass the site's
  bot-mitigation fingerprint checks, falling back to a warmed ``requests``
  session if ``curl_cffi`` is unavailable.
* Parsing is schema-tolerant: the flow table is located by detecting date rows
  and columns are mapped by header name (IBIT/FBTC/ARKB/GBTC/Total) rather than
  fixed index, so upstream column reordering won't silently corrupt output.
* The last good payload is cached to ``~/.openclaw/cache/farside_btc.json``; on
  any fetch/parse failure the cached payload is returned flagged ``stale``.

CLI
---
    farside_btc.py            # human-readable briefing block
    farside_btc.py --json     # full structured payload as JSON

All monetary values are in US$ millions; negative denotes net outflow.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

URL = "https://farside.co.uk/btc/"
CACHE = Path.home() / ".openclaw" / "cache" / "farside_btc.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
DATE_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$")
WANT = ("IBIT", "FBTC", "ARKB", "GBTC", "Total")


def parse_flow(s):
    """Parse a single flow cell into a float (US$ millions).

    Handles the table's formatting quirks: thousands separators, en-dashes used
    as minus signs, blank/``-`` cells (treated as 0.0), and accounting-style
    parentheses for negatives, e.g. ``"(444.5)"`` -> ``-444.5``.

    Args:
        s: Raw cell text.

    Returns:
        The parsed value, ``0.0`` for empty/placeholder cells, or ``None`` if
        the text is non-numeric and cannot be parsed.
    """
    s = s.strip().replace(",", "").replace("\u2013", "-")
    if s in ("", "-"):
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def fetch_html(url=URL, timeout=20):
    """Fetch the raw HTML of the flow page.

    Prefers ``curl_cffi`` with ``impersonate="chrome"`` so the TLS/JA3
    fingerprint matches a real browser and passes the site's bot mitigation.
    If ``curl_cffi`` is not installed, falls back to a ``requests`` session that
    first warms the site root to pick up cookies before requesting ``url``.

    Args:
        url: Page to fetch. Defaults to the BTC flow table URL.
        timeout: Per-request timeout in seconds.

    Returns:
        The response body as text.

    Raises:
        Exception: Propagates any network/HTTP error (e.g. ``raise_for_status``)
            so the caller can fall back to cache.
    """
    try:
        from curl_cffi import requests as creq
    except ImportError:
        creq = None
    if creq is not None:
        r = creq.get(url, headers=HEADERS, timeout=timeout, impersonate="chrome")
        r.raise_for_status()
        return r.text
    import requests
    sess = requests.Session()
    sess.headers.update(HEADERS)
    sess.get("https://farside.co.uk/", timeout=timeout)
    r = sess.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_table(html):
    """Extract daily per-ETF flows from the page HTML.

    Scans every ``<table>`` and selects the first one that looks like the flow
    table: it must contain rows whose first cell matches ``D MMM YYYY`` and a
    header region mapping at least the ``IBIT`` and ``Total`` columns. Columns
    are resolved by header name (see ``WANT``) rather than position, so the
    parser tolerates added/reordered columns.

    Args:
        html: Raw page HTML.

    Returns:
        A list of per-day dicts in document order, each shaped as
        ``{"date": "26 Jun 2026", "IBIT": -444.5, ..., "Total": -444.5}``.
        Missing cells for a wanted column are ``None``.

    Raises:
        ValueError: If no table matching the expected schema is found (e.g. the
            site layout changed).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = [
            [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")
        ]
        first_data = next(
            (i for i, c in enumerate(rows) if c and DATE_RE.match(c[0])), None
        )
        if first_data is None:
            continue
        colmap = {}
        for cells in rows[:first_data]:
            for idx, text in enumerate(cells):
                if text in WANT and text not in colmap:
                    colmap[text] = idx
        if "IBIT" not in colmap or "Total" not in colmap:
            continue
        data = []
        for cells in rows[first_data:]:
            if cells and DATE_RE.match(cells[0]):
                rec = {"date": cells[0]}
                for name in WANT:
                    idx = colmap.get(name)
                    rec[name] = (
                        parse_flow(cells[idx])
                        if idx is not None and idx < len(cells)
                        else None
                    )
                data.append(rec)
        if data:
            return data
    raise ValueError("flow table not found or schema changed")


def _age_days(date_str):
    """Return whole days elapsed (UTC) since ``date_str`` (``"D MMM YYYY"``).

    Returns ``None`` if the date string cannot be parsed.
    """
    try:
        d = datetime.strptime(date_str, "%d %b %Y").date()
        return (datetime.now(timezone.utc).date() - d).days
    except ValueError:
        return None


def _reported(data):
    """Filter to days that actually have flow data.

    Drops trailing placeholder rows the site lists before numbers are published
    (every wanted column ``None``/``0.0``), so summaries reflect real activity.
    """
    return [
        r for r in data
        if any(r.get(k) not in (None, 0.0) for k in WANT)
    ]


def summarize(data, window=5):
    """Compute summary metrics over the parsed daily rows.

    Args:
        data: Per-day rows as returned by :func:`parse_table` (document order).
        window: Number of most-recent reported days for the rolling net.

    Returns:
        A dict with: ``as_of`` (latest reported date), ``age_days``,
        ``pending_today`` (newest row exists but has no flows yet),
        ``latest_total``/``latest_ibit``, ``window`` and the windowed
        ``window_total``/``window_ibit`` nets, plus ``streak_days`` and
        ``streak_sign`` (``inflow``/``outflow``/``flat``) for the run of
        consecutive same-sign Total days. Fields are ``None``/zero when no day
        has reported data yet.
    """
    reported = _reported(data)
    pending = bool(data) and bool(reported) and data[-1] is not reported[-1]
    if not reported:
        return {
            "as_of": None,
            "age_days": None,
            "pending_today": pending,
            "latest_total": None,
            "latest_ibit": None,
            "window": window,
            "window_total": 0.0,
            "window_ibit": 0.0,
            "streak_days": 0,
            "streak_sign": "flat",
        }
    recent = reported[-window:]
    latest = reported[-1]
    total_w = sum(r["Total"] for r in recent if r["Total"] is not None)
    ibit_w = sum(r["IBIT"] for r in recent if r["IBIT"] is not None)
    sign = None
    streak = 0
    for r in reversed(reported):
        v = r["Total"]
        if v is None:
            break
        s = 1 if v > 0 else (-1 if v < 0 else 0)
        if sign is None:
            sign, streak = s, 1
        elif s == sign and s != 0:
            streak += 1
        else:
            break
    return {
        "as_of": latest["date"],
        "age_days": _age_days(latest["date"]),
        "pending_today": pending,
        "latest_total": latest["Total"],
        "latest_ibit": latest["IBIT"],
        "window": window,
        "window_total": round(total_w, 1),
        "window_ibit": round(ibit_w, 1),
        "streak_days": streak,
        "streak_sign": (
            "inflow" if sign and sign > 0
            else "outflow" if sign and sign < 0
            else "flat"
        ),
    }


def load_cache():
    """Load the last cached payload, or ``None`` if missing/unreadable."""
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return None


def save_cache(payload):
    """Write ``payload`` to the cache file as pretty JSON, creating dirs."""
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(payload, indent=2))


def get_flows(window=5):
    """Fetch, parse, summarize, and cache the latest flows.

    On success, builds a payload (``fetched_at``, ``stale=False``, ``summary``,
    the last ``window`` reported ``rows``, and a one-line ``line``), writes it to
    cache, and returns it. On any failure, returns the cached payload with
    ``stale=True`` and an ``error`` field; re-raises only if no cache exists.

    Args:
        window: Rolling window length passed to :func:`summarize`.

    Returns:
        The flow payload dict (fresh or stale-from-cache).
    """
    try:
        data = parse_table(fetch_html())
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "stale": False,
            "summary": summarize(data, window),
            "rows": _reported(data)[-window:],
        }
        payload["line"] = briefing_line(payload)
        save_cache(payload)
        return payload
    except Exception as e:
        cached = load_cache()
        if cached is None:
            raise
        cached["stale"] = True
        cached["error"] = str(e)
        return cached


def _fmt(v):
    """Format a value (US$m) with an explicit sign, e.g. ``+57.7``/``-444.5``.

    Returns ``"n/a"`` for ``None``.
    """
    if v is None:
        return "n/a"
    return f"+{v:.1f}" if v >= 0 else f"{v:.1f}"


def briefing_block(payload):
    """Render the default multi-line terminal briefing for a payload.

    Includes latest-day and rolling-window totals plus the streak, with inline
    flags appended when relevant: ``FETCH-FAILED`` (stale cache),
    ``TODAY-PENDING`` (newest day unreported), and ``DATA-Nd-OLD`` (latest data
    older than 4 days).

    Returns:
        A formatted multi-line string.
    """
    s = payload["summary"]
    flags = []
    if payload.get("stale"):
        flags.append("FETCH-FAILED")
    if s.get("pending_today"):
        flags.append("TODAY-PENDING")
    if s.get("age_days") is not None and s["age_days"] > 4:
        flags.append(f"DATA-{s['age_days']}D-OLD")
    tag = f" [{', '.join(flags)}]" if flags else ""
    return "\n".join([
        f"BTC ETF flows (Farside, as of {s['as_of']}){tag}:",
        f"  latest: {_fmt(s['latest_total'])}m total | {_fmt(s['latest_ibit'])}m IBIT",
        f"  {s['window']}d net: {_fmt(s['window_total'])}m total | {_fmt(s['window_ibit'])}m IBIT",
        f"  streak: {s['streak_days']}d {s['streak_sign']}",
    ])


def _abbr(v):
    """Abbreviate a US$m value for the compact one-liner.

    Scales magnitudes >= 1000 to billions with a sign (``-1.72B``); otherwise
    shows signed millions (``-444.5M``). Returns ``"n/a"`` for ``None``.
    """
    if v is None:
        return "n/a"
    if abs(v) >= 1000:
        return f"{v / 1000:+.2f}B"
    return f"{v:+.1f}M"


def briefing_line(payload):
    """Render the compact single-line summary stored as ``payload["line"]``.

    Combines latest total, windowed net, IBIT net and its share of the window,
    and the streak, then tags the regime by direction and IBIT concentration:
    ``conviction accumulation``/``distribution`` when IBIT is >= 60% of the
    same-signed window net, otherwise ``broad inflow``/``outflow`` (or
    ``mixed flows`` when flat). Appends ``today pending``/``data stale`` notes.

    Returns:
        A one-line summary string.
    """
    s = payload["summary"]
    if s["as_of"] is None:
        return "ETF Flows: n/a"
    wt, wi = s["window_total"], s["window_ibit"]
    share_txt, share_val = "", None
    if wt and wi is not None and (wt > 0) == (wi > 0):
        share_val = round(100 * wi / wt)
        share_txt = f" ({share_val}%)"
    if wt < 0:
        tag = "conviction distribution" if share_val and share_val >= 60 else "broad outflow"
    elif wt > 0:
        tag = "conviction accumulation" if share_val and share_val >= 60 else "broad inflow"
    else:
        tag = "mixed flows"
    asof_short = " ".join(s["as_of"].split()[:2])
    line = (
        f"ETF Flows: {_abbr(s['latest_total'])} ({asof_short}) | "
        f"{s['window']}d {_abbr(wt)} | IBIT {_abbr(wi)}{share_txt} | "
        f"{s['streak_days']}d {s['streak_sign']} — {tag}"
    )
    notes = []
    if s.get("pending_today"):
        notes.append("today pending")
    if payload.get("stale"):
        notes.append("data stale")
    if notes:
        line += "; " + "; ".join(notes)
    return line


if __name__ == "__main__":
    out = get_flows()
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2))
    else:
        print(briefing_block(out))
