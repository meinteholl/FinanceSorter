import csv
import io
import os
import re
import sqlite3
import sys
import threading
from calendar import monthrange
from datetime import datetime, date
from collections import defaultdict, Counter

from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, flash, abort,
    session, g
)
from werkzeug.security import generate_password_hash, check_password_hash

from db import init_db, get_connection
import llm_categorize


# PyInstaller --onefile unpacks templates/ and static/ into a temp dir exposed
# as sys._MEIPASS. Tell Flask to look there when frozen; in dev the defaults
# (templates/static next to this file) are correct.
if getattr(sys, "frozen", False):
    _BASE = sys._MEIPASS  # type: ignore[attr-defined]
    app = Flask(
        __name__,
        template_folder=os.path.join(_BASE, "templates"),
        static_folder=os.path.join(_BASE, "static"),
    )
else:
    app = Flask(__name__)
app.secret_key = "ing-finance-sorter-local"


# ---------- auth ----------

# Endpoints reachable without a logged-in session. Static assets and the auth
# pages themselves must stay open or the user can never reach them.
PUBLIC_ENDPOINTS = {"login", "signup", "logout", "static", "health"}


def _current_user():
    """Resolve the logged-in user from session, cached on flask.g per-request."""
    if "user" in g:
        return g.user
    uid = session.get("user_id")
    if not uid:
        g.user = None
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, name, email FROM users WHERE id = ?", (uid,)
        ).fetchone()
    g.user = dict(row) if row else None
    if g.user is None:
        # Stale session pointing at a deleted user — clear it.
        session.pop("user_id", None)
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _current_user() is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def _require_auth():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if _current_user() is None:
        return redirect(url_for("login", next=request.path))
    return None


@app.context_processor
def _inject_user():
    return {"current_user": _current_user()}


# ---------- helpers ----------


def parse_amount(s):
    if s is None or s == "":
        return None
    return float(str(s).replace(".", "").replace(",", ".")) if "," in str(s) else float(s)


def parse_amount_query(q):
    """Interpret a search term as an amount. Returns (low, high) or None.

    Amounts are stored unsigned — `direction` carries the sign — so a leading
    minus is dropped rather than used to filter: typing "-12,50" finds the
    €12,50 expense, which is what someone reading a statement means.

        "12,50" / "12.50"  -> that exact amount (float tolerance)
        "1.234,56"         -> Dutch thousands separator, exact
        "12"               -> the whole-euro band 12,00-12,99

    Whole numbers match a band because "12" in a search box means "about
    twelve euros", not "exactly twelve euros and zero cents".

    Returns None for anything not purely numeric, so ordinary text searches are
    unaffected.
    """
    s = (q or "").replace("€", "").strip().lstrip("+-").strip()
    if not s or not re.fullmatch(r"[\d.,]+", s):
        return None

    if "," in s:
        # Dutch style: dots group thousands, the comma is the decimal point.
        whole, _, frac = s.partition(",")
        if not re.fullmatch(r"\d{1,3}(?:\.\d{3})*|\d+", whole) or not re.fullmatch(r"\d{1,2}", frac):
            return None
        value = float(f"{whole.replace('.', '')}.{frac}")
        return (value - 0.005, value + 0.005)

    if "." in s:
        whole, _, frac = s.partition(".")
        # Only a genuine 1-2 digit decimal; "1.234" is ambiguous, so leave it
        # to the text search rather than guessing thousands vs decimals.
        if not whole.isdigit() or not re.fullmatch(r"\d{1,2}", frac):
            return None
        value = float(f"{whole}.{frac}")
        return (value - 0.005, value + 0.005)

    if not s.isdigit():
        return None
    value = float(s)
    return (value, value + 1.0)


def parse_date(s):
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    # ASN format: DD/MM/YYYY or DD-MM-YYYY
    if len(s) == 10 and s[2] in "/-" and s[5] in "/-":
        return f"{s[6:10]}-{s[3:5]}-{s[0:2]}"
    return s


_MONTH_LABELS_NL = {
    1: "jan", 2: "feb", 3: "mrt", 4: "apr", 5: "mei", 6: "jun",
    7: "jul", 8: "aug", 9: "sep", 10: "okt", 11: "nov", 12: "dec",
}


def format_month_label(ym: str) -> str:
    """Turn a 'YYYY-MM' string into 'mmm YYYY' (e.g. '2026-05' -> 'mei 2026')."""
    if not ym:
        return ""
    try:
        dt = datetime.strptime(ym, "%Y-%m")
        return f"{_MONTH_LABELS_NL[dt.month]} {dt.year}"
    except (ValueError, KeyError):
        return ym


# ---------- bank format detection ----------

ING_REQUIRED = {"Date", "Name / Description", "Amount (EUR)"}
# ING also exports in Dutch, comma-delimited, without the balance/tag columns.
# Same bank, same account, different download button — so it needs its own
# mapping rather than being treated as a broken file.
ING_NL_REQUIRED = {"Datum", "Naam / Omschrijving", "Bedrag (EUR)"}
ASN_REQUIRED = {"Datum", "Bedrag bij / af", "Omschrijving"}

# Delimiters to try, in order. ING English uses ';', ING Dutch uses ','.
CSV_DELIMITERS = (";", ",")


def detect_format(fieldnames):
    fields = set(fieldnames or [])
    if ING_REQUIRED.issubset(fields):
        return "ing"
    if ING_NL_REQUIRED.issubset(fields):
        return "ing_nl"
    if ASN_REQUIRED.issubset(fields):
        return "asn"
    return None


def _asn_name_from_description(desc: str) -> str:
    """ASN BEA rows have empty Naam; merchant lives at the start of Omschrijving,
    typically followed by `>LOCATION`. Strip that tail."""
    if not desc:
        return ""
    head = desc.split(">", 1)[0]
    return re.sub(r"\s+", " ", head).strip()


def extract_ing(row):
    return {
        "date": parse_date(row.get("Date")),
        "name": (row.get("Name / Description") or "").strip(),
        "account": (row.get("Account") or "").strip(),
        "counterparty": (row.get("Counterparty") or "").strip(),
        "code": (row.get("Code") or "").strip(),
        "direction": (row.get("Debit/credit") or "").strip(),
        "amount": parse_amount(row.get("Amount (EUR)")),
        "transaction_type": (row.get("Transaction type") or "").strip(),
        "notifications": (row.get("Notifications") or "").strip(),
        "balance": parse_amount(row.get("Resulting balance")),
        "tag": (row.get("Tag") or "").strip(),
    }


def extract_ing_nl(row):
    """ING's Dutch export. Same data as extract_ing under different headers.

    Two things differ beyond the names: direction is 'Af'/'Bij' rather than
    'Debit'/'Credit', and the balance and tag columns are absent entirely, so
    both come back empty. `Mutatiesoort` keeps its Dutch wording — it is
    descriptive only, and inventing a translation would be guessing.
    """
    af_bij = (row.get("Af Bij") or "").strip().lower()
    if af_bij == "af":
        direction = "Debit"
    elif af_bij == "bij":
        direction = "Credit"
    else:
        direction = ""

    return {
        "date": parse_date(row.get("Datum")),
        "name": (row.get("Naam / Omschrijving") or "").strip(),
        "account": (row.get("Rekening") or "").strip(),
        "counterparty": (row.get("Tegenrekening") or "").strip(),
        "code": (row.get("Code") or "").strip(),
        "direction": direction,
        "amount": parse_amount(row.get("Bedrag (EUR)")),
        "transaction_type": (row.get("Mutatiesoort") or "").strip(),
        "notifications": (row.get("Mededelingen") or "").strip(),
        # Not present in this export; parse_amount(None) is None.
        "balance": parse_amount(row.get("Saldo na mutatie")),
        "tag": (row.get("Tag") or "").strip(),
    }


def extract_asn(row):
    signed = parse_amount(row.get("Bedrag bij / af"))
    if signed is None:
        amount = None
        direction = ""
    else:
        direction = "Debit" if signed < 0 else "Credit"
        amount = abs(signed)

    name = (row.get("Naam") or "").strip()
    description = (row.get("Omschrijving") or "").strip()
    if not name:
        name = _asn_name_from_description(description)

    balance_before = parse_amount(row.get("Saldo voor boeking"))
    balance = (
        balance_before + signed
        if balance_before is not None and signed is not None
        else None
    )

    return {
        "date": parse_date(row.get("Datum")),
        "name": name,
        "account": (row.get("Je rekening") or "").strip(),
        "counterparty": (row.get("Van / naar") or "").strip(),
        "code": (row.get("Code") or "").strip(),
        "direction": direction,
        "amount": amount,
        "transaction_type": (row.get("Type") or "").strip(),
        "notifications": description,
        "balance": balance,
        "tag": (row.get("Categorie") or "").strip(),
    }


EXTRACTORS = {"ing": extract_ing, "ing_nl": extract_ing_nl, "asn": extract_asn}


CARD_PREFIX_RE = re.compile(r"^[A-Z]{2,5}\*")
_LONG_DIGITS_RE = re.compile(r"\b\d{4,}\b")

# Chains whose locations and store numbers vary but whose category never does.
# Each entry: (canonical_key, [variants the import data may use]).
# Variants matched as whole prefix of the lowercased token stream.
KNOWN_CHAINS = [
    ("albert heijn", ["albert heijn", "ah to go", "ah xpress", "ah", "albertheijn"]),
    ("jumbo",         ["jumbo"]),
    ("lidl",          ["lidl"]),
    ("aldi",          ["aldi"]),
    ("dirk",          ["dirk van den broek", "dirk"]),
    ("plus",          ["plus supermarkt", "plus"]),
    ("spar",          ["spar"]),
    ("hema",          ["hema"]),
    ("action",        ["action"]),
    ("kruidvat",      ["kruidvat"]),
    ("etos",          ["etos"]),
    ("blokker",       ["blokker"]),
    ("mcdonalds",     ["mcdonald s", "mcdonalds", "mcd"]),
    ("burger king",   ["burger king"]),
    ("kfc",           ["kfc"]),
    ("subway",        ["subway"]),
    ("starbucks",     ["starbucks", "sbux"]),
    ("dominos",       ["domino s", "dominos"]),
    ("shell",         ["shell"]),
    ("bp",            ["bp"]),
    ("esso",          ["esso"]),
    ("ns",            ["ns reizigers", "ns groep", "ns intern", "nederlandse spoorwegen"]),
    ("ov chipkaart",  ["ov chipkaart", "ovpay"]),
    ("gvb",           ["gvb"]),
    ("ret",           ["ret"]),
    ("htm",           ["htm"]),
    ("bol",           ["bol com", "bol"]),
    ("coolblue",      ["coolblue"]),
    ("mediamarkt",    ["mediamarkt", "media markt"]),
    ("ikea",          ["ikea"]),
    ("praxis",        ["praxis"]),
    ("gamma",         ["gamma"]),
    ("spotify",       ["spotify"]),
    ("netflix",       ["netflix"]),
    ("disney",        ["disney plus", "disney"]),
    ("youtube",       ["youtube", "google youtube"]),
    ("amazon",        ["amazon", "amzn"]),
    ("apple",         ["apple com", "apple"]),
    ("google",        ["google"]),
    ("paypal",        ["paypal"]),
]


def _clean_merchant_text(s: str) -> str:
    """Strip card prefix and long digit runs (store numbers, terminal IDs)."""
    if not s:
        return ""
    s = CARD_PREFIX_RE.sub("", s)
    s = _LONG_DIGITS_RE.sub(" ", s)
    return s


# ---------- rule engine ----------

def _row_get(row, key, default=None):
    """Read a value from sqlite3.Row OR dict — both are passed around in the rule engine."""
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        if key in row.keys():
            return row[key]
    except (AttributeError, IndexError):
        pass
    return default


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards so user text matches literally. The rule engine
    (_rule_matches) does plain substring containment, so any SQL LIKE that
    mirrors it must neutralize %/_ — pair with ESCAPE '\\' in the query."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_optional_amount(v):
    """Lenient amount parsing — accepts '13.99', '13,99', '1.234,56', or empty."""
    if v is None or str(v).strip() == "":
        return None
    try:
        return parse_amount(str(v).strip())
    except (ValueError, TypeError):
        return None


# Order rules by specificity — longest pattern first, then newest. So
# 'Albert Heijn Express' beats 'Albert Heijn' beats 'AH', regardless of insert order.
RULE_ORDER_SQL = "ORDER BY LENGTH(pattern) DESC, id DESC"
RULE_SELECT_COLS = (
    "id, pattern, field, category_id, "
    "COALESCE(word_boundary, 0) AS word_boundary, amount_min, amount_max"
)


def _rule_matches(rule, tx_row) -> bool:
    """Single source of truth for whether a rule matches a transaction.

    Accepts sqlite3.Row OR dict for both arguments — convenient for preview, where
    the 'rule' isn't yet persisted.
    """
    field = _row_get(rule, "field", "name")
    pattern = _row_get(rule, "pattern", "") or ""
    if not pattern or field not in ("name", "counterparty", "notifications"):
        return False

    haystack = _row_get(tx_row, field, "") or ""
    if not haystack:
        return False

    if _row_get(rule, "word_boundary", 0):
        try:
            if not re.search(r"\b" + re.escape(pattern) + r"\b", haystack, re.IGNORECASE):
                return False
        except re.error:
            return False
    else:
        if pattern.lower() not in haystack.lower():
            return False

    amount_min = _row_get(rule, "amount_min")
    amount_max = _row_get(rule, "amount_max")
    if amount_min is not None or amount_max is not None:
        amount = _row_get(tx_row, "amount")
        if amount is None:
            return False
        if amount_min is not None and amount < amount_min:
            return False
        if amount_max is not None and amount > amount_max:
            return False

    return True


def apply_rules_to_transaction(conn, tx_row) -> int | None:
    """Return category_id if any rule matches, else None. Increments hit counter."""
    rules = conn.execute(
        f"SELECT {RULE_SELECT_COLS} FROM rules {RULE_ORDER_SQL}"
    ).fetchall()
    for r in rules:
        if _rule_matches(r, tx_row):
            conn.execute("UPDATE rules SET hits = hits + 1 WHERE id = ?", (r["id"],))
            return r["category_id"]
    return None


def reapply_rules_to_uncategorized(conn) -> int:
    rows = conn.execute(
        "SELECT id, name, counterparty, notifications, amount FROM transactions WHERE category_id IS NULL"
    ).fetchall()
    rules = conn.execute(
        f"SELECT {RULE_SELECT_COLS} FROM rules {RULE_ORDER_SQL}"
    ).fetchall()
    matched = 0
    for tx in rows:
        for r in rules:
            if _rule_matches(r, tx):
                conn.execute(
                    "UPDATE transactions SET category_id = ?, categorization_source = 'rule' WHERE id = ?",
                    (r["category_id"], tx["id"]),
                )
                conn.execute("UPDATE rules SET hits = hits + 1 WHERE id = ?", (r["id"],))
                matched += 1
                break
    conn.commit()
    return matched


# ---------- suggestion engine (history-based) ----------

# Confidence thresholds.
SUGGESTION_MIN_SAMPLES = 5         # SUGGEST tier minimum
SUGGESTION_MIN_DOMINANCE = 0.80    # SUGGEST tier dominance
AUTO_MIN_SAMPLES = 10              # AUTO tier minimum
AUTO_MIN_DOMINANCE = 0.90          # AUTO tier dominance
AUTO_MIN_SIGNALS = 3               # AUTO tier needs ≥3 agreeing signals
SUGGEST_MIN_SIGNALS = 2            # SUGGEST tier needs ≥2 agreeing signals
AMOUNT_BAND_TOLERANCE = 0.15       # ±15% for amount-band match
RECURRING_AMOUNT_TOLERANCE = 0.05  # ±5% for recurring detection
CODE_AGREEMENT_THRESHOLD = 0.80    # ≥80% of supporting samples share the candidate's code

# ING transaction codes that are never P2P / always business-issued.
BUSINESS_CODES = frozenset({"BA", "IC"})

# Counterparties that aggregate many distinct merchants under one IBAN.
# A single PayPal/Klarna IBAN can bill 10+ different subscriptions, so we
# sub-bucket these by amount in the subscriptions view.
PAYMENT_PROCESSOR_PATTERNS = (
    "paypal",
    "klarna",
    "stripe",
    "mollie",
    "adyen",
    "buckaroo",
)


def _is_payment_processor(name):
    n = (name or "").lower()
    return any(p in n for p in PAYMENT_PROCESSOR_PATTERNS)

# Business-form indicators in the historical name field — signals "merchant".
BUSINESS_NAME_RE = re.compile(
    r"\b(b\.?v\.?|n\.?v\.?|holding|gmbh|ltd|inc|corp|s\.?a\.?|stichting|vof)\b",
    re.IGNORECASE,
)


def _counterparty_looks_specific(s: str) -> bool:
    """Filter out generic transaction-type codes from counterparty matching.

    A real counterparty identifier (IBAN, account number) has digits and is
    reasonably long. Generic codes like 'BEA', 'GEA', 'TIKKIE' have neither.
    """
    if not s:
        return False
    s = s.strip()
    if len(s) < 8:
        return False
    return any(c.isdigit() for c in s)


def _counterparty_is_business(candidate_code: str, history: list) -> bool:
    """Decide whether a counterparty IBAN can be safely used as a category signal.

    Personal counterparties (Tikkie friends, family transfers) routinely span
    many categories — letting their history drive suggestions produces the
    over-generalization the user reported. We only treat a counterparty as
    business if at least one structural signal says so:

    - The candidate row's ING code is BA (card / POS) or IC (direct debit) —
      these channels are never used for true P2P.
    - Any historical sample for this counterparty had a BA/IC code, or its
      name contains a business form (B.V., GmbH, ...).
    - Repeated identical amounts across ≥2 distinct months (subscription /
      recurring bill).
    - ≥10 historical samples with low coefficient-of-variation in amount.
    """
    if candidate_code in BUSINESS_CODES:
        return True
    if not history:
        return False
    for s in history:
        if (s["code"] or "") in BUSINESS_CODES:
            return True
        if BUSINESS_NAME_RE.search(s["name"] or ""):
            return True
    amount_counts = Counter()
    months_per_amount = defaultdict(set)
    for s in history:
        if s["amount"] is None:
            continue
        amt = round(float(s["amount"]), 2)
        amount_counts[amt] += 1
        months_per_amount[amt].add((s["date"] or "")[:7])
    for amt, n in amount_counts.items():
        if n >= 3 and len(months_per_amount[amt]) >= 2:
            return True
    amts = [float(s["amount"]) for s in history if s["amount"] is not None]
    if len(amts) >= 10:
        mean = sum(amts) / len(amts)
        if mean > 0:
            var = sum((a - mean) ** 2 for a in amts) / len(amts)
            cv = (var ** 0.5) / mean
            if cv < 0.4:
                return True
    return False


def _merchant_key(name: str) -> str:
    """Conservative merchant identifier used to group similar transactions.

    1. Strip card prefix and long digit runs (store numbers, terminal IDs).
    2. If the cleaned token stream starts with a known chain variant, collapse
       to the canonical chain name. Lets 'Hema Den Haag CS' and 'AH to go 1234'
       group with their siblings regardless of city or store-number.
    3. Otherwise fall back to the first two alphabetic tokens, lowercased.
    """
    if not name:
        return ""
    cleaned = _clean_merchant_text(name)
    tokens = re.findall(r"[A-Za-z][A-Za-z'&]+", cleaned)
    if not tokens:
        return ""
    lowered = " ".join(t.lower() for t in tokens)
    for canonical, variants in KNOWN_CHAINS:
        for v in sorted(variants, key=len, reverse=True):
            if lowered == v or lowered.startswith(v + " "):
                return canonical
    return " ".join(t.lower() for t in tokens[:2])


