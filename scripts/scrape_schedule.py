"""Scrape upcoming Japanese stock earnings (決算短信) announcement schedule.

Real data sources (no mock/fallback data):
  1. Kabutan 決算発表予定:    https://kabutan.jp/?mode=market_kessan&market=0
  2. Minkabu 決算発表予定:    https://minkabu.jp/financial_item_ranking/earnings_plans
  3. TraderWeb 決算スケジュール:https://www.traders.co.jp/domestic_stocks/stocks_data/kessan/kessan_yotei.asp

The scraper tries each source in order and uses the first one that returns
parseable rows. Multiple sources are used for *resilience* (so a single site's
HTML change doesn't break the pipeline) — they are NOT fallback data.

Output: docs/data/schedule.json
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path
from typing import Callable

from bs4 import BeautifulSoup

from common import FetchError, http_get, polite_sleep

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scrape_schedule")

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "data" / "schedule.json"

CODE_RE = re.compile(r"\b(\d{4})\b")
DATE_SLASH_RE = re.compile(r"(\d{1,2})/(\d{1,2})")
DATE_HYPHEN_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
DATE_JP_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")


def _resolve_date(month: int, day: int, today: dt.date) -> dt.date:
    """Resolve a month/day pair to a future date close to today."""
    for delta in (0, 1, -1):
        try:
            cand = dt.date(today.year + delta, month, day)
        except ValueError:
            continue
        diff = (cand - today).days
        if -30 <= diff <= 400:
            return cand
    return dt.date(today.year, month, day)


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
# Source 1: Kabutan
# --------------------------------------------------------------------------- #

KABUTAN_URLS = [
    "https://kabutan.jp/?mode=market_kessan&market=0",
    "https://kabutan.jp/?mode=market_kessan",
]


def scrape_kabutan() -> list[dict]:
    today = dt.date.today()
    items: list[dict] = []
    last_err: str = ""

    for url in KABUTAN_URLS:
        try:
            html = http_get(url)
        except FetchError as exc:
            last_err = str(exc)
            log.warning("kabutan %s failed: %s", url, exc)
            continue

        soup = BeautifulSoup(html, "lxml")
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue
                texts = [_norm_text(c.get_text(" ")) for c in cells]
                joined = " ".join(texts)

                code_m = CODE_RE.search(joined)
                if not code_m:
                    continue
                code = code_m.group(1)

                date_obj: dt.date | None = None
                m = DATE_SLASH_RE.search(joined)
                if m:
                    date_obj = _resolve_date(int(m.group(1)), int(m.group(2)), today)
                else:
                    m = DATE_JP_RE.search(joined)
                    if m:
                        date_obj = _resolve_date(int(m.group(1)), int(m.group(2)), today)

                name = None
                for link in row.find_all("a"):
                    txt = _norm_text(link.get_text(" "))
                    if txt and not txt.isdigit() and len(txt) >= 2 and txt != code:
                        if not CODE_RE.fullmatch(txt):
                            name = txt
                            break
                if not name:
                    for t in texts:
                        if t and not CODE_RE.fullmatch(t) and not DATE_SLASH_RE.fullmatch(t):
                            if any(ord(ch) > 127 for ch in t):
                                name = t
                                break

                if not name:
                    continue

                items.append(
                    {
                        "code": code,
                        "name": name,
                        "announcement_date": date_obj.isoformat() if date_obj else None,
                        "source": "kabutan",
                        "source_url": url,
                    }
                )

        if items:
            log.info("kabutan parsed %d rows from %s", len(items), url)
            return items

    if last_err:
        log.warning("kabutan: no rows parsed; last error: %s", last_err)
    return items


# --------------------------------------------------------------------------- #
# Source 2: Minkabu
# --------------------------------------------------------------------------- #

MINKABU_URLS = [
    "https://minkabu.jp/financial_item_ranking/earnings_plans",
    "https://minkabu.jp/financial_item_ranking/earnings_plans?type=plan_5d",
]


def scrape_minkabu() -> list[dict]:
    today = dt.date.today()
    items: list[dict] = []

    for url in MINKABU_URLS:
        try:
            html = http_get(url)
        except FetchError as exc:
            log.warning("minkabu %s failed: %s", url, exc)
            continue

        soup = BeautifulSoup(html, "lxml")
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) < 3:
                    continue
                texts = [_norm_text(c.get_text(" ")) for c in cells]
                joined = " ".join(texts)

                code_m = CODE_RE.search(joined)
                if not code_m:
                    continue
                code = code_m.group(1)

                date_obj: dt.date | None = None
                m = DATE_HYPHEN_RE.search(joined)
                if m:
                    try:
                        date_obj = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    except ValueError:
                        date_obj = None
                if not date_obj:
                    m = DATE_SLASH_RE.search(joined)
                    if m:
                        date_obj = _resolve_date(int(m.group(1)), int(m.group(2)), today)
                if not date_obj:
                    m = DATE_JP_RE.search(joined)
                    if m:
                        date_obj = _resolve_date(int(m.group(1)), int(m.group(2)), today)

                name = None
                for link in row.find_all("a"):
                    txt = _norm_text(link.get_text(" "))
                    if txt and not txt.isdigit() and txt != code and len(txt) >= 2:
                        if not CODE_RE.fullmatch(txt):
                            name = txt
                            break

                if not name:
                    continue

                items.append(
                    {
                        "code": code,
                        "name": name,
                        "announcement_date": date_obj.isoformat() if date_obj else None,
                        "source": "minkabu",
                        "source_url": url,
                    }
                )

        if items:
            log.info("minkabu parsed %d rows from %s", len(items), url)
            return items

    return items


# --------------------------------------------------------------------------- #
# Source 3: TraderWeb
# --------------------------------------------------------------------------- #

TRADERS_URLS = [
    "https://www.traders.co.jp/domestic_stocks/stocks_data/kessan/kessan_yotei.asp",
    "https://www.traders.co.jp/market_jp/stock_schedule/kessan",
]


def scrape_traders() -> list[dict]:
    today = dt.date.today()
    items: list[dict] = []

    for url in TRADERS_URLS:
        try:
            html = http_get(url)
        except FetchError as exc:
            log.warning("traders %s failed: %s", url, exc)
            continue

        soup = BeautifulSoup(html, "lxml")
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) < 3:
                    continue
                texts = [_norm_text(c.get_text(" ")) for c in cells]
                joined = " ".join(texts)

                code_m = CODE_RE.search(joined)
                if not code_m:
                    continue
                code = code_m.group(1)

                date_obj: dt.date | None = None
                m = DATE_HYPHEN_RE.search(joined)
                if m:
                    try:
                        date_obj = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    except ValueError:
                        pass
                if not date_obj:
                    m = DATE_SLASH_RE.search(joined)
                    if m:
                        date_obj = _resolve_date(int(m.group(1)), int(m.group(2)), today)

                name = None
                for t in texts:
                    if t and not CODE_RE.fullmatch(t) and any(ord(ch) > 127 for ch in t):
                        name = t
                        break
                if not name:
                    continue

                items.append(
                    {
                        "code": code,
                        "name": name,
                        "announcement_date": date_obj.isoformat() if date_obj else None,
                        "source": "traders",
                        "source_url": url,
                    }
                )

        if items:
            log.info("traders parsed %d rows from %s", len(items), url)
            return items

    return items


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def dedupe(items: list[dict]) -> list[dict]:
    seen: dict[tuple[str, str | None], dict] = {}
    for it in items:
        key = (it["code"], it.get("announcement_date"))
        if key not in seen:
            seen[key] = it
    return sorted(
        seen.values(),
        key=lambda x: (x.get("announcement_date") or "9999", x["code"]),
    )


SCRAPERS: list[tuple[str, Callable[[], list[dict]]]] = [
    ("kabutan", scrape_kabutan),
    ("minkabu", scrape_minkabu),
    ("traders", scrape_traders),
]


def main() -> int:
    all_items: list[dict] = []
    used_sources: list[str] = []
    errors: list[str] = []

    for name, fn in SCRAPERS:
        try:
            log.info("Trying source: %s", name)
            items = fn()
            polite_sleep(1.0)
            if items:
                all_items.extend(items)
                used_sources.append(name)
                # First successful source is enough — but try the others as
                # cross-validation if the first returns very few rows.
                if len(all_items) >= 50:
                    break
        except Exception as exc:  # noqa: BLE001 — log and try next
            log.exception("source %s raised: %s", name, exc)
            errors.append(f"{name}: {exc}")

    if not all_items:
        msg = "ERROR: no schedule items could be fetched from any source. " \
              "Real-data policy = no fallback. Errors: " + "; ".join(errors)
        log.error(msg)
        print(msg, file=sys.stderr)
        return 2

    items = dedupe(all_items)

    out = {
        "fetched_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "sources_used": used_sources,
        "count": len(items),
        "items": items,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    log.info("Wrote %d schedule items to %s", len(items), OUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
