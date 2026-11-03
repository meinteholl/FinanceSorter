"""Checks on merchant keying and reason grounding — the tier-3 suggester.

No pytest: the project ships a plain stdlib venv and these run with the same
interpreter as everything else in scripts/.

    .venv/Scripts/python.exe scripts/test_categorizer_grounding.py

The case these were written for: a -16,90 transfer from "Mw AB Voorbeeld" whose
description read "Lunch Deck" was shown as Cadeautjes, under the sentence
"Omschrijving vermeldt expliciet 'Cadeautje'." Nothing had hallucinated. The
model had been asked about a *different* Voorbeeld transfer, one that really was
a present, and both rows keyed to ('merchant', 'mw ab') — the honorific plus her
initials, with the surname discarded — so one verdict and one sentence about a
present were replayed onto a lunch.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import llm_categorize  # noqa: E402

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        FAILURES.append(label)


def row(**kw):
    """A transactions row as sqlite3.Row, which is what production passes."""
    kw.setdefault("id", 1)
    kw.setdefault("code", "GT")
    kw.setdefault("counterparty", "NL98INGB0008856393")
    kw.setdefault("transaction_type", "Online bankieren")
    kw.setdefault("notifications", "")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(kw)
    placeholders = ", ".join("?" * len(kw))
    conn.execute(f"CREATE TABLE t ({cols})")
    conn.execute(f"INSERT INTO t ({cols}) VALUES ({placeholders})", list(kw.values()))
    return conn.execute("SELECT * FROM t").fetchone()


LUNCH = row(
    id=191,
    name="Mw AB Voorbeeld",
    notifications="Naam: Mw AB Voorbeeld Omschrijving: Lunch Deck "
                  "IBAN: NL98INGB0008856393 Valutadatum: 01-06-2026",
)


def test_merchant_key_keeps_people_apart():
    print("merchant key")
    # The honorific and the initials identify nobody; the surname does.
    check("honorific + initials are dropped", app._merchant_key("Mw AB Voorbeeld"), "voorbeeld")
    check("two women no longer collide",
          app._merchant_key("Mw AB Voorbeeld") != app._merchant_key("Mw AB Vermeer"), True)
    check("tussenvoegsel survives", app._merchant_key("Mevr A B de Vries"), "de vries")
    # An all-caps name gives no way to tell an initial from a first name, so
    # nothing after the honorific is stripped and Jan stays apart from Piet.
    check("all-caps keeps first names",
          app._merchant_key("DHR JAN DE BOER") != app._merchant_key("DHR PIET DE BOER"), True)
    check("shops are untouched", app._merchant_key("Albert Heijn 1234"), "albert heijn")
    check("known chains still collapse", app._merchant_key("AH to go 1234"), "albert heijn")
    check("brand + place is not initials", app._merchant_key("IKEA Groningen"), "ikea")


def test_people_are_keyed_per_transaction():
    print("suggestion scope")
    check("honorific transfer", app._llm_suggestion_key(LUNCH), ("tx", "191"))
    check("initials transfer",
          app._llm_suggestion_key(row(id=7, name="P.J. Bosman")), ("tx", "7"))
    check("shop generalizes",
          app._llm_suggestion_key(row(id=8, name="Albert Heijn 1234", code="BA")),
          ("merchant", "albert heijn"))
    # A card terminal is a shop by definition, so a shop name that happens to
    # read like initials must not be split into per-row lookups.
    check("card payment is never a person",
          app._llm_suggestion_key(
              row(id=9, name="LS Greenwoods Middelburg NLD", code="BA")),
          ("merchant", "ls greenwoods"))


def test_reason_must_be_grounded():
    print("reason grounding")
    vocab = {"Uiteten", "Lunch", "Food"}

    def g(reason, tx=LUNCH):
        return llm_categorize.ground_reason(reason, tx, vocab)

    check("the reported sentence is refused",
          g("Omschrijving vermeldt expliciet 'Cadeautje'."), "")
    check("a quote leaked from another row is refused",
          g("Omschrijving vermeldt expliciet 'Cadeautje voor Jordi'."), "")
    kept = "Omschrijving vermeldt 'Lunch Deck', wat duidt op lunchkosten."
    check("a real quote is kept", g(kept), kept)
    check("case and spacing do not matter",
          g('Omschrijving vermeldt "lunch   deck".'),
          'Omschrijving vermeldt "lunch   deck".')
    # World knowledge is the model's opinion, not a claim about the text; the
    # user judges it by accepting or rejecting.
    opinion = "Albert Heijn is een bekende boodschappenketen."
    check("unquoted claims are left alone", g(opinion), opinion)
    # A Dutch genitive is not an opening quote.
    genitive = "Geeta's is een bekende Indiase restaurantketen."
    check("genitive apostrophe is not a quote", g(genitive), genitive)
    cat = "Capi Lounge is een restaurant en past bij 'Uiteten'."
    check("a quoted category name is allowed", g(cat), cat)
    check("one bad quote discards the whole sentence",
          g("Omschrijving vermeldt 'Lunch Deck' en 'Cadeautje'."), "")
    check("empty stays empty", g(""), "")
    check("name field counts as text",
          g("Naam bevat 'Voorbeeld'."), "Naam bevat 'Voorbeeld'.")


def main():
    for test in (test_merchant_key_keeps_people_apart,
                 test_people_are_keyed_per_transaction,
                 test_reason_must_be_grounded):
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
