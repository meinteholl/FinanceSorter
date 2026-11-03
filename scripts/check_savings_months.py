"""Why the Sparen page shows the months it shows. Read-only.

Run it in a normal terminal, not through an agent shell: under MSIX the
packaged app's %APPDATA%\\FinanceSorter is shadowed, so a sandboxed shell
reads a stale copy and the answer comes out wrong.

    .venv\\Scripts\\python.exe scripts\\check_savings_months.py

The Sparen page lists a month only when that month holds a transaction sitting
in a category under the Savings topic — it applies no date filter and no cap.
So a month goes missing for one of two reasons, and this says which: the rows
were never imported, or they were imported and never categorised as savings.
"""

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import DB_PATH, get_connection  # noqa: E402

# What a savings transfer tends to look like when it has not been categorised:
# an own-account transfer, which ING labels with these codes and descriptions.
HINTS = ("spaar", "savings", "oranje spaarrekening", "overboeking eigen rekening")


def main():
    print(f"database: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        print("  no database at that path — run the app once, or check you are")
        print("  in a normal terminal rather than a sandboxed one.")
        return 1

    with get_connection() as conn:
        span = conn.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM transactions").fetchone()
        print(f"transactions: {span[2]} rows, {span[0]} .. {span[1]}\n")

        all_months = {r[0]: r[1] for r in conn.execute(
            "SELECT substr(date,1,7) m, COUNT(*) FROM transactions GROUP BY m")}
        savings_months = {r[0]: r[1] for r in conn.execute(
            """SELECT substr(t.date,1,7) m, COUNT(*)
                 FROM transactions t
                 JOIN categories c ON c.id = t.category_id
                 JOIN topics tp ON tp.id = c.topic_id
                WHERE tp.name IN ('Savings', 'Sparen')
                GROUP BY m""")}

        # Rows that read like a savings transfer but carry no savings category:
        # these are the ones the Sparen page silently drops.
        missed = defaultdict(int)
        for r in conn.execute(
                """SELECT t.date, t.name, t.notifications, t.category_id,
                          COALESCE(tp.name, '') AS topic
                     FROM transactions t
                     LEFT JOIN categories c ON c.id = t.category_id
                     LEFT JOIN topics tp ON tp.id = c.topic_id"""):
            if r["topic"] in ("Savings", "Sparen"):
                continue
            hay = f"{r['name'] or ''} {r['notifications'] or ''}".lower()
            if any(h in hay for h in HINTS):
                missed[r["date"][:7]] += 1

        print(f"{'month':<9}{'all tx':>8}{'on Sparen':>11}{'looks like savings, not tagged':>33}")
        for m in sorted(all_months):
            flag = f"  <-- {missed[m]} missing" if missed.get(m) else ""
            print(f"{m:<9}{all_months[m]:>8}{savings_months.get(m, 0):>11}"
                  f"{missed.get(m, 0):>33}{flag}")

        gaps = [m for m in sorted(all_months) if m not in savings_months]
        print()
        if gaps:
            print("months with transactions but nothing on the Sparen page:")
            print("  " + ", ".join(gaps))
            print("  -> tag those transfers into a Savings category and they appear.")
        else:
            print("every month that has transactions also has savings on the Sparen page.")
        print(f"\nlatest month in the database: {max(all_months) if all_months else 'none'}")
        print("if months after that are missing, those statements were never imported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