def _detect_recurring_groups(categorized):
    """Find (scope, key, code) groups that look like a recurring subscription/bill.

    A group qualifies when:
      - ≥3 categorized samples,
      - amounts cluster within ±5% of the median,
      - ≥2 consecutive intervals between sorted dates fall in the 25-35 day band,
      - one category dominates ≥80%.
    Returns: { (scope, key, code) -> {cid, median_amount, sample_count} }.
    """
    groups = defaultdict(list)
    for s in categorized:
        cp = (s["counterparty"] or "").strip()
        code = (s["code"] or "")
        if cp and _counterparty_looks_specific(cp):
            groups[("cp", cp, code)].append(s)
        mk = _merchant_key(s["name"])
        if mk:
            groups[("mk", mk, code)].append(s)

    out = {}
    for gk, samples in groups.items():
        if len(samples) < 3:
            continue
        amts = [float(s["amount"]) for s in samples if s["amount"] is not None]
        if len(amts) < 3:
            continue
        amts_sorted = sorted(amts)
        median = amts_sorted[len(amts_sorted) // 2]
        if median <= 0:
            continue
        within_band = sum(
            1 for a in amts if abs(a - median) / median <= RECURRING_AMOUNT_TOLERANCE
        )
        if within_band < 3:
            continue

        sorted_samples = sorted(samples, key=lambda s: s["date"] or "")
        valid_intervals = 0
        prev = None
        for s in sorted_samples:
            try:
                d = datetime.strptime(s["date"], "%Y-%m-%d")
            except (ValueError, TypeError):
                continue
            if prev is not None and 25 <= (d - prev).days <= 35:
                valid_intervals += 1
            prev = d
        if valid_intervals < 2:
            continue

        counts = Counter(s["category_id"] for s in samples)
        cid, dom = counts.most_common(1)[0]
        if dom / len(samples) < SUGGESTION_MIN_DOMINANCE:
            continue
        out[gk] = {
            "cid": cid,
            "median_amount": median,
            "sample_count": len(samples),
        }
    return out


def _evaluate_history_match(samples, candidate_code, candidate_amount):
    """Score how well a list of historical samples supports a category.

    Returns (cid, signals, sample_count, dominance) or None.
      - signals: subset of {"code_match", "amount_band_match"} earned by this match,
        as secondary boosts on top of the calling site's primary signal.
    """
    if len(samples) < SUGGESTION_MIN_SAMPLES:
        return None
    counts = Counter(s["category_id"] for s in samples)
    cid, dom_count = counts.most_common(1)[0]
    dominance = dom_count / len(samples)
    if dominance < SUGGESTION_MIN_DOMINANCE:
        return None

    same_cid = [s for s in samples if s["category_id"] == cid]
    signals = set()

    if candidate_code:
        same_code = sum(1 for s in same_cid if (s["code"] or "") == candidate_code)
        if same_cid and same_code / len(same_cid) >= CODE_AGREEMENT_THRESHOLD:
            signals.add("code_match")

    if candidate_amount is not None:
        amts = sorted(
            float(s["amount"]) for s in same_cid if s["amount"] is not None
        )
        if amts:
            median = amts[len(amts) // 2]
            if median > 0 and abs(candidate_amount - median) / median <= AMOUNT_BAND_TOLERANCE:
                signals.add("amount_band_match")

    return cid, signals, len(samples), dominance


def _load_corrections(conn):
    """Negative-learning lookup. Suppress AUTO once corrected; suppress entirely after 2.

    Returns: { (signal_source, signal_key, suggested_category_id) -> count }.
    Safe no-op if the corrections table doesn't exist yet (Phase 2.1).
    """
    try:
        rows = conn.execute(
            """SELECT signal_source, signal_key, suggested_category_id, COUNT(*) AS n
               FROM corrections
               WHERE signal_source IS NOT NULL AND signal_key IS NOT NULL
                 AND suggested_category_id IS NOT NULL
               GROUP BY signal_source, signal_key, suggested_category_id"""
        ).fetchall()
    except Exception:
        return {}
    return {(r["signal_source"], r["signal_key"], r["suggested_category_id"]): r["n"] for r in rows}


def suggest_categories_for_rows(conn, uncat_rows):
    """Bulk-compute history-based category suggestions for uncategorized rows.

    Multi-signal scorer. For each candidate row, we evaluate up to four signals:
      - merchant_key_match: normalized merchant key has dominant category.
      - counterparty_match: business-classified counterparty has dominant category.
      - code_match: candidate's ING code matches ≥80% of supporting samples.
      - amount_band_match: candidate amount within ±15% of supporting median.
    Plus a special "recurring" path: if (counterparty/merchant, code) forms a
    monthly-cadence group and the candidate amount fits, that itself is treated
    as three independent signals (cadence + amount + name agreement) → AUTO.

    Tiering:
      AUTO    — ≥3 signals, ≥10 samples, dominance ≥0.90 → safe to auto-apply.
      SUGGEST — ≥2 signals, ≥5 samples,  dominance ≥0.80 → show but don't apply.
      else    — drop.

    User corrections (Phase 2.1) act as negative evidence: 1 correction
    downgrades AUTO→SUGGEST, 2 corrections suppress entirely.

    Returns: {tx_id: {category_id, category_name, category_color, sample_count,
                      source, tier, signals}}.
    """
    if not uncat_rows:
        return {}

    categorized = conn.execute(
        """SELECT t.name, t.counterparty, t.code, t.amount, t.date, t.category_id,
                  c.name AS category_name,
                  COALESCE(c.color, tp.color, '#94a3b8') AS category_color
           FROM transactions t
           JOIN categories c ON c.id = t.category_id
           LEFT JOIN topics tp ON tp.id = c.topic_id
           WHERE t.category_id IS NOT NULL"""
    ).fetchall()
    if not categorized:
        return {}

    by_counterparty = defaultdict(list)
    by_merchant = defaultdict(list)
    cat_meta = {}
    for r in categorized:
        cid = r["category_id"]
        cat_meta[cid] = {"name": r["category_name"], "color": r["category_color"]}
        cp = (r["counterparty"] or "").strip()
        if cp and _counterparty_looks_specific(cp):
            by_counterparty[cp].append(r)
        key = _merchant_key(r["name"])
        if key:
            by_merchant[key].append(r)

    recurring_groups = _detect_recurring_groups(categorized)
    corrections = _load_corrections(conn)

    out = {}
    for tx in uncat_rows:
        cp = (tx["counterparty"] or "").strip()
        code = (tx["code"] or "") if "code" in tx.keys() else ""
        amount = tx["amount"] if "amount" in tx.keys() else None
        merchant = _merchant_key(tx["name"])

        # ---- Recurring-group fast path: any matching group → AUTO ----
        recurring_hit = None
        for scope, key in (("cp", cp), ("mk", merchant)):
            if not key:
                continue
            gk = (scope, key, code)
            grp = recurring_groups.get(gk)
            if not grp:
                continue
            median = grp["median_amount"]
            if amount is None or median <= 0:
                continue
            if abs(float(amount) - median) / median > RECURRING_AMOUNT_TOLERANCE:
                continue
            sig_source = "counterparty" if scope == "cp" else "merchant_name"
            corr_count = corrections.get((sig_source, key, grp["cid"]), 0)
            if corr_count >= 2:
                continue
            recurring_hit = {
                "cid": grp["cid"],
                "scope": scope,
                "key": key,
                "sample_count": grp["sample_count"],
                "tier": "SUGGEST" if corr_count else "AUTO",
                "signals": ["recurring_cadence", "recurring_amount", "name_agreement"],
                "source": "recurring",
                "signal_source": sig_source,
                "signal_key": key,
            }
            break

        # ---- Composite scorer: counterparty (business-only) + merchant key ----
        candidates = []
        if cp:
            cp_history = by_counterparty.get(cp, [])
            if cp_history and _counterparty_is_business(code, cp_history):
                res = _evaluate_history_match(cp_history, code, amount)
                if res:
                    cid, sigs, n, dom = res
                    candidates.append({
                        "cid": cid,
                        "signals": {"counterparty_match"} | sigs,
                        "samples": n,
                        "dominance": dom,
                        "signal_source": "counterparty",
                        "signal_key": cp,
                    })
        if merchant:
            mk_history = by_merchant.get(merchant, [])
            if mk_history:
                res = _evaluate_history_match(mk_history, code, amount)
                if res:
                    cid, sigs, n, dom = res
                    candidates.append({
                        "cid": cid,
                        "signals": {"merchant_key_match"} | sigs,
                        "samples": n,
                        "dominance": dom,
                        "signal_source": "merchant_name",
                        "signal_key": merchant,
                    })

        # Merge per-cid: when both counterparty and merchant agree, signals union.
        merged = defaultdict(lambda: {"signals": set(), "samples": 0, "dominance": 0.0,
                                      "signal_source": None, "signal_key": None})
        for c in candidates:
            slot = merged[c["cid"]]
            slot["signals"].update(c["signals"])
            slot["samples"] = max(slot["samples"], c["samples"])
            slot["dominance"] = max(slot["dominance"], c["dominance"])
            # Prefer counterparty as the primary signal source for corrections — more specific.
            if slot["signal_source"] is None or c["signal_source"] == "counterparty":
                slot["signal_source"] = c["signal_source"]
                slot["signal_key"] = c["signal_key"]

        chosen = None
        if merged:
            best_cid, slot = max(
                merged.items(),
                key=lambda kv: (len(kv[1]["signals"]), kv[1]["samples"]),
            )
            sig_count = len(slot["signals"])
            n = slot["samples"]
            dom = slot["dominance"]
            corr_count = corrections.get((slot["signal_source"], slot["signal_key"], best_cid), 0)
            if corr_count >= 2:
                tier = None
            elif sig_count >= AUTO_MIN_SIGNALS and n >= AUTO_MIN_SAMPLES and dom >= AUTO_MIN_DOMINANCE:
                tier = "SUGGEST" if corr_count else "AUTO"
            elif sig_count >= SUGGEST_MIN_SIGNALS and n >= SUGGESTION_MIN_SAMPLES and dom >= SUGGESTION_MIN_DOMINANCE:
                tier = "SUGGEST"
            else:
                tier = None
            if tier:
                chosen = {
                    "cid": best_cid,
                    "tier": tier,
                    "signals": sorted(slot["signals"]),
                    "samples": n,
                    "source": slot["signal_source"],
                    "signal_source": slot["signal_source"],
                    "signal_key": slot["signal_key"],
                }

        # Recurring path beats the composite path when both agree on AUTO.
        # When recurring fired but composite has more signals, keep recurring (it's
        # the more specific evidence — same amount month after month).
        winner = None
        if recurring_hit and chosen:
            winner = recurring_hit if recurring_hit["tier"] == "AUTO" or chosen["tier"] != "AUTO" else chosen
        elif recurring_hit:
            winner = recurring_hit
        elif chosen:
            winner = {
                "cid": chosen["cid"],
                "tier": chosen["tier"],
                "signals": chosen["signals"],
                "sample_count": chosen["samples"],
                "source": chosen["source"],
                "signal_source": chosen["signal_source"],
                "signal_key": chosen["signal_key"],
            }

        if winner is None:
            continue

        cid = winner["cid"]
        if cid not in cat_meta:
            continue
        out[tx["id"]] = {
            "category_id": cid,
            "category_name": cat_meta[cid]["name"],
            "category_color": cat_meta[cid]["color"],
            "sample_count": winner.get("sample_count") or winner.get("samples", 0),
            "source": winner["source"],
            "tier": winner["tier"],
            "signals": winner["signals"],
            "signal_source": winner["signal_source"],
            "signal_key": winner["signal_key"],
        }

    return out


# ---------- suggestion engine (local model, tier 3) ----------
# Only ever consulted for rows tiers 1 and 2 could not place. Results are
# cached per merchant key, never auto-applied, and stop being asked for once
# accepted rows give the history suggester enough samples to take over.

def _llm_suggestion_key(row):
    """(scope, key) this row is cached and grouped under.

    Prefers the same merchant key the history suggester groups on, so a guess
    made for 'albert heijn' covers every future Albert Heijn row. Falls back to
    the counterparty, then to the row itself for one-offs that generalize to
    nothing.
    """
    mk = _merchant_key(_row_get(row, "name") or "")
    if mk:
        return ("merchant", mk)
    cp = (_row_get(row, "counterparty") or "").strip()
    if cp:
        return ("counterparty", cp.lower())
    return ("tx", str(_row_get(row, "id")))


def _llm_signal_key(scope, key):
    """Flatten (scope, key) into the single signal_key the corrections table stores."""
    return f"{scope}:{key}"


def _llm_rejected_pairs(conn):
    """{(signal_key, category_id)} the user has already rejected for this tier.

    The history tier needs two rejections before it suppresses, because its
    evidence is statistical and one disagreement may be noise. A local-model
    guess is a definite claim, so one rejection is enough — re-proposing it
    would just be nagging. A *different* category for the same merchant is
    still allowed through on a later sweep.
    """
    try:
        rows = conn.execute(
            """SELECT signal_key, suggested_category_id
               FROM corrections
               WHERE signal_source = 'llm'
                 AND signal_key IS NOT NULL
                 AND suggested_category_id IS NOT NULL"""
        ).fetchall()
    except Exception:
        return set()
    return {(r["signal_key"], r["suggested_category_id"]) for r in rows}


def _load_llm_suggestions(conn, rows):
    """Stored local-model guesses for `rows`, keyed by tx id.

    Returns the same dict shape suggest_categories_for_rows produces, so the
    template and the Accept/Reject handlers need no special-casing — only the
    extra `confidence`/`reason` fields, which drive the badge tooltip.
    """
    if not rows:
        return {}
    try:
        cached = conn.execute(
            """SELECT s.scope, s.key, s.category_id, s.confidence, s.reason, s.model,
                      c.name AS category_name,
                      COALESCE(c.color, tp.color, '#94a3b8') AS category_color
               FROM llm_suggestions s
               JOIN categories c ON c.id = s.category_id
               LEFT JOIN topics tp ON tp.id = c.topic_id"""
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    if not cached:
        return {}

    by_key = {(r["scope"], r["key"]): r for r in cached}
    rejected = _llm_rejected_pairs(conn)

    out = {}
    for tx in rows:
        scope, key = _llm_suggestion_key(tx)
        hit = by_key.get((scope, key))
        if not hit:
            continue
        signal_key = _llm_signal_key(scope, key)
        if (signal_key, hit["category_id"]) in rejected:
            continue
        out[tx["id"]] = {
            "category_id": hit["category_id"],
            "category_name": hit["category_name"],
            "category_color": hit["category_color"],
            "sample_count": 0,
            "source": "llm",
            "tier": "SUGGEST",
            "signals": ["local_model"],
            "signal_source": "llm",
            "signal_key": signal_key,
            "confidence": hit["confidence"],
            "reason": hit["reason"],
            "model": hit["model"],
        }
    return out


def _get_user_llm_settings(conn, user_id):
    """Local-model settings for the user, with defaults for a missing row."""
    defaults = {"enabled": False, "model": "", "url": llm_categorize.DEFAULT_URL}
    if not user_id:
        return defaults
    try:
        row = conn.execute(
            "SELECT llm_enabled, llm_model, llm_url FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return defaults
    if not row:
        return defaults
    return {
        "enabled": bool(row["llm_enabled"]),
        "model": (row["llm_model"] or "").strip(),
        "url": (row["llm_url"] or "").strip() or llm_categorize.DEFAULT_URL,
    }


# ---------- routes ----------

@app.route("/")
def index():
    return redirect(url_for("transactions"))


def _safe_next(target):
    """Reject open-redirects: only allow internal paths starting with '/'."""
    if not target:
        return None
    if target.startswith("/") and not target.startswith("//"):
        return target
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if _current_user() is not None:
        return redirect(url_for("transactions"))
    signup_locked = _signup_locked()

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not email or not password:
            flash("Vul zowel e-mail als wachtwoord in.", "error")
            return render_template("login.html", email=email, signup_locked=signup_locked), 400

        with get_connection() as conn:
            row = conn.execute(
                "SELECT id, name, password_hash FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        if row is None or not check_password_hash(row["password_hash"], password):
            flash("Ongeldig e-mailadres of wachtwoord.", "error")
            return render_template("login.html", email=email, signup_locked=signup_locked), 401

        session.clear()
        session["user_id"] = row["id"]
        nxt = _safe_next(request.args.get("next") or request.form.get("next"))
        return redirect(nxt or url_for("transactions"))

    return render_template("login.html", email="", signup_locked=signup_locked)


def _signup_locked():
    """Single-user app: signup is open until the first account is created,
    then permanently closed. Anyone else has to be invited via the database."""
    with get_connection() as conn:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if _current_user() is not None:
        return redirect(url_for("transactions"))

    if _signup_locked():
        if request.method == "POST":
            flash("Aanmeldingen zijn gesloten.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        errors = []
        if not name:
            errors.append("Vul je naam in.")
        if not email or "@" not in email:
            errors.append("Vul een geldig e-mailadres in.")
        if len(password) < 8:
            errors.append("Wachtwoord moet minimaal 8 tekens zijn.")
        if password != confirm:
            errors.append("Wachtwoorden komen niet overeen.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("signup.html", name=name, email=email), 400

        now = datetime.utcnow().isoformat(timespec="seconds")
        with get_connection() as conn:
            existing = conn.execute(
                "SELECT 1 FROM users WHERE email = ?", (email,)
            ).fetchone()
            if existing:
                flash("Er bestaat al een account met dit e-mailadres.", "error")
                return render_template("signup.html", name=name, email=email), 400

            cur = conn.execute(
                "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (name, email, generate_password_hash(password), now),
            )
            conn.commit()
            user_id = cur.lastrowid

        session.clear()
        session["user_id"] = user_id
        return redirect(url_for("transactions"))

    return render_template("signup.html", name="", email="")


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/profile")
def profile():
    user = _current_user()
    user_id = user["id"] if user else None
    with get_connection() as conn:
        active_goal_id = _get_user_active_goal_id(conn, user_id)
        auto_generate_ai = _get_user_auto_generate(conn, user_id)
        llm = _get_user_llm_settings(conn, user_id)
    return render_template(
        "profile.html",
        goals=GOALS,
        active_goal_id=active_goal_id,
        auto_generate_ai=auto_generate_ai,
        llm=llm,
    )


@app.route("/api/profile/active_goal", methods=["POST"])
def api_profile_active_goal():
    """Persist the user's selected goal id. Validated against the hardcoded
    GOALS catalog — unknown ids are rejected so the column stays clean."""
    user = _current_user()
    if not user:
        return jsonify({"error": "Niet ingelogd."}), 401
    body = request.get_json(silent=True) or {}
    goal_id = (body.get("goal_id") or "").strip()
    if goal_id not in GOALS:
        return jsonify({"error": f"Onbekend doel-id '{goal_id}'."}), 400
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET active_goal = ? WHERE id = ?",
            (goal_id, user["id"]),
        )
        conn.commit()
    return jsonify({"ok": True, "active_goal": goal_id})


@app.route("/api/profile/auto_generate", methods=["POST"])
def api_profile_auto_generate():
    """Toggle the per-user auto-generate-on-clean-period flag for Diagnostics."""
    user = _current_user()
    if not user:
        return jsonify({"error": "Niet ingelogd."}), 401
    body = request.get_json(silent=True) or {}
    enabled = 1 if bool(body.get("enabled")) else 0
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET auto_generate_ai = ? WHERE id = ?",
            (enabled, user["id"]),
        )
        conn.commit()
    return jsonify({"ok": True, "enabled": bool(enabled)})


@app.route("/api/profile/llm", methods=["POST"])
def api_profile_llm():
    """Persist the local-model settings (enabled / model tag / Ollama URL)."""
    user = _current_user()
    if not user:
        return jsonify({"error": "Niet ingelogd."}), 401
    body = request.get_json(silent=True) or {}

    enabled = 1 if bool(body.get("enabled")) else 0
    model = (body.get("model") or "").strip()
    url = (body.get("url") or "").strip() or llm_categorize.DEFAULT_URL

    if not url.startswith("http://") and not url.startswith("https://"):
        return jsonify({"error": "URL moet met http:// of https:// beginnen."}), 400
    # Enabled-without-a-model is a valid intermediate state: it's what puts the
    # download button on the Transactions page, which is where a new user is
    # told a model is missing and offered one in the same click.

    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET llm_enabled = ?, llm_model = ?, llm_url = ? WHERE id = ?",
            (enabled, model, url, user["id"]),
        )
        conn.commit()
    return jsonify({"ok": True, "enabled": bool(enabled), "model": model, "url": url})


# ---------- model download ----------
# A pull is multi-GB and takes minutes, so it runs on a background thread and
# the client polls for progress. Flask serves threaded by default, so the poll
# is answered while the download is still streaming.

_PULL_LOCK = threading.Lock()
_PULL_STATE = {
    "active": False, "model": None, "phase": "", "percent": 0,
    "done": False, "error": None,
}


def _pull_worker(model, url, user_id):
    """Stream the download, then wire the model up so the user is done."""
    def on_progress(msg):
        phase = str(msg.get("status") or "")
        total = msg.get("total") or 0
        completed = msg.get("completed") or 0
        with _PULL_LOCK:
            _PULL_STATE["phase"] = phase
            if total:
                _PULL_STATE["percent"] = int(completed / total * 100)

    try:
        llm_categorize.pull_model(model, url, on_progress=on_progress)
    except llm_categorize.OllamaError as e:
        with _PULL_LOCK:
            _PULL_STATE.update(active=False, done=True, error=str(e))
        return
    except Exception as e:  # defensive: a worker thread must never die silently
        with _PULL_LOCK:
            _PULL_STATE.update(active=False, done=True,
                               error=f"{e.__class__.__name__}: {e}")
        return

    # Select the model we just downloaded and switch the feature on, so the
    # download is the whole setup step rather than the first of three.
    try:
        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET llm_model = ?, llm_enabled = 1 WHERE id = ?",
                (model, user_id),
            )
            conn.commit()
    except Exception as e:
        with _PULL_LOCK:
            _PULL_STATE.update(active=False, done=True,
                               error=f"Model gedownload, maar opslaan mislukte: {e}")
        return

    with _PULL_LOCK:
        _PULL_STATE.update(active=False, done=True, percent=100,
                           phase="klaar", error=None)


@app.route("/api/llm/pull", methods=["POST"])
def api_llm_pull_start():
    """Start downloading a model. Returns immediately; poll the GET for progress."""
    user = _current_user()
    if not user:
        return jsonify({"error": "Niet ingelogd."}), 401

    body = request.get_json(silent=True) or {}
    model = (body.get("model") or "").strip()

    with get_connection() as conn:
        settings = _get_user_llm_settings(conn, user["id"])
    url = settings["url"]

    if not model:
        model = llm_categorize.recommend_model()["model"]

    # Claim the slot before probing: checking and then setting under separate
    # lock acquisitions would let two concurrent clicks both start a download,
    # and probing first would report an unreachable daemon when the real reason
    # is that a pull is already running.
    with _PULL_LOCK:
        if _PULL_STATE["active"]:
            return jsonify({"error": "Er loopt al een download.",
                            "model": _PULL_STATE["model"]}), 409
        _PULL_STATE.update(active=True, model=model, phase="starten…",
                           percent=0, done=False, error=None)

    reachable = llm_categorize.probe(url)
    if not reachable["ok"]:
        with _PULL_LOCK:
            _PULL_STATE.update(active=False, done=True, error=reachable["error"])
        return jsonify({"error": reachable["error"]}), 503

    threading.Thread(
        target=_pull_worker, args=(model, url, user["id"]), daemon=True
    ).start()
    return jsonify({"ok": True, "model": model}), 202


@app.route("/api/llm/pull")
def api_llm_pull_status():
    """Progress of the running (or last) download."""
    if not _current_user():
        return jsonify({"error": "Niet ingelogd."}), 401
    with _PULL_LOCK:
        return jsonify(dict(_PULL_STATE))


@app.route("/api/profile/llm/probe")
def api_profile_llm_probe():
    """Is Ollama reachable, and which models are installed?

    Used to populate the model picker and by the 'test connection' button. A
    down daemon is a normal answer here, not an error — the feature is designed
    to be entirely absent rather than broken when Ollama isn't running.
    """
    if not _current_user():
        return jsonify({"error": "Niet ingelogd."}), 401
    url = (request.args.get("url") or "").strip() or llm_categorize.DEFAULT_URL
    if not url.startswith("http://") and not url.startswith("https://"):
        return jsonify({"ok": False, "models": [], "error": "Ongeldige URL."})
    result = llm_categorize.probe(url)
    # Carried alongside the model list so the UI can offer a download without a
    # second round trip when nothing is installed yet.
    result["recommended"] = llm_categorize.recommend_model(result.get("vram_mb"))
    return jsonify(result)


# How many distinct merchant keys one sweep request handles. The client loops
# until done, which keeps each request short (Flask's dev server handles one at
# a time) and gives the progress bar something to move on.
LLM_SWEEP_BATCH = 6


@app.route("/api/categorize/llm", methods=["POST"])
def api_categorize_llm():
    """Ask the local model about uncategorized rows that tiers 1 and 2 can't place.

    Deduplicates by merchant key first, so a backlog of 26 rows across 19 names
    costs roughly 15 model calls, not 26 — and the answers are cached, so future
    rows from those merchants cost nothing.

    Returns progress counters; the client re-POSTs until `done`.
    """
    user = _current_user()
    if not user:
        return jsonify({"error": "Niet ingelogd."}), 401

    body = request.get_json(silent=True) or {}
    limit = body.get("limit")
    try:
        limit = max(1, min(int(limit), 25)) if limit is not None else LLM_SWEEP_BATCH
    except (TypeError, ValueError):
        limit = LLM_SWEEP_BATCH
    force = bool(body.get("force"))

    with get_connection() as conn:
        settings = _get_user_llm_settings(conn, user["id"])
        if not settings["enabled"] or not settings["model"]:
            return jsonify({
                "error": "Lokaal model staat uit. Zet het aan bij Profiel en kies een model."
            }), 409

        # The configured model can vanish from under us — a failed pull that
        # never finalized, or `ollama rm`. Detect it here and clear the setting
        # so the UI falls back to offering a download, rather than leaving the
        # user with a sweep button that 404s every time they press it.
        reachable = llm_categorize.probe(settings["url"])
        if reachable["ok"]:
            installed = {m["name"] for m in reachable["models"]}
            if settings["model"] not in installed:
                conn.execute(
                    "UPDATE users SET llm_model = '' WHERE id = ?", (user["id"],)
                )
                conn.commit()
                return jsonify({
                    "error": f"{settings['model']} staat niet in Ollama — de download is "
                             f"waarschijnlijk niet afgerond. Herlaad de pagina; "
                             f"je krijgt dan weer een downloadknop.",
                    "model_missing": True,
                }), 409

        rows = conn.execute(
            """SELECT id, name, counterparty, code, transaction_type,
                      direction, amount, notifications
               FROM transactions
               WHERE category_id IS NULL AND match_parent_id IS NULL
               ORDER BY date DESC, id DESC"""
        ).fetchall()
        if not rows:
            return jsonify({"done": True, "processed": 0, "stored": 0,
                            "abstained": 0, "remaining": 0, "message": "Niets te doen."})

        # Don't spend model calls on rows the history tier already answers —
        # its evidence is this user's own behaviour and outranks a guess.
        history_hits = suggest_categories_for_rows(conn, rows)

        # One representative row per key. Rows arrive newest-first, so the
        # representative carries the merchant string the bank currently emits.
        pending = {}
        for r in rows:
            if r["id"] in history_hits:
                continue
            k = _llm_suggestion_key(r)
            if k not in pending:
                pending[k] = r

        if not force:
            done_keys = {
                (s["scope"], s["key"])
                for s in conn.execute("SELECT scope, key FROM llm_suggestions").fetchall()
            }
            pending = {k: v for k, v in pending.items() if k not in done_keys}

        total_pending = len(pending)
        if total_pending == 0:
            return jsonify({"done": True, "processed": 0, "stored": 0, "abstained": 0,
                            "remaining": 0, "message": "Alle openstaande transacties zijn al beoordeeld."})

        try:
            context = llm_categorize.build_context(conn)
        except llm_categorize.OllamaError as e:
            return jsonify({"error": str(e)}), 400

        rejected = _llm_rejected_pairs(conn)
        batch = list(pending.items())[:limit]
        now = datetime.utcnow().isoformat(timespec="seconds")
        stored = abstained = 0

        for (scope, key), row in batch:
            try:
                result = llm_categorize.classify(
                    context, row, settings["model"], settings["url"]
                )
            except llm_categorize.OllamaError as e:
                # Transport-level failure: stop and report honestly rather than
                # burning through the rest of the batch hitting the same wall.
                conn.commit()
                return jsonify({
                    "error": str(e),
                    "processed": stored + abstained,
                    "stored": stored,
                    "abstained": abstained,
                    "remaining": total_pending - (stored + abstained),
                    "done": False,
                }), 502

            if result is None or (_llm_signal_key(scope, key), result["category_id"]) in rejected:
                # Abstention is a real answer. Record it so the sweep doesn't
                # re-ask the same unanswerable row on every run; a NULL
                # category_id never renders as a suggestion.
                abstained += 1
                conn.execute(
                    """INSERT INTO llm_suggestions
                         (scope, key, category_id, confidence, reason, model, created_at)
                       VALUES (?, ?, NULL, NULL, NULL, ?, ?)
                       ON CONFLICT(scope, key) DO UPDATE SET
                         category_id = NULL, confidence = NULL, reason = NULL,
                         model = excluded.model, created_at = excluded.created_at""",
                    (scope, key, settings["model"], now),
                )
                continue

            stored += 1
            conn.execute(
                """INSERT INTO llm_suggestions
                     (scope, key, category_id, confidence, reason, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(scope, key) DO UPDATE SET
                     category_id = excluded.category_id,
                     confidence  = excluded.confidence,
                     reason      = excluded.reason,
                     model       = excluded.model,
                     created_at  = excluded.created_at""",
                (scope, key, result["category_id"], result["confidence"],
                 result["reason"], settings["model"], now),
            )

        conn.commit()

    remaining = total_pending - len(batch)
    return jsonify({
        "done": remaining <= 0,
        "processed": len(batch),
        "stored": stored,
        "abstained": abstained,
        "remaining": remaining,
    })


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Kies een CSV-bestand.", "error")
        return redirect(url_for("transactions"))

    raw = file.read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        flash("Bestand kon niet worden gedecodeerd. Sla het op als UTF-8.", "error")
        return redirect(url_for("transactions"))

    # Try each delimiter and keep the one that yields a format we recognise —
    # ING exports semicolon-delimited in English and comma-delimited in Dutch,
    # and the file itself gives no other clue which you were handed.
    reader = None
    fmt = None
    seen_headers = []
    for delimiter in CSV_DELIMITERS:
        candidate = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        headers = candidate.fieldnames
        seen_headers.append((delimiter, headers))
        detected = detect_format(headers)
        if detected:
            reader, fmt = candidate, detected
            break

    if fmt is None:
        # Report the split that found the most columns — that's the delimiter the
        # file actually uses, so the listed headers are the useful ones to see.
        best = max(seen_headers, key=lambda h: len(h[1] or []))
        flash(
            "Onbekend CSV-formaat. Verwacht een ING- of ASN-export. "
            f"Gevonden kolommen: {best[1]}",
            "error",
        )
        return redirect(url_for("transactions"))
    extract = EXTRACTORS[fmt]

    imported = 0
    skipped = 0
    auto_categorized = 0
    now = datetime.utcnow().isoformat(timespec="seconds")
    last_month = None

    with get_connection() as conn:
        for row in reader:
            try:
                fields = extract(row)
            except Exception:
                skipped += 1
                continue

            if not fields["date"] or not fields["name"] or fields["amount"] is None:
                skipped += 1
                continue

            cur = conn.execute(
                """INSERT OR IGNORE INTO transactions
                   (date, name, account, counterparty, code, direction, amount,
                    transaction_type, notifications, resulting_balance, tag,
                    source_file, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fields["date"],
                    fields["name"],
                    fields["account"],
                    fields["counterparty"],
                    fields["code"],
                    fields["direction"],
                    fields["amount"],
                    fields["transaction_type"],
                    fields["notifications"],
                    fields["balance"],
                    fields["tag"],
                    file.filename,
                    now,
                ),
            )
            if cur.rowcount == 0:
                skipped += 1
                continue

            tx_id = cur.lastrowid
            imported += 1
            last_month = fields["date"][:7]

            tx_row = conn.execute(
                "SELECT name, counterparty, notifications, amount FROM transactions WHERE id = ?",
                (tx_id,),
            ).fetchone()
            cat_id = apply_rules_to_transaction(conn, tx_row)
            if cat_id is not None:
                conn.execute(
                    "UPDATE transactions SET category_id = ?, categorization_source = 'rule' WHERE id = ?",
                    (cat_id, tx_id),
                )
                auto_categorized += 1

        conn.commit()

        # AUTO-tier suggestions: apply only the very high-confidence ones at import.
        # SUGGEST-tier rows stay uncategorized for the user to confirm in the UI.
        uncat_imported = conn.execute(
            """SELECT id, name, counterparty, code, amount
               FROM transactions
               WHERE category_id IS NULL AND imported_at = ?""",
            (now,),
        ).fetchall()
        if uncat_imported:
            suggestions = suggest_categories_for_rows(conn, uncat_imported)
            for tx_id, info in suggestions.items():
                if info.get("tier") != "AUTO":
                    continue
                src = "auto_recurring" if info.get("source") == "recurring" else "auto_high_conf"
                conn.execute(
                    """UPDATE transactions
                       SET category_id = ?, categorization_source = ?,
                           suggestion_signal_source = ?, suggestion_signal_key = ?
                       WHERE id = ?""",
                    (
                        info["category_id"],
                        src,
                        info.get("signal_source"),
                        info.get("signal_key"),
                        tx_id,
                    ),
                )
                auto_categorized += 1
            conn.commit()

    flash(
        f"{imported} nieuw geïmporteerd · {skipped} duplicaten overgeslagen · {auto_categorized} automatisch gecategoriseerd.",
        "success",
    )
    target = url_for("transactions")
    if last_month:
        target = url_for("transactions", month=last_month)
    return redirect(target)


@app.route("/transactions")
def transactions():
    month = request.args.get("month") or ""
    salary_period = request.args.get("salary_period") or ""
    # Category filter is multi-select: any number of category ids, plus the
    # special "uncategorized" token. Empty values are ignored.
    category_values = [c for c in request.args.getlist("category") if c]
    selected_cat_ids = [c for c in category_values if c.isdigit()]
    selected_uncat = "uncategorized" in category_values
    only_uncat = request.args.get("uncategorized") == "1"
    q = (request.args.get("q") or "").strip()
    direction = (request.args.get("direction") or "").strip().lower()
    flow = (request.args.get("flow") or "").strip().lower()  # all | income | expenses | excluded
    if flow not in ("", "all", "income", "expenses", "excluded"):
        flow = ""
    date_filter = (request.args.get("date") or "").strip()
    if date_filter and _safe_parse_date(date_filter) is None:
        date_filter = ""

    # A specific-day filter overrides month/salary period to avoid empty intersections.
    if date_filter:
        month = ""
        salary_period = ""
    # Salary period overrides the month filter to avoid empty intersections.
    if salary_period:
        month = ""

    # Children of matched pairs are loaded separately and nested under their
    # parent — never directly through the main query. All filters apply to the
    # parent only; the child rides along whenever its parent passes.
    where = ["t.match_parent_id IS NULL"]
    params = []
    if date_filter:
        where.append("t.date = ?")
        params.append(date_filter)
    if month:
        where.append("substr(t.date, 1, 7) = ?")
        params.append(month)
    if only_uncat:
        where.append("t.category_id IS NULL")
    else:
        # Combine selected categories (OR) with an optional "uncategorized"
        # bucket, e.g. Salary + Transportation, or Transportation + no-category.
        cat_clauses = []
        if selected_cat_ids:
            placeholders = ",".join("?" * len(selected_cat_ids))
            cat_clauses.append(f"t.category_id IN ({placeholders})")
            params.extend(int(c) for c in selected_cat_ids)
        if selected_uncat:
            cat_clauses.append("t.category_id IS NULL")
        if cat_clauses:
            where.append("(" + " OR ".join(cat_clauses) + ")")
    if q:
        # A purely numeric term is an amount search, not a text search. ORing the
        # two sounded safer but made the feature useless: the notifications field
        # is full of card sequence numbers, dates and transaction ids, so "10"
        # matched 54 irrelevant rows for every 4 genuine ~€10 ones. Anything that
        # isn't a bare number still searches text exactly as before.
        amount_band = parse_amount_query(q)
        if amount_band:
            where.append("(t.amount >= ? AND t.amount < ?)")
            params.extend(amount_band)
        else:
            where.append(
                "(LOWER(t.name) LIKE ? OR LOWER(t.counterparty) LIKE ? "
                "OR LOWER(t.notifications) LIKE ?)"
            )
            like = f"%{q.lower()}%"
            params.extend([like, like, like])

    # Flow filter — limits to real income, real expenses, or transfer-only rows.
    # Real income/expenses respect the "Exclude from totals" flag on the topic.
    if flow == "income":
        where.append("t.direction = 'Credit' AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0")
    elif flow == "expenses":
        where.append("t.direction = 'Debit' AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0")
    elif flow == "excluded":
        where.append("COALESCE(tp.exclude_from_totals, 0) = 1")
    elif direction == "credit":
        where.append("t.direction = 'Credit'")
    elif direction == "debit":
        where.append("t.direction = 'Debit'")

    with get_connection() as conn:
        salary_dates_asc = [
            r["date"] for r in conn.execute(
                """SELECT DISTINCT t.date FROM transactions t
                   JOIN categories c ON c.id = t.category_id
                   WHERE c.is_primary_salary = 1
                   ORDER BY t.date ASC"""
            ).fetchall()
        ]

        salary_periods = []
        for i, start in enumerate(salary_dates_asc):
            end = salary_dates_asc[i + 1] if i + 1 < len(salary_dates_asc) else None
            label = f"{start} → {end}" if end else f"{start} → nu"
            salary_periods.append({"start": start, "end": end, "label": label})
        salary_periods.reverse()  # newest first in the dropdown

        if salary_period:
            next_date = next((d for d in salary_dates_asc if d > salary_period), None)
            where.append("t.date >= ?")
            params.append(salary_period)
            if next_date:
                where.append("t.date < ?")
                params.append(next_date)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        parent_rows = conn.execute(
            f"""SELECT t.*, c.name AS category_name, c.color AS category_color,
                       c.topic_id AS topic_id,
                       tp.color AS topic_color,
                       COALESCE(tp.exclude_from_totals, 0) AS category_exclude
                FROM transactions t
                LEFT JOIN categories c ON c.id = t.category_id
                LEFT JOIN topics tp ON tp.id = c.topic_id
                {where_sql}
                ORDER BY t.date DESC, t.id DESC""",
            params,
        ).fetchall()

        # Fetch children of any visible parent. Empty result if no parents are
        # matched, so the second query stays cheap in the common case.
        matched_parent_ids = [r["id"] for r in parent_rows if r["is_matched"]]
        children_by_parent = defaultdict(list)
        if matched_parent_ids:
            placeholders = ",".join("?" * len(matched_parent_ids))
            child_rows = conn.execute(
                f"""SELECT t.*, c.name AS category_name, c.color AS category_color,
                           c.topic_id AS topic_id,
                           tp.color AS topic_color,
                           COALESCE(tp.exclude_from_totals, 0) AS category_exclude
                    FROM transactions t
                    LEFT JOIN categories c ON c.id = t.category_id
                    LEFT JOIN topics tp ON tp.id = c.topic_id
                    WHERE t.match_parent_id IN ({placeholders})
                    ORDER BY t.date ASC, t.id ASC""",
                matched_parent_ids,
            ).fetchall()
            for ch in child_rows:
                children_by_parent[ch["match_parent_id"]].append(ch)

        # Build the displayed rows. Every row becomes a dict so we can attach
        # synthetic fields (display_amount, is_ghost) without fighting Row.
        #
        # Layout for a matched group:
        #   parent (display_amount = NET of parent + all children, signed)
        #     └─ ghost copy of parent (original debit amount, read-only)
        #     └─ child 1 (real credit)
        #     └─ child 2 (real credit)
        #     ...
        def signed(r):
            return -r["amount"] if (r["direction"] or "").lower() == "debit" else r["amount"]

        rows = []
        for p in parent_rows:
            pd = dict(p)
            pd["is_ghost"] = False
            if p["is_matched"]:
                children = children_by_parent.get(p["id"], [])
                net = signed(p) + sum(signed(c) for c in children)
                pd["display_amount"] = abs(net)
                if net > 0:
                    pd["display_direction"] = "Credit"
                elif net < 0:
                    pd["display_direction"] = "Debit"
                else:
                    # Net zero: keep parent's original sign so the row still
                    # reads as "expense, fully refunded" rather than ambiguous.
                    pd["display_direction"] = p["direction"]
                rows.append(pd)
                # Ghost copy of the parent — shows the original debit so the
                # user can see what they actually paid before reimbursements.
                ghost = dict(p)
                ghost["is_ghost"] = True
                ghost["display_amount"] = p["amount"]
                ghost["display_direction"] = p["direction"]
                rows.append(ghost)
                for c in children:
                    cd = dict(c)
                    cd["is_ghost"] = False
                    cd["display_amount"] = c["amount"]
                    cd["display_direction"] = c["direction"]
                    rows.append(cd)
            else:
                pd["display_amount"] = p["amount"]
                pd["display_direction"] = p["direction"]
                rows.append(pd)

        picker_topics = _load_picker_topics(conn)
        # Flat (id, name, color, topic_name) for the filter dropdown.
        categories = [
            {
                "id": c["id"],
                "name": c["name"],
                "color": c["color"],
                "topic_name": t["name"],
            }
            for t in picker_topics for c in t["categories"]
        ]
        categories.sort(key=lambda c: (c["topic_name"].lower(), c["name"].lower()))

        months = [
            {"value": r["m"], "label": format_month_label(r["m"])}
            for r in conn.execute(
                "SELECT DISTINCT substr(date, 1, 7) AS m FROM transactions ORDER BY m DESC"
            ).fetchall()
        ]

    def counts_in_totals(r):
        # Skip topic-excluded (Savings topic), ghost-parent display rows,
        # and matched children. The parent itself counts — its display_amount
        # is already the net of the whole match group.
        if r["category_exclude"]:
            return False
        if r.get("is_ghost"):
            return False
        if r["match_parent_id"] is not None:
            return False
        return True

    income = sum(
        r["display_amount"] for r in rows
        if r["display_direction"] == "Credit" and counts_in_totals(r)
    )
    expenses = sum(
        r["display_amount"] for r in rows
        if r["display_direction"] == "Debit" and counts_in_totals(r)
    )
    # Uncategorized counter ignores children and ghosts — they're not
    # independently actionable from this view.
    uncat_count = sum(
        1 for r in rows
        if r["category_id"] is None
        and r["match_parent_id"] is None
        and not r.get("is_ghost")
    )
    # Excluded tile hint refers to the category-level "exclude from totals"
    # flag only (Savings topic). Matched groups have their net counted,
    # so they aren't "excluded" in the user-visible sense.
    excluded_count = sum(1 for r in rows if r["category_exclude"] and not r.get("is_ghost"))

    by_category = defaultdict(float)
    for r in rows:
        if r["display_direction"] == "Debit" and counts_in_totals(r):
            key = r["category_name"] or "Ongecategoriseerd"
            by_category[key] += r["display_amount"]
    summary = sorted(by_category.items(), key=lambda kv: -kv[1])

    # History-based suggestions for the visible uncategorized rows.
    # Skip children and ghost rows — they ride along with their parent and
    # don't need their own suggestion UI.
    with get_connection() as conn:
        uncat_rows_for_sug = [
            r for r in rows
            if r["category_id"] is None
            and r["match_parent_id"] is None
            and not r.get("is_ghost")
        ]
        suggestions = suggest_categories_for_rows(conn, uncat_rows_for_sug)

        # Tier 3 fills the gaps only. History evidence is grounded in what this
        # user actually did, so it always outranks a model guess — the local
        # model is consulted for rows history had nothing to say about.
        llm_hits = _load_llm_suggestions(
            conn, [r for r in uncat_rows_for_sug if r["id"] not in suggestions]
        )
        suggestions.update(llm_hits)

        llm_settings = _get_user_llm_settings(conn, (_current_user() or {}).get("id"))

    # Without a model the button offers to download one instead of sweeping —
    # so a new machine needs no terminal, just the two clicks.
    llm_ui = {
        "enabled": llm_settings["enabled"],
        "has_model": bool(llm_settings["model"]),
        "model": llm_settings["model"],
    }
    if llm_ui["enabled"] and not llm_ui["has_model"]:
        llm_ui["recommended"] = llm_categorize.recommend_model()

    return render_template(
        "transactions.html",
        llm_ui=llm_ui,
        rows=rows,
        categories=categories,
        picker_topics=picker_topics,
        months=months,
        salary_periods=salary_periods,
        selected_month=month,
        selected_month_label=format_month_label(month),
        selected_salary_period=salary_period,
        selected_cat_ids=selected_cat_ids,
        selected_uncat=selected_uncat,
        selected_flow=flow,
        selected_date=date_filter,
        only_uncat=only_uncat,
        q=q,
        income=income,
        expenses=expenses,
        net=income - expenses,
        uncat_count=uncat_count,
        excluded_count=excluded_count,
        summary=summary,
        suggestions=suggestions,
    )


_AUTO_SOURCES = {"auto_high_conf", "auto_recurring", "suggestion_accepted"}


def _record_correction(conn, tx_id, prior_cat_id, prior_signal_source, prior_signal_key, new_cat_id):
    """Insert a row into corrections so the suggester learns from the disagreement.

    Skips if no prior auto/suggested category was set, or if the user just
    re-confirmed the same category, or if the signal wasn't tracked.
    """
    if prior_cat_id is None or prior_cat_id == new_cat_id:
        return
    if not prior_signal_source or not prior_signal_key:
        return
    conn.execute(
        """INSERT INTO corrections
           (tx_id, suggested_category_id, chosen_category_id, signal_source, signal_key, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            tx_id,
            prior_cat_id,
            new_cat_id,
            prior_signal_source,
            prior_signal_key,
            datetime.utcnow().isoformat(timespec="seconds"),
        ),
    )


@app.route("/transactions/<int:tx_id>/category", methods=["POST"])
def set_category(tx_id):
    data = request.get_json(silent=True) or request.form
    raw_cat = data.get("category_id")
    create_rule = str(data.get("create_rule", "")).lower() in ("1", "true", "on", "yes")
    pattern = (data.get("pattern") or "").strip()
    field = (data.get("field") or "name").strip()
    if field not in ("name", "counterparty", "notifications"):
        field = "name"
    raw_source = (data.get("source") or "").strip().lower()
    # Accept 'suggestion' for legacy clients; normalize to 'suggestion_accepted'.
    if raw_source == "suggestion":
        raw_source = "suggestion_accepted"
    source = raw_source if raw_source in ("manual", "suggestion_accepted") else "manual"

    # Optional amount band attached to the auto-created rule. Lets a Spotify €11.99
    # subscription rule avoid catching a €5 gift card from the same merchant.
    amount_min = _parse_optional_amount(data.get("amount_min"))
    amount_max = _parse_optional_amount(data.get("amount_max"))

    # Optional signal info — included by the client when accepting a SUGGEST tier
    # so we can persist the evidence trail and credit the right signal later.
    signal_source = (data.get("signal_source") or "").strip() or None
    signal_key = (data.get("signal_key") or "").strip() or None
    if signal_source not in (None, "counterparty", "merchant_name", "recurring"):
        signal_source = None

    try:
        cat_id = None if raw_cat in (None, "", "null", "none") else int(raw_cat)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid category_id"}), 400

    rule_applied_to = 0
    updated_tx_ids = []
    category_info = None
    with get_connection() as conn:
        if cat_id is not None:
            category_info = _category_with_effective_color(conn, cat_id)
            if not category_info:
                return jsonify({"error": "unknown category"}), 400

        prior = conn.execute(
            """SELECT category_id, categorization_source,
                      suggestion_signal_source, suggestion_signal_key
               FROM transactions WHERE id = ?""",
            (tx_id,),
        ).fetchone()
        if prior and prior["categorization_source"] in _AUTO_SOURCES:
            _record_correction(
                conn, tx_id,
                prior["category_id"],
                prior["suggestion_signal_source"],
                prior["suggestion_signal_key"],
                cat_id,
            )

        # Persist signal info only when we know it (i.e. user accepted a SUGGEST).
        # Manual changes clear it — no longer evidence-backed.
        if cat_id is not None and source == "suggestion_accepted" and signal_source and signal_key:
            persist_sig_source, persist_sig_key = signal_source, signal_key
        else:
            persist_sig_source, persist_sig_key = None, None

        conn.execute(
            """UPDATE transactions
               SET category_id = ?, categorization_source = ?,
                   suggestion_signal_source = ?, suggestion_signal_key = ?
               WHERE id = ?""",
            (
                cat_id,
                source if cat_id is not None else None,
                persist_sig_source,
                persist_sig_key,
                tx_id,
            ),
        )

        if create_rule and cat_id is not None and pattern:
            # Skip duplicates — same pattern+field+category+band already wired up.
            # The band is part of the identity: a €12 Spotify rule and a €5 Spotify
            # rule pointing to different categories must coexist.
            existing = conn.execute(
                """SELECT 1 FROM rules
                   WHERE LOWER(pattern) = LOWER(?) AND field = ? AND category_id = ?
                     AND COALESCE(amount_min, -1) = COALESCE(?, -1)
                     AND COALESCE(amount_max, -1) = COALESCE(?, -1)""",
                (pattern, field, cat_id, amount_min, amount_max),
            ).fetchone()
            if not existing:
                conn.execute(
                    """INSERT INTO rules
                       (pattern, field, category_id, amount_min, amount_max, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        pattern, field, cat_id,
                        amount_min, amount_max,
                        datetime.utcnow().isoformat(timespec="seconds"),
                    ),
                )

            # Capture which rows we're about to recategorize so the client can update them in-place.
            # Honour the same amount band so we don't sweep up unrelated charges.
            band_clauses = []
            band_params = []
            if amount_min is not None:
                band_clauses.append("amount >= ?")
                band_params.append(amount_min)
            if amount_max is not None:
                band_clauses.append("amount <= ?")
                band_params.append(amount_max)
            band_sql = (" AND " + " AND ".join(band_clauses)) if band_clauses else ""

            id_rows = conn.execute(
                f"""SELECT id FROM transactions
                    WHERE category_id IS NULL
                      AND id != ?
                      AND LOWER({field}) LIKE ? ESCAPE '\\'{band_sql}""",
                (tx_id, f"%{_escape_like(pattern.lower())}%", *band_params),
            ).fetchall()
            updated_tx_ids = [r["id"] for r in id_rows]

            if updated_tx_ids:
                placeholders = ",".join("?" * len(updated_tx_ids))
                conn.execute(
                    f"UPDATE transactions SET category_id = ?, categorization_source = 'rule' WHERE id IN ({placeholders})",
                    (cat_id, *updated_tx_ids),
                )
            rule_applied_to = len(updated_tx_ids)

        conn.commit()

    return jsonify({
        "ok": True,
        "rule_applied_to": rule_applied_to,
        "updated_tx_ids": updated_tx_ids,
        "category": category_info,
    })


@app.route("/transactions/<int:tx_id>/reject_suggestion", methods=["POST"])
def reject_suggestion(tx_id):
    """Record a SUGGEST-tier rejection without changing the row's category.

    The client posts the (signal_source, signal_key, suggested_category_id) it
    received from the suggestion engine. After two rejections for the same
    triple, the suggester suppresses that suggestion entirely.
    """
    data = request.get_json(silent=True) or request.form
    suggested_cat = data.get("suggested_category_id")
    signal_source = (data.get("signal_source") or "").strip() or None
    signal_key = (data.get("signal_key") or "").strip() or None

    if signal_source not in ("counterparty", "merchant_name", "recurring", "llm"):
        return jsonify({"error": "invalid signal_source"}), 400
    if not signal_key:
        return jsonify({"error": "signal_key required"}), 400
    try:
        suggested_cat_id = int(suggested_cat) if suggested_cat not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "invalid suggested_category_id"}), 400
    if suggested_cat_id is None:
        return jsonify({"error": "suggested_category_id required"}), 400

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO corrections
               (tx_id, suggested_category_id, chosen_category_id, signal_source, signal_key, created_at)
               VALUES (?, ?, NULL, ?, ?, ?)""",
            (
                tx_id,
                suggested_cat_id,
                signal_source,
                signal_key,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        # Drop the cached local-model guess too. The correction alone would stop
        # it being served, but leaving the row would make the next sweep think
        # this key was already handled and never reconsider it.
        if signal_source == "llm" and ":" in signal_key:
            scope, _, key = signal_key.partition(":")
            conn.execute(
                "DELETE FROM llm_suggestions WHERE scope = ? AND key = ?",
                (scope, key),
            )
        conn.commit()
    return jsonify({"ok": True})


# ---------- transaction matching ----------
# Two rows can be matched as parent/child to cancel each other from the totals
# (e.g. €77.95 spend at MediaMarkt + €77.95 refund from a friend who took the
# item). Children inherit visibility from their parent — filters apply only to
# the parent row.

MATCH_AMOUNT_TOLERANCE = 0.01
MATCH_DATE_WINDOW_DAYS = 60


def _row_is_match_eligible(row):
    """Reject rows that are already participating in a match."""
    return not row["is_matched"]


@app.route("/api/transactions/<int:tx_id>/match_candidate")
def api_match_candidate(tx_id):
    """Return the single best counterpart for tx_id, or null.

    Direction constraint: parent must be Debit, child must be Credit. So a
    Debit row's candidate is an unmatched Credit; a Credit row's candidate
    is a Debit (either unmatched or already a parent — partial-reimbursement
    case where another credit is being added to an existing group).

    Amount matching: |amount| equal within MATCH_AMOUNT_TOLERANCE within
    MATCH_DATE_WINDOW_DAYS. Suggestions only fire for the full-refund shape;
    partial-reimbursement targets are reached by manual drag.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, date, amount, direction, is_matched, match_parent_id FROM transactions WHERE id = ?",
            (tx_id,),
        ).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404
        # Children can't initiate matches; only parents/unmatched rows can.
        if row["match_parent_id"] is not None:
            return jsonify({"candidate": None, "reason": "already_a_child"})
        # Existing parents can still GET more children dropped on them, but
        # they don't initiate a search for one — they're already the lead.
        if row["is_matched"]:
            return jsonify({"candidate": None, "reason": "already_a_parent"})

        self_dir = (row["direction"] or "").lower()
        if self_dir == "debit":
            cand_clause = "t.direction = 'Credit' AND COALESCE(t.is_matched, 0) = 0 AND t.match_parent_id IS NULL"
        elif self_dir == "credit":
            # Debits can be unmatched (fresh group) or already-parent (sibling join).
            cand_clause = "t.direction = 'Debit' AND t.match_parent_id IS NULL"
        else:
            return jsonify({"candidate": None})

        candidate = conn.execute(
            f"""SELECT t.id, t.date, t.name, t.amount, t.direction, t.counterparty,
                      c.name AS category_name,
                      COALESCE(c.color, tp.color) AS category_color,
                      ABS(julianday(t.date) - julianday(?)) AS day_gap
                 FROM transactions t
                 LEFT JOIN categories c ON c.id = t.category_id
                 LEFT JOIN topics tp ON tp.id = c.topic_id
                WHERE t.id != ?
                  AND {cand_clause}
                  AND ABS(t.amount - ?) < ?
                  AND ABS(julianday(t.date) - julianday(?)) <= ?
                ORDER BY day_gap ASC
                LIMIT 1""",
            (
                row["date"], row["id"],
                row["amount"], MATCH_AMOUNT_TOLERANCE,
                row["date"], MATCH_DATE_WINDOW_DAYS,
            ),
        ).fetchone()
    if candidate is None:
        return jsonify({"candidate": None})
    return jsonify({
        "candidate": {
            "id": candidate["id"],
            "date": candidate["date"],
            "name": candidate["name"],
            "amount": candidate["amount"],
            "direction": candidate["direction"],
            "counterparty": candidate["counterparty"],
            "category_name": candidate["category_name"],
            "category_color": candidate["category_color"],
            "day_gap": candidate["day_gap"],
        }
    })


@app.route("/transactions/match", methods=["POST"])
def match_transactions():
    """Attach a child transaction to a parent (anchor) transaction.

    Anchor model: the drop TARGET becomes the parent (the group's leader); the
    dragged row becomes its child. Direction is irrelevant — a group nets out
    by signed sum, so it can be N debits under one credit (e.g. transport spends
    under a travel allowance), N credits under one debit (a spend with refunds),
    or any mix. The parent's displayed amount is the net remainder, and its sign
    (+/-) follows that net (see the display builder in the transactions route).

    Rules: the child must be completely unmatched; the parent must not itself be
    a child of another group. A row can't be matched to itself.

    Accepts parent_id/child_id (preferred) or legacy a_id/b_id, where b_id is
    treated as the parent (drop target) and a_id as the child (dragged row).
    """
    data = request.get_json(silent=True) or request.form
    try:
        parent_id = int(data.get("parent_id") if data.get("parent_id") is not None else data.get("b_id"))
        child_id = int(data.get("child_id") if data.get("child_id") is not None else data.get("a_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "parent_id and child_id required"}), 400
    if parent_id == child_id:
        return jsonify({"error": "cannot match a row to itself"}), 400

    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, date, amount, direction, is_matched, match_parent_id
                 FROM transactions WHERE id IN (?, ?)""",
            (parent_id, child_id),
        ).fetchall()
        if len(rows) != 2:
            return jsonify({"error": "transaction not found"}), 404
        parent = next(r for r in rows if r["id"] == parent_id)
        child = next(r for r in rows if r["id"] == child_id)

        # Parent can be unmatched OR already a top-level parent; never a child.
        if parent["match_parent_id"] is not None:
            return jsonify({"error": "the anchor row is itself a child of another match"}), 409
        # Child must be completely unmatched (not a parent, not already a child).
        if child["is_matched"] or child["match_parent_id"] is not None:
            return jsonify({"error": "the dragged row is already part of a match"}), 409

        conn.execute(
            "UPDATE transactions SET match_parent_id = ?, is_matched = 1 WHERE id = ?",
            (parent["id"], child["id"]),
        )
        conn.execute(
            "UPDATE transactions SET is_matched = 1 WHERE id = ?",
            (parent["id"],),
        )
        conn.commit()

    return jsonify({"ok": True, "parent_id": parent["id"], "child_id": child["id"]})


@app.route("/transactions/<int:tx_id>/unmatch", methods=["POST"])
def unmatch_transaction(tx_id):
    """Detach this row from its match group.

    - If tx_id is a child, clear its parent link; clear parent.is_matched if it
      has no other children left.
    - If tx_id is a parent, detach all its children. All involved rows become
      free to match again.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, match_parent_id FROM transactions WHERE id = ?",
            (tx_id,),
        ).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404

        detached_ids = []
        if row["match_parent_id"] is not None:
            parent_id = row["match_parent_id"]
            conn.execute(
                "UPDATE transactions SET match_parent_id = NULL, is_matched = 0 WHERE id = ?",
                (tx_id,),
            )
            detached_ids.append(tx_id)
            remaining = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE match_parent_id = ?", (parent_id,)
            ).fetchone()[0]
            if remaining == 0:
                conn.execute(
                    "UPDATE transactions SET is_matched = 0 WHERE id = ?", (parent_id,)
                )
                detached_ids.append(parent_id)
        else:
            # Treat tx_id as the parent: detach every child and clear self.
            child_ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM transactions WHERE match_parent_id = ?", (tx_id,)
                ).fetchall()
            ]
            if not child_ids:
                return jsonify({"error": "row is not matched"}), 400
            conn.executemany(
                "UPDATE transactions SET match_parent_id = NULL, is_matched = 0 WHERE id = ?",
                [(cid,) for cid in child_ids],
            )
            conn.execute(
                "UPDATE transactions SET is_matched = 0 WHERE id = ?", (tx_id,)
            )
            detached_ids = [tx_id, *child_ids]
        conn.commit()
    return jsonify({"ok": True, "detached_ids": detached_ids})


TOPIC_COLOR_PALETTE = [
    "#16a34a", "#0ea5e9", "#8b5cf6", "#f59e0b", "#ec4899",
    "#10b981", "#6366f1", "#ef4444", "#14b8a6", "#f97316",
    "#64748b", "#84cc16", "#06b6d4", "#a855f7", "#eab308",
    "#d946ef", "#22c55e", "#3b82f6", "#7c3aed", "#f43f5e",
]


def _next_topic_color(existing_colors) -> str:
    """Pick the first palette color not already in use; cycle once exhausted."""
    used = {(c or "").lower() for c in existing_colors}
    for c in TOPIC_COLOR_PALETTE:
        if c.lower() not in used:
            return c
    return TOPIC_COLOR_PALETTE[len(used) % len(TOPIC_COLOR_PALETTE)]


def _hex_to_hsl(hex_color: str):
    """#rrggbb → (h:0-360, s:0-1, l:0-1). Returns (0, 0, 0.5) for unparseable input."""
    h = (hex_color or "").lstrip("#")
    if len(h) != 6:
        return 0.0, 0.0, 0.5
    try:
        r = int(h[0:2], 16) / 255.0
        g = int(h[2:4], 16) / 255.0
        b = int(h[4:6], 16) / 255.0
    except ValueError:
        return 0.0, 0.0, 0.5
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        hue = ((g - b) / d + (6 if g < b else 0)) * 60
    elif mx == g:
        hue = ((b - r) / d + 2) * 60
    else:
        hue = ((r - g) / d + 4) * 60
    return hue, s, l


def _hsl_to_hex(h: float, s: float, l: float) -> str:
    """(h:0-360, s:0-1, l:0-1) → #rrggbb."""
    s = max(0.0, min(1.0, s))
    l = max(0.0, min(1.0, l))
    h = h % 360
    if s == 0:
        v = round(l * 255)
        return f"#{v:02x}{v:02x}{v:02x}"
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    def f(t):
        t = t % 1.0
        if t < 1 / 6: return p + (q - p) * 6 * t
        if t < 1 / 2: return q
        if t < 2 / 3: return p + (q - p) * (2 / 3 - t) * 6
        return p
    r = round(f(h / 360 + 1 / 3) * 255)
    g = round(f(h / 360) * 255)
    b = round(f(h / 360 - 1 / 3) * 255)
    return f"#{r:02x}{g:02x}{b:02x}"


def derive_category_color(topic_color: str, index: int, total: int) -> str:
    """Auto-derived category color: lightness spread + small hue jitter around
    the topic's base hue. Keeps the family resemblance with the topic but
    stays distinguishable when a topic has many categories.

    `index` is the category's position within its topic (0-based, by id ASC).
    `total` is the count of categories in the topic.
    """
    h, s, l = _hex_to_hsl(topic_color)
    if total <= 1:
        return _hsl_to_hex(h, s, l)
    # Spread index across [-1, +1] then scale.
    t = (index - (total - 1) / 2) / max(1, (total - 1) / 2)
    l_offset = 0.15 * t           # ±15% lightness
    h_jitter = 8 * t              # ±8° hue
    new_l = max(0.18, min(0.82, l + l_offset))
    return _hsl_to_hex(h + h_jitter, s, new_l)


def effective_category_color(cat_row, topic_row, siblings_ordered=None) -> str:
    """Return the manual override when set, else the auto-derived shade.

    `siblings_ordered` is the topic's categories in id ASC order (any iterable
    of objects with an `id` attribute / key). When omitted, the auto color
    falls back to the topic's base color — only used for one-off renders that
    don't have the sibling context handy.
    """
    if cat_row["color"]:
        return cat_row["color"]
    topic_color = topic_row["color"] if topic_row else "#64748b"
    if not siblings_ordered:
        return topic_color
    ids = [s["id"] for s in siblings_ordered]
    try:
        idx = ids.index(cat_row["id"])
    except ValueError:
        return topic_color
    return derive_category_color(topic_color, idx, len(ids))


def _load_picker_topics(conn):
    """Topics + nested categories with resolved effective colors, ordered for
    the cat-select picker. Used by transactions.html and any other view that
    renders the picker."""
    topic_rows = conn.execute(
        "SELECT id, name, color FROM topics ORDER BY name"
    ).fetchall()
    cat_rows = conn.execute(
        "SELECT id, topic_id, name, color FROM categories ORDER BY topic_id, id"
    ).fetchall()
    cats_by_topic = defaultdict(list)
    for c in cat_rows:
        cats_by_topic[c["topic_id"]].append(c)
    out = []
    for t in topic_rows:
        siblings = cats_by_topic.get(t["id"], [])
        out.append({
            "id": t["id"],
            "name": t["name"],
            "color": t["color"],
            "categories": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "color": effective_category_color(c, t, siblings),
                }
                for c in siblings
            ],
        })
    return out


def _category_with_effective_color(conn, cat_id):
    """Fetch a single category row with its effective color resolved. Used by
    /transactions/<id>/category to return a JSON-friendly category record."""
    row = conn.execute(
        """SELECT c.id, c.name, c.color, c.topic_id,
                  t.color AS topic_color
           FROM categories c JOIN topics t ON t.id = c.topic_id
           WHERE c.id = ?""",
        (cat_id,),
    ).fetchone()
    if not row:
        return None
    siblings = conn.execute(
        "SELECT id FROM categories WHERE topic_id = ? ORDER BY id",
        (row["topic_id"],),
    ).fetchall()
    color = effective_category_color(
        row,
        {"color": row["topic_color"]},
        siblings,
    )
    return {"id": row["id"], "name": row["name"], "color": color}


@app.route("/topics", methods=["GET", "POST"])
def topics():
    with get_connection() as conn:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "create":
                name = (request.form.get("name") or "").strip()
                color = (request.form.get("color") or "").strip()
                if not color:
                    existing = [
                        r["color"] for r in conn.execute("SELECT color FROM topics").fetchall()
                    ]
                    color = _next_topic_color(existing)
                if name:
                    try:
                        conn.execute(
                            "INSERT INTO topics (name, color) VALUES (?, ?)",
                            (name, color),
                        )
                        conn.commit()
                        flash(f"Onderwerp '{name}' aangemaakt.", "success")
                    except Exception as e:
                        flash(f"Kon niet aanmaken: {e}", "error")
            elif action == "rename":
                topic_id = int(request.form.get("id"))
                name = (request.form.get("name") or "").strip()
                if name:
                    try:
                        conn.execute("UPDATE topics SET name = ? WHERE id = ?", (name, topic_id))
                        conn.commit()
                    except Exception as e:
                        flash(f"Kon niet hernoemen: {e}", "error")
            elif action == "recolor":
                topic_id = int(request.form.get("id"))
                color = (request.form.get("color") or "").strip()
                if color:
                    conn.execute("UPDATE topics SET color = ? WHERE id = ?", (color, topic_id))
                    conn.commit()
            elif action == "delete":
                topic_id = int(request.form.get("id"))
                row = conn.execute(
                    "SELECT name, is_salary_topic FROM topics WHERE id = ?", (topic_id,)
                ).fetchone()
                if not row:
                    flash("Onderwerp niet gevonden.", "error")
                elif row["is_salary_topic"]:
                    flash("Het onderwerp Salaris is vergrendeld en kan niet worden verwijderd.", "error")
                else:
                    in_use = conn.execute(
                        """SELECT COUNT(*) AS n FROM transactions t
                           JOIN categories c ON c.id = t.category_id
                           WHERE c.topic_id = ?""",
                        (topic_id,),
                    ).fetchone()["n"]
                    if in_use:
                        flash(
                            f"Onderwerp '{row['name']}' heeft nog {in_use} transactie(s). "
                            "Verplaats of verwijder eerst de bijbehorende categorieën.",
                            "error",
                        )
                    else:
                        conn.execute("DELETE FROM categories WHERE topic_id = ?", (topic_id,))
                        conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
                        conn.commit()
                        flash(f"Onderwerp '{row['name']}' verwijderd.", "success")
            elif action == "toggle_exclude":
                topic_id = int(request.form.get("id"))
                conn.execute(
                    "UPDATE topics SET exclude_from_totals = 1 - COALESCE(exclude_from_totals, 0) WHERE id = ?",
                    (topic_id,),
                )
                conn.commit()
            elif action == "toggle_fixed":
                topic_id = int(request.form.get("id"))
                conn.execute(
                    "UPDATE topics SET is_fixed = 1 - COALESCE(is_fixed, 0) WHERE id = ?",
                    (topic_id,),
                )
                conn.commit()
            return redirect(url_for("categories"))
        return redirect(url_for("categories"))


@app.route("/categories", methods=["GET", "POST"])
def categories():
    with get_connection() as conn:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "create":
                name = (request.form.get("name") or "").strip()
                topic_id_raw = request.form.get("topic_id")
                color = (request.form.get("color") or "").strip() or None
                try:
                    topic_id = int(topic_id_raw) if topic_id_raw else None
                except ValueError:
                    topic_id = None
                if name and topic_id:
                    try:
                        conn.execute(
                            "INSERT INTO categories (topic_id, name, color) VALUES (?, ?, ?)",
                            (topic_id, name, color),
                        )
                        conn.commit()
                        flash(f"Categorie '{name}' aangemaakt.", "success")
                    except Exception as e:
                        flash(f"Kon niet aanmaken: {e}", "error")
                else:
                    flash("Naam en onderwerp zijn verplicht.", "error")
            elif action == "rename":
                cat_id = int(request.form.get("id"))
                name = (request.form.get("name") or "").strip()
                if name:
                    try:
                        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, cat_id))
                        conn.commit()
                    except Exception as e:
                        flash(f"Kon niet hernoemen: {e}", "error")
            elif action == "recolor":
                cat_id = int(request.form.get("id"))
                use_auto = request.form.get("auto_color") in ("1", "on", "true", "yes")
                color = (request.form.get("color") or "").strip()
                if use_auto or not color:
                    conn.execute("UPDATE categories SET color = NULL WHERE id = ?", (cat_id,))
                else:
                    conn.execute("UPDATE categories SET color = ? WHERE id = ?", (color, cat_id))
                conn.commit()
            elif action == "delete":
                cat_id = int(request.form.get("id"))
                row = conn.execute(
                    "SELECT name, is_primary_salary FROM categories WHERE id = ?", (cat_id,)
                ).fetchone()
                if row and row["is_primary_salary"]:
                    flash(
                        "Deze categorie is het primaire salarisanker en kan niet worden verwijderd. "
                        "Maak eerst een andere categorie primair.",
                        "error",
                    )
                else:
                    conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
                    conn.commit()
                    flash("Categorie verwijderd.", "success")
            elif action == "set_primary_salary":
                cat_id = int(request.form.get("id"))
                # Ensure the category sits in the salary topic.
                row = conn.execute(
                    """SELECT c.id, t.is_salary_topic
                       FROM categories c JOIN topics t ON t.id = c.topic_id
                       WHERE c.id = ?""",
                    (cat_id,),
                ).fetchone()
                if not row or not row["is_salary_topic"]:
                    flash(
                        "Primair salaris kan alleen worden ingesteld op een categorie binnen het onderwerp Salaris.",
                        "error",
                    )
                else:
                    # Clear all primaries first to honor the partial-unique index,
                    # then promote the chosen category.
                    conn.execute("UPDATE categories SET is_primary_salary = 0 WHERE is_primary_salary = 1")
                    conn.execute("UPDATE categories SET is_primary_salary = 1 WHERE id = ?", (cat_id,))
                    conn.commit()
            return redirect(url_for("categories"))

        topic_rows = conn.execute(
            """SELECT t.id, t.name, t.color,
                      COALESCE(t.exclude_from_totals, 0) AS exclude_from_totals,
                      COALESCE(t.is_fixed, 0) AS is_fixed,
                      COALESCE(t.is_salary_topic, 0) AS is_salary_topic,
                      (SELECT COUNT(*) FROM categories c WHERE c.topic_id = t.id) AS cat_count,
                      (SELECT COUNT(*) FROM transactions tx
                       JOIN categories c2 ON c2.id = tx.category_id
                       WHERE c2.topic_id = t.id) AS tx_count
               FROM topics t ORDER BY t.is_salary_topic DESC, t.name"""
        ).fetchall()
        cat_rows = conn.execute(
            """SELECT c.id, c.topic_id, c.name, c.color AS manual_color,
                      COALESCE(c.is_primary_salary, 0) AS is_primary_salary,
                      (SELECT COUNT(*) FROM transactions t WHERE t.category_id = c.id) AS tx_count
               FROM categories c ORDER BY c.topic_id, c.id"""
        ).fetchall()

        # Group categories under topics, resolving each category's effective color.
        cats_by_topic = defaultdict(list)
        for c in cat_rows:
            cats_by_topic[c["topic_id"]].append(c)

        topics_view = []
        for t in topic_rows:
            siblings = cats_by_topic.get(t["id"], [])
            cats = []
            for c in siblings:
                cats.append({
                    "id": c["id"],
                    "name": c["name"],
                    "manual_color": c["manual_color"],
                    "is_auto_color": c["manual_color"] is None,
                    "effective_color": effective_category_color(
                        {"id": c["id"], "color": c["manual_color"]},
                        {"color": t["color"]},
                        siblings,
                    ),
                    "is_primary_salary": bool(c["is_primary_salary"]),
                    "tx_count": c["tx_count"],
                })
            topics_view.append({
                "id": t["id"],
                "name": t["name"],
                "color": t["color"],
                "exclude_from_totals": t["exclude_from_totals"],
                "is_fixed": t["is_fixed"],
                "is_salary_topic": t["is_salary_topic"],
                "cat_count": t["cat_count"],
                "tx_count": t["tx_count"],
                "categories": cats,
            })

        next_topic_color = _next_topic_color(t["color"] for t in topic_rows)
    return render_template(
        "categories.html",
        topics=topics_view,
        next_topic_color=next_topic_color,
    )


@app.route("/rules", methods=["GET", "POST"])
def rules():
    with get_connection() as conn:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "create":
                pattern = (request.form.get("pattern") or "").strip()
                field = (request.form.get("field") or "name").strip()
                cat_id = request.form.get("category_id")
                word_boundary = 1 if request.form.get("word_boundary") in ("1", "on", "true", "yes") else 0
                amount_min = _parse_optional_amount(request.form.get("amount_min"))
                amount_max = _parse_optional_amount(request.form.get("amount_max"))
                if pattern and cat_id and field in ("name", "counterparty", "notifications"):
                    conn.execute(
                        """INSERT INTO rules
                           (pattern, field, category_id, word_boundary, amount_min, amount_max, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            pattern,
                            field,
                            int(cat_id),
                            word_boundary,
                            amount_min,
                            amount_max,
                            datetime.utcnow().isoformat(timespec="seconds"),
                        ),
                    )
                    conn.commit()
                    flash(f"Regel toegevoegd voor '{pattern}'.", "success")
                else:
                    flash("Patroon, veld en categorie zijn verplicht.", "error")
            elif action == "delete":
                rule_id = int(request.form.get("id"))
                conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
                conn.commit()
                flash("Regel verwijderd.", "success")
            elif action == "rerun":
                matched = reapply_rules_to_uncategorized(conn)
                flash(f"Regels opnieuw uitgevoerd. {matched} transacties gecategoriseerd.", "success")
            return redirect(url_for("rules"))

        rules_rows = conn.execute(
            """SELECT r.*, c.name AS category_name,
                      COALESCE(c.color, tp.color) AS category_color
               FROM rules r
               LEFT JOIN categories c ON c.id = r.category_id
               LEFT JOIN topics tp ON tp.id = c.topic_id
               ORDER BY r.id DESC"""
        ).fetchall()
        cats = conn.execute(
            """SELECT c.id, c.name, COALESCE(c.color, tp.color) AS color,
                      tp.name AS topic_name
               FROM categories c
               JOIN topics tp ON tp.id = c.topic_id
               ORDER BY tp.name, c.name"""
        ).fetchall()
        picker_topics = _load_picker_topics(conn)

    # Optional prefill — when arriving via the transactions context menu, the
    # row's value lands in the pattern input and the matching field is selected.
    prefill_pattern = (request.args.get("pattern") or "").strip()
    prefill_field = (request.args.get("field") or "").strip()
    if prefill_field not in ("name", "counterparty", "notifications"):
        prefill_field = ""

    return render_template(
        "rules.html",
        rules=rules_rows,
        categories=cats,
        picker_topics=picker_topics,
        prefill_pattern=prefill_pattern,
        prefill_field=prefill_field,
    )


@app.route("/api/rules/preview", methods=["POST"])
def api_rules_preview():
    """Dry-run a rule definition. Returns count + 5 sample rows.

    Used by the Rules page Preview button so the user can see how broad a
    pattern is BEFORE saving it.
    """
    data = request.get_json(silent=True) or request.form
    pattern = (data.get("pattern") or "").strip()
    field = (data.get("field") or "name").strip()
    if field not in ("name", "counterparty", "notifications"):
        return jsonify({"error": "invalid field"}), 400
    if not pattern:
        return jsonify({"error": "pattern required"}), 400

    word_boundary = 1 if str(data.get("word_boundary", "")).lower() in ("1", "true", "on", "yes") else 0
    amount_min = _parse_optional_amount(data.get("amount_min"))
    amount_max = _parse_optional_amount(data.get("amount_max"))

    fake_rule = {
        "field": field,
        "pattern": pattern,
        "word_boundary": word_boundary,
        "amount_min": amount_min,
        "amount_max": amount_max,
    }

    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, date, name, counterparty, notifications, amount, direction, category_id
               FROM transactions ORDER BY date DESC, id DESC"""
        ).fetchall()

    matched = [r for r in rows if _rule_matches(fake_rule, r)]
    cat_match = sum(1 for r in matched if r["category_id"] is not None)
    uncat_match = len(matched) - cat_match

    samples = [
        {
            "id": s["id"],
            "date": s["date"],
            "name": s["name"],
            "amount": s["amount"],
            "direction": s["direction"],
            "is_categorized": s["category_id"] is not None,
        }
        for s in matched[:5]
    ]
    return jsonify({
        "count": len(matched),
        "uncategorized_count": uncat_match,
        "already_categorized_count": cat_match,
        "samples": samples,
    })


@app.route("/api/summary")
def api_summary():
    month = request.args.get("month") or ""
    where_clauses = ["COALESCE(t.is_matched, 0) = 0"]
    params = []
    if month:
        where_clauses.append("substr(t.date, 1, 7) = ?")
        params.append(month)
    where = "WHERE " + " AND ".join(where_clauses)
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT COALESCE(c.name, 'Ongecategoriseerd') AS category,
                       COALESCE(c.color, tp.color, '#94a3b8') AS color,
                       COALESCE(tp.exclude_from_totals, 0) AS excluded,
                       SUM(CASE WHEN t.direction = 'Debit' THEN t.amount ELSE 0 END) AS spent,
                       SUM(CASE WHEN t.direction = 'Credit' THEN t.amount ELSE 0 END) AS received,
                       COUNT(*) AS count
                FROM transactions t
                LEFT JOIN categories c ON c.id = t.category_id
                LEFT JOIN topics tp ON tp.id = c.topic_id
                {where}
                GROUP BY c.id ORDER BY spent DESC""",
            params,
        ).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------- diagnostics ----------

def _add_month(ym: str, delta: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m += delta
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def _months_back(end_ym: str, count: int) -> list[str]:
    """Return `count` consecutive YYYY-MM strings ending at end_ym (inclusive), oldest first."""
    return [_add_month(end_ym, -i) for i in range(count - 1, -1, -1)]


def _safe_parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _detect_anomalies(conn, month, period, rows_in_month, mom_table, mom_prior_label="gem. 3 maanden"):
    anomalies = []

    # Category spike — current spend > 1.5× trailing-3-month avg, with a real-money floor
    for r in mom_table:
        if r["prior_avg"] >= 25 and r["current"] >= r["prior_avg"] * 1.5 and r["delta_abs"] >= 25:
            anomalies.append({
                "severity": "warning",
                "title": f"{r['name']} gestegen met {r['delta_pct']:.0f}%",
                "detail": f"{eur_filter(r['current'])} deze periode vs {eur_filter(r['prior_avg'])} {mom_prior_label}",
                "color": r["color"],
            })

    # New merchants this period — never seen before the window start.
    #
    # Suppress entirely when the import history is too short (<6 months before
    # the period start), otherwise every merchant in a fresh CSV looks "new"
    # and the user gets a wall of false positives on first use. Also require
    # the merchant's first sighting to fall within the last 14 days of the
    # period so we flag actual new behavior, not just import gaps.
    earliest_row = conn.execute(
        "SELECT MIN(date) AS min_date FROM transactions WHERE date < ?",
        (period["start"],),
    ).fetchone()
    earliest_d = _safe_parse_date(earliest_row["min_date"]) if earliest_row else None
    period_start_d = _safe_parse_date(period["start"])
    has_enough_history = (
        earliest_d is not None and period_start_d is not None
        and (period_start_d - earliest_d).days >= 180
    )

    if has_enough_history:
        historical = conn.execute(
            "SELECT DISTINCT name FROM transactions WHERE date < ?",
            (period["start"],),
        ).fetchall()
        historical_keys = {_merchant_key(r["name"]) for r in historical if r["name"]}
        period_end_d = _safe_parse_date(period["end_exclusive"]) if period["end_exclusive"] else None
        recent_cutoff = (
            date.fromordinal(period_end_d.toordinal() - 14)
            if period_end_d else None
        )
        new_merchants = defaultdict(list)
        for tx in rows_in_month:
            if tx["direction"] != "Debit" or tx["excluded"]:
                continue
            key = _merchant_key(tx["name"] or "")
            if key and key not in historical_keys:
                new_merchants[key].append(tx)
        ranked_new = sorted(new_merchants.items(), key=lambda kv: -sum(t["amount"] or 0 for t in kv[1]))
        for _key, txs in ranked_new[:5]:
            total = sum(t["amount"] or 0 for t in txs)
            if total < 10:
                continue
            # First sighting must be in the back end of the period — anything
            # earlier in a 12m window is more likely an import gap than a
            # genuinely new merchant.
            first_dates = sorted(d for d in (_safe_parse_date(t["date"]) for t in txs) if d)
            if recent_cutoff and first_dates and first_dates[0] < recent_cutoff:
                continue
            first = txs[0]
            anomalies.append({
                "severity": "info",
                "title": f"Nieuwe winkel: {first['name']}",
                "detail": f"{eur_filter(total)} verdeeld over {len(txs)} transactie{'s' if len(txs) != 1 else ''}",
            })

    # Possible duplicates — same name + amount + code, very tight window.
    # Tightened to cut false positives on routine repeats: daily coffee, two
    # supermarket trips for the same round amount. We now require:
    #   - amount ≥ €20 (small repeats are noise),
    #   - matching `code` AND `transaction_type` (a real ING duplicate would),
    #   - both charges on the same day,
    #   - merchant not seen ≥5× at this amount in the trailing 90 days
    #     (that's a pattern, not a duplicate).
    DUP_MIN_AMOUNT = 20.0
    period_start_iso = period["start"]
    by_signature = defaultdict(list)
    for tx in rows_in_month:
        if tx["direction"] != "Debit":
            continue
        if (tx["amount"] or 0) < DUP_MIN_AMOUNT:
            continue
        sig = (
            tx["name"],
            round(tx["amount"], 2),
            tx["code"] if "code" in tx.keys() else None,
            tx["transaction_type"] if "transaction_type" in tx.keys() else None,
        )
        by_signature[sig].append(tx)
    for sig, txs in by_signature.items():
        if len(txs) < 2:
            continue
        name, amt, _code, _ttype = sig
        sorted_txs = sorted(txs, key=lambda t: t["date"] or "")
        for i in range(1, len(sorted_txs)):
            d1 = _safe_parse_date(sorted_txs[i - 1]["date"])
            d2 = _safe_parse_date(sorted_txs[i]["date"])
            if not (d1 and d2 and d1 == d2):
                continue
            # Pattern check: same name + amount appearing repeatedly in the
            # last 90 days is routine, not a duplicate.
            cutoff_90 = date.fromordinal(d2.toordinal() - 90).isoformat()
            hits = conn.execute(
                """SELECT COUNT(*) AS n FROM transactions
                   WHERE name = ? AND ROUND(amount, 2) = ?
                     AND date >= ? AND date <= ?""",
                (name, round(amt, 2), cutoff_90, d2.isoformat()),
            ).fetchone()["n"]
            if hits >= 5:
                break
            anomalies.append({
                "severity": "warning",
                "title": f"Mogelijk duplicaat: {name}",
                "detail": f"{eur_filter(amt)} tweemaal op {sorted_txs[i-1]['date']}",
            })
            break

    # Probable internal transfer — same amount in opposite directions within
    # 2 days, both with code 'GT', and neither already excluded. Surfaces
    # transfers the user hasn't yet tagged into a category under the Savings topic,
    # so the trend chart isn't double-counting them.
    untagged_gt = [
        tx for tx in rows_in_month
        if not tx["excluded"]
        and (tx["code"] if "code" in tx.keys() else None) == "GT"
    ]
    debits_by_amt = defaultdict(list)
    credits_by_amt = defaultdict(list)
    for tx in untagged_gt:
        amt = round(float(tx["amount"] or 0), 2)
        if tx["direction"] == "Debit":
            debits_by_amt[amt].append(tx)
        elif tx["direction"] == "Credit":
            credits_by_amt[amt].append(tx)
    seen_transfer_pairs = set()
    for amt, debits in debits_by_amt.items():
        credits = credits_by_amt.get(amt, [])
        if not credits or amt < 10:
            continue
        for d_tx in debits:
            dd = _safe_parse_date(d_tx["date"])
            if not dd:
                continue
            for c_tx in credits:
                cd = _safe_parse_date(c_tx["date"])
                if not cd or abs((cd - dd).days) > 2:
                    continue
                pair = tuple(sorted([d_tx["id"], c_tx["id"]]))
                if pair in seen_transfer_pairs:
                    continue
                seen_transfer_pairs.add(pair)
                anomalies.append({
                    "severity": "info",
                    "title": "Lijkt op een overschrijving",
                    "detail": (
                        f"{eur_filter(amt)} uit op {d_tx['date']} en terug in "
                        f"op {c_tx['date']} — markeer beide onder het onderwerp "
                        f"Sparen om ze buiten de totalen te houden."
                    ),
                })
                break

    # Subscription drift + missing — driven by the Subscriptions category.
    # We only nag about subscriptions the user has explicitly tagged.
    sub_buckets = _build_subscription_buckets(conn)
    end_y, end_m = int(month[:4]), int(month[5:7])
    selected_end = date(end_y, end_m, monthrange(end_y, end_m)[1])

    for (scope, key, amount_tag), records in sub_buckets.items():
        summary = _summarize_subscription_bucket(records)
        if summary is None:
            continue
        latest = summary["latest"]
        latest_d = summary["latest_d"]
        if not latest_d:
            continue
        latest_amt = summary["latest_amt"]
        median = summary["median"]
        cadence_days = summary["cadence_days"]
        cat_color = latest["category_color"]
        label = _subscription_label(scope, key, amount_tag, latest)

        # Drift on this period's latest charge
        if latest["date"][:7] == month and latest_amt is not None and median > 0:
            drift = (latest_amt - median) / median
            if abs(drift) > 0.05 and abs(latest_amt - median) >= 1:
                arrow = "↑" if drift > 0 else "↓"
                anomalies.append({
                    "severity": "info",
                    "title": f"{label} {arrow} {eur_filter(abs(latest_amt - median))}",
                    "detail": f"In rekening gebracht {eur_filter(latest_amt)}, gewoonlijk {eur_filter(median)}",
                    "color": cat_color,
                })

        # Missed charge — fire only when the gap is genuinely longer than the
        # bucket's own cadence. Annual subs (cadence ≈ 365) shouldn't trigger
        # this in their 11 off months; quarterly (≈90) shouldn't trigger
        # between charges either.
        days_since = (selected_end - latest_d).days
        if cadence_days > 0 and days_since > cadence_days * 1.5 and days_since < cadence_days * 2.5:
            # Human cadence label
            if cadence_days <= 40:
                cadence_label = f"{eur_filter(median)}/mnd"
            elif cadence_days <= 100:
                cadence_label = f"{eur_filter(median)} elke ~{cadence_days}d"
            else:
                cadence_label = f"{eur_filter(median)} elke ~{round(cadence_days / 30)}mnd"
            anomalies.append({
                "severity": "info",
                "title": f"{label} overgeslagen",
                "detail": f"Laatste afschrijving {days_since} dagen geleden ({cadence_label})",
                "color": cat_color,
            })

    return anomalies[:25]


def _list_salary_periods(conn):
    """Salary-anchored periods, newest first. Each period runs from one
    primary-salary transaction to the next (exclusive). The newest period
    ends 'now'."""
    salary_dates_asc = [
        r["date"] for r in conn.execute(
            """SELECT DISTINCT t.date FROM transactions t
               JOIN categories c ON c.id = t.category_id
               WHERE c.is_primary_salary = 1
               ORDER BY t.date ASC"""
        ).fetchall()
    ]
    periods = []
    for i, start in enumerate(salary_dates_asc):
        end = salary_dates_asc[i + 1] if i + 1 < len(salary_dates_asc) else None
        label = f"{start} → {end}" if end else f"{start} → nu"
        periods.append({"start": start, "end": end, "label": label})
    periods.reverse()
    return periods


# Range options for the diagnostics filter — N months ending at the anchor month.
RANGE_SIZES = {"1m": 1, "3m": 3, "6m": 6, "12m": 12}
RANGE_LABELS = {"1m": "Eén maand", "3m": "Laatste 3 maanden",
                "6m": "Laatste 6 maanden", "12m": "Afgelopen jaar"}


def _resolve_period(conn, requested_month, requested_salary_period, requested_range="1m"):
    """Translate the (month, salary_period, range) query params into a uniform period dict.

    Returns: { mode, start, end_exclusive, label, key, month, range, range_size }
      - mode: "month" (single-month), "range" (multi-month window), or "salary"
      - start: ISO date (inclusive)
      - end_exclusive: ISO date (exclusive) or None for open-ended
      - month: anchor YYYY-MM string when in month/range mode (else "")
      - range: '1m'|'3m'|'6m'|'12m' (always populated; '1m' means single month)
      - range_size: integer count of months in the window
    """
    if requested_salary_period:
        periods = _list_salary_periods(conn)
        match = next((p for p in periods if p["start"] == requested_salary_period), None)
        if match:
            return {
                "mode": "salary",
                "start": match["start"],
                "end_exclusive": match["end"],
                "label": f"Salaris van {match['start']}",
                "key": match["start"],
                "month": "",
                "range": "1m",
                "range_size": 1,
            }
    if requested_month:
        rng = requested_range if requested_range in RANGE_SIZES else "1m"
        size = RANGE_SIZES[rng]
        anchor = requested_month
        start_month = _add_month(anchor, -(size - 1))
        end_y, end_m = int(anchor[:4]), int(anchor[5:7])
        last_day = monthrange(end_y, end_m)[1]
        if size == 1:
            label = format_month_label(anchor)
            mode = "month"
        else:
            label = f"{RANGE_LABELS[rng]} · eindigend {format_month_label(anchor)}"
            mode = "range"
        return {
            "mode": mode,
            "start": f"{start_month}-01",
            "end_exclusive": _add_month(anchor, 1) + "-01",
            "label": label,
            "key": anchor,
            "month": anchor,
            "range": rng,
            "range_size": size,
            "_last_day": last_day,
        }
    return None


_ANCHOR_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _build_month_grid(months_with_data, grid_year, selected_month, selected_range,
                      selected_salary):
    """Data for the Diagnostiek period picker: 12 months, 4 quarters, one year.

    Each cell maps onto the existing (month, range) model rather than a new one:
    a month is `range=1m`, a quarter is `range=3m` anchored on its last month,
    the year is `range=12m` anchored on December. So the picker is purely a
    friendlier way to set the two params the backend already understands.

    Cells with no transactions behind them are marked dead, which is what keeps
    the relaxed anchor validation safe — you cannot click your way to an empty
    period, only hand-craft a URL to one.
    """
    years = sorted({m[:4] for m in months_with_data})
    has_month = {i: f"{grid_year}-{i:02d}" in months_with_data for i in range(1, 13)}

    grid_months = [
        {
            "num": i,
            "value": f"{grid_year}-{i:02d}",
            "label": _MONTH_LABELS_NL[i],
            "has_data": has_month[i],
            "selected": (not selected_salary and selected_range == "1m"
                         and selected_month == f"{grid_year}-{i:02d}"),
        }
        for i in range(1, 13)
    ]

    grid_quarters = []
    for q in range(1, 5):
        member_months = (q * 3 - 2, q * 3 - 1, q * 3)
        anchor = f"{grid_year}-{q * 3:02d}"
        # Shift-clicking a quarter selects its half-year: Q1/Q2 -> Jan-Jun,
        # Q3/Q4 -> Jul-Dec. That is just range=6m anchored on the half's last
        # month, so it needs no new period model — and it puts the 6m range
        # back within reach, which the months/quarters/year grid otherwise drops.
        half_anchor = f"{grid_year}-06" if q <= 2 else f"{grid_year}-12"
        half_months = (1, 2, 3, 4, 5, 6) if q <= 2 else (7, 8, 9, 10, 11, 12)
        grid_quarters.append({
            "num": q,
            "anchor": anchor,
            "has_data": any(has_month[m] for m in member_months),
            "half_anchor": half_anchor,
            "half_has_data": any(has_month[m] for m in half_months),
            "half_label": ("eerste helft" if q <= 2 else "tweede helft"),
            # A half-year selection lights up both of its quarters, so the grid
            # shows the span rather than a single anchor cell.
            "selected": (not selected_salary and (
                (selected_range == "3m" and selected_month == anchor)
                or (selected_range == "6m" and selected_month == half_anchor)
            )),
        })

    year_has_data = grid_year in years
    return {
        "year": grid_year,
        "months": grid_months,
        "quarters": grid_quarters,
        "year_has_data": year_has_data,
        "year_selected": (not selected_salary and selected_range == "12m"
                          and selected_month == f"{grid_year}-12"),
        "year_anchor": f"{grid_year}-12",
        "prev_year": str(int(grid_year) - 1) if str(int(grid_year) - 1) in years else "",
        "next_year": str(int(grid_year) + 1) if str(int(grid_year) + 1) in years else "",
    }


def _period_where_sql(period):
    """Return a (sql_fragment, params) pair filtering t.date to within the period."""
    where = ["t.date >= ?"]
    params = [period["start"]]
    if period["end_exclusive"]:
        where.append("t.date < ?")
        params.append(period["end_exclusive"])
    return " AND ".join(where), params


def _build_balance_trajectory(conn, period, account=None):
    """Daily resulting_balance line for a single account, with one point per
    day (last balance of day), plus a marker for any salary day in the window.

    Mixing accounts produces a sawtooth (savings → checking jumps), so we
    pick one account: the caller-supplied `account` if provided, else the
    primary checking account (highest debit volume in the period, falling
    back across all time when the period has no debits). All known accounts
    are returned for the dropdown.
    """
    where_sql, params = _period_where_sql(period)

    accounts = [
        r["account"] for r in conn.execute(
            "SELECT DISTINCT account FROM transactions WHERE account IS NOT NULL AND account <> '' ORDER BY account"
        ).fetchall()
    ]

    selected_account = None
    if account and account in accounts:
        selected_account = account
    elif accounts:
        # "Primary checking" heuristic: highest debit volume in the window,
        # else highest all-time debit volume, else the first account by name.
        ranked = conn.execute(
            f"""SELECT t.account, SUM(t.amount) AS total
                FROM transactions t
                WHERE {where_sql} AND t.direction = 'Debit'
                  AND t.account IS NOT NULL AND t.account <> ''
                GROUP BY t.account ORDER BY total DESC LIMIT 1""",
            params,
        ).fetchone()
        if ranked is None or not (ranked["total"] or 0):
            ranked = conn.execute(
                """SELECT t.account, SUM(t.amount) AS total
                   FROM transactions t
                   WHERE t.direction = 'Debit'
                     AND t.account IS NOT NULL AND t.account <> ''
                   GROUP BY t.account ORDER BY total DESC LIMIT 1"""
            ).fetchone()
        selected_account = ranked["account"] if ranked else accounts[0]

    # ING CSVs are newest-first, so within a day the latest transaction gets
    # the lowest id. Order id DESC so the last write per date picks min(id).
    if selected_account is not None:
        rows = conn.execute(
            f"""SELECT t.date, t.resulting_balance, t.id
                FROM transactions t
                WHERE {where_sql} AND t.resulting_balance IS NOT NULL
                  AND t.account = ?
                ORDER BY t.date ASC, t.id DESC""",
            params + [selected_account],
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT t.date, t.resulting_balance, t.id
                FROM transactions t
                WHERE {where_sql} AND t.resulting_balance IS NOT NULL
                ORDER BY t.date ASC, t.id DESC""",
            params,
        ).fetchall()
    by_day = {}
    for r in rows:
        by_day[r["date"]] = float(r["resulting_balance"])
    points = [{"date": d, "balance": b} for d, b in sorted(by_day.items())]

    salary_params = params + ([selected_account] if selected_account is not None else [])
    account_clause = " AND t.account = ?" if selected_account is not None else ""
    salary_rows = conn.execute(
        f"""SELECT DISTINCT t.date FROM transactions t
            JOIN categories c ON c.id = t.category_id
            WHERE c.is_primary_salary = 1 AND {where_sql}{account_clause}
            ORDER BY t.date ASC""",
        salary_params,
    ).fetchall()
    salary_dates = [r["date"] for r in salary_rows]

    lowest = None
    if points:
        low = min(points, key=lambda p: p["balance"])
        lowest = {"date": low["date"], "balance": low["balance"]}
    return {
        "points": points,
        "salary_dates": salary_dates,
        "lowest": lowest,
        "account": selected_account,
        "accounts": accounts,
    }


