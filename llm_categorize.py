"""Local-model (Ollama) category suggestions — tier 3 of the categorization stack.

Tier 1 is the rule engine and tier 2 is the history-based suggester, both in
app.py. Both are exact or statistical: they say nothing about a merchant they
have never seen, and tier 2 needs five samples and 80% dominance before it
speaks at all. This module handles that residue by asking a locally-running
LLM, which brings outside world knowledge ("SUMUP *BAKKERIJ DE KORENAAR" is a
bakery) that the user's own history cannot supply yet.

Two properties are deliberate:

* It self-extinguishes. Suggestions are never auto-applied. The user accepts
  them, accepted rows become labeled history, and once a merchant accumulates
  enough samples tier 2 takes over and this module is never asked again.

* It runs locally. The Gemini path (api_diagnostics_ai_summary in app.py) is
  deliberately forbidden from sending `name` or `counterparty` off the machine
  — which is precisely the data categorization needs. Keeping inference on
  localhost is what makes the feature possible at all.

No third-party dependencies: stdlib urllib only, so finance-sorter-backend.spec
and requirements.txt are unaffected.
"""

import functools
import json
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:11434"

# First call in a sweep pays for loading the model into VRAM; later ones don't.
PROBE_TIMEOUT = 2.0
CLASSIFY_TIMEOUT = 60.0

# Hold the model in VRAM between calls in a sweep. Without this Ollama may
# unload after each request and every transaction re-pays the load cost.
KEEP_ALIVE = "5m"

# The reason field is capped at a short phrase, so there is no need to let the
# model ramble — and a low cap bounds worst-case latency per transaction.
MAX_TOKENS = 120

# Few-shot budget. Examples come from the user's own labeled rows, which is the
# single biggest accuracy lever: it teaches the model this specific Dutch
# taxonomy using real ING/ASN merchant strings instead of a generic prior.
EXAMPLES_PER_CATEGORY = 3
MAX_EXAMPLES = 120

# Context window requested from Ollama. Without this the daemon uses its own
# default (commonly 4096), and a prompt that exceeds it is silently TRUNCATED
# rather than rejected — you get a confident answer built from a fraction of
# the instructions.
NUM_CTX = 8192

# Hard ceiling on the assembled system prompt, in characters (~3.5 chars per
# token), leaving room inside NUM_CTX for the transaction line and the reply.
#
# This is the important one. The taxonomy sits at the top of the prompt, so an
# overflow drops the category list and the output rules while the examples at
# the end survive — the model then reasons correctly about the merchant and
# attaches an essentially arbitrary id, because constrained decoding still
# forces a valid one. Measured on a padded prompt: 4/4 correct at 2.7k tokens,
# 1/4 at 25k, with unrelated merchants all collapsing onto the same category.
# Examples are dropped until the prompt fits; the taxonomy is never sacrificed.
PROMPT_CHAR_BUDGET = 16000

# Representative merchant names shown inline next to each category. The category
# names alone are undefined — nothing in "Food > Lunch" vs "Food > Snacks" tells
# the model where this user draws the line. Naming a few real merchants per
# category defines them from the user's own data, costs a handful of tokens, and
# reaches every category, including the ones too sparse to earn a full few-shot
# example.
REPS_PER_CATEGORY = 3
REP_NAME_MAXLEN = 26

# Whether few-shot examples carry the description field. Format parity with the
# query line sounds obviously right, but measured worse: the extra context makes
# the model surer of what a merchant *is* in the world, which fights the user's
# own idiosyncratic use of it. Kept as a switch so the tradeoff stays testable
# via scripts/eval_categorizer.py rather than being re-litigated from intuition.
EXAMPLES_INCLUDE_NOTES = False

CONFIDENCE_VALUES = ("high", "medium", "low")

# A pull streams for minutes on a slow line; the timeout is per socket read,
# not for the whole transfer, so this only trips on a genuinely stalled link.
PULL_READ_TIMEOUT = 300.0

