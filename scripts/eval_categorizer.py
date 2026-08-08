"""Measure how well a local model categorizes *this* user's transactions.

Model choice for this task shouldn't be guessed at. Every already-categorized
row in the database is a labeled example, so the accuracy of a given model on
this specific taxonomy is directly measurable — run this once per candidate tag
and pick from the numbers.

The split is by **merchant key, not by row**. If 'albert heijn' appears in the
few-shot block, scoring the model on another Albert Heijn row measures copying,
not categorization — and inflates the number precisely where it matters least,
since tier 3 only ever runs on merchants with no usable history. Holding out
whole merchants measures the real job: an unfamiliar name, seen once.

Usage:
    .venv/Scripts/python.exe scripts/eval_categorizer.py --model qwen3:4b
    .venv/Scripts/python.exe scripts/eval_categorizer.py --model qwen3:14b --limit 40
"""

import argparse
import hashlib
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_categorize  # noqa: E402
from app import _merchant_key  # noqa: E402
from db import get_connection  # noqa: E402

EVAL_FRACTION = 0.30


def _stable_bucket(key):
    """Deterministic 0.0-1.0 position for a merchant key.

    Hash-based rather than random so every model is scored on the identical
    split — otherwise run-to-run variance swamps the difference between models.
    """
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Ollama model tag, e.g. qwen3:4b")
    ap.add_argument("--url", default=llm_categorize.DEFAULT_URL)
    ap.add_argument("--limit", type=int, default=0, help="Cap eval rows (0 = all)")
    ap.add_argument("--verbose", action="store_true", help="Print every miss")
    ap.add_argument("--example-notes", action="store_true",
                    help="Include the description field in few-shot examples "
                         "(overrides llm_categorize.EXAMPLES_INCLUDE_NOTES)")
    args = ap.parse_args()

    if args.example_notes:
        llm_categorize.EXAMPLES_INCLUDE_NOTES = True

    probe = llm_categorize.probe(args.url)
    if not probe["ok"]:
        print(f"FAIL: {probe['error']}")
        return 1
    installed = {m["name"] for m in probe["models"]}
    if args.model not in installed:
        print(f"FAIL: model '{args.model}' not installed. Available: {', '.join(sorted(installed)) or '(none)'}")
        return 1

    with get_connection() as conn:
        cat_meta = {
            r["id"]: (r["topic_name"], r["name"])
            for r in conn.execute(
                """SELECT c.id, c.name, t.name AS topic_name
                   FROM categories c JOIN topics t ON t.id = c.topic_id"""
            ).fetchall()
        }
        labeled = conn.execute(
            """SELECT id, name, counterparty, code, transaction_type,
                      direction, amount, notifications, category_id
               FROM transactions
               WHERE category_id IS NOT NULL AND match_parent_id IS NULL
               ORDER BY date DESC, id DESC"""
        ).fetchall()

        if not labeled:
            print("FAIL: no categorized transactions to evaluate against.")
            return 1

        # Whole merchants go to one side of the split or the other.
        by_key = defaultdict(list)
        for row in labeled:
            by_key[_merchant_key(row["name"]) or f"tx:{row['id']}"].append(row)

        eval_keys = {k for k in by_key if _stable_bucket(k) < EVAL_FRACTION}
        eval_rows = [r for k in eval_keys for r in by_key[k]]
        held_out_ids = {r["id"] for r in eval_rows}

        if not eval_rows:
            print("FAIL: split produced an empty eval set.")
            return 1
        if len(held_out_ids) == len(labeled):
            print("FAIL: every merchant landed in the eval set — nothing left for examples.")
            return 1

        if args.limit:
            eval_rows = eval_rows[:args.limit]

        context = llm_categorize.build_context(conn, exclude_tx_ids=held_out_ids)

    print(f"model      : {args.model}")
    print(f"taxonomy   : {context['category_count']} categories")
    print(f"few-shot   : {context['example_count']} examples "
          f"(from {len(by_key) - len(eval_keys)} merchants)")
    print(f"eval set   : {len(eval_rows)} rows from {len(eval_keys)} unseen merchants")
    print("-" * 62)

    exact = topic_ok = abstained = failed = 0
    confusions = Counter()
    latencies = []

    for i, row in enumerate(eval_rows, 1):
        truth = row["category_id"]
        t0 = time.time()
        try:
            result = llm_categorize.classify(context, row, args.model, args.url)
        except llm_categorize.OllamaError as e:
            print(f"  ABORT after {i - 1} rows: {e}")
            failed += 1
            break
        latencies.append(time.time() - t0)

        if result is None:
            abstained += 1
            verdict = "abstain"
        elif result["category_id"] == truth:
            exact += 1
            topic_ok += 1
            verdict = "ok"
        else:
            got = cat_meta.get(result["category_id"], ("?", "?"))
            want = cat_meta.get(truth, ("?", "?"))
            if got[0] == want[0]:
                topic_ok += 1
                verdict = "topic-only"
            else:
                verdict = "miss"
            confusions[(f"{want[0]}>{want[1]}", f"{got[0]}>{got[1]}")] += 1
            if args.verbose:
                print(f"  [{verdict}] {row['name'][:38]:38s} want={want[1]:20s} got={got[1]}")

        if i % 10 == 0:
            print(f"  … {i}/{len(eval_rows)}")

    n = len(latencies)  # rows actually attempted (a transport abort stops early)
    if not n:
        print("No rows scored.")
        return 1

    decided = n - abstained
    print("-" * 62)
    print(f"rows scored      : {n}")
    print(f"exact category   : {exact}/{n}  ({exact / n:.0%})")
    print(f"correct topic    : {topic_ok}/{n}  ({topic_ok / n:.0%})")
    if decided:
        print(f"accuracy when it answered : {exact}/{decided}  ({exact / decided:.0%})")
    print(f"abstained        : {abstained}/{n}  ({abstained / n:.0%})"
          "   <- declining on an unplaceable row is correct, not a miss")
    print(f"latency          : {sum(latencies) / n:.2f}s avg, {max(latencies):.2f}s worst")

    if confusions:
        print("\ntop confusions (want -> got):")
        for (want, got), count in confusions.most_common(8):
            print(f"  {count:2d}x  {want}  ->  {got}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