def _build_top_merchants(conn, period, limit=10):
    """Top merchants by spend in the period. Uses _merchant_key to group chains."""
    where_sql, params = _period_where_sql(period)
    rows = conn.execute(
        f"""SELECT t.name, t.amount, c.name AS category_name,
                   COALESCE(c.color, tp.color) AS category_color
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE {where_sql}
              AND t.direction = 'Debit'
              AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0""",
        params,
    ).fetchall()

    groups = defaultdict(lambda: {"name": "", "amount": 0.0, "count": 0,
                                  "category_name": None, "category_color": None})
    for r in rows:
        key = _merchant_key(r["name"] or "") or (r["name"] or "").strip().lower()
        if not key:
            continue
        g = groups[key]
        g["amount"] += float(r["amount"] or 0)
        g["count"] += 1
        # Use the most "human" name we've seen — first encountered, after cleaning
        if not g["name"]:
            cleaned = _clean_merchant_text(r["name"] or "").strip()
            g["name"] = cleaned or (r["name"] or "")
        if not g["category_name"] and r["category_name"]:
            g["category_name"] = r["category_name"]
            g["category_color"] = r["category_color"]
    ranked = sorted(groups.values(), key=lambda g: -g["amount"])[:limit]
    return ranked


def _build_categorization_quality(conn, period):
    """Data hygiene panel — auto vs manual %, top firing rules, suggestion
    acceptance rate, stale rules. All sourced from existing fields."""
    where_sql, params = _period_where_sql(period)
    bucket_rows = conn.execute(
        f"""SELECT t.categorization_source, COUNT(*) AS n
            FROM transactions t
            WHERE {where_sql}
            GROUP BY t.categorization_source""",
        params,
    ).fetchall()
    buckets = {"auto": 0, "rule": 0, "manual": 0, "uncategorized": 0}
    for r in bucket_rows:
        src = r["categorization_source"]
        n = r["n"] or 0
        if src in ("auto_high_conf", "auto_recurring", "suggestion_accepted"):
            buckets["auto"] += n
        elif src == "rule":
            buckets["rule"] += n
        elif src == "manual":
            buckets["manual"] += n
        else:
            buckets["uncategorized"] += n
    total = sum(buckets.values()) or 1
    pct = {k: (v / total) * 100 for k, v in buckets.items()}

    top_rules = conn.execute(
        """SELECT r.id, r.pattern, r.field, COALESCE(r.hits, 0) AS hits,
                  c.name AS category_name, c.color AS category_color
           FROM rules r LEFT JOIN categories c ON c.id = r.category_id
           WHERE COALESCE(r.hits, 0) > 0
           ORDER BY r.hits DESC, r.id DESC
           LIMIT 5"""
    ).fetchall()

    # Suggestion acceptance — filter to the same period as the rest of the
    # page so the rate reflects this window, not the user's lifetime history.
    # The transactions side keys off date; corrections key off created_at,
    # which is when the user reviewed the suggestion — a reasonable proxy.
    accepted = conn.execute(
        f"""SELECT COUNT(*) FROM transactions t
            WHERE t.categorization_source = 'suggestion_accepted'
              AND {where_sql}""",
        params,
    ).fetchone()[0] or 0
    corr_where = ["COALESCE(created_at, '') >= ?"]
    corr_params = [period["start"]]
    if period["end_exclusive"]:
        corr_where.append("COALESCE(created_at, '') < ?")
        corr_params.append(period["end_exclusive"])
    corr_where_sql = " AND ".join(corr_where)
    rejected = conn.execute(
        f"SELECT COUNT(*) FROM corrections WHERE chosen_category_id IS NULL AND {corr_where_sql}",
        corr_params,
    ).fetchone()[0] or 0
    overridden = conn.execute(
        f"SELECT COUNT(*) FROM corrections WHERE chosen_category_id IS NOT NULL AND {corr_where_sql}",
        corr_params,
    ).fetchone()[0] or 0
    presented = accepted + rejected + overridden
    acceptance = (accepted / presented * 100) if presented else None

    cutoff = (date.today().replace(day=1)).isoformat()
    cutoff_90 = (datetime.strptime(cutoff, "%Y-%m-%d").date()).toordinal() - 90
    cutoff_iso = date.fromordinal(cutoff_90).isoformat()
    # Stale rules — `hits` only increments when the rule actually fires during
    # categorization, but the user might be manually categorizing transactions
    # that the rule WOULD have matched. Before flagging as stale we check
    # whether the rule's pattern would still match historical rows. If yes,
    # the rule isn't stale; the user is overriding it.
    raw_stale = conn.execute(
        """SELECT r.id, r.pattern, r.field, r.word_boundary,
                  r.amount_min, r.amount_max, r.created_at,
                  c.name AS category_name, c.color AS category_color
           FROM rules r LEFT JOIN categories c ON c.id = r.category_id
           WHERE COALESCE(r.hits, 0) = 0
             AND COALESCE(r.created_at, '') < ?
           ORDER BY r.created_at ASC""",
        (cutoff_iso,),
    ).fetchall()
    stale_rules = []
    for r in raw_stale:
        if _rule_would_match_any(conn, r):
            continue
        stale_rules.append(dict(r))
        if len(stale_rules) >= 8:
            break

    return {
        "buckets": buckets,
        "pct": pct,
        "total": total,
        "top_rules": [dict(r) for r in top_rules],
        "acceptance_rate": acceptance,
        "acceptance_presented": presented,
        "acceptance_accepted": accepted,
        "stale_rules": stale_rules,
    }