# Recommended model per VRAM tier. Sizes are the published Q4 download sizes and
# are shown to the user before a multi-GB download starts.
#
# The rule here is "smallest model that does the job", not "largest that fits".
# Picking one label from a fixed 40-item enum, with worked examples from the
# user's own history in the prompt and constrained decoding making invalid
# output impossible, is close to the easiest task you can give an LLM — so a 4b
# handles the bulk of it, and the leftover VRAM stays available for whatever
# else the card is doing. A bigger model is a deliberate upgrade, chosen from
# the picker and justified by scripts/eval_categorizer.py rather than assumed.
#
# These tags are a moving target — if a pull fails with "model not found", check
# ollama.com/library and update this table; the error is surfaced verbatim so
# the cause is visible rather than mysterious. qwen3.6 is deliberately absent:
# it only ships at 27b/35b (17-24 GB).
RECOMMENDED_MODELS = [
    # (min VRAM MB, tag, approx download bytes)
    (5000, "qwen3.5:4b",   3400000000),
    (3500, "qwen3.5:2b",   2700000000),
    (0,    "qwen3.5:0.8b", 1000000000),
]


def recommend_model(vram_mb=None):
    """Pick a model that fits the detected card.

    Falls back to the smallest entry when VRAM can't be detected — a model that
    is smaller than necessary still works, where one that's too big silently
    spills into system RAM and makes the feature feel broken.
    """
    if vram_mb is None:
        vram_mb = detect_vram_mb()
    for min_mb, tag, size in RECOMMENDED_MODELS:
        if vram_mb is not None and vram_mb >= min_mb:
            return {"model": tag, "size": size, "vram_mb": vram_mb}
    tag, size = RECOMMENDED_MODELS[-1][1], RECOMMENDED_MODELS[-1][2]
    return {"model": tag, "size": size, "vram_mb": vram_mb}


class OllamaError(Exception):
    """Ollama was unreachable, timed out, or returned something unusable."""


# ---------- transport ----------

