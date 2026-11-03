"""Checks on the savings quote when a period has barely any income.

No pytest: the project ships a plain stdlib venv and these run with the same
interpreter as everything else in scripts/.

    .venv/Scripts/python.exe scripts/test_savings_rate_thin_income.py

The case these were written for: Diagnostiek for July 2026 showed a spaarquote
of -2888%. Nothing had miscounted — €42,20 of incidental credits came in
against €1.260,91 of spending, and net/income on those two numbers is -28,88.
The quote was reporting on the €42, not on the month. A denominator that small
is not an income, so the quote is withheld instead.
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


def conn_with_rows(rows):
    """A connection on the real schema holding (date, direction, amount,
    category) rows, where category 1 is the primary salary and category 2 is
    ordinary spending."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.executescript(
        """INSERT INTO topics (id, name, color, exclude_from_totals, is_fixed)
                VALUES (1, 'Salaris', '#10b981', 0, 0),
                       (2, 'Boodschappen', '#f59e0b', 0, 0);
           INSERT INTO categories (id, name, topic_id, is_primary_salary)
                VALUES (1, 'Salaris', 1, 1), (2, 'Boodschappen', 2, 0);"""
    )
    conn.executemany(
        "INSERT INTO transactions (date, name, direction, amount, category_id) "
        "VALUES (?,?,?,?,?)",
        [(d, "Teller", direction, amount, cat) for d, direction, amount, cat in rows],
    )
    return conn


def test_rate_needs_an_income_to_divide_by():
    print("the quote needs an income to divide by")
    check("july 2026 as reported", app._savings_rate(-1218.71, 42.20, 1260.91), None)
    check("no income at all is still none", app._savings_rate(-500.0, 0.0, 500.0), None)
    check("a normal month is unchanged",
          round(app._savings_rate(700.0, 2500.0, 1800.0), 3), 0.28)
    # Overspending stays visible as long as the income is a real one: that is
    # the month the guard must not swallow.
    check("spending half again your income still reads as a rate",
          round(app._savings_rate(-500.0, 1000.0, 1500.0), 3), -0.5)
    check("exactly at the floor is kept",
          round(app._savings_rate(-600.0, 300.0, 900.0), 3), -2.0)
    check("just under the floor is withheld",
          app._savings_rate(-610.0, 290.0, 900.0), None)
    # A period with income and nothing spent has no floor to fail.
    check("income without spending keeps its rate",
          app._savings_rate(100.0, 100.0, 0.0), 1.0)


def test_history_withholds_the_same_months():
    print("the history withholds the same months")
    rows = [
        ("2026-06-25", "Credit", 2500.0, 1),
        ("2026-06-26", "Debit", 1800.0, 2),
        # July: no salary, one small refund, a full month of spending.
        ("2026-07-10", "Credit", 42.20, 2),
        ("2026-07-11", "Debit", 1260.91, 2),
    ]
    conn = conn_with_rows(rows)
    period = app._resolve_period(conn, "2026-07", None, "1m")
    hist = {h["month"]: h["savings_rate"] for h
            in app._compute_savings_rate_history(conn, period, 2)}
    check("june keeps its rate", hist["2026-06"], 0.28)
    check("july is withheld rather than plotted at -28.88", hist["2026-07"], None)


def test_diagnostics_tile_explains_itself():
    print("the diagnostics tile explains itself")
    conn = conn_with_rows([
        ("2026-07-10", "Credit", 42.20, 2),
        ("2026-07-11", "Debit", 1260.91, 2),
    ])
    diag = app._build_diagnostics(conn, app._resolve_period(conn, "2026-07", None, "1m"))
    check("no quote is shown", diag["savings_rate"], None)
    check("nor an all-credits one behind the tooltip", diag["savings_rate_all"], None)
    check("the tile knows income was the problem", diag["savings_rate_thin"], True)
    check("the basis is still all credits", diag["savings_rate_basis"], "all")
    check("the month itself is unchanged", round(diag["net"], 2), -1218.71)


def test_a_salaried_month_is_untouched():
    print("a salaried month is untouched")
    conn = conn_with_rows([
        ("2026-06-25", "Credit", 2500.0, 1),
        ("2026-06-26", "Debit", 1800.0, 2),
    ])
    diag = app._build_diagnostics(conn, app._resolve_period(conn, "2026-06", None, "1m"))
    check("the salary rate is reported", round(diag["savings_rate"], 3), 0.28)
    check("on the salary basis", diag["savings_rate_basis"], "salary")
    check("and nothing is flagged as thin", diag["savings_rate_thin"], False)


def main():
    for test in (test_rate_needs_an_income_to_divide_by,
                 test_history_withholds_the_same_months,
                 test_diagnostics_tile_explains_itself,
                 test_a_salaried_month_is_untouched):
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