def _rule_would_match_any(conn, rule):
    """True if any existing transaction in the DB matches `rule`'s pattern.
    Lets us tell genuinely-unused rules from rules the user is manually
    overriding — the latter still match rows but never got the chance to
    fire because the row was categorized by hand first.

    Mirrors the matching shape used in reapply_rules_to_uncategorized:
    field/pattern + optional amount band + optional word_boundary.
    """
    field = rule["field"] if "field" in rule.keys() else "name"
    if field not in ("name", "counterparty", "notifications"):
        field = "name"
    pattern = rule["pattern"] or ""
    if not pattern:
        return False
    where = []
    params = []
    if rule["word_boundary"]:
        # SQLite LIKE doesn't do word boundaries; use REGEXP fallback below.
        where.append(f"t.{field} REGEXP ?")
        params.append(rf"\b{re.escape(pattern)}\b")
    else:
        where.append(f"t.{field} LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(pattern)}%")
    if rule["amount_min"] is not None:
        where.append("t.amount >= ?")
        params.append(rule["amount_min"])
    if rule["amount_max"] is not None:
        where.append("t.amount <= ?")
        params.append(rule["amount_max"])
    sql = "SELECT 1 FROM transactions t WHERE " + " AND ".join(where) + " LIMIT 1"
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        # No REGEXP extension loaded — fall back to a containment check that
        # ignores word_boundary. Slight over-match (it'll mark a few rules as
        # "still match" when boundary would have rejected them), but that's
        # safer than crashing the diagnostics page.
        where_simple = [f"t.{field} LIKE ? ESCAPE '\\'"]
        params_simple = [f"%{_escape_like(pattern)}%"]
        if rule["amount_min"] is not None:
            where_simple.append("t.amount >= ?")
            params_simple.append(rule["amount_min"])
        if rule["amount_max"] is not None:
            where_simple.append("t.amount <= ?")
            params_simple.append(rule["amount_max"])
        sql_simple = "SELECT 1 FROM transactions t WHERE " + " AND ".join(where_simple) + " LIMIT 1"
        row = conn.execute(sql_simple, params_simple).fetchone()
    return row is not None


