"""Fetch fundamental indicators from IR Bank (irbank.net) for a list of codes.

Real data only. Each ticker is fetched from three IR Bank pages:
  * https://irbank.net/{code}/results   - 通期実績 (sales, op income, op margin)
  * https://irbank.net/{code}/dividend  - 年間配当履歴
  * https://irbank.net/{code}/forecast  - 業績予想と修正履歴

Output: docs/data/fundamentals.json (keyed by 4-digit code)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from common import FetchError, http_get, polite_sleep

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fetch_fundamentals")

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_PATH = ROOT / "docs" / "data" / "schedule.json"
OUT_PATH = ROOT / "docs" / "data" / "fundamentals.json"

NUM_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")
YEAR_RE = re.compile(r"(\d{4})")
PCT_RE = re.compile(r"(-?[\d,]+(?:\.\d+)?)\s*%")
UPWARD_KEYWORDS = ("上方修正", "上方", "上振れ")
DOWNWARD_KEYWORDS = ("下方修正", "下方", "下振れ")


def _to_float(s: str) -> Optional[float]:
    s = s.replace(",", "").replace("円", "").strip()
    if not s or s in {"-", "—", "–", "ー"}:
        return None
    try:
        return float(s)
    except ValueError:
        m = NUM_RE.search(s)
        if not m:
            return None
        try:
            return float(m.group().replace(",", ""))
        except ValueError:
            return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
# /results — operating income / margin
# --------------------------------------------------------------------------- #


def parse_results(html: str) -> list[dict]:
    """Extract list of {fiscal_year, sales, op_income, op_margin} ordered by year asc."""
    soup = BeautifulSoup(html, "lxml")
    rows: dict[int, dict] = {}

    for table in soup.find_all("table"):
        headers = [
            _norm(th.get_text(" ")) for th in table.find_all("th")
        ]
        if not headers:
            continue

        header_blob = " ".join(headers)
        # Looking for a table that has 売上 + 営業利益
        if "売上" not in header_blob or "営業" not in header_blob:
            continue

        # Try to find rows with year + numeric data
        for tr in table.find_all("tr"):
            cells = [_norm(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            year_match = None
            for c in cells:
                m = YEAR_RE.search(c)
                if m:
                    y = int(m.group(1))
                    if 1990 <= y <= 2100:
                        year_match = y
                        break
            if year_match is None:
                continue

            # collect numeric cells
            nums: list[Optional[float]] = []
            for c in cells:
                if YEAR_RE.search(c) and len(c) <= 12:
                    continue
                nums.append(_to_float(c))

            non_null = [n for n in nums if n is not None]
            if len(non_null) < 2:
                continue

            sales = non_null[0] if non_null else None
            op_income = non_null[1] if len(non_null) > 1 else None
            op_margin = None
            if sales and op_income is not None and sales != 0:
                op_margin = round(op_income / sales * 100.0, 2)

            rows.setdefault(
                year_match,
                {
                    "fiscal_year": year_match,
                    "sales": sales,
                    "op_income": op_income,
                    "op_margin": op_margin,
                },
            )

    if not rows:
        return []
    return [rows[y] for y in sorted(rows.keys())]


# --------------------------------------------------------------------------- #
# /dividend — annual dividend history
# --------------------------------------------------------------------------- #


def parse_dividend(html: str) -> list[dict]:
    """Return list of {fiscal_year, annual_dividend} sorted by year asc."""
    soup = BeautifulSoup(html, "lxml")
    rows: dict[int, float] = {}

    for table in soup.find_all("table"):
        header_blob = " ".join(_norm(th.get_text(" ")) for th in table.find_all("th"))
        if "配当" not in header_blob:
            continue

        for tr in table.find_all("tr"):
            cells = [_norm(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue

            year = None
            for c in cells:
                m = YEAR_RE.search(c)
                if m:
                    y = int(m.group(1))
                    if 1990 <= y <= 2100:
                        year = y
                        break
            if year is None:
                continue

            div = None
            for c in cells:
                if YEAR_RE.search(c) and len(c) <= 12:
                    continue
                v = _to_float(c)
                if v is not None and v >= 0:
                    div = v
                    break

            if div is None:
                continue

            # earliest occurrence wins (top-of-table is usually 通期 total)
            rows.setdefault(year, div)

    return [{"fiscal_year": y, "annual_dividend": rows[y]} for y in sorted(rows.keys())]


# --------------------------------------------------------------------------- #
# /forecast — past forecast revisions
# --------------------------------------------------------------------------- #


def parse_forecast_revisions(html: str) -> dict:
    """Count upward/downward forecast revisions over the page's history."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    upward = sum(text.count(k) for k in UPWARD_KEYWORDS)
    downward = sum(text.count(k) for k in DOWNWARD_KEYWORDS)

    # also try to count actual rows in revision tables
    table_upward = 0
    table_downward = 0
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            row_text = _norm(tr.get_text(" "))
            if any(k in row_text for k in UPWARD_KEYWORDS):
                table_upward += 1
            if any(k in row_text for k in DOWNWARD_KEYWORDS):
                table_downward += 1

    return {
        "upward_count": max(upward, table_upward),
        "downward_count": max(downward, table_downward),
    }