def _post_json(url, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


@functools.lru_cache(maxsize=1)
def detect_vram_mb():
    """Total VRAM of the largest installed NVIDIA GPU, or None if undetectable.

    Memoized: this shells out to nvidia-smi, and total VRAM doesn't change
    without a reboot — so a page render never pays for the subprocess twice.

    Used to warn when a chosen model won't fit. Returns None on AMD/Intel or a
    machine without nvidia-smi, in which case the UI simply doesn't warn —
    guessing wrong here would be worse than staying quiet.
    """
    kwargs = {}
    if sys.platform == "win32":
        # Suppress the console flash when the frozen (windowed) app shells out.
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, **kwargs
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    values = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            values.append(int(line))
    return max(values) if values else None


def probe(url=DEFAULT_URL, timeout=PROBE_TIMEOUT):
    """Is Ollama up, and which models are installed?

    Never raises — the caller uses this to decide whether to offer the feature,
    so an unreachable daemon is an expected answer, not an error.
    """
    base = (url or DEFAULT_URL).rstrip("/")
    try:
        req = urllib.request.Request(f"{base}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return {"ok": False, "models": [], "vram_mb": detect_vram_mb(),
                "error": f"Ollama niet bereikbaar op {base} ({reason})."}
    except Exception as e:
        return {"ok": False, "models": [], "vram_mb": detect_vram_mb(),
                "error": f"{e.__class__.__name__}: {e}"}

    models = []
    for m in body.get("models") or []:
        name = m.get("name") or m.get("model")
        if name:
            models.append({"name": name, "size": m.get("size")})
    models.sort(key=lambda m: m["name"])
    return {"ok": True, "models": models, "vram_mb": detect_vram_mb(), "error": None}


def pull_model(model, url=DEFAULT_URL, on_progress=None):
    """Download a model through Ollama, reporting progress as it streams.

    Exists so the user never has to open a terminal to run `ollama pull`. The
    response is NDJSON — one JSON object per line — carrying a phase string and,
    during the transfer itself, completed/total byte counts.

    `on_progress` receives each decoded message. Raises OllamaError on transport
    failure or on an error reported by Ollama (an unknown tag lands here, and
    the message is passed through verbatim so the cause is obvious).

    Reaching the end of the stream is NOT taken as success. Ollama does its
    final work — verifying the digest, renaming the blob, writing the manifest —
    while the connection is still open, so a stream that ends early leaves a
    fully-downloaded `-partial` blob and no usable model. We therefore require
    the terminal `success` message *and* confirm the tag actually shows up in
    /api/tags before reporting the download done. Without that check the app
    happily configures itself to use a model that isn't there, and only fails
    later at classify time with a confusing 404.
    """
    base = (url or DEFAULT_URL).rstrip("/")
    data = json.dumps({"model": model, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/pull",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    saw_success = False
    last_phase = ""
    try:
        with urllib.request.urlopen(req, timeout=PULL_READ_TIMEOUT) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("error"):
                    raise OllamaError(str(msg["error"]))
                status = str(msg.get("status") or "")
                if status:
                    last_phase = status
                if status == "success":
                    saw_success = True
                if on_progress:
                    on_progress(msg)
    except urllib.error.HTTPError as e:
        raise OllamaError(f"HTTP {e.code} van Ollama: {e.reason}") from e
    except urllib.error.URLError as e:
        raise OllamaError(f"Ollama niet bereikbaar op {base} ({getattr(e, 'reason', e)}).") from e
    except OllamaError:
        raise
    except Exception as e:
        raise OllamaError(f"{e.__class__.__name__}: {e}") from e

    # Authoritative check: is the tag actually installed now? This catches both
    # a stream that ended without `success` and the subtler case where success
    # arrived but the model still isn't registered.
    installed = {m["name"] for m in probe(base).get("models", [])}
    if model in installed:
        return True

    if not saw_success:
        raise OllamaError(
            f"Download afgebroken tijdens '{last_phase or 'onbekend'}' — "
            f"{model} is niet geïnstalleerd. De deels gedownloade data blijft "
            f"bewaard, dus opnieuw proberen hervat waar het gebleven was."
        )
    raise OllamaError(
        f"Ollama meldde succes maar {model} staat niet in de modellenlijst. "
        f"Probeer het opnieuw."
    )


# ---------- prompt construction ----------

def _fmt_amount(amount, direction):
    """Sign the amount by direction — income vs expense is a strong signal."""
    try:
        v = abs(float(amount))
    except (TypeError, ValueError):
        return "?"
    return f"{'-' if direction == 'Debit' else '+'}{v:.2f}"


def _fmt_tx(row, include_notes=True):
    """One transaction as a single compact line.

    Keeping this on one line (rather than pretty JSON) matters: it is repeated
    up to 80 times in the few-shot block, so the token cost of the format
    itself is multiplied.
    """
    def get(key):
        try:
            return (row[key] or "").strip() if row[key] is not None else ""
        except (KeyError, IndexError, TypeError):
            return ""

    parts = [f"naam={get('name')}"]
    cp = get("counterparty")
    if cp:
        parts.append(f"tegenpartij={cp}")
    code = get("code")
    if code:
        parts.append(f"code={code}")
    ttype = get("transaction_type")
    if ttype:
        parts.append(f"type={ttype}")
    parts.append(f"bedrag={_fmt_amount(row['amount'], get('direction'))}")
    if include_notes:
        note = get("notifications")
        if note:
            parts.append(f"omschrijving={note[:120]}")
    return " | ".join(parts)


def build_context(conn, examples_per_category=EXAMPLES_PER_CATEGORY,
                  max_examples=MAX_EXAMPLES, exclude_tx_ids=None):
    """Assemble everything that stays constant for a whole sweep.

    The returned `system` string is byte-identical across every call in a
    sweep, which lets Ollama reuse its KV cache for the entire prefix — only
    the short per-transaction suffix is actually processed each time. Building
    it once and reusing it is the difference between a sweep taking seconds and
    taking minutes.

    `exclude_tx_ids` keeps specific rows out of the few-shot block. Production
    never needs it; the eval harness uses it to hold out a test set so it isn't
    scoring the model on examples it was just shown.
    """
    exclude_tx_ids = exclude_tx_ids or set()
    cats = conn.execute(
        """SELECT c.id, c.name, t.name AS topic_name
           FROM categories c
           JOIN topics t ON t.id = c.topic_id
           ORDER BY t.name COLLATE NOCASE, c.name COLLATE NOCASE"""
    ).fetchall()
    if not cats:
        raise OllamaError("Geen categorieën gedefinieerd — maak eerst categorieën aan.")

    valid_ids = [c["id"] for c in cats]

    labeled = conn.execute(
        """SELECT t.id, t.name, t.counterparty, t.code, t.transaction_type,
                  t.direction, t.amount, t.notifications, t.category_id
           FROM transactions t
           WHERE t.category_id IS NOT NULL
             AND t.match_parent_id IS NULL
           ORDER BY t.date DESC, t.id DESC"""
    ).fetchall()

    # Representative merchants per category, honouring the same hold-out as the
    # few-shot block — otherwise the eval would be reading its own answers off
    # the category list.
    reps = {}
    for row in labeled:
        if row["id"] in exclude_tx_ids:
            continue
        cid = row["category_id"]
        bucket = reps.setdefault(cid, [])
        if len(bucket) >= REPS_PER_CATEGORY:
            continue
        name = " ".join((row["name"] or "").split())[:REP_NAME_MAXLEN].strip()
        if name and name.lower() not in {b.lower() for b in bucket}:
            bucket.append(name)

    cat_lines = []
    for c in cats:
        line = f"[{c['id']}] {c['topic_name']} > {c['name']}"
        sample = reps.get(c["id"])
        if sample:
            line += f"   (bijv. {', '.join(sample)})"
        cat_lines.append(line)

    # Few-shot examples drawn from the user's own labeled rows. Newest first so
    # the model sees the merchant strings the bank currently emits, and capped
    # per category so one big category can't crowd out the rest.
    examples = []
    per_cat = {}
    seen_lines = set()
    for row in labeled:
        if row["id"] in exclude_tx_ids:
            continue
        cid = row["category_id"]
        if per_cat.get(cid, 0) >= examples_per_category:
            continue
        line = _fmt_tx(row, include_notes=EXAMPLES_INCLUDE_NOTES)
        if line in seen_lines:
            continue
        seen_lines.add(line)
        per_cat[cid] = per_cat.get(cid, 0) + 1
        examples.append(f"{line} -> {cid}")
        if len(examples) >= max_examples:
            break

    def assemble(example_lines):
        return _SYSTEM_TEMPLATE.format(
            categories="\n".join(cat_lines),
            examples="\n".join(example_lines) if example_lines
            else "(nog geen gecategoriseerde transacties beschikbaar)",
        )

    # Drop examples until the prompt fits the budget. Examples are the
    # expendable part; the rules and the category list are not, and letting
    # Ollama truncate instead would silently discard exactly those.
    system = assemble(examples)
    while examples and len(system) > PROMPT_CHAR_BUDGET:
        examples.pop()
        system = assemble(examples)

    return {
        "system": system,
        "valid_ids": set(valid_ids),
        "schema": _response_schema(valid_ids),
        "category_count": len(cats),
        "example_count": len(examples),
    }


_SYSTEM_TEMPLATE = """\
Je bent een classificatie-assistent voor Nederlandse bankafschriften (ING/ASN).
Je krijgt één transactie en kiest daarvoor precies één categorie.

Antwoord uitsluitend met JSON in deze vorm:
{{"category_id": <id of null>, "confidence": "high|medium|low", "reason": "<korte reden>"}}

Regels:
- Kies alleen een id dat letterlijk in de lijst hieronder staat. Verzin nooit een id.
- Herken je de winkel, het bedrijf of de dienst? Kies dan de best passende
  categorie, ook als je niet volledig zeker bent. Elke suggestie wordt door de
  gebruiker bevestigd of afgewezen, dus een onderbouwde keuze is nuttiger dan
  geen antwoord. Gebruik "confidence" om aan te geven hoe zeker je bent.
- Antwoord alleen category_id: null als er echt geen aanknopingspunt is: een
  persoonsnaam zonder verdere context, een kaal IBAN, of een betaaldienst
  (PayPal, Tikkie, Mollie) waarbij de onderliggende winkel nergens in de
  gegevens voorkomt. Twijfel op zich is geen reden voor null.
- Kijk ook naar de omschrijving: daar staat vaak de echte winkel of dienst.
- Let op het teken van het bedrag: een plusbedrag is inkomsten, een minbedrag uitgaven.
- "reason" is Nederlands en maximaal 15 woorden.

BESCHIKBARE CATEGORIEEN:
{categories}

VOORBEELDEN UIT DE EIGEN HISTORIE VAN DE GEBRUIKER:
{examples}"""


def _response_schema(valid_ids):
    """JSON schema handed to Ollama's `format` for constrained decoding.

    Restricting category_id to an enum of the real ids makes a hallucinated
    category structurally impossible rather than merely unlikely. `null` is in
    the enum on purpose: it is the model's escape hatch for rows it genuinely
    cannot place, and forcing a pick on those manufactures noise the user then
    has to clean up.

    Enforcement is not guaranteed on every model/Ollama version combination,
    so parse_response validates the result against the same id set anyway.
    """
    return {
        "type": "object",
        "properties": {
            "category_id": {"type": ["integer", "null"], "enum": list(valid_ids) + [None]},
            "confidence": {"type": "string", "enum": list(CONFIDENCE_VALUES)},
            "reason": {"type": "string"},
        },
        "required": ["category_id", "confidence", "reason"],
    }


# ---------- classification ----------

def parse_response(raw, valid_ids):
    """Validate a model reply. Returns None when it is unusable or an abstain.

    Anything unexpected — a hallucinated id, malformed JSON, a missing field —
    is treated as an abstain rather than an error. A tier-3 suggester that
    silently declines is strictly better than one that guesses.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    cid = data.get("category_id")
    if cid is None:
        return None
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return None
    if cid not in valid_ids:
        return None

    confidence = str(data.get("confidence") or "").strip().lower()
    if confidence not in CONFIDENCE_VALUES:
        confidence = "low"

    reason = str(data.get("reason") or "").strip()[:200]
    return {"category_id": cid, "confidence": confidence, "reason": reason}


def classify(context, tx, model, url=DEFAULT_URL, timeout=CLASSIFY_TIMEOUT):
    """Classify one transaction. Returns a validated dict, or None to abstain.

    Raises OllamaError only for transport-level problems, which the caller
    treats as "abort the sweep" — a per-row failure just abstains.
    """
    base = (url or DEFAULT_URL).rstrip("/")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": context["system"]},
            {"role": "user", "content": _fmt_tx(tx)},
        ],
        "format": context["schema"],
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0, "num_predict": MAX_TOKENS, "num_ctx": NUM_CTX},
        # Reasoning-capable models (Qwen3 and friends) would otherwise burn
        # seconds per row on a think block we throw away.
        "think": False,
    }

    try:
        body = _post_json(f"{base}/api/chat", payload, timeout)
    except urllib.error.HTTPError as e:
        # Older Ollama builds reject the unknown `think` field. Retry without it
        # rather than failing the whole sweep on a version difference.
        if e.code == 400:
            payload.pop("think", None)
            try:
                body = _post_json(f"{base}/api/chat", payload, timeout)
            except Exception as e2:
                raise OllamaError(f"{e2.__class__.__name__}: {e2}") from e2
        else:
            raise OllamaError(f"HTTP {e.code} van Ollama: {e.reason}") from e
    except urllib.error.URLError as e:
        raise OllamaError(f"Ollama niet bereikbaar op {base} ({getattr(e, 'reason', e)}).") from e
    except Exception as e:
        raise OllamaError(f"{e.__class__.__name__}: {e}") from e

    content = ((body or {}).get("message") or {}).get("content")
    return parse_response(content, context["valid_ids"])