def _build_calendar_heatmap(conn, end_date, start_d=None, end_d=None):
    """Daily debit-spend grid for the selected window when one is wide enough,
    otherwise a rolling 365-day grid anchored at end_date (inclusive).

    When `start_d`/`end_d` are provided, those bracket the rendered window
    (end_d is exclusive, mirroring period['end_exclusive']). Days with no
    spend get amount=0."""
    end = end_date if isinstance(end_date, date) else _safe_parse_date(end_date) or date.today()
    if start_d is not None and end_d is not None:
        start = start_d
        end_inclusive = date.fromordinal(end_d.toordinal() - 1)
        scope = "period"
    else:
        start = date.fromordinal(end.toordinal() - 364)
        end_inclusive = end
        scope = "rolling"
    rows = conn.execute(
        """SELECT t.date, SUM(t.amount) AS total
           FROM transactions t
           LEFT JOIN categories c ON c.id = t.category_id
           LEFT JOIN topics tp ON tp.id = c.topic_id
           WHERE t.date >= ? AND t.date <= ?
             AND t.direction = 'Debit'
             AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0
           GROUP BY t.date""",
        (start.isoformat(), end_inclusive.isoformat()),
    ).fetchall()
    by_day = {r["date"]: float(r["total"] or 0) for r in rows}
    span = end_inclusive.toordinal() - start.toordinal() + 1
    days = []
    for offset in range(span):
        d = date.fromordinal(start.toordinal() + offset)
        iso = d.isoformat()
        days.append({"date": iso, "amount": by_day.get(iso, 0.0), "weekday": d.weekday()})
    return {
        "start": start.isoformat(),
        "end": end_inclusive.isoformat(),
        "days": days,
        "max": max((d["amount"] for d in days), default=0),
        "scope": scope,
    }