# --------------------------------------------------------------------------- #
# Per-ticker fetch
# --------------------------------------------------------------------------- #


def fetch_one(code: str) -> dict:
    base = f"https://irbank.net/{code}"

    results_html = http_get(f"{base}/results")
    polite_sleep(1.0)
    dividend_html = http_get(f"{base}/dividend")
    polite_sleep(1.0)
    forecast_html = http_get(f"{base}/forecast")
    polite_sleep(1.0)

    results = parse_results(results_html)
    dividends = parse_dividend(dividend_html)
    revisions = parse_forecast_revisions(forecast_html)

    return {
        "code": code,
        "results": results,
        "dividends": dividends,
        "revisions": revisions,
        "source": "irbank.net",
        "fetched_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def main() -> int:
    if not SCHEDULE_PATH.exists():
        print(f"ERROR: schedule file not found: {SCHEDULE_PATH}", file=sys.stderr)
        return 2

    schedule = json.loads(SCHEDULE_PATH.read_text())
    items = schedule.get("items", [])

    today = dt.date.today()
    horizon = today + dt.timedelta(days=30)

    targets: list[str] = []
    seen: set[str] = set()
    for it in items:
        code = it.get("code")
        if not code or code in seen:
            continue
        date_str = it.get("announcement_date")
        if date_str:
            try:
                d = dt.date.fromisoformat(date_str)
            except ValueError:
                d = None
            if d and (d < today - dt.timedelta(days=2) or d > horizon):
                continue
        seen.add(code)
        targets.append(code)

    # Reuse cache to skip codes we already fetched recently
    cache: dict[str, dict] = {}
    if OUT_PATH.exists():
        try:
            cached = json.loads(OUT_PATH.read_text())
            cache = cached.get("data", {})
        except Exception:
            cache = {}

    cache_max_age = dt.timedelta(days=3)
    now = dt.datetime.utcnow()

    results: dict[str, dict] = {}
    failures: list[tuple[str, str]] = []

    log.info("Fetching fundamentals for %d codes (horizon=%s)", len(targets), horizon)

    for i, code in enumerate(targets, 1):
        cached_entry = cache.get(code)
        if cached_entry:
            try:
                ts = dt.datetime.fromisoformat(cached_entry["fetched_at"].rstrip("Z"))
                if now - ts < cache_max_age:
                    results[code] = cached_entry
                    log.info("[%d/%d] %s cache-hit", i, len(targets), code)
                    continue
            except Exception:
                pass

        try:
            log.info("[%d/%d] %s fetching...", i, len(targets), code)
            results[code] = fetch_one(code)
        except FetchError as exc:
            log.warning("[%d/%d] %s failed: %s", i, len(targets), code, exc)
            failures.append((code, str(exc)))
            # Keep the stale cache entry rather than dropping the ticker
            if cached_entry:
                results[code] = cached_entry
        except Exception as exc:  # noqa: BLE001
            log.exception("[%d/%d] %s unexpected: %s", i, len(targets), code, exc)
            failures.append((code, repr(exc)))
            if cached_entry:
                results[code] = cached_entry

    if not results:
        print(
            "ERROR: no fundamentals fetched. Real-data policy = no fallback.",
            file=sys.stderr,
        )
        return 3

    out = {
        "fetched_at": now.isoformat(timespec="seconds") + "Z",
        "count": len(results),
        "failure_count": len(failures),
        "data": results,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    log.info(
        "Wrote fundamentals for %d codes (failures=%d) to %s",
        len(results),
        len(failures),
        OUT_PATH,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
