"""Apply the Pre-IR screening rules to schedule + fundamentals.

Screening criteria (can be tuned via constants below):

  1. Past upward revisions (上方修正) — at least MIN_UPWARD_REVISIONS in the
     forecast revision history reported by IR Bank.
  2. Consecutive dividend increases (連続増配) — at least
     MIN_CONSECUTIVE_DIVIDEND_YEARS years of strict increases.
  3. Operating margin (営業利益率) trend — most-recent year > earliest
     observed year (within the last RECENT_YEARS), AND the linear-regression
     slope across the available years is positive.

A composite "Pre-IR score" (0-100) is also produced so users can sort the
list by likelihood of an upside surprise.

Output: docs/data/screened.json  (only stocks that pass all three criteria)
        docs/data/all_evaluated.json  (every evaluated stock with detail —
                                       useful for transparency / debugging)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("screen")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
SCHEDULE_PATH = DATA / "schedule.json"
FUNDAMENTALS_PATH = DATA / "fundamentals.json"
SCREENED_PATH = DATA / "screened.json"
ALL_EVAL_PATH = DATA / "all_evaluated.json"

# --- Tunable thresholds --- #
MIN_UPWARD_REVISIONS = 2
MIN_CONSECUTIVE_DIVIDEND_YEARS = 3
RECENT_YEARS = 5  # window for the operating-margin trend


# --------------------------------------------------------------------------- #
# Indicator helpers
# --------------------------------------------------------------------------- #


def consecutive_dividend_increases(dividends: list[dict]) -> int:
    """Return the longest streak of strictly-increasing dividends ending at
    the most recent year."""
    if not dividends:
        return 0
    series = [d["annual_dividend"] for d in sorted(dividends, key=lambda x: x["fiscal_year"])]
    streak = 0
    for i in range(len(series) - 1, 0, -1):
        prev = series[i - 1]
        cur = series[i]
        if prev is None or cur is None:
            break
        if cur > prev:
            streak += 1
        else:
            break
    return streak


def op_margin_trend(results: list[dict]) -> dict:
    """Return {recent, earliest, slope, increasing} for op margin within the
    last RECENT_YEARS of available data. `increasing` is True if recent >
    earliest AND slope > 0."""
    if not results:
        return {
            "recent": None,
            "earliest": None,
            "slope": None,
            "increasing": False,
            "years_available": 0,
        }

    sorted_r = sorted(
        [r for r in results if r.get("op_margin") is not None],
        key=lambda x: x["fiscal_year"],
    )
    if len(sorted_r) < 2:
        return {
            "recent": sorted_r[-1]["op_margin"] if sorted_r else None,
            "earliest": sorted_r[0]["op_margin"] if sorted_r else None,
            "slope": None,
            "increasing": False,
            "years_available": len(sorted_r),
        }

    window = sorted_r[-RECENT_YEARS:]
    xs = [r["fiscal_year"] for r in window]
    ys = [r["op_margin"] for r in window]

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    slope = num / den if den else 0.0

    recent = ys[-1]
    earliest = ys[0]

    return {
        "recent": round(recent, 2),
        "earliest": round(earliest, 2),
        "slope": round(slope, 4),
        "increasing": (recent > earliest) and (slope > 0),
        "years_available": n,
    }


def latest_op_margin(results: list[dict]) -> Optional[float]:
    if not results:
        return None
    valid = [r for r in results if r.get("op_margin") is not None]
    if not valid:
        return None
    return sorted(valid, key=lambda x: x["fiscal_year"])[-1]["op_margin"]


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def score_stock(upward: int, div_streak: int, trend: dict) -> int:
    """0-100 composite score."""
    rev_pts = min(upward, 6) / 6.0 * 30.0
    div_pts = min(div_streak, 10) / 10.0 * 30.0

    slope = trend.get("slope") or 0.0
    if slope <= 0:
        margin_pts = 0.0
    else:
        margin_pts = min(slope, 2.0) / 2.0 * 25.0

    recent = trend.get("recent") or 0.0
    margin_bonus = min(max(recent, 0.0), 30.0) / 30.0 * 15.0

    return int(round(rev_pts + div_pts + margin_pts + margin_bonus))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> int:
    if not SCHEDULE_PATH.exists():
        print(f"ERROR: schedule file missing: {SCHEDULE_PATH}", file=sys.stderr)
        return 2
    if not FUNDAMENTALS_PATH.exists():
        print(f"ERROR: fundamentals file missing: {FUNDAMENTALS_PATH}", file=sys.stderr)
        return 2

    schedule = json.loads(SCHEDULE_PATH.read_text())
    fundamentals = json.loads(FUNDAMENTALS_PATH.read_text())

    fund_data: dict[str, dict] = fundamentals.get("data", {})
    items = schedule.get("items", [])

    today = dt.date.today()

    by_code: dict[str, dict] = {}
    for it in items:
        code = it.get("code")
        if not code:
            continue
        date_str = it.get("announcement_date")
        if date_str:
            try:
                d = dt.date.fromisoformat(date_str)
                if d < today - dt.timedelta(days=1):
                    continue
            except ValueError:
                pass
        # Prefer the earliest upcoming date if a code appears multiple times
        prev = by_code.get(code)
        if prev is None or (
            it.get("announcement_date")
            and (not prev.get("announcement_date") or it["announcement_date"] < prev["announcement_date"])
        ):
            by_code[code] = it

    evaluated: list[dict] = []
    passed: list[dict] = []

    for code, it in by_code.items():
        fund = fund_data.get(code)
        if not fund:
            continue

        upward = fund.get("revisions", {}).get("upward_count", 0)
        downward = fund.get("revisions", {}).get("downward_count", 0)
        div_streak = consecutive_dividend_increases(fund.get("dividends", []))
        trend = op_margin_trend(fund.get("results", []))
        latest_margin = latest_op_margin(fund.get("results", []))

        score = score_stock(upward, div_streak, trend)

        rec = {
            "code": code,
            "name": it.get("name"),
            "announcement_date": it.get("announcement_date"),
            "upward_revisions": upward,
            "downward_revisions": downward,
            "dividend_streak_years": div_streak,
            "op_margin_latest": latest_margin,
            "op_margin_trend": trend,
            "score": score,
            "criteria": {
                "upward_ok": upward >= MIN_UPWARD_REVISIONS,
                "dividend_ok": div_streak >= MIN_CONSECUTIVE_DIVIDEND_YEARS,
                "margin_trend_ok": bool(trend.get("increasing")),
            },
            "passes_all": (
                upward >= MIN_UPWARD_REVISIONS
                and div_streak >= MIN_CONSECUTIVE_DIVIDEND_YEARS
                and bool(trend.get("increasing"))
            ),
        }
        evaluated.append(rec)
        if rec["passes_all"]:
            passed.append(rec)

    evaluated.sort(
        key=lambda x: (x.get("announcement_date") or "9999", -x["score"])
    )
    passed.sort(
        key=lambda x: (x.get("announcement_date") or "9999", -x["score"])
    )

    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    SCREENED_PATH.write_text(
        json.dumps(
            {
                "generated_at": now,
                "criteria": {
                    "min_upward_revisions": MIN_UPWARD_REVISIONS,
                    "min_consecutive_dividend_years": MIN_CONSECUTIVE_DIVIDEND_YEARS,
                    "recent_years_window": RECENT_YEARS,
                    "rule": "upward_revisions>=min AND dividend_streak>=min AND op_margin trending up",
                },
                "count": len(passed),
                "items": passed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    ALL_EVAL_PATH.write_text(
        json.dumps(
            {
                "generated_at": now,
                "count": len(evaluated),
                "items": evaluated,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    log.info(
        "Screening complete: %d passed / %d evaluated",
        len(passed),
        len(evaluated),
    )

    # Update last_updated file used by the frontend
    (DATA / "last_updated.json").write_text(
        json.dumps(
            {
                "generated_at": now,
                "schedule_count": len(items),
                "evaluated_count": len(evaluated),
                "screened_count": len(passed),
                "schedule_sources": schedule.get("sources_used", []),
                "fundamentals_source": "irbank.net",
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