_DOW_NAMES = ["Ma", "Di", "Wo", "Do", "Vr", "Za", "Zo"]


def _build_dow_breakdown(conn, end_date, start_d=None, end_d=None):
    """Average daily spend by day-of-week. Honors the selected window when one
    is provided (≥3 months wide); otherwise falls back to the last 90 days
    ending at `end_date`."""
    end = end_date if isinstance(end_date, date) else _safe_parse_date(end_date) or date.today()
    if start_d is not None and end_d is not None:
        start = start_d
        end_inclusive = date.fromordinal(end_d.toordinal() - 1)
        scope = "period"
    else:
        start = date.fromordinal(end.toordinal() - 89)
        end_inclusive = end
        scope = "rolling"
    rows = conn.execute(
        """SELECT t.date, SUM(t.amount) AS total
           FROM transactions t
           LEFT JOIN categories c ON c.id = t.category_id
           LEFT JOIN topics tp ON tp.id = c.topic_id
           WHERE t.date >= ? AND t.date <= ?
             AND t.direction = 'Debit'
             AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0
           GROUP BY t.date""",
        (start.isoformat(), end_inclusive.isoformat()),
    ).fetchall()
    by_day = {r["date"]: float(r["total"] or 0) for r in rows}
    sums = [0.0] * 7
    counts = [0] * 7
    span = end_inclusive.toordinal() - start.toordinal() + 1
    for offset in range(span):
        d = date.fromordinal(start.toordinal() + offset)
        sums[d.weekday()] += by_day.get(d.isoformat(), 0.0)
        counts[d.weekday()] += 1
    averages = [
        {
            "label": _DOW_NAMES[i],
            "average": (sums[i] / counts[i]) if counts[i] else 0.0,
            "total": sums[i],
            "days": counts[i],
        }
        for i in range(7)
    ]
    overall_avg = (sum(sums) / sum(counts)) if sum(counts) else 0
    peak = max(averages, key=lambda x: x["average"]) if averages else None
    return {"by_dow": averages, "overall_avg": overall_avg, "peak": peak,
            "window_start": start.isoformat(), "window_end": end_inclusive.isoformat(),
            "scope": scope, "span_days": span}


def _build_yoy(conn, period):
    """Same-month-last-year per-category comparison. Returns None when there's
    insufficient history (≥13 months required)."""
    if period["mode"] != "month":
        return None
    distinct_months = conn.execute(
        "SELECT COUNT(DISTINCT substr(date, 1, 7)) AS n FROM transactions"
    ).fetchone()["n"] or 0
    if distinct_months < 13:
        return None

    cur_month = period["month"]
    py, pm = int(cur_month[:4]), int(cur_month[5:7])
    last_year = f"{py - 1:04d}-{pm:02d}"

    cur_rows = conn.execute(
        """SELECT COALESCE(c.name, 'Ongecategoriseerd') AS name,
                  COALESCE(c.color, tp.color, '#94a3b8') AS color,
                  SUM(t.amount) AS spent
           FROM transactions t
           LEFT JOIN categories c ON c.id = t.category_id
           LEFT JOIN topics tp ON tp.id = c.topic_id
           WHERE substr(t.date, 1, 7) = ?
             AND t.direction = 'Debit'
             AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0
           GROUP BY c.id""",
        (cur_month,),
    ).fetchall()
    prior_rows = conn.execute(
        """SELECT COALESCE(c.name, 'Ongecategoriseerd') AS name,
                  SUM(t.amount) AS spent
           FROM transactions t
           LEFT JOIN categories c ON c.id = t.category_id
           LEFT JOIN topics tp ON tp.id = c.topic_id
           WHERE substr(t.date, 1, 7) = ?
             AND t.direction = 'Debit'
             AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0
           GROUP BY c.id""",
        (last_year,),
    ).fetchall()
    prior = {r["name"]: r["spent"] or 0 for r in prior_rows}
    out = []
    for r in cur_rows:
        cur = r["spent"] or 0
        prv = prior.get(r["name"], 0)
        delta_abs = cur - prv
        if prv > 0:
            delta_pct = (delta_abs / prv) * 100
        elif cur > 0:
            delta_pct = None
        else:
            continue
        out.append({
            "name": r["name"],
            "color": r["color"],
            "current": cur,
            "prior": prv,
            "delta_abs": delta_abs,
            "delta_pct": delta_pct,
        })
    out.sort(key=lambda r: -abs(r["delta_abs"]))
    inflation = [r for r in out if r["delta_pct"] is not None and r["delta_pct"] >= 25 and r["prior"] >= 25]
    return {
        "current_month": cur_month,
        "prior_year_month": last_year,
        "rows": out[:12],
        "inflation_flags": inflation[:5],
    }


def _build_subscription_buckets(conn):
    """Group all transactions categorized as 'Subscriptions' into single
    buckets — each transaction lands in exactly one bucket, so no totals
    can double-count. Keyed by counterparty when specific, else merchant
    key, else cleaned name as a last resort.

    Payment-processor counterparties (PayPal, Klarna, …) bill many distinct
    subscriptions through a single IBAN, so we add a third tuple element
    `amount_tag` (the rounded charge amount) for those — splitting "PayPal
    €5.99/mo" and "PayPal €22.00/mo" into separate buckets. For ordinary
    merchants `amount_tag` is None and bucketing is unchanged.

    Returns: { (scope, key, amount_tag) -> [tx_row, ...] }, scope in
    {"cp","mk","nm"}; amount_tag is float or None.
    """
    rows = conn.execute(
        """SELECT t.id, t.name, t.counterparty, t.code, t.amount, t.date,
                  t.direction, c.name AS category_name,
                  COALESCE(c.color, tp.color) AS category_color
           FROM transactions t
           JOIN categories c ON c.id = t.category_id
           JOIN topics tp ON tp.id = c.topic_id
           WHERE tp.name IN ('Subscriptions', 'Abonnementen')
             AND t.direction = 'Debit'"""
    ).fetchall()

    buckets = defaultdict(list)
    for tx in rows:
        cp = (tx["counterparty"] or "").strip()
        name = (tx["name"] or "").strip()
        amount = float(tx["amount"]) if tx["amount"] is not None else 0.0
        # ING's `counterparty` column is the IBAN ("NL12INGB...") — the
        # human-readable "PayPal Europe S.a.r.l." sits in `name`. Check both
        # so payment-processor detection works whichever field carries it.
        is_processor = _is_payment_processor(cp) or _is_payment_processor(name)
        amt_tag = round(amount, 2) if is_processor and amount > 0 else None

        if cp and _counterparty_looks_specific(cp):
            buckets[("cp", cp, amt_tag)].append(tx)
            continue
        mk = _merchant_key(name)
        if mk:
            buckets[("mk", mk, amt_tag)].append(tx)
            continue
        nm = name.lower()
        if nm:
            buckets[("nm", nm, amt_tag)].append(tx)
    return buckets


