"""Checks that the totals count a match group at its net.

No pytest: the project ships a plain stdlib venv and these run with the same
interpreter as everything else in scripts/.

    .venv/Scripts/python.exe scripts/test_match_group_netting.py

The case these were written for: July 2026 showed EUR 42,20 of income beside
EUR 1.260,91 of spending, and no salary at all. The salary was there — EUR
2.905,43 on 20 July — but it was the anchor of a match group with seven
commuting fares under it, and every total filtered on `is_matched = 0`, which
drops a group whole, anchor included. The transactions list meanwhile drew
that same group at its net. The money was visible on one page and absent on
the other.

A group nets out by signed sum, which is only zero for the spend-plus-refund
shape the matcher was first written for. The tx_effective view does that sum
once, and the totals read it instead of the table.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import db  # noqa: E402

FAILURES = []

# The July 2026 shape, rounded to the rows that matter.
SALARY = 2905.43
FARES = [20.00, 20.00, 20.00, 20.00, 12.42, 20.00, 20.00]   # 132,42 together
GROCERIES = 1128.49                                          # the rest of the month
REFUND_ME = 42.20                                            # the lone small credit


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        FAILURES.append(label)


def july_conn():
    """July 2026: a salary anchoring seven fares, one ordinary spend, one
    small credit, and a fully refunded purchase to keep the old shape honest."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.executescript(
        """INSERT INTO topics (id, name, color, exclude_from_totals, is_fixed)
                VALUES (1, 'Salary', '#10b981', 0, 0),
                       (2, 'Transport', '#f59e0b', 0, 0),
                       (3, 'Food', '#ef4444', 0, 0);
           INSERT INTO categories (id, name, topic_id, is_primary_salary)
                VALUES (1, 'Salaris', 1, 1), (2, 'OV', 2, 0),
                       (3, 'Boodschappen', 3, 0);"""
    )

    def add(date, name, direction, amount, cat, parent=None, matched=0):
        cur = conn.execute(
            "INSERT INTO transactions (date, name, direction, amount, category_id,"
            " match_parent_id, is_matched) VALUES (?,?,?,?,?,?,?)",
            (date, name, direction, amount, cat, parent, matched),
        )
        return cur.lastrowid

    salary_id = add("2026-07-20", "OMODA SERVICEKANTOOR", "Credit", SALARY, 1, matched=1)
    for i, fare in enumerate(FARES):
        add(f"2026-07-{2 + i * 3:02d}", "OV-chipkaart", "Debit", fare, 2,
            parent=salary_id, matched=1)
    add("2026-07-05", "Jumbo", "Debit", GROCERIES, 3)
    add("2026-07-10", "Terugbetaling", "Credit", REFUND_ME, 3)
    # A spend that was fully refunded: the shape the matcher was written for,
    # which must still cancel to nothing.
    bought = add("2026-07-06", "MediaMarkt", "Debit", 77.95, 3, matched=1)
    add("2026-07-07", "Vriend", "Credit", 77.95, 3, parent=bought, matched=1)
    conn.commit()
    return conn


def test_the_view_nets_each_group():
    print("the view nets each group")
    conn = july_conn()
    rows = {r["name"]: r for r in conn.execute(
        "SELECT name, amount, direction, gross_amount, gross_direction FROM tx_effective")}
    check("the children are gone", "OV-chipkaart" in rows, False)
    check("the salary carries the remainder",
          round(rows["OMODA SERVICEKANTOOR"]["amount"], 2), 2773.01)
    check("and stays a credit", rows["OMODA SERVICEKANTOOR"]["direction"], "Credit")
    check("the gross is still readable",
          rows["OMODA SERVICEKANTOOR"]["gross_amount"], SALARY)
    check("a full refund cancels to nothing",
          round(rows["MediaMarkt"]["amount"], 2), 0.0)
    check("and keeps the anchor's own sign",
          rows["MediaMarkt"]["direction"], "Debit")
    check("an unmatched row is untouched", rows["Jumbo"]["amount"], GROCERIES)


def test_july_gets_its_salary_back():
    print("july gets its salary back")
    conn = july_conn()
    diag = app._build_diagnostics(conn, app._resolve_period(conn, "2026-07", None, "1m"))
    check("income is the netted salary plus the small credit",
          round(diag["income"], 2), round(2773.01 + REFUND_ME, 2))
    check("the fares are no longer spending too",
          round(diag["expenses"], 2), GROCERIES)
    check("net follows", round(diag["net"], 2), round(2773.01 + REFUND_ME - GROCERIES, 2))
    check("the quote is back on the salary basis", diag["savings_rate_basis"], "salary")
    check("and reads as a share of it",
          round(diag["savings_rate"], 3), round((2773.01 + REFUND_ME - GROCERIES) / 2773.01, 3))
    check("nothing is withheld for a thin base", diag["savings_rate_thin"], False)


def test_a_fare_is_not_spending_of_your_own():
    print("a fare under a salary is not spending of your own")
    conn = july_conn()
    diag = app._build_diagnostics(conn, app._resolve_period(conn, "2026-07", None, "1m"))
    topics = {p["name"]: round(float(p["amount"]), 2) for p in diag["pie"]}
    check("Transport is empty", topics.get("Transport"), None)
    check("Food holds the groceries alone", topics.get("Food"), GROCERIES)


def test_the_history_agrees_with_the_tile():
    print("the history agrees with the tile")
    conn = july_conn()
    period = app._resolve_period(conn, "2026-07", None, "1m")
    diag = app._build_diagnostics(conn, period)
    hist = {h["month"]: h["savings_rate"] for h
            in app._compute_savings_rate_history(conn, period, 1)}
    check("same month, same rate",
          hist["2026-07"], round(diag["savings_rate"], 3))


def main():
    for test in (test_the_view_nets_each_group,
                 test_july_gets_its_salary_back,
                 test_a_fare_is_not_spending_of_your_own,
                 test_the_history_agrees_with_the_tile):
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
