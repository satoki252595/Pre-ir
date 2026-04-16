"""Fetch fundamental indicators from IR Bank (irbank.net) for a list of codes.

Real data only. Per-ticker fetches go to:
  * https://irbank.net/{code}/results   — 通期実績 (sales / op income / op margin)
  * https://irbank.net/{code}/dividend  — 年間配当履歴
  * https://irbank.net/{code}/forecast  — 業績予想と修正履歴

If a ticker fails, it is skipped and the failure is recorded. The job never
exits non-zero on network failures — it records what it was able to fetch
and the frontend displays whatever is real. No mock values are generated.
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

from common import FetchError, http_get, make_session, polite_sleep

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("fetch_fundamentals")

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_PATH = ROOT / "docs" / "data" / "schedule.json"
OUT_PATH = ROOT / "docs" / "data" / "fundamentals.json"

NUM_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")
YEAR_RE = re.compile(r"(\d{4})")
UPWARD_KEYWORDS = ("上方修正", "上方", "上振れ")
DOWNWARD_KEYWORDS = ("下方修正", "下方", "下振れ")

# Hard cap to prevent a single run from hammering irbank.net
MAX_TICKERS = 400


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
    soup = BeautifulSoup(html, "lxml")
    rows: dict[int, dict] = {}

    for table in soup.find_all("table"):
        header_blob = " ".join(_norm(th.get_text(" ")) for th in table.find_all("th"))
        if "売上" not in header_blob or "営業" not in header_blob:
            continue

        for tr in table.find_all("tr"):
            cells = [_norm(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
            if len(cells) < 3:
                continue

            year_match: Optional[int] = None
            for c in cells:
                m = YEAR_RE.search(c)
                if m:
                    y = int(m.group(1))
                    if 1990 <= y <= 2100:
                        year_match = y
                        break
            if year_match is None:
                continue

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
            op_margin: Optional[float] = None
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

            year: Optional[int] = None
            for c in cells:
                m = YEAR_RE.search(c)
                if m:
                    y = int(m.group(1))
                    if 1990 <= y <= 2100:
                        year = y
                        break
            if year is None:
                continue

            div: Optional[float] = None
            for c in cells:
                if YEAR_RE.search(c) and len(c) <= 12:
                    continue
                v = _to_float(c)
                if v is not None and v >= 0:
                    div = v
                    break

            if div is None:
                continue
            rows.setdefault(year, div)

    return [{"fiscal_year": y, "annual_dividend": rows[y]} for y in sorted(rows.keys())]


# --------------------------------------------------------------------------- #
# /forecast — past forecast revisions
# --------------------------------------------------------------------------- #


def parse_forecast_revisions(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    table_upward = 0
    table_downward = 0
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            row_text = _norm(tr.get_text(" "))
            if any(k in row_text for k in UPWARD_KEYWORDS):
                table_upward += 1
            if any(k in row_text for k in DOWNWARD_KEYWORDS):
                table_downward += 1

    if table_upward == 0 and table_downward == 0:
        text = soup.get_text(" ", strip=True)
        table_upward = sum(text.count(k) for k in UPWARD_KEYWORDS)
        table_downward = sum(text.count(k) for k in DOWNWARD_KEYWORDS)

    return {"upward_count": table_upward, "downward_count": table_downward}


# --------------------------------------------------------------------------- #
# Per-ticker fetch
# --------------------------------------------------------------------------- #


def fetch_one(code: str, session) -> dict:
    base = f"https://irbank.net/{code}"
    session.headers["Referer"] = f"{base}"

    results_html = http_get(f"{base}/results", session=session)
    polite_sleep(0.8)
    dividend_html = http_get(f"{base}/dividend", session=session)
    polite_sleep(0.8)
    forecast_html = http_get(f"{base}/forecast", session=session)
    polite_sleep(0.8)

    return {
        "code": code,
        "results": parse_results(results_html),
        "dividends": parse_dividend(dividend_html),
        "revisions": parse_forecast_revisions(forecast_html),
        "source": "irbank.net",
        "fetched_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def main() -> int:
    if not SCHEDULE_PATH.exists():
        log.error("schedule file not found: %s", SCHEDULE_PATH)
        _write_empty(error="schedule.json missing")
        return 0

    schedule = json.loads(SCHEDULE_PATH.read_text())
    items = schedule.get("items", [])
    log.info("Loaded %d schedule items", len(items))

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
                if d < today - dt.timedelta(days=2) or d > horizon:
                    continue
            except ValueError:
                pass
        seen.add(code)
        targets.append(code)

    if len(targets) > MAX_TICKERS:
        log.warning("Capping %d -> %d tickers", len(targets), MAX_TICKERS)
        targets = targets[:MAX_TICKERS]

    # Cache reuse
    cache: dict[str, dict] = {}
    if OUT_PATH.exists():
        try:
            cached = json.loads(OUT_PATH.read_text())
            cache = cached.get("data", {}) or {}
        except Exception:
            cache = {}

    cache_max_age = dt.timedelta(days=3)
    now = dt.datetime.utcnow()

    results: dict[str, dict] = {}
    failures: list[dict] = []
    session = make_session()

    log.info("Fetching fundamentals for %d codes", len(targets))

    # Quick connectivity probe for IR BANK
    probe_ok = False
    probe_status: Optional[int] = None
    probe_body: Optional[str] = None
    try:
        http_get("https://irbank.net/", session=session)
        probe_ok = True
        log.info("irbank.net connectivity probe: OK")
    except FetchError as exc:
        probe_status = exc.status
        probe_body = exc.body
        log.warning(
            "irbank.net connectivity probe failed: %s (status=%s)", exc, exc.status
        )

    if not probe_ok:
        # No point hammering individual tickers if the root is blocked.
        out = {
            "fetched_at": now.isoformat(timespec="seconds") + "Z",
            "count": len(cache),
            "failure_count": len(targets),
            "connectivity_error": {
                "source": "irbank.net",
                "status": probe_status,
                "body": (probe_body or "")[:300],
            },
            "data": cache,  # reuse any cached data we already have
        }
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        log.error(
            "IR Bank unreachable — skipping individual fetches. "
            "Reusing %d cached entries.",
            len(cache),
        )
        return 0

    for i, code in enumerate(targets, 1):
        cached_entry = cache.get(code)
        if cached_entry:
            try:
                ts = dt.datetime.fromisoformat(
                    cached_entry["fetched_at"].rstrip("Z")
                )
                if now - ts < cache_max_age:
                    results[code] = cached_entry
                    log.info("[%d/%d] %s cache-hit", i, len(targets), code)
                    continue
            except Exception:
                pass

        try:
            log.info("[%d/%d] %s fetching...", i, len(targets), code)
            results[code] = fetch_one(code, session)
        except FetchError as exc:
            log.warning("[%d/%d] %s failed: %s", i, len(targets), code, exc)
            failures.append({"code": code, "error": str(exc), "status": exc.status})
            if cached_entry:
                results[code] = cached_entry
        except Exception as exc:  # noqa: BLE001
            log.exception("[%d/%d] %s crashed", i, len(targets), code)
            failures.append({"code": code, "error": repr(exc)})
            if cached_entry:
                results[code] = cached_entry

    out = {
        "fetched_at": now.isoformat(timespec="seconds") + "Z",
        "count": len(results),
        "failure_count": len(failures),
        "failures": failures[:50],
        "data": results,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    log.info(
        "Wrote fundamentals for %d codes (failures=%d)",
        len(results),
        len(failures),
    )
    return 0


def _write_empty(*, error: str) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {
                "fetched_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "count": 0,
                "failure_count": 0,
                "error": error,
                "data": {},
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
