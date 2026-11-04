"""Checks on the savings-rate history window handed to the advice model.

No pytest: the project ships a plain stdlib venv and these run with the same
interpreter as everything else in scripts/.

    .venv/Scripts/python.exe scripts/test_savings_history_window.py

The case these were written for: a 2026 advice opened with "de historie van je
spaarquote minder dan drie maanden aan data bevat" while the Sparen page listed
eight months of saving for that same year. Nothing had miscounted. A year view
anchors on December, and the payload asked for a fixed six months ending at the
anchor — Jul–Dec 2026 — of which only two had been imported. The model reported
what it was given; the window was wrong.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import db  # noqa: E402

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        FAILURES.append(label)


def conn_with_months(months):
    """A connection on the real schema holding one salary credit and one spend
    per listed month, which is the least that makes a month count as having a
    savings rate."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.executescript(
        """INSERT INTO topics (id, name, color) VALUES (1, 'Salary', '#10b981'),
                                                      (2, 'Food', '#f59e0b');
           INSERT INTO categories (id, name, topic_id, is_primary_salary)
                VALUES (1, 'Salaris', 1, 1), (2, 'Boodschappen', 2, 0);"""
    )
    for m in months:
        conn.executemany(
            "INSERT INTO transactions (date, name, direction, amount, category_id)"
            " VALUES (?,?,?,?,?)",
            [(f"{m}-01", "Salaris", "Credit", 2500.0, 1),
             (f"{m}-05", "Jumbo", "Debit", 1800.0, 2)],
        )
    return conn


def year_period(conn, year=2026):
    """The year cell of the Diagnostiek picker: range=12m anchored on December."""
    return app._resolve_period(conn, f"{year}-12", None, "12m")


def test_window_follows_the_period():
    print("window follows the selected period")
    conn = conn_with_months([])
    check("a year view spans twelve months",
          app._savings_history_window(year_period(conn)), 12)
    check("a single month keeps six months of trend",
          app._savings_history_window(app._resolve_period(conn, "2026-08", None, "1m")), 6)
    check("a quarter keeps the six-month floor",
          app._savings_history_window(app._resolve_period(conn, "2026-08", None, "3m")), 6)
    check("six months is unchanged",
          app._savings_history_window(app._resolve_period(conn, "2026-08", None, "6m")), 6)
    # A salary period carries no range_size; it must not collapse to zero months.
    check("a period without a range size falls back to six",
          app._savings_history_window({"start": "2026-08-01", "month": ""}), 6)


def test_year_advice_sees_the_whole_year():
    print("a year advice sees the whole year")
    months = [f"2026-{m:02d}" for m in range(1, 9)]  # Jan–Aug imported, as in the report
    conn = conn_with_months(months)
    period = year_period(conn)

    old = app._compute_savings_rate_history(conn, period, 6)
    check("the old fixed window started after the data",
          [h["month"] for h in old][0], "2026-07")
    check("the old fixed window found two months",
          len([h for h in old if h["savings_rate"] is not None]), 2)

    new = app._compute_savings_rate_history(conn, period, app._savings_history_window(period))
    check("the period window starts in January", [h["month"] for h in new][0], "2026-01")
    check("the period window covers twelve months", len(new), 12)
    check("every imported month is counted",
          [h["month"] for h in new if h["savings_rate"] is not None], months)
    check("no month is invented past the data",
          [h["savings_rate"] for h in new if h["month"] > "2026-08"], [None] * 4)
    # The rule in the prompt that produced the sentence under investigation.
    check("the model is no longer told it has under three months",
          len([h for h in new if h["savings_rate"] is not None]) < 3, False)


def test_single_month_view_is_untouched():
    print("a single-month view is untouched")
    conn = conn_with_months([f"2026-{m:02d}" for m in range(1, 9)])
    period = app._resolve_period(conn, "2026-08", None, "1m")
    hist = app._compute_savings_rate_history(conn, period, app._savings_history_window(period))
    check("still six months ending at the anchor",
          [h["month"] for h in hist],
          ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"])


def main():
    for test in (test_window_follows_the_period,
                 test_year_advice_sees_the_whole_year,
                 test_single_month_view_is_untouched):
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
