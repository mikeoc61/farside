#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["curl_cffi", "beautifulsoup4", "requests"]
# ///
"""Fetch and summarize U.S. spot crypto ETF net flows from Farside Investors.

Supports the Bitcoin, Ethereum, and Solana flow tables (``btc`` is the default).
Scrapes the daily per-fund net flows (US$ millions) for the chosen asset and
derives a few summary metrics: rolling 5/20/60-day nets, an inflow/outflow
streak, and a "conviction vs. breadth" tag based on the flagship ("lead")
fund's share.

Design notes
------------
* Fetch uses ``curl_cffi`` with Chrome TLS impersonation to pass the site's
  bot-mitigation fingerprint checks, falling back to a warmed ``requests``
  session if ``curl_cffi`` is unavailable.
* History comes from each asset's "all data" page where one exists; the shorter
  nav page (~3 weeks) is the fallback. Windows longer than the available history
  report as uncovered rather than quietly netting fewer days.
* Parsing is schema-tolerant: the flow table is located by detecting date rows
  and columns are mapped by header name (per-asset tickers + ``Total``) rather
  than fixed index, so upstream column reordering won't silently corrupt output.
* Each asset caches to its own ``~/.openclaw/cache/farside_<asset>.json``; on any
  fetch/parse failure the cached payload is returned flagged ``stale``.

Assets (lead fund)
------------------
    btc -> IBIT      eth -> ETHA      sol -> BSOL

CLI
---
    farside_flows.py             # BTC briefing block (default asset)
    farside_flows.py eth         # Ethereum briefing block
    farside_flows.py sol --json  # Solana, full payload as JSON

All monetary values are in US$ millions; negative denotes net outflow.
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

# Per-asset config: the nav page URL, an optional full-history ("all data") page,
# the flagship ("lead") fund used for the share/streak metrics, and the curated
# set of funds tracked in each row (lead listed first).
#
# The nav pages (``url``) only publish a rolling ~3-week window — roughly 13
# fully-reported days — which covers the 5-day net but not the longer ones.
# Farside also publishes a full-history table per asset at ``all_data`` (BTC back
# to Jan 2024, ETH to Jul 2024); those pages use the identical table schema, so
# the same parser handles both. Solana has no all-data page, so ``sol`` is capped
# at whatever the nav page carries and its long windows report as uncovered.
ASSETS = {
    "btc": {
        "url": "https://farside.co.uk/btc/",
        "all_data": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
        "lead": "IBIT",
        "funds": ("IBIT", "FBTC", "ARKB", "GBTC"),
    },
    "eth": {
        "url": "https://farside.co.uk/eth/",
        "all_data": "https://farside.co.uk/ethereum-etf-flow-all-data/",
        "lead": "ETHA",
        "funds": ("ETHA", "FETH", "ETHW", "ETHE"),
    },
    "sol": {
        "url": "https://farside.co.uk/sol/",
        "all_data": None,
        "lead": "BSOL",
        "funds": ("BSOL", "FSOL", "VSOL", "GSOL"),
    },
}
DEFAULT_ASSET = "btc"
# Rolling windows (in fully-reported days) reported by :func:`summarize`. The
# first is the *primary* window: it drives the flat ``window_*`` summary keys and
# the one-liner's share/regime classification.
DEFAULT_WINDOWS = (5, 20, 60)
# Reported days included in the payload's ``rows`` — independent of the windows,
# so asking for a 60-day net doesn't dump 60 rows into the cache.
DEFAULT_ROWS = 5
CACHE_DIR = Path.home() / ".openclaw" / "cache"
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


def want_cols(cfg):
    """Column names to extract for an asset: its tracked funds plus ``Total``."""
    return (*cfg["funds"], "Total")


def parse_flow(s):
    """Parse a single flow cell into a float (US$ millions).

    Handles the table's formatting quirks: thousands separators, en-dashes used
    as minus signs, and accounting-style parentheses for negatives, e.g.
    ``"(444.5)"`` -> ``-444.5``.

    Farside distinguishes a *reported zero* flow (rendered ``"0.0"``) from a
    *not-yet-reported* cell (rendered blank or ``"-"``). We preserve that
    distinction: blank/``-`` cells return ``None`` (missing) rather than being
    coerced to ``0.0``, so a pending fund is never silently counted as a zero.

    Args:
        s: Raw cell text.

    Returns:
        The parsed value, ``None`` for blank/``-`` (not reported) or
        non-numeric cells, and ``0.0`` only for an explicit ``"0"``/``"0.0"``.
    """
    s = s.strip().replace(",", "").replace("\u2013", "-")
    if s in ("", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def fetch_html(url, timeout=20):
    """Fetch the raw HTML of the flow page.

    Prefers ``curl_cffi`` with ``impersonate="chrome"`` so the TLS/JA3
    fingerprint matches a real browser and passes the site's bot mitigation.
    If ``curl_cffi`` is not installed, falls back to a ``requests`` session that
    first warms the site root to pick up cookies before requesting ``url``.

    Args:
        url: Page to fetch (an asset's flow-table URL).
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


def fetch_table(cfg, timeout=20):
    """Fetch and parse an asset's flow table, preferring full history.

    Tries the asset's ``all_data`` page first (hundreds of days, needed for the
    longer windows) and falls back to the shorter nav page (``url``) if it is
    absent, 404s, or fails to parse. Assets with no all-data page (``sol``) go
    straight to the nav page.

    Args:
        cfg: Asset config (``url``/``all_data``/``lead``/``funds``/``asset``).
        timeout: Per-request timeout in seconds.

    Returns:
        ``(rows, source_url)`` — the parsed per-day rows and the page they came
        from, so the payload can record which table was actually used.

    Raises:
        ValueError: If every candidate page failed; the message lists each
            page's error so a site change is diagnosable from the cached
            payload's ``error`` field.
    """
    errors = []
    for url in (cfg.get("all_data"), cfg["url"]):
        if not url:
            continue
        try:
            return parse_table(fetch_html(url, timeout), cfg), url
        except Exception as e:
            errors.append(f"{url}: {e}")
    raise ValueError("; ".join(errors) or "no source URL configured")


def parse_table(html, cfg):
    """Extract daily per-fund flows from the page HTML for one asset.

    Scans every ``<table>`` and selects the first one that looks like the flow
    table: it must contain rows whose first cell matches ``D MMM YYYY`` and a
    header region mapping at least the asset's lead fund and the ``Total``
    column. Columns are resolved by header name (``want_cols(cfg)``) rather than
    position, so the parser tolerates added/reordered columns.

    Args:
        html: Raw page HTML.
        cfg: Asset config (``url``/``lead``/``funds``/``asset``).

    Returns:
        A list of per-day dicts in document order, each shaped as
        ``{"date": "26 Jun 2026", <lead>: -444.5, ..., "Total": -444.5}``.
        Missing cells for a wanted column are ``None``.

    Raises:
        ValueError: If no table matching the expected schema is found (e.g. the
            site layout changed).
    """
    from bs4 import BeautifulSoup

    want = want_cols(cfg)
    lead = cfg["lead"]
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        # Farside opens ``<tbody>`` with a stray empty ``<tr>``, so the parser
        # nests the real rows inside it. That wrapper reports every descendant
        # cell -- thousands of them -- and its leading cells are the first
        # row's, yielding a duplicate of the asset's launch day that inflates
        # ``days_complete`` and can be double-counted by a window. Keep only
        # innermost rows.
        rows = [
            [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")
            if tr.find("tr") is None
        ]
        first_data = next(
            (i for i, c in enumerate(rows) if c and DATE_RE.match(c[0])), None
        )
        if first_data is None:
            continue
        colmap = {}
        for cells in rows[:first_data]:
            for idx, text in enumerate(cells):
                if text in want and text not in colmap:
                    colmap[text] = idx
        if lead not in colmap or "Total" not in colmap:
            continue
        data = []
        for cells in rows[first_data:]:
            if cells and DATE_RE.match(cells[0]):
                rec = {"date": cells[0]}
                for name in want:
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


def _reported(data, funds):
    """Filter to days that actually reported.

    Two kinds of row are not days and must go: U.S. market closures, and the
    current day before its numbers are published. Left in, either becomes the
    newest reported day and ``partial`` announces an in-progress session -- on a
    holiday, one that never happened.

    They are the same shape. Both print every fund blank with ``Total`` rendered
    ``0.0``.

    The site did stop listing closures: the BTC history holds 16 (MLK through
    the Jan 2025 day of mourning), the last on 19 Jun 2025, and of the 11 U.S.
    market holidays since then it lists none. The *shape* did not stop -- on
    03 Sep 2026 all three assets carried one, an ordinary Thursday whose flows
    had not posted yet. Both are true, and reading either fact as the other is
    the mistake: one shape, two meanings, nothing in the row to say which.

    That is *why* this tests fund blankness alone and never consults ``Total``:
    it does not need to tell them apart, only to know that neither is a session
    with data.

    Blankness, never a value of ``0.0``. That distinction is the one
    :func:`parse_flow` goes out of its way to preserve: ``0.0`` is a reported
    zero, blank/``-`` is not reported. Testing ``Total`` for ``0.0`` instead
    would be a cheaper proxy and a wrong one -- it discards real sessions on
    which every tracked fund genuinely printed ``0.0``, of which the ETH history
    has twelve (05 Nov 2024 and 14 Aug 2026 among them, all ordinary trading
    days).

    A day on which only untracked funds moved is dropped too -- a deliberate
    choice, and the one point where a Total-consulting predicate would differ.
    Not a limitation: ``_partial`` carries ``other``, and :func:`briefing_line`
    already sums it, so such a row would render as "all four pending, +42.0M
    ex-tracked". It is dropped because a day on which no tracked fund posted
    says nothing about the funds this asset follows. No such row occurs in any
    of the three histories, so this decides a hypothetical -- revisit it on
    evidence, not on symmetry.

    Args:
        data: Per-day rows from :func:`parse_table`.
        funds: The asset's tracked fund tickers (``cfg["funds"]``) -- not
            ``want_cols``, whose ``Total`` is exactly the column a closure row
            fills in.
    """
    return [r for r in data if any(r.get(k) is not None for k in funds)]


def _other(row, funds):
    """Net flow of funds inside Farside's ``Total`` that we don't itemize.

    Farside's ``Total`` column sums *every* listed ETF, but each asset tracks
    only a curated subset (``cfg["funds"]``). ``Other`` is the residual —
    ``Total`` minus the tracked funds present in the row — i.e. the aggregate
    of the untracked funds. By construction ``sum(tracked present) + Other ==
    Total`` for the row, so the per-fund breakdown reconciles to ``Total``.

    Returns ``None`` if the row has no ``Total``.
    """
    total = row.get("Total")
    if total is None:
        return None
    tracked = sum(row[k] for k in funds if row.get(k) is not None)
    return round(total - tracked, 1)


def _with_other(row, funds):
    """Return an ordered copy of ``row`` with a derived ``Other`` field.

    ``Other`` (see :func:`_other`) is inserted just before ``Total`` so the
    emitted row reads funds → Other → Total and satisfies
    ``sum(tracked present) + Other == Total``.
    """
    out = {"date": row["date"]}
    for k in funds:
        out[k] = row.get(k)
    out["Other"] = _other(row, funds)
    out["Total"] = row.get("Total")
    return out


def _partial(row, funds):
    """Summarize a partially-reported latest day (not all funds posted yet).

    Farside posts funds (and a provisional ``Total``) progressively through the
    day; while any tracked fund is still blank the day's ``Total`` is
    incomplete and its direction is indeterminate. This captures what is known —
    the net of the tracked funds that have reported (``reported_total``), the
    untracked residual (``other``; see :func:`_other`), and which tracked funds
    are outstanding. Note ``reported_total + other == Total`` for the day, so
    the pending value is *only* the flagship's — untracked flow is not mistaken
    for it.

    Args:
        row: The most-recent reported row.
        funds: The tracked fund tickers for the asset (``cfg["funds"]``).

    Returns:
        ``{"date", "reported_total", "other", "reported": [...],
        "pending": [...]}``.
    """
    have = [k for k in funds if row.get(k) is not None]
    return {
        "date": row["date"],
        "reported_total": round(sum(row[k] for k in have), 1),
        "other": _other(row, funds),
        "reported": have,
        "pending": [k for k in funds if row.get(k) is None],
    }


def _windows(windows):
    """Normalize the ``windows`` argument to a tuple of ints.

    Accepts a bare int (legacy single-window callers) or any iterable of ints.
    """
    if isinstance(windows, int):
        return (windows,)
    return tuple(windows)


def _window_net(complete, lead, days):
    """Net flows over the most recent ``days`` fully-reported rows.

    Because the nets are only meaningful when the full span is present, this
    reports coverage explicitly rather than silently returning a shorter
    window's net under a longer window's label: ``covered`` is ``False`` and
    ``total``/``lead`` are ``None`` when fewer than ``days`` complete days exist
    (the case for ``sol``, and for any asset in its first weeks of trading).
    ``days_available`` always reports how many days actually backed the slice.

    Args:
        complete: Fully-reported rows in document order (oldest first).
        lead: The asset's flagship ticker.
        days: Requested window length.

    Returns:
        ``{"days", "days_available", "covered", "total", "lead", "dates"}``.
    """
    recent = complete[-days:] if days > 0 else []
    covered = len(recent) == days
    return {
        "days": days,
        "days_available": len(recent),
        "covered": covered,
        # No ``is not None`` filter on either sum: ``complete`` is where that
        # invariant lives (see :func:`summarize`), and every row here has a
        # published ``Total`` and every tracked fund in. Skipping a ``None``
        # here would not avert a crash, it would quietly return a short sum
        # under a full window's label -- exactly what ``covered`` exists to
        # prevent.
        "total": round(sum(r["Total"] for r in recent), 1) if covered else None,
        "lead": round(sum(r[lead] for r in recent), 1) if covered else None,
        "dates": [r["date"] for r in recent],
    }


def summarize(data, cfg, windows=DEFAULT_WINDOWS):
    """Compute summary metrics over the parsed daily rows.

    Args:
        data: Per-day rows as returned by :func:`parse_table` (document order).
        cfg: Asset config (provides ``asset`` and the ``lead`` fund).
        windows: Rolling window lengths in fully-reported days. A bare int is
            accepted for a single window. The first entry is the *primary*
            window and populates the flat ``window_*`` keys.

    Returns:
        A dict with: ``asset`` and ``lead`` (the flagship ticker), ``as_of``
        (latest *fully-reported* date), ``age_days``, ``pending_today`` (newest
        row exists but has no flows yet), ``partial_pending`` (newest reported
        day has some but not all tracked funds in), ``partial`` (summary of that
        in-progress day, else ``None``), ``latest_total``/``latest_lead``,
        ``days_complete`` (how many fully-reported days the source supplied, the
        ceiling on window coverage), and ``windows`` — a list of per-window nets
        (see :func:`_window_net`), each carrying its own ``dates`` and a
        ``covered`` flag that is ``False`` when the source has too little
        history to fill it.

        For compatibility the primary window is also mirrored onto the flat
        ``window``/``window_dates``/``window_total``/``window_lead`` keys; note
        ``window_dates`` can differ from the ``rows`` payload, which lists the
        most recent *reported* days incl. any partial one.

        Also ``streak_days`` and ``streak_sign``
        (``inflow``/``outflow``/``flat``) for the run of consecutive same-sign
        Total days. All latest/streak/window metrics are computed over
        fully-reported days only (every tracked fund posted *and* a published
        ``Total``); they are ``None``/zero when no such day exists yet. A day
        the site never published a ``Total`` for is absent from every one of
        them rather than counted as a zero.
    """
    windows = _windows(windows)
    lead = cfg["lead"]
    funds = cfg["funds"]
    reported = _reported(data, funds)
    pending = bool(data) and bool(reported) and data[-1] is not reported[-1]
    # A day can be partially reported: funds (and a provisional Total) post
    # progressively, so a day's Total and direction are indeterminate until
    # every tracked fund is in. Gate day-completeness on all tracked funds
    # having reported, and compute every latest/streak/window/direction metric
    # over fully-reported days only. The in-progress day, if any, is surfaced
    # separately via ``partial``.
    #
    # ``Total`` is required too, and not merely because it is another column:
    # every figure below is on Farside's ``Total`` basis, so a day the site
    # never published one for cannot contribute to any of them. Admitting it
    # would let a window count it toward ``covered`` while summing it as
    # nothing -- a short sum wearing a longer window's label.
    complete = [
        r for r in reported
        if r.get("Total") is not None and all(r.get(k) is not None for k in funds)
    ]
    partial_pending = bool(reported) and any(
        reported[-1].get(k) is None for k in funds
    )
    partial = _partial(reported[-1], funds) if partial_pending else None
    base = {"asset": cfg["asset"], "lead": lead}
    nets = [_window_net(complete, lead, w) for w in windows]
    primary = nets[0]
    flat = {
        "window": primary["days"],
        "window_dates": primary["dates"],
        "window_total": primary["total"],
        "window_lead": primary["lead"],
    }
    if not complete:
        return {
            **base,
            "as_of": None,
            "age_days": None,
            "pending_today": pending,
            "partial_pending": partial_pending,
            "partial": partial,
            "latest_total": None,
            "latest_lead": None,
            "days_complete": 0,
            "windows": nets,
            **flat,
            "streak_days": 0,
            "streak_sign": "flat",
        }
    latest = complete[-1]
    sign = None
    streak = 0
    for r in reversed(complete):
        # ``complete`` guarantees a published Total, so there is no None case to
        # break on here; a day without one is excluded upstream rather than
        # truncating the run at the point it is met.
        v = r["Total"]
        s = 1 if v > 0 else (-1 if v < 0 else 0)
        if sign is None:
            sign, streak = s, 1
        elif s == sign and s != 0:
            streak += 1
        else:
            break
    return {
        **base,
        "as_of": latest["date"],
        "age_days": _age_days(latest["date"]),
        "pending_today": pending,
        "partial_pending": partial_pending,
        "partial": partial,
        "latest_total": latest["Total"],
        "latest_lead": latest[lead],
        "days_complete": len(complete),
        "windows": nets,
        **flat,
        "streak_days": streak,
        "streak_sign": (
            "inflow" if sign and sign > 0
            else "outflow" if sign and sign < 0
            else "flat"
        ),
    }


def cache_path(asset):
    """Return the per-asset cache file: ``~/.openclaw/cache/farside_<asset>.json``.

    Each asset gets its own file so refreshing one never clobbers another's
    cached payload.
    """
    return CACHE_DIR / f"farside_{asset}.json"


def load_cache(path):
    """Load the cached payload at ``path``, or ``None`` if missing/unreadable."""
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def save_cache(payload, path):
    """Write ``payload`` to ``path`` as pretty JSON, creating parent dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def get_flows(asset=DEFAULT_ASSET, windows=DEFAULT_WINDOWS, rows=DEFAULT_ROWS):
    """Fetch, parse, summarize, and cache the latest flows for one asset.

    On success, builds a payload (``fetched_at``, ``source`` — the page the
    table came from, ``stale=False``, ``summary``, the last ``rows`` reported
    ``rows``, and a one-line ``line``), writes it to the asset's cache, and
    returns it. On any failure, returns that asset's cached payload with
    ``stale=True`` and an ``error`` field; re-raises only if no cache exists.

    Args:
        asset: Which asset to fetch (``btc``/``eth``/``sol``).
        windows: Rolling window lengths passed to :func:`summarize`.
        rows: How many recent reported days to include in the ``rows`` payload.
            Deliberately independent of ``windows`` so a 60-day net doesn't drag
            60 rows into the cache.

    Returns:
        The flow payload dict (fresh or stale-from-cache).
    """
    cfg = {**ASSETS[asset], "asset": asset}
    cache = cache_path(asset)
    try:
        data, source = fetch_table(cfg)
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "stale": False,
            "summary": summarize(data, cfg, windows),
            "rows": [
                _with_other(r, cfg["funds"])
                for r in _reported(data, cfg["funds"])[-rows:]
            ],
        }
        payload["line"] = briefing_line(payload)
        save_cache(payload, cache)
        return payload
    except Exception as e:
        cached = load_cache(cache)
        if cached is None:
            raise
        cached["stale"] = True
        cached["error"] = str(e)
        # The cached payload's derived freshness fields were frozen at write
        # time, when the fetch had *succeeded*: ``age_days`` counted from that
        # moment and ``line`` was rendered with ``stale=False``, so it carries no
        # staleness note. Served as-is they would read as fresh. Recompute both
        # now — ``as_of`` is a plain date string, so its age is exact no matter
        # how long the cache sat unrefreshed.
        try:
            summary = cached.get("summary")
            if isinstance(summary, dict) and summary.get("as_of"):
                summary["age_days"] = _age_days(summary["as_of"])
            cached["line"] = briefing_line(cached)
        except Exception:
            # Re-deriving is best-effort: returning the cached payload matters
            # more than annotating it, so a malformed cache still gets served.
            pass
        return cached


def _fmt(v):
    """Format a value (US$m) with an explicit sign, e.g. ``+57.7``/``-444.5``.

    Returns ``"n/a"`` for ``None``.
    """
    if v is None:
        return "n/a"
    return f"+{v:.1f}" if v >= 0 else f"{v:.1f}"


def _summary_windows(s):
    """Per-window nets for a summary, tolerating older cached payloads.

    Payloads cached before multi-window support carry only the flat ``window_*``
    keys; synthesize the single-entry list from those so a stale cache still
    renders instead of raising.
    """
    nets = s.get("windows")
    if nets:
        return nets
    days = s.get("window")
    if days is None:
        return []
    dates = s.get("window_dates") or []
    return [{
        "days": days,
        "days_available": len(dates),
        "covered": True,
        "total": s.get("window_total"),
        "lead": s.get("window_lead"),
        "dates": dates,
    }]


def briefing_block(payload):
    """Render the default multi-line terminal briefing for a payload.

    Includes the latest day, one line per rolling window, and the streak, with
    inline flags appended when relevant: ``FETCH-FAILED`` (stale cache),
    ``TODAY-PENDING`` (newest day unreported), ``PARTIAL:<funds>`` (newest day
    reported but some tracked funds still outstanding), and ``DATA-Nd-OLD``
    (latest data older than 4 days).

    A window the source can't fill renders as ``n/a (Nd available)`` rather than
    a shorter net under a longer label.

    Returns:
        A formatted multi-line string.
    """
    s = payload["summary"]
    flags = []
    if payload.get("stale"):
        flags.append("FETCH-FAILED")
    if s.get("pending_today"):
        flags.append("TODAY-PENDING")
    if s.get("partial_pending") and s.get("partial"):
        flags.append("PARTIAL:" + "/".join(s["partial"]["pending"]))
    if s.get("age_days") is not None and s["age_days"] > 4:
        flags.append(f"DATA-{s['age_days']}D-OLD")
    tag = f" [{', '.join(flags)}]" if flags else ""
    lead = s["lead"]
    lines = [
        f"{s['asset'].upper()} ETF flows (Farside, as of {s['as_of']}){tag}:",
        f"  latest: {_fmt(s['latest_total'])}m total | {_fmt(s['latest_lead'])}m {lead}",
    ]
    for w in _summary_windows(s):
        label = f"  {w['days']}d net:"
        if w["covered"]:
            lines.append(
                f"{label} {_fmt(w['total'])}m total | {_fmt(w['lead'])}m {lead}"
            )
        else:
            lines.append(f"{label} n/a ({w['days_available']}d available)")
    lines.append(f"  streak: {s['streak_days']}d {s['streak_sign']}")
    return "\n".join(lines)


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

    Combines latest total, the primary window's net, the lead fund's net and its
    share of that window, any longer windows' nets, and the streak, then tags the
    regime by direction and lead-fund concentration: ``conviction
    accumulation``/``distribution`` when the lead fund is 60-120% of the
    same-signed primary net, ``offsetting flows`` when the lead exceeds 120%
    (other funds net-offset it, leaving a small residual), otherwise ``broad
    inflow``/``outflow`` (or ``mixed flows`` when flat). Only the primary window
    drives the classification; the longer windows are reported as context.
    Appends ``today pending``/``{lead} pending``/``fetch failed`` notes. The
    last mirrors the block's ``FETCH-FAILED``: the flag records that a fetch
    failed and a cache was served, not that the data in it is old. Age is a
    separate question, and one this one-liner does not answer -- the block's
    ``DATA-Nd-OLD`` does.

    Returns:
        A one-line summary string.
    """
    s = payload["summary"]
    if s["as_of"] is None:
        return f"{s['asset'].upper()} ETF Flows: n/a"
    lead = s["lead"]
    nets = _summary_windows(s)
    primary = nets[0] if nets else None
    wt, wl = (primary["total"], primary["lead"]) if primary else (None, None)
    share_txt, share_val = "", None
    if wt and wl is not None and (wt > 0) == (wl > 0):
        share_val = round(100 * wl / wt)
        share_txt = f" ({share_val}%)"
    direction = None if wt is None else (
        "outflow" if wt < 0 else "inflow" if wt > 0 else None
    )
    if wt is None:
        # Primary window isn't covered by the available history, so there is no
        # net to classify — say so rather than implying a flat/mixed regime.
        tag = "insufficient history"
    elif direction is None:
        tag = "mixed flows"
    elif share_val and share_val > 120:
        # Lead's net exceeds the window net by >20%: the other funds are
        # net-offsetting it, so the small residual is not a "conviction" signal.
        tag = f"offsetting flows (net {direction})"
    elif share_val and share_val >= 60:
        tag = "conviction distribution" if wt < 0 else "conviction accumulation"
    else:
        tag = f"broad {direction}"
    asof_short = " ".join(s["as_of"].split()[:2])
    pd = primary["days"] if primary else None
    extra = "".join(
        f" | {w['days']}d {_abbr(w['total']) if w['covered'] else 'n/a'}"
        for w in nets[1:]
    )
    line = (
        f"{s['asset'].upper()} ETF Flows: {_abbr(s['latest_total'])} ({asof_short}, "
        f"{lead} {_abbr(s['latest_lead'])}) | "
        f"{pd}d net {_abbr(wt)} | {lead} {pd}d {_abbr(wl)}{share_txt}{extra} | "
        f"{s['streak_days']}d {s['streak_sign']} — {tag}"
    )
    notes = []
    if s.get("pending_today"):
        notes.append("today pending")
    if s.get("partial_pending") and s.get("partial"):
        p = s["partial"]
        lbl = "/".join(p["pending"])
        # Everything posted so far = the day's Total excluding the pending
        # fund(s): reported_total (tracked) + other (untracked). Report that,
        # not the tracked-only slice, so the figure matches the row Total.
        posted = (
            p["reported_total"] if p["other"] is None
            else round(p["reported_total"] + p["other"], 1)
        )
        notes.append(
            f"{lbl} pending "
            f"({' '.join(p['date'].split()[:2])}: {_abbr(posted)} ex-{lbl})"
        )
    if payload.get("stale"):
        notes.append("fetch failed")
    if notes:
        line += "; " + "; ".join(notes)
    return line


def _parse_args(argv=None):
    """Parse CLI arguments: an optional asset choice and ``--json``."""
    p = argparse.ArgumentParser(
        description="Fetch and summarize U.S. spot crypto ETF net flows "
        "from Farside Investors (btc/eth/sol)."
    )
    p.add_argument(
        "asset", nargs="?", default=DEFAULT_ASSET, choices=sorted(ASSETS),
        help="which ETF flow table to fetch (default: %(default)s)",
    )
    p.add_argument(
        "--json", action="store_true", help="emit the full payload as JSON"
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    out = get_flows(args.asset)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(briefing_block(out))
