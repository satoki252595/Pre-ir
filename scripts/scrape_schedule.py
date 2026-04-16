"""Scrape upcoming Japanese stock earnings (決算短信) announcement schedule.

Real data only. No mock/fallback data is ever generated — if every source
fails, the output JSON records the exact failure (per-source error) and
zero items. The frontend surfaces that as a clear error state.

Sources tried in order:
  1. JPX  (Japan Exchange Group) — official Excel files
      https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html
  2. Nikkeiyosoku (投資の森)     — https://nikkeiyosoku.com/stock/financial_statement/month/
  3. Kabutan                       — https://kabutan.jp/?mode=market_kessan
  4. Minkabu                       — https://minkabu.jp/financial_item_ranking/earnings_plans
  5. TraderWeb                     — https://www.traders.co.jp/market_jp/earnings_calendar
  6. Matsui                        — https://finance.matsui.co.jp/find-by-schedule/index

Every source is attempted and its result / error is recorded. The first
source returning items populates `items`; subsequent sources are run for
diagnostic logging only. Output: docs/data/schedule.json
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import re
import sys
import traceback
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from common import FetchError, http_get, make_session, polite_sleep

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("scrape_schedule")

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "data" / "schedule.json"

CODE_RE = re.compile(r"\b([0-9]{4})\b")
DATE_SLASH_RE = re.compile(r"(\d{1,2})/(\d{1,2})")
DATE_HYPHEN_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
DATE_JP_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")


def _resolve_date(month: int, day: int, today: dt.date) -> Optional[dt.date]:
    """Resolve a month/day pair to a date near today."""
    for delta in (0, 1, -1):
        try:
            cand = dt.date(today.year + delta, month, day)
        except ValueError:
            continue
        diff = (cand - today).days
        if -30 <= diff <= 400:
            return cand
    return None


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
# Source 1: JPX official Excel files
# --------------------------------------------------------------------------- #

JPX_INDEX_URL = (
    "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
)
JPX_BASE = "https://www.jpx.co.jp"


def scrape_jpx() -> list[dict]:
    import pandas as pd  # imported lazily — heavy dep

    session = make_session()
    session.headers["Referer"] = "https://www.jpx.co.jp/"

    html = http_get(JPX_INDEX_URL, session=session)
    soup = BeautifulSoup(html, "lxml")

    excel_links: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        low = href.lower()
        if low.endswith(".xls") or low.endswith(".xlsx"):
            full = urljoin(JPX_INDEX_URL, href)
            excel_links.append(
                {"url": full, "text": _norm_text(a.get_text(" ") or "")}
            )

    log.info("JPX index: found %d Excel links", len(excel_links))
    for el in excel_links[:8]:
        log.info("  [JPX xls] %s — %s", el["text"][:60], el["url"])

    if not excel_links:
        raise FetchError("JPX index page contained no .xls/.xlsx links")

    items: list[dict] = []
    parsed_any = False

    for el in excel_links[:6]:  # Try top few — newest first usually
        try:
            log.info("JPX: downloading %s", el["url"])
            raw = http_get(el["url"], session=session, return_bytes=True, timeout=30)
            df = _read_excel_bytes(raw)
            log.info(
                "JPX: loaded %s shape=%s", el["url"].rsplit("/", 1)[-1], df.shape
            )
            parsed = _parse_jpx_df(df, el["url"])
            if parsed:
                parsed_any = True
                items.extend(parsed)
                log.info("JPX: parsed %d rows from %s", len(parsed), el["url"])
            polite_sleep(0.8)
        except Exception as exc:  # noqa: BLE001
            log.warning("JPX: failed %s: %s", el["url"], exc)

    if not parsed_any:
        raise FetchError("JPX: no Excel file yielded parseable rows")
    return items


def _read_excel_bytes(raw: bytes):
    """Read an Excel file (xls or xlsx) into a DataFrame, tolerating missing engines."""
    import pandas as pd

    errors: list[str] = []
    for engine in ("calamine", "openpyxl", "xlrd", None):
        try:
            buf = io.BytesIO(raw)
            if engine is None:
                df = pd.read_excel(buf, header=None)
            else:
                df = pd.read_excel(buf, header=None, engine=engine)
            return df
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{engine}:{exc}")
    raise FetchError("pandas.read_excel failed: " + " | ".join(errors))


def _parse_jpx_df(df, source_url: str) -> list[dict]:
    """Parse a JPX earnings-schedule DataFrame into our item format.

    JPX Excel files have a free-form title row or two, then a header row
    containing columns like コード / 銘柄名 / 決算発表日.
    """
    import pandas as pd

    header_row = None
    for i in range(min(15, len(df))):
        row_vals = [str(c) for c in df.iloc[i].tolist() if pd.notna(c)]
        joined = " ".join(row_vals)
        if "コード" in joined and ("銘柄" in joined or "会社" in joined):
            header_row = i
            break

    if header_row is None:
        log.warning("JPX %s: no header row with コード+銘柄 found", source_url)
        return []

    headers = [str(c).strip() for c in df.iloc[header_row].tolist()]
    data = df.iloc[header_row + 1 :].reset_index(drop=True)
    data.columns = headers

    # column lookup
    def find_col(patterns: list[str]) -> Optional[str]:
        for pat in patterns:
            for c in data.columns:
                if pat in str(c):
                    return c
        return None

    code_col = find_col(["コード", "証券コード"])
    name_col = find_col(["銘柄名", "会社名", "銘柄"])
    date_col = find_col(["決算発表", "発表日", "発表予定", "決算日"])

    if not code_col or not name_col:
        log.warning(
            "JPX %s: missing required columns. cols=%s",
            source_url,
            list(data.columns),
        )
        return []

    today = dt.date.today()
    items: list[dict] = []

    for _, row in data.iterrows():
        raw_code = row.get(code_col)
        if pd.isna(raw_code):
            continue
        code_txt = str(raw_code)
        m = re.search(r"\d{4}", code_txt)
        if not m:
            continue
        code = m.group()

        raw_name = row.get(name_col)
        if pd.isna(raw_name):
            continue
        name = str(raw_name).strip()
        if not name or name.lower() == "nan":
            continue

        date_val: Optional[dt.date] = None
        if date_col is not None:
            raw_date = row.get(date_col)
            if pd.notna(raw_date):
                if isinstance(raw_date, (dt.datetime, pd.Timestamp)):
                    try:
                        date_val = raw_date.date()
                    except Exception:
                        date_val = None
                elif isinstance(raw_date, dt.date):
                    date_val = raw_date
                else:
                    s = str(raw_date)
                    m2 = DATE_HYPHEN_RE.search(s)
                    if m2:
                        try:
                            date_val = dt.date(
                                int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
                            )
                        except ValueError:
                            pass
                    else:
                        m3 = DATE_SLASH_RE.search(s)
                        if m3:
                            date_val = _resolve_date(
                                int(m3.group(1)), int(m3.group(2)), today
                            )
                        else:
                            m4 = DATE_JP_RE.search(s)
                            if m4:
                                date_val = _resolve_date(
                                    int(m4.group(1)), int(m4.group(2)), today
                                )

        items.append(
            {
                "code": code,
                "name": name,
                "announcement_date": date_val.isoformat() if date_val else None,
                "source": "jpx",
                "source_url": source_url,
            }
        )

    return items


# --------------------------------------------------------------------------- #
# Source 2: Nikkeiyosoku (投資の森)
# --------------------------------------------------------------------------- #


def scrape_nikkeiyosoku() -> list[dict]:
    today = dt.date.today()
    urls = [
        "https://nikkeiyosoku.com/stock/financial_statement/month/",
        f"https://nikkeiyosoku.com/stock/financial_statement/month/{today.strftime('%Y-%m')}/",
    ]

    session = make_session()
    session.headers["Referer"] = "https://nikkeiyosoku.com/"
    items: list[dict] = []

    for url in urls:
        try:
            html = http_get(url, session=session)
        except FetchError as exc:
            log.warning("nikkeiyosoku %s failed: %s (status=%s)", url, exc, exc.status)
            continue

        rows_parsed = _parse_generic_table(html, url, "nikkeiyosoku")
        log.info("nikkeiyosoku %s: parsed %d rows", url, len(rows_parsed))
        if rows_parsed:
            items.extend(rows_parsed)
            break
        polite_sleep(0.8)

    if not items:
        raise FetchError("nikkeiyosoku: no rows parsed")
    return items


# --------------------------------------------------------------------------- #
# Source 3: Kabutan
# --------------------------------------------------------------------------- #


def scrape_kabutan() -> list[dict]:
    urls = [
        "https://kabutan.jp/?mode=market_kessan&market=0",
        "https://kabutan.jp/warning/?mode=5_1",
        "https://kabutan.jp/?mode=market_kessan",
    ]
    session = make_session()
    session.headers["Referer"] = "https://kabutan.jp/"

    items: list[dict] = []
    for url in urls:
        try:
            html = http_get(url, session=session)
        except FetchError as exc:
            log.warning("kabutan %s failed: %s (status=%s)", url, exc, exc.status)
            continue
        rows = _parse_generic_table(html, url, "kabutan")
        log.info("kabutan %s: parsed %d rows", url, len(rows))
        if rows:
            items.extend(rows)
            break
        polite_sleep(0.8)

    if not items:
        raise FetchError("kabutan: no rows parsed")
    return items


# --------------------------------------------------------------------------- #
# Source 4: Minkabu
# --------------------------------------------------------------------------- #


def scrape_minkabu() -> list[dict]:
    urls = [
        "https://minkabu.jp/financial_item_ranking/earnings_plans",
        "https://minkabu.jp/financial_item_ranking/earnings_plans?type=plan_5d",
    ]
    session = make_session()
    session.headers["Referer"] = "https://minkabu.jp/"

    items: list[dict] = []
    for url in urls:
        try:
            html = http_get(url, session=session)
        except FetchError as exc:
            log.warning("minkabu %s failed: %s (status=%s)", url, exc, exc.status)
            continue
        rows = _parse_generic_table(html, url, "minkabu")
        log.info("minkabu %s: parsed %d rows", url, len(rows))
        if rows:
            items.extend(rows)
            break
        polite_sleep(0.8)

    if not items:
        raise FetchError("minkabu: no rows parsed")
    return items


# --------------------------------------------------------------------------- #
# Source 5: TraderWeb
# --------------------------------------------------------------------------- #


def scrape_traders() -> list[dict]:
    urls = [
        "https://www.traders.co.jp/market_jp/earnings_calendar",
        "https://www.traders.co.jp/domestic_stocks/stocks_data/kessan/kessan_yotei.asp",
    ]
    session = make_session()
    session.headers["Referer"] = "https://www.traders.co.jp/"

    items: list[dict] = []
    for url in urls:
        try:
            html = http_get(url, session=session)
        except FetchError as exc:
            log.warning("traders %s failed: %s (status=%s)", url, exc, exc.status)
            continue
        rows = _parse_generic_table(html, url, "traders")
        log.info("traders %s: parsed %d rows", url, len(rows))
        if rows:
            items.extend(rows)
            break
        polite_sleep(0.8)

    if not items:
        raise FetchError("traders: no rows parsed")
    return items


# --------------------------------------------------------------------------- #
# Source 6: Matsui Securities
# --------------------------------------------------------------------------- #


def scrape_matsui() -> list[dict]:
    url = "https://finance.matsui.co.jp/find-by-schedule/index"
    session = make_session()
    session.headers["Referer"] = "https://finance.matsui.co.jp/"

    try:
        html = http_get(url, session=session)
    except FetchError as exc:
        raise FetchError(f"matsui failed: {exc} (status={exc.status})")

    rows = _parse_generic_table(html, url, "matsui")
    log.info("matsui: parsed %d rows", len(rows))
    if not rows:
        raise FetchError("matsui: no rows parsed")
    return rows


# --------------------------------------------------------------------------- #
# Generic HTML table parser
# --------------------------------------------------------------------------- #


def _parse_generic_table(html: str, source_url: str, source: str) -> list[dict]:
    """Generic: find table rows that contain a 4-digit stock code and a date."""
    today = dt.date.today()
    soup = BeautifulSoup(html, "lxml")

    items: list[dict] = []
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

            date_val: Optional[dt.date] = None
            m = DATE_HYPHEN_RE.search(joined)
            if m:
                try:
                    date_val = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except ValueError:
                    date_val = None
            if not date_val:
                m = DATE_SLASH_RE.search(joined)
                if m:
                    date_val = _resolve_date(int(m.group(1)), int(m.group(2)), today)
            if not date_val:
                m = DATE_JP_RE.search(joined)
                if m:
                    date_val = _resolve_date(int(m.group(1)), int(m.group(2)), today)

            name = None
            for link in row.find_all("a"):
                txt = _norm_text(link.get_text(" "))
                if (
                    txt
                    and not CODE_RE.fullmatch(txt)
                    and len(txt) >= 2
                    and txt != code
                    and any(ord(ch) > 127 for ch in txt)
                ):
                    name = txt
                    break
            if not name:
                for t in texts:
                    if (
                        t
                        and not CODE_RE.fullmatch(t)
                        and not DATE_SLASH_RE.fullmatch(t)
                        and any(ord(ch) > 127 for ch in t)
                        and len(t) >= 2
                        and len(t) <= 40
                    ):
                        name = t
                        break

            if not name:
                continue

            items.append(
                {
                    "code": code,
                    "name": name,
                    "announcement_date": date_val.isoformat() if date_val else None,
                    "source": source,
                    "source_url": source_url,
                }
            )
    return items


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


SCRAPERS: list[tuple[str, Callable[[], list[dict]]]] = [
    ("jpx", scrape_jpx),
    ("nikkeiyosoku", scrape_nikkeiyosoku),
    ("kabutan", scrape_kabutan),
    ("minkabu", scrape_minkabu),
    ("traders", scrape_traders),
    ("matsui", scrape_matsui),
]


def dedupe(items: list[dict]) -> list[dict]:
    seen: dict[tuple[str, str], dict] = {}
    for it in items:
        key = (it["code"], it.get("announcement_date") or "")
        if key not in seen:
            seen[key] = it
    return sorted(
        seen.values(),
        key=lambda x: (x.get("announcement_date") or "9999", x["code"]),
    )


def main() -> int:
    results_by_source: dict[str, dict] = {}
    first_items: list[dict] = []
    first_source: Optional[str] = None

    for name, fn in SCRAPERS:
        log.info("=" * 70)
        log.info("Trying source: %s", name)
        try:
            items = fn()
            results_by_source[name] = {
                "items": len(items),
                "error": None,
            }
            log.info("SUCCESS %s: %d items", name, len(items))
            if items and not first_items:
                first_items = items
                first_source = name
        except FetchError as exc:
            results_by_source[name] = {
                "items": 0,
                "error": str(exc),
                "http_status": exc.status,
                "body_snippet": (exc.body or "")[:300],
            }
            log.warning("FAILED %s: %s (status=%s)", name, exc, exc.status)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=3)
            results_by_source[name] = {
                "items": 0,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": tb,
            }
            log.exception("CRASHED %s: %s", name, exc)

        polite_sleep(1.0)

    items = dedupe(first_items)
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    out = {
        "fetched_at": now,
        "primary_source": first_source,
        "sources_results": results_by_source,
        "count": len(items),
        "items": items,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))

    log.info("=" * 70)
    if items:
        log.info("Wrote %d schedule items from source=%s", len(items), first_source)
    else:
        log.error("NO ITEMS parsed from any source. Diagnostics:")
        for name, info in results_by_source.items():
            log.error(
                "  %s: items=%s error=%s",
                name,
                info.get("items"),
                info.get("error"),
            )

    # Always return 0 — the frontend will render the error state from
    # sources_results. This is not a fallback-data fake: the JSON is an
    # accurate report of what happened, with zero fabricated stock rows.
    return 0


if __name__ == "__main__":
    sys.exit(main())