def _summarize_subscription_bucket(records):
    """Compute median, latest, days_since, price-change, cadence and label
    fields for a single subscription bucket. Returns None when there's
    nothing usable.

    `cadence_days` is the median gap between consecutive charges — used to
    distinguish monthly (~30d) from quarterly (~90d) and annual (~365d)
    subscriptions, so the "skipped" detector doesn't fire on every off
    month of an annual sub.
    """
    records = sorted(records, key=lambda r: r["date"] or "")
    amts = [float(r["amount"]) for r in records if r["amount"] is not None]
    if not amts:
        return None
    amts_sorted = sorted(amts)
    median = amts_sorted[len(amts_sorted) // 2]
    latest = records[-1]
    latest_d = _safe_parse_date(latest["date"])
    latest_amt = float(latest["amount"]) if latest["amount"] is not None else None

    # Cadence — median gap (in days) between consecutive charges.
    parsed_dates = [d for d in (_safe_parse_date(r["date"]) for r in records) if d]
    gaps = [
        (parsed_dates[i] - parsed_dates[i - 1]).days
        for i in range(1, len(parsed_dates))
        if (parsed_dates[i] - parsed_dates[i - 1]).days > 0
    ]
    if gaps:
        gaps_sorted = sorted(gaps)
        cadence_days = gaps_sorted[len(gaps_sorted) // 2]
    else:
        cadence_days = 30  # fallback: assume monthly when there's only one charge

    # Price-change detection: median of all-but-last vs latest charge
    older = [float(r["amount"]) for r in records[:-1] if r["amount"] is not None]
    price_change = None
    if older and latest_amt is not None:
        older_median = sorted(older)[len(older) // 2]
        if (older_median > 0
                and abs(latest_amt - older_median) / older_median > 0.05
                and abs(latest_amt - older_median) >= 0.5):
            price_change = {
                "from": older_median,
                "to": latest_amt,
                "delta_pct": ((latest_amt - older_median) / older_median) * 100,
            }

    return {
        "records": records,
        "median": median,
        "latest": latest,
        "latest_d": latest_d,
        "latest_amt": latest_amt,
        "cadence_days": cadence_days,
        "price_change": price_change,
    }


def _subscription_label(scope, key, amount_tag, latest):
    """Pick a human label for a subscription bucket. When `amount_tag` is set
    (payment-processor sub-bucket) the amount is appended so the user can tell
    PayPal-billed subscriptions apart even though we don't know the underlying
    merchant name."""
    if scope == "cp":
        base = (latest["name"] or key).strip()
    elif scope == "mk":
        base = key.title()
    else:
        base = (latest["name"] or key).strip()
    if amount_tag is not None:
        return f"{base} · €{amount_tag:.2f}"
    return base


def _build_fixed_vs_variable(conn, period):
    """Within the period, classify debits as fixed (categories tagged is_fixed)
    or variable (everything else). Rent, utilities, insurance, mortgage and
    subscriptions all count as fixed when the user has tagged them on the
    Categories page. Returns totals, share, the tagged categories list, and a
    per-month breakdown for the last 6 months."""
    where_sql, params = _period_where_sql(period)

    cur_row = conn.execute(
        f"""SELECT
              COALESCE(SUM(CASE WHEN COALESCE(tp.is_fixed, 0) = 1
                                THEN t.amount ELSE 0 END), 0) AS fixed,
              COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE {where_sql}
              AND t.direction = 'Debit'
              AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0""",
        params,
    ).fetchone()
    fixed = float(cur_row["fixed"] or 0)
    total = float(cur_row["total"] or 0)
    variable = max(0.0, total - fixed)

    # Which topics are currently counted as fixed — surfaced in the tile so the
    # user understands what the headline "truly discretionary" excludes, and
    # can spot when a missing topic (Rent, Utilities) still needs creating.
    fixed_cats = [
        r["name"] for r in conn.execute(
            "SELECT name FROM topics WHERE COALESCE(is_fixed, 0) = 1 ORDER BY name"
        ).fetchall()
    ]

    # 6-month series for stacked bar, ending at the period's anchor month so
    # the chart stays inside the selected window (salary mode anchors at the
    # period's start month).
    months = _months_back(period.get("month") or period["start"][:7], 6)
    placeholders = ",".join("?" * len(months))
    series_rows = conn.execute(
        f"""SELECT substr(t.date, 1, 7) AS m,
                   COALESCE(SUM(CASE WHEN COALESCE(tp.is_fixed, 0) = 1
                                     THEN t.amount ELSE 0 END), 0) AS fixed,
                   COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE substr(t.date, 1, 7) IN ({placeholders})
              AND t.direction = 'Debit'
              AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0
            GROUP BY substr(t.date, 1, 7)""",
        months,
    ).fetchall()
    series_map = {
        r["m"]: {"fixed": float(r["fixed"] or 0), "total": float(r["total"] or 0)}
        for r in series_rows
    }
    series = []
    for m in months:
        s = series_map.get(m, {"fixed": 0.0, "total": 0.0})
        series.append({
            "month": m,
            "label": format_month_label(m),
            "fixed": s["fixed"],
            "variable": max(0.0, s["total"] - s["fixed"]),
        })

    return {
        "fixed": fixed,
        "variable": variable,
        "total": total,
        "fixed_pct": (fixed / total * 100) if total > 0 else 0,
        "series": series,
        "fixed_categories": fixed_cats,
    }


def _build_income_breakdown(conn, period):
    """Split the period's income into Salary / Refunds / Other.
    - Salary: rows in the Salary category.
    - Refunds: small Credits with merchant-side counterparties (BA/IC code).
    - Other: everything else credit (interest, gifts, transfers in)."""
    where_sql, params = _period_where_sql(period)
    rows = conn.execute(
        f"""SELECT t.amount, t.code, t.counterparty, c.name AS category_name,
                   COALESCE(c.is_primary_salary, 0) AS is_salary,
                   COALESCE(tp.exclude_from_totals, 0) AS excluded
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE {where_sql}
              AND t.direction = 'Credit'
              AND COALESCE(t.is_matched, 0) = 0""",
        params,
    ).fetchall()
    salary = refunds = other = 0.0
    for r in rows:
        if r["excluded"]:
            continue
        amt = float(r["amount"] or 0)
        if r["is_salary"]:
            salary += amt
        elif (r["code"] or "") in BUSINESS_CODES and amt < 200:
            refunds += amt
        else:
            other += amt
    total = salary + refunds + other
    parts = [
        {"name": "Salaris", "amount": salary, "color": "#5C6E36"},
        {"name": "Terugbetalingen", "amount": refunds, "color": "#A07852"},
        {"name": "Overig", "amount": other, "color": "#80735C"},
    ]
    return {
        "total": total,
        "parts": [p for p in parts if p["amount"] > 0],
        "salary": salary,
        "refunds": refunds,
        "other": other,
    }


# Channel labels — ING transaction codes we group into the stacked area chart.
CHANNEL_LABELS = {
    "BA": "Pinpas",
    "IC": "Automatische incasso",
    "GT": "Overschrijving",
    "OV": "Periodieke overboeking",
    "GM": "Geldautomaat",
    "VZ": "Balie",
    "DV": "Overig",
    "ID": "iDEAL",
}


def _build_channel_mix(conn, period):
    """Stacked-area data: monthly debit spend grouped by code, last 6 months
    ending at the period's reference month. Surface 'creeping autopay' if
    direct-debit share grew month-over-month in the latest 3 of those months."""
    anchor = period.get("month") or period["start"][:7]
    months = _months_back(anchor, 6)
    placeholders = ",".join("?" * len(months))
    rows = conn.execute(
        f"""SELECT substr(t.date, 1, 7) AS m, t.code, SUM(t.amount) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE substr(t.date, 1, 7) IN ({placeholders})
              AND t.direction = 'Debit'
              AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0
            GROUP BY substr(t.date, 1, 7), t.code""",
        months,
    ).fetchall()
    # codes seen at all
    by_month = defaultdict(lambda: defaultdict(float))
    code_totals = defaultdict(float)
    for r in rows:
        code = (r["code"] or "")
        by_month[r["m"]][code] += float(r["total"] or 0)
        code_totals[code] += float(r["total"] or 0)

    visible_codes = [c for c, _ in sorted(code_totals.items(), key=lambda kv: -kv[1])][:6]

    series = []
    palette = ["#5C6E36", "#8E3A23", "#A07852", "#80735C", "#6B5E4A", "#BFB18E"]
    for i, code in enumerate(visible_codes):
        label = CHANNEL_LABELS.get(code, code or "—")
        series.append({
            "code": code,
            "label": label,
            "color": palette[i % len(palette)],
            "data": [by_month[m].get(code, 0.0) for m in months],
        })

    # Creeping autopay detector — only fires when the trend is genuinely a
    # trend, not just a noisy two-point swing:
    #   - direct-debit share rises monotonically across the last 3 months,
    #   - the rise is ≥ 5 percentage points end-to-end,
    #   - IC volume averages ≥ €100/month over the window (otherwise it's
    #     not really "autopay" yet — too low to matter).
    autopay_creep = None
    if "IC" in code_totals and len(months) >= 3:
        last3 = months[-3:]
        ic_shares = []
        ic_amounts = []
        for m in last3:
            mt = sum(by_month[m].values())
            ic_amounts.append(by_month[m].get("IC", 0))
            if mt > 0:
                ic_shares.append(by_month[m].get("IC", 0) / mt)
            else:
                ic_shares.append(None)
        ic_avg = sum(ic_amounts) / len(ic_amounts) if ic_amounts else 0
        monotonic = (
            len(ic_shares) == 3
            and all(s is not None for s in ic_shares)
            and ic_shares[0] < ic_shares[1] < ic_shares[2]
        )
        if monotonic and ic_avg >= 100 and ic_shares[2] - ic_shares[0] >= 0.05:
            autopay_creep = {
                "from_pct": ic_shares[0] * 100,
                "to_pct": ic_shares[2] * 100,
                "delta_pct": (ic_shares[2] - ic_shares[0]) * 100,
            }

    return {
        "months": [{"value": m, "label": format_month_label(m)} for m in months],
        "series": series,
        "autopay_creep": autopay_creep,
    }


def _build_category_pie(conn, period, topic_id):
    """Drill-down: category-level breakdown of a single topic, within the period.
    Categories' effective colors are auto-derived shades of the topic when no
    manual override is set."""
    where_sql, params = _period_where_sql(period)
    topic = conn.execute(
        "SELECT id, name, color FROM topics WHERE id = ?", (topic_id,)
    ).fetchone()
    if not topic:
        return None
    siblings = conn.execute(
        "SELECT id FROM categories WHERE topic_id = ? ORDER BY id", (topic_id,)
    ).fetchall()
    rows = conn.execute(
        f"""SELECT c.id, c.name, c.color AS manual_color, SUM(t.amount) AS amount
            FROM transactions t
            JOIN categories c ON c.id = t.category_id
            WHERE c.topic_id = ? AND {where_sql}
              AND t.direction = 'Debit'
              AND COALESCE(t.is_matched, 0) = 0
            GROUP BY c.id ORDER BY amount DESC""",
        [topic_id, *params],
    ).fetchall()
    total = sum(r["amount"] for r in rows)
    out = []
    for r in rows:
        color = effective_category_color(
            {"id": r["id"], "color": r["manual_color"]},
            {"color": topic["color"]},
            siblings,
        )
        out.append({
            "name": r["name"],
            "color": color,
            "amount": float(r["amount"] or 0),
            "pct": (r["amount"] / total * 100) if total else 0,
        })
    return {
        "topic": {"id": topic["id"], "name": topic["name"], "color": topic["color"]},
        "slices": out,
        "total": total,
    }


def _build_subscription_bloat(conn, period):
    """Detect bloat on any topic flagged is_fixed. Compares the period's monthly
    average spend on that topic to the 6-month prior baseline, calls out the
    dominant category, and surfaces any categories that first appeared within
    the trailing 2 months."""
    fixed_topics = conn.execute(
        "SELECT id, name, color FROM topics WHERE COALESCE(is_fixed, 0) = 1 ORDER BY name"
    ).fetchall()
    if not fixed_topics:
        return None

    where_sql, params = _period_where_sql(period)
    range_size = period.get("range_size", 1) or 1
    callouts = []
    for tp in fixed_topics:
        # Current-window spend, by category.
        cat_rows = conn.execute(
            f"""SELECT c.id, c.name, SUM(t.amount) AS spent,
                       MIN(t.date) AS first_seen
                FROM transactions t
                JOIN categories c ON c.id = t.category_id
                WHERE c.topic_id = ? AND {where_sql}
                  AND t.direction = 'Debit'
                  AND COALESCE(t.is_matched, 0) = 0
                GROUP BY c.id""",
            [tp["id"], *params],
        ).fetchall()
        cur_total = sum(float(r["spent"] or 0) for r in cat_rows)
        cur_monthly = cur_total / range_size if range_size > 0 else cur_total

        # Prior 6-month baseline ending where the current window started.
        prior_start = period["start"]
        prior_end = period["start"]
        prior_row = conn.execute(
            """SELECT COALESCE(SUM(t.amount), 0) AS spent
               FROM transactions t
               JOIN categories c ON c.id = t.category_id
               WHERE c.topic_id = ?
                 AND t.date < ?
                 AND t.date >= date(?, '-6 months')
                 AND t.direction = 'Debit'
                 AND COALESCE(t.is_matched, 0) = 0""",
            (tp["id"], prior_end, prior_start),
        ).fetchone()
        prior_total = float(prior_row["spent"] or 0)
        prior_monthly = prior_total / 6.0

        # Topic-level growth callout.
        if prior_monthly >= 10 and cur_monthly >= prior_monthly * 1.2:
            delta_pct = (cur_monthly - prior_monthly) / prior_monthly * 100
            callouts.append({
                "severity": "warning",
                "color": tp["color"],
                "title": f"{tp['name']} gestegen met {delta_pct:.0f}% per maand",
                "detail": f"€{cur_monthly:.0f}/mnd deze periode vs €{prior_monthly:.0f}/mnd over afgelopen 6 maanden",
            })

        # Single-category concentration.
        if cur_total > 0:
            top = max(cat_rows, key=lambda r: float(r["spent"] or 0))
            top_share = float(top["spent"] or 0) / cur_total
            if top_share >= 0.40 and len(cat_rows) > 1:
                callouts.append({
                    "severity": "info",
                    "color": tp["color"],
                    "title": f"{top['name']} domineert {tp['name']}",
                    "detail": f"{top_share*100:.0f}% van uitgaven bij {tp['name']} (€{float(top['spent']):.0f})",
                })

        # New categories — first transaction within the trailing 2 months.
        recent_cutoff = conn.execute("SELECT date('now', '-2 months') AS d").fetchone()["d"]
        for r in cat_rows:
            if r["first_seen"] and r["first_seen"] >= recent_cutoff and float(r["spent"] or 0) > 0:
                callouts.append({
                    "severity": "info",
                    "color": tp["color"],
                    "title": f"Nieuw: {r['name']} in {tp['name']}",
                    "detail": f"Eerste afschrijving {r['first_seen']} (€{float(r['spent']):.0f} tot nu toe)",
                })

    if not callouts:
        return None
    return {"callouts": callouts[:10]}


def _build_topic_concentration(pie, expenses):
    """Top-1 share + HHI across topics in the period. `pie` is the topic-level
    list produced for the doughnut chart. Returns None when there isn't enough
    spread to make the metric meaningful."""
    real_topics = [p for p in pie if p["name"] != "Ongecategoriseerd" and p["amount"] > 0]
    if not real_topics or expenses <= 0:
        return None
    # HHI shares must sum to 1 over the topics being counted, so they're taken
    # against the categorized total — dividing by all expenses while dropping
    # the uncategorized slice understates concentration.
    real_total = sum(p["amount"] for p in real_topics)
    shares = [p["amount"] / real_total for p in real_topics]
    top = real_topics[0]
    top_share = top["amount"] / expenses
    hhi = sum(s * s for s in shares) * 10000  # standard 0-10000 scale
    if hhi >= 4000:
        label = "sterk geconcentreerd"
    elif hhi >= 2500:
        label = "geconcentreerd"
    elif hhi >= 1500:
        label = "gematigd"
    else:
        label = "goed verdeeld"
    return {
        "top_topic": top["name"],
        "top_color": top["color"],
        "top_share": top_share,
        "hhi": hhi,
        "label": label,
        "topic_count": len(real_topics),
    }


def _build_topic_trends(conn, period):
    """Per-topic monthly debit totals over the trailing 12 months ending at the
    period's anchor month. Excluded topics are skipped — they're not "spend"."""
    month = period.get("month") or period["start"][:7]
    months = _months_back(month, 12)
    placeholders = ",".join("?" * len(months))
    rows = conn.execute(
        f"""SELECT substr(t.date, 1, 7) AS m,
                   tp.id AS topic_id, tp.name AS topic_name, tp.color AS color,
                   SUM(t.amount) AS spent
            FROM transactions t
            JOIN categories c ON c.id = t.category_id
            JOIN topics tp ON tp.id = c.topic_id
            WHERE substr(t.date, 1, 7) IN ({placeholders})
              AND t.direction = 'Debit'
              AND COALESCE(tp.exclude_from_totals, 0) = 0
              AND COALESCE(t.is_matched, 0) = 0
            GROUP BY substr(t.date, 1, 7), tp.id
            ORDER BY tp.name, m""",
        months,
    ).fetchall()
    by_topic = defaultdict(lambda: {"name": "", "color": "", "data": [0.0] * len(months)})
    month_idx = {m: i for i, m in enumerate(months)}
    for r in rows:
        slot = by_topic[r["topic_id"]]
        slot["name"] = r["topic_name"]
        slot["color"] = r["color"]
        slot["data"][month_idx[r["m"]]] = float(r["spent"] or 0)
    series = sorted(
        by_topic.values(),
        key=lambda s: -sum(s["data"]),
    )[:8]
    return {
        "months": [{"value": m, "label": format_month_label(m)} for m in months],
        "series": series,
    }


def _build_diagnostics(conn, period, balance_account=None):
    today = date.today()
    month = period.get("month") or period["start"][:7]
    where_sql, params = _period_where_sql(period)

    rows_in_window = conn.execute(
        f"""SELECT t.id, t.date, t.name, t.amount, t.direction, t.category_id,
                   t.code, t.transaction_type,
                   c.name AS category_name,
                   c.topic_id AS topic_id,
                   tp.name AS topic_name,
                   COALESCE(tp.color, '#94a3b8') AS topic_color,
                   COALESCE(c.is_primary_salary, 0) AS is_salary,
                   COALESCE(tp.exclude_from_totals, 0) AS excluded,
                   COALESCE(tp.is_fixed, 0) AS is_fixed
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE {where_sql}
              AND COALESCE(t.is_matched, 0) = 0
            ORDER BY t.date""",
        params,
    ).fetchall()
    # Backwards-compat: some legacy code calls expect rows_in_month
    rows_in_month = rows_in_window

    income = sum(r["amount"] for r in rows_in_month if r["direction"] == "Credit" and not r["excluded"])
    expenses = sum(r["amount"] for r in rows_in_month if r["direction"] == "Debit" and not r["excluded"])
    net = income - expenses

    # Salary-only savings rate is the honest one — refunds, gifts, and friend
    # reimbursements inflate the "all credits" denominator. We still keep the
    # all-credits rate for transparency in case Salary isn't tagged yet.
    salary_income = sum(
        r["amount"] for r in rows_in_month
        if r["direction"] == "Credit" and not r["excluded"]
        and r["is_salary"]
    )
    savings_rate_salary = (
        (income - expenses) / salary_income if salary_income > 0 else None
    )
    savings_rate_all = (net / income) if income > 0 else None
    # Public 'savings_rate' = salary-based when Salary is tagged, else all-credits.
    savings_rate = savings_rate_salary if savings_rate_salary is not None else savings_rate_all

    end_y, end_m = int(month[:4]), int(month[5:7])
    days_in_month_n = monthrange(end_y, end_m)[1]
    is_current = month == today.strftime("%Y-%m")

    # Window length must match the actual filtered period, not just the anchor
    # month. In 3m/6m/12m range mode the old code divided a year of expenses
    # by ~30, producing burn figures roughly 12× too high.
    start_d = _safe_parse_date(period["start"])
    end_excl_d = _safe_parse_date(period["end_exclusive"]) if period["end_exclusive"] else None
    if period["mode"] == "month":
        window_days = days_in_month_n
        days_elapsed = today.day if is_current else days_in_month_n
    else:
        # range or salary: window length is (end_exclusive - start) in days, but
        # clamped to "today" for an open-ended or current-spanning window.
        if start_d and end_excl_d:
            window_days = (end_excl_d - start_d).days
        elif start_d:
            window_days = (today - start_d).days + 1
        else:
            window_days = days_in_month_n
        # Days elapsed = full window when historical, capped at today otherwise.
        if start_d:
            elapsed_to_today = (today - start_d).days + 1
            days_elapsed = max(1, min(window_days, elapsed_to_today))
        else:
            days_elapsed = window_days
    daily_burn = expenses / days_elapsed if days_elapsed > 0 else 0

    # Projected spend is only meaningful for the current month — and only after
    # enough days have elapsed for the daily burn to stabilise. In the first
    # week, a single rent or salary-day big-ticket charge produces wildly
    # inflated projections, so we suppress it.
    projected_spend = None
    if period["mode"] == "month" and is_current and days_elapsed >= 7 and days_elapsed < days_in_month_n:
        projected_spend = daily_burn * days_in_month_n

    debit_rows = [r for r in rows_in_month if r["direction"] == "Debit" and not r["excluded"]]
    largest_tx = max(debit_rows, key=lambda r: r["amount"]) if debit_rows else None

    uncat_count = sum(1 for r in rows_in_month if r["category_id"] is None)
    uncat_pct = (uncat_count / len(rows_in_month) * 100) if rows_in_month else 0

    months_window = _months_back(month, 12)
    placeholders = ",".join("?" * len(months_window))
    series_rows = conn.execute(
        f"""SELECT substr(t.date, 1, 7) AS m, t.direction, SUM(t.amount) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE substr(t.date, 1, 7) IN ({placeholders})
              AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0
            GROUP BY substr(t.date, 1, 7), t.direction""",
        months_window,
    ).fetchall()
    series_map = defaultdict(lambda: {"income": 0.0, "expenses": 0.0})
    for r in series_rows:
        if r["direction"] == "Credit":
            series_map[r["m"]]["income"] = r["total"] or 0.0
        elif r["direction"] == "Debit":
            series_map[r["m"]]["expenses"] = r["total"] or 0.0
    monthly_series = [
        {
            "month": m,
            "label": format_month_label(m),
            "income": series_map[m]["income"],
            "expenses": series_map[m]["expenses"],
            "net": series_map[m]["income"] - series_map[m]["expenses"],
        }
        for m in months_window
    ]

    # Topic-level pie: every debit aggregates up to its topic. Categories
    # without a parent topic (Uncategorized) bucket together under a single
    # synthetic slice.
    pie_rows = conn.execute(
        f"""SELECT COALESCE(tp.id, 0) AS topic_id,
                   COALESCE(tp.name, 'Ongecategoriseerd') AS name,
                   COALESCE(tp.color, '#94a3b8') AS color,
                   SUM(t.amount) AS amount
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE {where_sql}
              AND t.direction = 'Debit'
              AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0
            GROUP BY tp.id ORDER BY amount DESC""",
        params,
    ).fetchall()
    pie_total = sum(r["amount"] for r in pie_rows)
    pie = [
        {
            "topic_id": r["topic_id"] or None,
            "name": r["name"],
            "color": r["color"],
            "amount": r["amount"],
            "pct": (r["amount"] / pie_total * 100) if pie_total else 0,
        }
        for r in pie_rows
    ]

    # Comparison baseline: same length as the current window, immediately preceding it.
    # In single-month mode (range_size=1) we use 3 months for a steadier average.
    range_size = period.get("range_size", 1)
    prior_n = 3 if range_size == 1 else range_size
    prior_anchor = _add_month(month, -range_size)
    prior_months = _months_back(prior_anchor, prior_n)
    placeholders_prior = ",".join("?" * len(prior_months))
    prior_rows = conn.execute(
        f"""SELECT COALESCE(tp.name, 'Ongecategoriseerd') AS name,
                   COALESCE(tp.color, '#94a3b8') AS color,
                   SUM(t.amount) AS spent
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE substr(t.date, 1, 7) IN ({placeholders_prior})
              AND t.direction = 'Debit'
              AND COALESCE(tp.exclude_from_totals, 0) = 0 AND COALESCE(t.is_matched, 0) = 0
            GROUP BY tp.id""",
        prior_months,
    ).fetchall()

    # Apples-to-apples: scale prior to match the current window length.
    # range_size=1 → prior_avg is one month's avg of the trailing 3 months.
    # range_size=N → prior is the previous N months' total (same length as current).
    if range_size == 1:
        prior_scale = 1.0 / prior_n  # per-month average
    else:
        prior_scale = 1.0  # totals over equal-length windows
    prior_avg_per_cat = {r["name"]: (r["spent"] or 0) * prior_scale for r in prior_rows}
    cat_meta = {r["name"]: r["color"] for r in prior_rows}
    current_per_cat = {p["name"]: p["amount"] for p in pie}
    for p in pie:
        cat_meta.setdefault(p["name"], p["color"])

    mom = []
    for name in set(prior_avg_per_cat) | set(current_per_cat):
        cur = current_per_cat.get(name, 0.0)
        prv = prior_avg_per_cat.get(name, 0.0)
        delta_abs = cur - prv
        if prv > 0:
            delta_pct = (delta_abs / prv) * 100
        elif cur > 0:
            delta_pct = None  # "new"
        else:
            continue
        mom.append({
            "name": name,
            "color": cat_meta.get(name, "#94a3b8"),
            "current": cur,
            "prior_avg": prv,
            "delta_abs": delta_abs,
            "delta_pct": delta_pct,
        })
    mom.sort(key=lambda r: -abs(r["delta_abs"]))

    # Label that matches what `prior_avg` actually represents — so anomaly
    # text and the table header don't claim "3-month avg" in 12m mode.
    if range_size == 1:
        mom_prior_label = "gem. 3 maanden"
    elif range_size == 12:
        mom_prior_label = "totaal vorig jaar"
    else:
        mom_prior_label = f"totaal vorige {range_size} maanden"

    anomalies = _detect_anomalies(conn, month, period, rows_in_month, mom, mom_prior_label)

    # Reference end-date for non-period-bounded views (heatmap, day-of-week)
    if period["mode"] == "month":
        end_y2, end_m2 = int(month[:4]), int(month[5:7])
        ref_end = date(end_y2, end_m2, monthrange(end_y2, end_m2)[1])
        if is_current:
            ref_end = today
    else:
        # end_exclusive is the first day *outside* the period (e.g. the next
        # salary date) — step back one day so rolling windows don't bleed in
        # the next period's transactions.
        end_excl = _safe_parse_date(period["end_exclusive"]) if period["end_exclusive"] else None
        ref_end = date.fromordinal(end_excl.toordinal() - 1) if end_excl else today
        if ref_end > today:
            ref_end = today

    balance_trajectory = _build_balance_trajectory(conn, period, account=balance_account)
    top_merchants = _build_top_merchants(conn, period, limit=10)
    cat_quality = _build_categorization_quality(conn, period)
    subscription_bloat = _build_subscription_bloat(conn, period)
    topic_concentration = _build_topic_concentration(pie, expenses)
    topic_trends = _build_topic_trends(conn, period)
    # Honor the selected window when it's wide enough (≥3 months) for the
    # heatmap/DoW patterns to be meaningful; fall back to the rolling
    # last-365/last-90 view for shorter windows (salary periods, single months).
    pattern_window_days = (
        (end_excl_d - start_d).days if (start_d and end_excl_d) else 0
    )
    if pattern_window_days >= 90:
        heatmap = _build_calendar_heatmap(conn, ref_end, start_d=start_d, end_d=end_excl_d)
        dow = _build_dow_breakdown(conn, ref_end, start_d=start_d, end_d=end_excl_d)
    else:
        heatmap = _build_calendar_heatmap(conn, ref_end)
        dow = _build_dow_breakdown(conn, ref_end)
    yoy = _build_yoy(conn, period)
    fixed_var = _build_fixed_vs_variable(conn, period)
    income_split = _build_income_breakdown(conn, period)
    channel_mix = _build_channel_mix(conn, period)

    return {
        "period_mode": period["mode"],
        "period_label": period["label"],
        "period_start": period["start"],
        "period_end": period["end_exclusive"],
        "range_size": range_size,
        "window_days": window_days,
        "income": income,
        "expenses": expenses,
        "net": net,
        "savings_rate": savings_rate,
        "savings_rate_salary": savings_rate_salary,
        "savings_rate_all": savings_rate_all,
        "salary_income": salary_income,
        "daily_burn": daily_burn,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month_n,
        "projected_spend": projected_spend,
        "is_current_month": is_current,
        "largest_tx": dict(largest_tx) if largest_tx else None,
        "uncat_count": uncat_count,
        "uncat_pct": uncat_pct,
        "tx_count": len(rows_in_month),
        "monthly_series": monthly_series,
        "pie": pie,
        "mom": mom[:12],
        "mom_prior_label": mom_prior_label,
        "anomalies": anomalies,
        "balance_trajectory": balance_trajectory,
        "top_merchants": top_merchants,
        "cat_quality": cat_quality,
        "heatmap": heatmap,
        "dow": dow,
        "yoy": yoy,
        "fixed_var": fixed_var,
        "income_split": income_split,
        "channel_mix": channel_mix,
        "subscription_bloat": subscription_bloat,
        "topic_concentration": topic_concentration,
        "topic_trends": topic_trends,
    }


def _compute_topic_history(conn, period, months_back=12):
    """Per-topic monthly debit totals over the trailing N months ending at the
    period's anchor month. Mirrors _build_topic_trends but flattened for the
    AI payload (no color, no Jinja labels) and limited to topic + category-free
    aggregates."""
    month = period.get("month") or period["start"][:7]
    months = _months_back(month, months_back)
    placeholders = ",".join("?" * len(months))
    rows = conn.execute(
        f"""SELECT substr(t.date, 1, 7) AS m,
                   tp.id AS topic_id, tp.name AS topic_name,
                   SUM(t.amount) AS spent
            FROM transactions t
            JOIN categories c ON c.id = t.category_id
            JOIN topics tp ON tp.id = c.topic_id
            WHERE substr(t.date, 1, 7) IN ({placeholders})
              AND t.direction = 'Debit'
              AND COALESCE(tp.exclude_from_totals, 0) = 0
              AND COALESCE(t.is_matched, 0) = 0
            GROUP BY substr(t.date, 1, 7), tp.id""",
        months,
    ).fetchall()
    month_idx = {m: i for i, m in enumerate(months)}
    by_topic = defaultdict(lambda: {"name": "", "monthly": [0.0] * len(months)})
    for r in rows:
        slot = by_topic[r["topic_id"]]
        slot["name"] = r["topic_name"]
        slot["monthly"][month_idx[r["m"]]] = round(float(r["spent"] or 0), 2)
    series = sorted(by_topic.values(), key=lambda s: -sum(s["monthly"]))[:8]
    return {"months": months, "series": series}


def _compute_savings_rate_history(conn, period, months_back=6):
    """Monthly savings rate over the last N months ending at the period's
    anchor month. Uses salary-anchored savings rate when a primary-salary
    category is tagged, falling back to all-income otherwise — matching
    _build_diagnostics' public savings_rate definition."""
    month = period.get("month") or period["start"][:7]
    months = _months_back(month, months_back)
    placeholders = ",".join("?" * len(months))
    flow_rows = conn.execute(
        f"""SELECT substr(t.date, 1, 7) AS m, t.direction, SUM(t.amount) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE substr(t.date, 1, 7) IN ({placeholders})
              AND COALESCE(tp.exclude_from_totals, 0) = 0
              AND COALESCE(t.is_matched, 0) = 0
            GROUP BY substr(t.date, 1, 7), t.direction""",
        months,
    ).fetchall()
    salary_rows = conn.execute(
        f"""SELECT substr(t.date, 1, 7) AS m, SUM(t.amount) AS salary
            FROM transactions t
            JOIN categories c ON c.id = t.category_id
            WHERE c.is_primary_salary = 1
              AND substr(t.date, 1, 7) IN ({placeholders})
              AND t.direction = 'Credit'
            GROUP BY substr(t.date, 1, 7)""",
        months,
    ).fetchall()

    flows = {m: {"income": 0.0, "expenses": 0.0} for m in months}
    for r in flow_rows:
        if r["direction"] == "Credit":
            flows[r["m"]]["income"] = float(r["total"] or 0)
        elif r["direction"] == "Debit":
            flows[r["m"]]["expenses"] = float(r["total"] or 0)
    salary_by_month = {r["m"]: float(r["salary"] or 0) for r in salary_rows}

    out = []
    for m in months:
        income = flows[m]["income"]
        expenses = flows[m]["expenses"]
        net = income - expenses
        salary = salary_by_month.get(m, 0.0)
        if salary > 0:
            rate = net / salary
        elif income > 0:
            rate = net / income
        else:
            rate = None
        out.append({
            "month": m,
            "savings_rate": (round(rate, 3) if rate is not None else None),
        })
    return out


def _compute_salary_cadence(conn):
    """Typical day-of-month + amount range from the last 6 primary-salary
    rows. Pulls only date + amount, never name/counterparty/notifications."""
    rows = conn.execute(
        """SELECT date, amount FROM transactions t
           JOIN categories c ON c.id = t.category_id
           WHERE c.is_primary_salary = 1 AND t.direction = 'Credit'
           ORDER BY date DESC LIMIT 6"""
    ).fetchall()
    if not rows:
        return None
    days = [int(r["date"][8:10]) for r in rows if len(r["date"]) >= 10]
    amounts = [float(r["amount"]) for r in rows]
    if not days or not amounts:
        return None
    typical_day = Counter(days).most_common(1)[0][0]
    return {
        "typical_day_of_month": typical_day,
        "amount_range": [round(min(amounts), 2), round(max(amounts), 2)],
        "sample_size": len(amounts),
    }


def _compute_recurring_load(conn, period):
    """Total debit spend in the period for topics flagged is_fixed=1, plus the
    list of fixed topic names. Used by Forecast to reason about next-period
    runway. Reuses the is_fixed pattern from _build_fixed_vs_variable."""
    where_sql, params = _period_where_sql(period)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE {where_sql}
              AND t.direction = 'Debit'
              AND COALESCE(tp.is_fixed, 0) = 1
              AND COALESCE(tp.exclude_from_totals, 0) = 0
              AND COALESCE(t.is_matched, 0) = 0""",
        params,
    ).fetchone()
    fixed_topics = [
        r["name"] for r in conn.execute(
            "SELECT name FROM topics WHERE COALESCE(is_fixed, 0) = 1 ORDER BY name"
        ).fetchall()
    ]
    return {
        "total": round(float(row["total"] or 0), 2),
        "topics": fixed_topics,
    }


def _compute_day_of_period_distribution(conn, period):
    """Bucket the period's debit spend into weeks since the period start.
    For 'month' mode the anchor is the 1st; for 'salary' mode it's the salary
    date — both produce 'days since the period started' which the model can
    reason about as 'days after payday' for salary periods."""
    where_sql, params = _period_where_sql(period)
    rows = conn.execute(
        f"""SELECT CAST(julianday(t.date) - julianday(?) AS INTEGER) AS d,
                   SUM(t.amount) AS spent
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE {where_sql}
              AND t.direction = 'Debit'
              AND COALESCE(tp.exclude_from_totals, 0) = 0
              AND COALESCE(t.is_matched, 0) = 0
            GROUP BY d""",
        [period["start"], *params],
    ).fetchall()
    buckets = {"week_1": 0.0, "week_2": 0.0, "week_3": 0.0, "week_4_plus": 0.0}
    for r in rows:
        d = r["d"]
        if d is None or d < 0:
            continue
        amt = float(r["spent"] or 0)
        if d <= 6:
            buckets["week_1"] += amt
        elif d <= 13:
            buckets["week_2"] += amt
        elif d <= 20:
            buckets["week_3"] += amt
        else:
            buckets["week_4_plus"] += amt
    return {
        "period_start": period["start"],
        "week_1": round(buckets["week_1"], 2),
        "week_2": round(buckets["week_2"], 2),
        "week_3": round(buckets["week_3"], 2),
        "week_4_plus": round(buckets["week_4_plus"], 2),
    }


def _build_ai_summary_payload(conn, period, goal=None):
    """Aggregate the period into a JSON-safe payload, structured by topic with
    categories nested. Never sends raw transaction free-text (name,
    notifications, account, counterparty) — only topic/category-level
    aggregates and anomaly summaries (whose detail strings are IBAN-scrubbed).
    Topic and category names ARE included so the model can reference them
    by name (e.g. "Subscriptions") in the prose summary.

    When `goal` is provided (Advice mode), the goal title + description are
    included in the payload so the model can frame recommendations against
    the user's chosen direction."""
    diag = _build_diagnostics(conn, period)

    # Per-topic category-level spend within the period, for the nested payload.
    where_sql, params = _period_where_sql(period)
    cat_rows = conn.execute(
        f"""SELECT tp.id AS topic_id,
                   COALESCE(c.name, 'Ongecategoriseerd') AS cat_name,
                   SUM(t.amount) AS spent
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN topics tp ON tp.id = c.topic_id
            WHERE {where_sql}
              AND t.direction = 'Debit'
              AND COALESCE(tp.exclude_from_totals, 0) = 0
              AND COALESCE(t.is_matched, 0) = 0
            GROUP BY tp.id, c.id""",
        params,
    ).fetchall()
    cats_by_topic = defaultdict(list)
    for r in cat_rows:
        cats_by_topic[r["topic_id"]].append(r)

    by_topic = []
    for p in diag["pie"]:
        topic_id = p.get("topic_id")
        children = cats_by_topic.get(topic_id, [])
        topic_total = float(p["amount"])
        cats_payload = [
            {
                "name": ch["cat_name"],
                "spent": round(float(ch["spent"] or 0), 2),
                "pct_of_topic": round((float(ch["spent"] or 0) / topic_total) * 100, 1) if topic_total else 0,
            }
            for ch in sorted(children, key=lambda r: -float(r["spent"] or 0))
        ]
        by_topic.append({
            "name": p["name"],
            "spent": round(topic_total, 2),
            "pct_of_expenses": round(p["pct"], 1),
            "categories": cats_payload,
        })

    vs_prior_period = [
        {"name": m["name"],
         "current": round(m["current"], 2),
         "prior_avg": round(m["prior_avg"], 2),
         "delta_abs": round(m["delta_abs"], 2),
         "delta_pct": (round(m["delta_pct"], 1) if m["delta_pct"] is not None else None)}
        for m in diag["mom"][:8]
    ]

    payload = {
        "period_label": diag["period_label"],
        "period_mode": diag["period_mode"],
        "tx_count": diag["tx_count"],
        "income": round(diag["income"], 2),
        "expenses": round(diag["expenses"], 2),
        "net": round(diag["net"], 2),
        "savings_rate": (round(diag["savings_rate"], 3)
                         if diag["savings_rate"] is not None else None),
        "by_topic": by_topic,
        "vs_prior_topic": vs_prior_period,
        "uncategorized_pct": round(diag["uncat_pct"], 1),
        "lowest_balance": diag["balance_trajectory"].get("lowest"),
        "topic_history_12mo": _compute_topic_history(conn, period, 12),
        "savings_rate_history": _compute_savings_rate_history(conn, period, 6),
        "salary_cadence": _compute_salary_cadence(conn),
        "recurring_load": _compute_recurring_load(conn, period),
        "day_of_period": _compute_day_of_period_distribution(conn, period),
    }
    if goal:
        payload["goal"] = {
            "id": goal.get("id"),
            "title": goal.get("title"),
            "description": goal.get("description"),
        }
    return payload


GEMINI_MODEL = "gemini-3-flash-preview"


def _ai_summary_period_key(period, mode="digest", goal_id=None):
    """Stable cache key for the period. Encodes range/mode + anchor so the
    four range options (1m/3m/6m/12m) and salary periods cache independently.
    For advice mode with a goal selected, suffix the key with the goal id so
    each goal caches independently — switching the active goal naturally
    bypasses any cached advice from the prior goal."""
    if period["mode"] == "salary":
        base = f"salary:{period['key']}"
    else:
        # range mode covers '1m', '3m', '6m', '12m' — all keyed by their anchor month
        base = f"{period['range']}:{period['key']}"
    if mode == "advice" and goal_id:
        return f"{base}:{goal_id}"
    return base


def _load_cached_ai_summary(conn, period, mode="digest", goal_id=None):
    """Return the cached summary for this (period, mode) with a stale flag set
    to True when transactions have been imported into the period after the
    summary was generated. Returns None if nothing is cached."""
    key = _ai_summary_period_key(period, mode, goal_id)
    row = conn.execute(
        "SELECT summary, model, created_at FROM ai_summaries WHERE period_key = ? AND mode = ?",
        (key, mode),
    ).fetchone()
    if not row:
        return None
    where_sql, params = _period_where_sql(period)
    latest = conn.execute(
        f"""SELECT MAX(t.imported_at) AS latest
            FROM transactions t WHERE {where_sql}""",
        params,
    ).fetchone()
    stale = bool(
        latest and latest["latest"] and row["created_at"]
        and latest["latest"] > row["created_at"]
    )
    return {
        "summary": row["summary"],
        "model": row["model"],
        "created_at": row["created_at"],
        "stale": stale,
    }


def _store_ai_summary(conn, period, summary_text, model, mode="digest", goal_id=None):
    """Upsert the summary by (period_key, mode) — Regenerate always replaces."""
    key = _ai_summary_period_key(period, mode, goal_id)
    conn.execute(
        """INSERT INTO ai_summaries
             (period_key, mode, period_label, summary, model, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(period_key, mode) DO UPDATE SET
             period_label = excluded.period_label,
             summary      = excluded.summary,
             model        = excluded.model,
             created_at   = excluded.created_at""",
        (
            key,
            mode,
            period.get("label", ""),
            summary_text,
            model,
            datetime.utcnow().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


AI_DIGEST_PROMPT = (
    "Je bent een persoonlijke financiële assistent die de uitgaven van één "
    "gebruiker over een specifieke periode samenvat. De data is gegroepeerd "
    "in onderwerpen (overkoepelende buckets zoals 'Abonnementen' of 'Eten') "
    "met categorieën genest onder elk onderwerp (bijv. 'Spotify' onder "
    "'Abonnementen'). De data bevat alleen aggregaten — geen individuele "
    "transacties, rekeningnummers, tegenpartijen of namen van winkels. "
    "Schrijf een korte samenvatting in proza van 4 tot 7 zinnen voor de gebruiker. "
    "Schrijf in het Nederlands.\n\n"
    "Regels:\n"
    "- Houd je strikt aan de feiten in de JSON. Verzin nooit cijfers.\n"
    "- Gebruik bedragen in euro's, geformatteerd als \"€420\" of \"€1.890\".\n"
    "- Begin met inkomsten / uitgaven / netto.\n"
    "- Noem 1 tot 3 opvallende onderwerpen; wanneer één enkele categorie een "
    "onderwerp domineert, noem dan ook die categorie (bijv. \"Abonnementen "
    "kwamen grotendeels van Spotify\").\n"
    "- Als vs_prior_topic betekenisvolle veranderingen toont, noem dan de "
    "grootste een of twee.\n"
    "- Vermijd algemeen advies (\"overweeg te budgetteren\"). De gebruiker wil "
    "een samenvatting.\n"
    "- Alleen prozaparagrafen — geen opsommingen, geen koppen.\n\n"
    "Periodedata (JSON):\n"
)

# Backwards-compat alias in case anything still imports the old constant.
AI_SUMMARY_PROMPT = AI_DIGEST_PROMPT


AI_FORECAST_PROMPT = (
    "Je bent een persoonlijke financiële assistent die een prognose schrijft "
    "voor één gebruiker, en de vraag 'waar gaat het naartoe?' beantwoordt. "
    "De data is gegroepeerd in onderwerpen met geneste categorieën — alleen "
    "aggregaten, geen transactienamen, rekeningnummers, tegenpartijen of "
    "winkels. Schrijf 4 tot 7 zinnen in gewoon proza, beschrijvend en "
    "gekwantificeerd, niet voorschrijvend. Schrijf in het Nederlands.\n\n"
    "Behandel, ongeveer in deze volgorde:\n"
    "- De landingszone voor uitgaven aan het einde van de periode, met "
    "vermelding van de 1 tot 2 onderwerpen die afwijkingen van het vorige "
    "gemiddelde veroorzaken (gebruik `vs_prior_topic` en `topic_history_12mo`).\n"
    "- Maximaal TWEE waarschuwingen per onderwerp over het tempo, alleen "
    "wanneer dat betekenisvol afwijkt van de trend.\n"
    "- Ontwikkeling van de spaarquote over de recente maanden in "
    "`savings_rate_history`.\n"
    "- Een schatting van de financiële ruimte wanneer `recurring_load` + "
    "het huidige tempo het saldo richting een dieptepunt zou duwen "
    "(kruisverwijs naar `lowest_balance` en `salary_cadence`).\n\n"
    "Regels:\n"
    "- Houd je strikt aan de feiten in de JSON. Verzin nooit cijfers.\n"
    "- Gebruik bedragen in euro's, geformatteerd als \"€420\" of \"€1.890\".\n"
    "- Elke bewering verwijst naar een getal uit de payload.\n"
    "- Geen voorschrijvende formuleringen — geen \"je moet\", geen aanbevelingen.\n"
    "- Als `savings_rate_history` minder dan 3 voorgaande maanden met data "
    "bevat, erken dan dat de betrouwbaarheid beperkt is.\n"
    "- Verwijs nooit naar winkels of tegenpartijen; spreek alleen in termen "
    "van onderwerpen/categorieën.\n"
    "- Alleen prozaparagrafen — geen opsommingen, geen koppen.\n\n"
    "Periodedata (JSON):\n"
)


AI_ADVICE_PROMPT = (
    "Je bent een persoonlijke financiële assistent die advies schrijft voor "
    "één gebruiker, en de vraag beantwoordt: 'wat is de meest impactvolle "
    "wijziging die ik zou moeten doorvoeren, gegeven mijn doel?'. De data "
    "is gegroepeerd in onderwerpen met geneste categorieën — alleen "
    "aggregaten, geen transactienamen, rekeningnummers, tegenpartijen of "
    "winkels. Schrijf 4 tot 7 zinnen gewoon proza. Schrijf in het Nederlands.\n\n"
    "Als de payload een `goal`-object bevat, formuleer elke aanbeveling dan "
    "in relatie tot de `title` en `description` van dat doel. Specifiek:\n"
    "- `general` — richt op het verbeteren van de algemene spaarquote.\n"
    "- `emergency_fund` — geef voorkeur aan consistente maandelijkse "
    "stortingen richting een buffer van 3 tot 6 maanden essentiële uitgaven.\n"
    "- `big_purchase` — voeg waar mogelijk doorlooptijd-berekeningen toe.\n"
    "- `cut_spending` — focus op terugkerende uitgaven en sluipende "
    "kostenstijgingen.\n"
    "Als er geen `goal` aanwezig is, val dan terug op een spaarquote-frame.\n\n"
    "Behandel:\n"
    "- De grootste afwijking t.o.v. de trend, met een impact-frame — hoeveel "
    "het de spaarquote of het netto verschuift (gebruik `topic_history_12mo` "
    "en `vs_prior_topic`).\n"
    "- HOOGSTENS ÉÉN herverdelingssuggestie met expliciete rekensom "
    "(bijv. \"X met €30 en Y met €50 inkrimpen verhoogt de spaarquote van "
    "14% naar 22%\").\n"
    "- HOOGSTENS ÉÉN gewoonteobservatie wanneer `day_of_period` een duidelijk "
    "patroon laat zien (bijv. geconcentreerde uitgaven in week 1 na het begin "
    "van de periode).\n\n"
    "Regels:\n"
    "- Elke aanbeveling MOET een eurobedrag uit de payload noemen. Geen "
    "\"overweeg te minderen\" zonder een getal erbij.\n"
    "- Houd je strikt aan de feiten in de JSON. Verzin nooit cijfers.\n"
    "- Gebruik bedragen in euro's, geformatteerd als \"€420\" of \"€1.890\".\n"
    "- Beperk aanbevelingen tot in totaal 1 tot 2. Drie leest als een preek.\n"
    "- Verwijs nooit naar winkels of tegenpartijen; spreek alleen in termen "
    "van onderwerpen/categorieën.\n"
    "- Als `savings_rate_history` minder dan 3 maanden data bevat, zeg dat dan.\n"
    "- Alleen prozaparagrafen — geen opsommingen, geen koppen.\n\n"
    "Periodedata (JSON):\n"
)


# Hardcoded goal catalog. Title + description are user-facing and are passed
# verbatim into the Advice payload so the model can frame recommendations.
GOALS = {
    "general": {
        "title": "Algemeen advies",
        "description": (
            "Geen specifiek doel — toon de kans met de meeste impact om "
            "de algemene financiële gezondheid en spaarquote te verbeteren."
        ),
    },
    "emergency_fund": {
        "title": "Bouw een noodfonds op",
        "description": (
            "Leg 3 tot 6 maanden aan essentiële uitgaven opzij als veiligheidsbuffer, "
            "met prioriteit voor consistente maandelijkse stortingen boven "
            "eenmalige grote overschrijvingen."
        ),
    },
    "big_purchase": {
        "title": "Sparen voor een grote aankoop",
        "description": (
            "Werk toe naar een concreet eenmalig doel zoals een aanbetaling voor "
            "een huis, een auto of een verbouwing, met advies gericht op "
            "doorlooptijd-berekeningen."
        ),
    },
    "cut_spending": {
        "title": "Maandelijkse uitgaven verlagen",
        "description": (
            "Verminder terugkerende en vrij besteedbare uitgaven — abonnementen, "
            "uit eten, sluipende kostenstijgingen — zonder het inkomen te wijzigen."
        ),
    },
}
DEFAULT_GOAL_ID = "general"

_AI_MODE_PROMPTS = {
    "digest":   AI_DIGEST_PROMPT,
    "forecast": AI_FORECAST_PROMPT,
    "advice":   AI_ADVICE_PROMPT,
}


def _get_user_active_goal_id(conn, user_id):
    """Return the user's selected goal id, or DEFAULT_GOAL_ID if unset/unknown."""
    if not user_id:
        return DEFAULT_GOAL_ID
    row = conn.execute(
        "SELECT active_goal FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not row:
        return DEFAULT_GOAL_ID
    goal_id = (row["active_goal"] or "").strip()
    return goal_id if goal_id in GOALS else DEFAULT_GOAL_ID


def _get_user_auto_generate(conn, user_id):
    """Return 1/0 for the user's auto_generate_ai flag (default 1 if missing)."""
    if not user_id:
        return 1
    row = conn.execute(
        "SELECT auto_generate_ai FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not row:
        return 1
    val = row["auto_generate_ai"]
    return 1 if val is None or int(val) == 1 else 0


@app.route("/api/diagnostics/ai_summary", methods=["POST"])
def api_diagnostics_ai_summary():
    """Send a scrubbed, aggregated summary of the period to Gemini and return
    the prose response. The caller supplies their own Gemini API key in the
    JSON body — it is used once per request and never persisted server-side.
    Never sends `account` or `counterparty`; never sends `name` or
    `notifications` either — only topic/category-level aggregates.

    Query param `mode` selects one of three prompt templates:
    digest (default), forecast, or advice. For advice the user's active
    goal is read from `users.active_goal` and baked into the cache key so
    each goal caches independently."""
    import json

    body = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"error": "api_key ontbreekt in de request body."}), 400

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return jsonify({"error": "google-genai is niet geïnstalleerd. Voer uit: pip install google-genai"}), 503

    requested_month = (request.args.get("month") or "").strip()
    requested_salary = (request.args.get("salary_period") or "").strip()
    requested_range = (request.args.get("range") or "1m").strip()
    if requested_range not in RANGE_SIZES:
        requested_range = "1m"

    mode = (request.args.get("mode") or "digest").strip().lower()
    if mode not in _AI_MODE_PROMPTS:
        return jsonify({"error": f"Onbekende modus '{mode}'. Verwacht één van digest, forecast, advice."}), 400

    user = _current_user()
    user_id = user["id"] if user else None

    with get_connection() as conn:
        period = _resolve_period(conn, requested_month, requested_salary, requested_range)
        if not period:
            return jsonify({"error": "Geen periode geselecteerd."}), 400

        goal_id = None
        goal_for_payload = None
        if mode == "advice":
            goal_id = _get_user_active_goal_id(conn, user_id)
            goal_for_payload = {"id": goal_id, **GOALS[goal_id]}

        payload = _build_ai_summary_payload(conn, period, goal=goal_for_payload)

    prompt_text = _AI_MODE_PROMPTS[mode] + json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        client = genai.Client(api_key=api_key)
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=prompt_text)],
            ),
        ]
        config = types.GenerateContentConfig()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=config,
        )
        text = (response.text or "").strip()
    except Exception as e:
        return jsonify({"error": f"Gemini-aanroep mislukt: {e.__class__.__name__}: {e}"}), 502

    # Cache the summary for this (period, mode). Always overwrites —
    # Regenerate is the explicit "I want a fresh take" affordance.
    cached_at = None
    if text:
        with get_connection() as conn:
            _store_ai_summary(conn, period, text, GEMINI_MODEL, mode=mode, goal_id=goal_id)
            cached = _load_cached_ai_summary(conn, period, mode=mode, goal_id=goal_id)
            if cached:
                cached_at = cached["created_at"]

    result = {
        "period_label": payload["period_label"],
        "mode": mode,
        "summary": text,
        "created_at": cached_at,
        "model": GEMINI_MODEL,
        "stale": False,
    }
    if request.args.get("debug") == "1":
        result["payload_sent"] = payload
    return jsonify(result)


