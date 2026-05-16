"""Run with:  python diagnose.py
Prints why the suggestion engine is or isn't firing for the rows currently visible.
Safe to delete after diagnosing.
"""
from collections import Counter, defaultdict
from db import get_connection
from app import _merchant_key, _counterparty_looks_specific, SUGGESTION_MIN_SAMPLES, SUGGESTION_MIN_DOMINANCE

with get_connection() as conn:
    total = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
    cat   = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE category_id IS NOT NULL").fetchone()["n"]
    uncat = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE category_id IS NULL").fetchone()["n"]

    print(f"📊 transactions: {total} total · {cat} categorized · {uncat} uncategorized")
    print(f"📐 thresholds:   ≥{SUGGESTION_MIN_SAMPLES} samples AND ≥{int(SUGGESTION_MIN_DOMINANCE*100)}% dominance\n")

    # Build the same indexes the suggestion engine builds
    rows = conn.execute(
        """SELECT t.name, t.counterparty, t.category_id, c.name AS cname
           FROM transactions t JOIN categories c ON c.id = t.category_id
           WHERE t.category_id IS NOT NULL"""
    ).fetchall()

    by_merchant = defaultdict(lambda: defaultdict(int))
    by_cp = defaultdict(lambda: defaultdict(int))
    for r in rows:
        k = _merchant_key(r["name"])
        if k:
            by_merchant[k][r["cname"]] += 1
        cp = (r["counterparty"] or "").strip()
        if cp and _counterparty_looks_specific(cp):
            by_cp[cp][r["cname"]] += 1

    # Summarize merchant groups by sample count, descending
    print("🏪 Top merchant groups in your categorized history:")
    summary = sorted(by_merchant.items(), key=lambda kv: -sum(kv[1].values()))[:15]
    for key, cats in summary:
        total = sum(cats.values())
        top_cat, top_n = max(cats.items(), key=lambda kv: kv[1])
        dom = top_n / total
        ok_n = total >= SUGGESTION_MIN_SAMPLES
        ok_d = dom >= SUGGESTION_MIN_DOMINANCE
        flag = "✅" if (ok_n and ok_d) else ("📉" if not ok_n else "🎲")
        print(f"  {flag} '{key}': {total} rows, {top_n}/{total} = {dom:.0%} {top_cat}")

    # Now check uncategorized rows: would any of them trigger?
    uncat_rows = conn.execute(
        "SELECT id, name, counterparty FROM transactions WHERE category_id IS NULL ORDER BY date DESC LIMIT 20"
    ).fetchall()

    print(f"\n🔎 Checking up to 20 uncategorized rows:")
    triggers = 0
    for r in uncat_rows:
        k = _merchant_key(r["name"])
        cp = (r["counterparty"] or "").strip()
        cat_via_cp = bool(cp and _counterparty_looks_specific(cp) and cp in by_cp)
        cat_via_merchant = bool(k and k in by_merchant)
        if not (cat_via_cp or cat_via_merchant):
            continue
        # Would it pass thresholds?
        candidates = by_cp[cp] if cat_via_cp else by_merchant[k]
        total = sum(candidates.values())
        top_cat, top_n = max(candidates.items(), key=lambda kv: kv[1])
        dom = top_n / total
        if total >= SUGGESTION_MIN_SAMPLES and dom >= SUGGESTION_MIN_DOMINANCE:
            triggers += 1
            print(f"  ✅ '{r['name']}' → would suggest {top_cat} ({total} samples, {dom:.0%})")
        else:
            why = []
            if total < SUGGESTION_MIN_SAMPLES: why.append(f"only {total} samples")
            if dom < SUGGESTION_MIN_DOMINANCE: why.append(f"only {dom:.0%} dominance")
            print(f"  ⏸  '{r['name']}' → near miss: {', '.join(why)}")

    if triggers == 0 and uncat > 0:
        print(f"\n💡 No uncategorized row meets both thresholds. Either:")
        print(f"   · keep categorizing (the engine learns from history), or")
        print(f"   · loosen thresholds in app.py (SUGGESTION_MIN_SAMPLES / SUGGESTION_MIN_DOMINANCE)")