@app.route("/api/diagnostics/category_pie")
def api_diagnostics_category_pie():
    """Drill-down: category breakdown for a single topic, filtered by the same
    period params the diagnostics page uses (month/salary_period/range)."""
    raw_topic = request.args.get("topic_id")
    try:
        topic_id = int(raw_topic) if raw_topic else None
    except (TypeError, ValueError):
        return jsonify({"error": "invalid topic_id"}), 400
    if topic_id is None:
        return jsonify({"error": "topic_id required"}), 400

    requested_month = (request.args.get("month") or "").strip()
    requested_salary = (request.args.get("salary_period") or "").strip()
    requested_range = (request.args.get("range") or "1m").strip()
    if requested_range not in RANGE_SIZES:
        requested_range = "1m"

    with get_connection() as conn:
        period = _resolve_period(conn, requested_month, requested_salary, requested_range)
        if not period:
            return jsonify({"error": "no period selected"}), 400
        result = _build_category_pie(conn, period, topic_id)
        if result is None:
            return jsonify({"error": "unknown topic"}), 404
    return jsonify(result)


@app.route("/diagnostics")
def diagnostics():
    requested_month = (request.args.get("month") or "").strip()
    requested_salary = (request.args.get("salary_period") or "").strip()
    requested_range = (request.args.get("range") or "1m").strip()
    requested_account = (request.args.get("balance_account") or "").strip() or None
    if requested_range not in RANGE_SIZES:
        requested_range = "1m"
    with get_connection() as conn:
        months = [
            {"value": r["m"], "label": format_month_label(r["m"])}
            for r in conn.execute(
                "SELECT DISTINCT substr(date, 1, 7) AS m FROM transactions ORDER BY m DESC"
            ).fetchall()
        ]
        salary_periods = _list_salary_periods(conn)
        range_options = [{"value": k, "label": RANGE_LABELS[k]}
                         for k in ("1m", "3m", "6m", "12m")]
        if not months:
            empty_user = _current_user()
            empty_uid = empty_user["id"] if empty_user else None
            empty_goal_id = _get_user_active_goal_id(conn, empty_uid)
            empty_auto = _get_user_auto_generate(conn, empty_uid)
            return render_template(
                "diagnostics.html",
                months=[], salary_periods=[], range_options=range_options, data=None,
                selected_month="", selected_month_label="",
                selected_salary_period="", selected_period_label="",
                selected_range="1m",
                month_grid=None,
                salary_target="",
                cached_digest=None,
                cached_forecast=None,
                cached_advice=None,
                active_goal=GOALS[empty_goal_id] | {"id": empty_goal_id},
                auto_generate_ai=empty_auto,
            )

        valid_months = {m["value"] for m in months}
        valid_salary = {p["start"] for p in salary_periods}

        if requested_salary and requested_salary in valid_salary:
            period = _resolve_period(conn, "", requested_salary)
            selected_month = ""
            selected_salary = requested_salary
            selected_range = "1m"
        else:
            # Anchors are accepted on shape rather than membership: a quarter or
            # year selection legitimately anchors on a month that holds no
            # transactions itself (Q4 anchors on December). The grid greys out
            # cells with no data behind them, so this can't be reached by
            # clicking — only by editing the URL, which lands on an empty
            # dashboard rather than a wrong one.
            if _ANCHOR_MONTH_RE.match(requested_month) and requested_month[:4] in {
                m["value"][:4] for m in months
            }:
                month = requested_month
            else:
                month = months[0]["value"]
            period = _resolve_period(conn, month, "", requested_range)
            selected_month = month
            selected_salary = ""
            selected_range = requested_range

        # Which year the picker displays. Defaults to the year of whatever is
        # selected, so the grid always opens showing the current selection, but
        # the arrows can move it independently without changing the period.
        anchor_year = (selected_month or (salary_periods[0]["start"] if salary_periods else ""))[:4]
        years_with_data = {m["value"][:4] for m in months}
        requested_year = (request.args.get("year") or "").strip()
        grid_year = requested_year if requested_year in years_with_data else anchor_year
        month_grid = _build_month_grid(
            valid_months, grid_year, selected_month, selected_range, selected_salary
        )

        # The salary toggle needs a concrete period to switch to: prefer one
        # starting inside the month you're looking at, else the most recent.
        salary_target = ""
        if salary_periods:
            same_month = next(
                (p["start"] for p in salary_periods if p["start"][:7] == selected_month), None
            )
            salary_target = same_month or salary_periods[0]["start"]

        data = _build_diagnostics(conn, period, balance_account=requested_account)
        user = _current_user()
        user_id = user["id"] if user else None
        active_goal_id = _get_user_active_goal_id(conn, user_id)
        auto_generate_ai = _get_user_auto_generate(conn, user_id)
        cached_digest = _load_cached_ai_summary(conn, period, mode="digest")
        cached_forecast = _load_cached_ai_summary(conn, period, mode="forecast")
        cached_advice = _load_cached_ai_summary(
            conn, period, mode="advice", goal_id=active_goal_id
        )

    return render_template(
        "diagnostics.html",
        months=months,
        salary_periods=salary_periods,
        range_options=range_options,
        data=data,
        selected_month=selected_month,
        selected_month_label=format_month_label(selected_month),
        selected_salary_period=selected_salary,
        selected_period_label=period["label"] if period else "",
        selected_range=selected_range,
        month_grid=month_grid,
        salary_target=salary_target,
        cached_digest=cached_digest,
        cached_forecast=cached_forecast,
        cached_advice=cached_advice,
        active_goal=GOALS[active_goal_id] | {"id": active_goal_id},
        auto_generate_ai=auto_generate_ai,
    )


def _build_savings_data(conn):
    """Money flowing through the Savings topic, grouped by month.

    Convention: a Debit leaves the checking account and lands in savings (an
    'In' from the savings account's perspective). A Credit comes back to
    checking (an 'Out' of savings). Net saved = ins - outs."""
    rows = conn.execute(
        """SELECT t.id, t.date, t.name, t.amount, t.direction, t.notifications,
                  t.counterparty, COALESCE(c.color, tp.color) AS category_color
           FROM transactions t
           JOIN categories c ON c.id = t.category_id
           JOIN topics tp ON tp.id = c.topic_id
           WHERE tp.name IN ('Savings', 'Sparen')
           ORDER BY t.date DESC, t.id DESC"""
    ).fetchall()

    months = defaultdict(lambda: {"ins": 0.0, "outs": 0.0, "tx": []})
    total_in = total_out = 0.0
    for r in rows:
        ym = (r["date"] or "")[:7]
        amt = float(r["amount"] or 0)
        bucket = months[ym]
        if r["direction"] == "Debit":
            bucket["ins"] += amt
            total_in += amt
        elif r["direction"] == "Credit":
            bucket["outs"] += amt
            total_out += amt
        bucket["tx"].append(dict(r))

    monthly = []
    for ym in sorted(months.keys(), reverse=True):
        b = months[ym]
        monthly.append({
            "month": ym,
            "label": format_month_label(ym),
            "ins": b["ins"],
            "outs": b["outs"],
            "net": b["ins"] - b["outs"],
            "tx": b["tx"],
        })

    net_total = total_in - total_out
    months_with_data = len([m for m in monthly if m["ins"] > 0 or m["outs"] > 0])
    avg_net = (net_total / months_with_data) if months_with_data else 0.0
    color = next((dict(r)["category_color"] for r in rows), "#14b8a6")

    return {
        "monthly": monthly,
        "total_in": total_in,
        "total_out": total_out,
        "net_total": net_total,
        "avg_net": avg_net,
        "tx_count": len(rows),
        "months_count": months_with_data,
        "category_color": color,
    }


@app.route("/savings")
def savings():
    with get_connection() as conn:
        data = _build_savings_data(conn)
    return render_template("savings.html", data=data)


@app.template_filter("eur")
def eur_filter(v):
    if v is None:
        return ""
    return f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@app.route("/hulp")
@login_required
def help_page():
    # In-app handleiding. Linked from the main nav so a family member who
    # hasn't seen the app before can find the basics without leaving the window.
    return render_template("help.html")


@app.route("/health")
def health():
    # Polled by the Tauri shell on startup to know when the WebView can load.
    return jsonify(ok=True)


def _parse_int_flag(name: str) -> int | None:
    """Pull `--name N` or `--name=N` out of sys.argv. Returns None if absent."""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return None
        prefix = name + "="
        if a.startswith(prefix):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _parse_port() -> int:
    # Tauri picks a free port and passes --port; dev mode keeps 5004.
    return _parse_int_flag("--port") or 5004


def _make_parent_alive_probe(parent_pid: int):
    """Return a no-arg callable that returns True while the parent is alive.

    Windows: OpenProcess(QUERY_LIMITED_INFORMATION) + GetExitCodeProcess; the
    process is alive iff its exit code is STILL_ACTIVE (259). We can't use
    os.kill(pid, 0) here — on Windows that maps to TerminateProcess / CTRL_C
    semantics, not a POSIX-style liveness probe, and raises OSError even when
    the process is alive.

    POSIX: os.kill(pid, 0) is the canonical liveness check.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        def alive() -> bool:
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, parent_pid)
            if not h:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(h)
        return alive

    def alive() -> bool:
        import os
        try:
            os.kill(parent_pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # process exists, we just can't signal it
    return alive


def _watch_parent(parent_pid: int) -> None:
    """Self-terminate when the Tauri shell goes away.

    Tauri kills our sidecar's process on window close, but PyInstaller --onefile
    spawns a bootloader that forks a child Python process; killing the bootloader
    does not propagate to the Python child. This watchdog polls for the shell
    and exits when it disappears — also catches force-quit from Task Manager.
    """
    import os
    import time
    alive = _make_parent_alive_probe(parent_pid)
    while True:
        if not alive():
            os._exit(0)
        time.sleep(2)


if __name__ == "__main__":
    init_db()
    frozen = getattr(sys, "frozen", False)

    parent_pid = _parse_int_flag("--parent-pid")
    if parent_pid:
        import threading
        threading.Thread(
            target=_watch_parent, args=(parent_pid,), daemon=True
        ).start()

    # Frozen mode: no debug, no reloader (reloader spawns a second process and
    # breaks PyInstaller + the Tauri sidecar lifecycle).
    app.run(
        host="127.0.0.1",
        port=_parse_port(),
        debug=not frozen,
        use_reloader=not frozen,
    )
