# Finance Sorter — read-only API

A key-authenticated, read-only view of the same figures the Diagnostiek page shows,
for callers that are not a browser. It exists so the Mei assistant, running beside
this app on the same Pi, can answer questions about the finances without being
handed a login.

The whole surface is read-only. Every write still goes through the
cookie-authenticated routes, so a key can read the numbers but never change a
category, a rule, or a transaction.

## Setup

One App Hub Variable on the app's profile page:

| Variable | Required | Effect |
|---|---|---|
| `FINANCE_API_KEY` | yes | Enables the API. **Unset, every authenticated route returns 503** — it fails closed, never open. |

Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Variables are injected at launch, so **restart the app after setting it**. The
same value goes in Mei's `.env` as `FINANCE_API_KEY`.

The app binds loopback on the App Hub–assigned `PORT`, so callers must be on the
Pi itself. Nothing here is reachable from the tailnet.

**Find that port before you configure a caller.** App Hub picks it per install
from 5001–5999 and it is shown on the app's profile page; the `5004` in the
examples below is only the fallback `app.py` uses when `PORT` is unset (a plain
`python app.py` on Windows), and every install has its own. A caller pointed at
the wrong port gets a connection refused, and one left holding a literal
placeholder — `http://127.0.0.1:<PORT>` — fails earlier still, with Python's
`nonnumeric port` before a packet is ever sent. Mei reads the port from
`FINANCE_API_URL` in its own `.env`; re-check it whenever the app is removed and
re-added in App Hub, because the hub is free to hand out a different one.

## Auth

`Authorization: Bearer <key>` or `X-API-Key: <key>`. Compared with
`secrets.compare_digest`, so the check is constant-time.

```bash
curl -H "Authorization: Bearer $FINANCE_API_KEY" http://127.0.0.1:5004/api/v1/overview
```

## Data freshness — read this before quoting any number

Transactions only ever arrive by hand, through a CSV import on the Transacties
page. There is no bank connection and nothing on a schedule. So an empty month
usually means *not imported yet*, not *nothing was spent*.

Every authenticated response carries a `data_freshness` block:

```json
{"latest_transaction": "2026-02-28", "earliest_transaction": "2026-01-01",
 "days_stale": 181, "newest_month": "2026-02", "months_available": 2,
 "total_transactions": 189, "tx_in_period": 58}
```

A caller should say the figures run to `latest_transaction` rather than present
them as current whenever `days_stale` is large.

For the same reason, `/overview` and `/spending` default to **this month if it
holds any transactions, otherwise the newest month that does** — so the answer is
always about real data.

## Endpoints

All are `GET`. All return JSON.

### `GET /api/v1/ping` — unauthenticated

Deliberately open: it exposes nothing personal, and a caller debugging a 401 needs
to tell "the app is down" apart from "my key is wrong" without already holding a
valid key.

```json
{"ok": true, "app": "finance-sorter", "api_enabled": true,
 "today": "2026-08-28", "has_data": true}
```

`has_data` rather than the newest transaction date on purpose: whether anything has
been imported is harmless, *when* the owner last touched their finances is not.

### `GET /api/v1/overview`

Where things stand over a period. The workhorse.

| Param | Default | Notes |
|---|---|---|
| `month` | see above | `JJJJ-MM` |
| `range` | `1m` | `1m` \| `3m` \| `6m` \| `12m` — a window *ending* at `month` |
| `salary_period` | — | ISO date of a salary period start; overrides `month` |

Returns totals (`income`, `expenses`, `net`, `savings_rate`), `fixed_costs`,
`by_topic` with categories nested, `vs_prior_topic`, `uncategorized_pct`,
`lowest_balance`, `topic_history_12mo`, `savings_rate_history`, `salary_cadence`,
`recurring_load`, plus `period` and `data_freshness`.

Built from the same `_build_ai_summary_payload` the in-app Gemini digest renders
from, which is what stops the page, this API and the assistant drifting into
different numbers for the same month. **Add a new figure there, not in one of the
renderers.** That function also sets the privacy boundary: it never emits raw
transaction free-text, only topic and category aggregates plus IBAN-scrubbed
anomaly strings.

### `GET /api/v1/spending`

Spend per topic or per category, month by month.

| Param | Default | Notes |
|---|---|---|
| `by` | `topic` | `topic` \| `category` |
| `months` | `6` | 1–24 |
| `topic` | — | filter to one topic; must match an existing name |

### `GET /api/v1/subscriptions`

Recurring charges and what they add up to. `monthly_total` normalises by
`cadence_days`, so a quarterly or annual charge does not inflate the headline.

### `GET /api/v1/savings`

Money moving through the Sparen topic, by month. `months` (1–24, default 12).

**Note the sign convention.** It is written from the savings account's point of
view and is inverted relative to the rest of the app: a Debit on the current
account is an `in` (money arriving in savings), a Credit is an `out`. This is
deliberate, not a bug — the response repeats it in a `convention` field.

### `GET /api/v1/transactions`

Individual line items. **Requires at least one of `q`, `topic` or `category`** —
without a filter this would be a route that dumps the entire history, which is
exactly what it is not for. Callers get a 400 listing what to supply.

| Param | Default | Notes |
|---|---|---|
| `q` | — | substring of the merchant name |
| `topic` / `category` | — | must match an existing name |
| `direction` | — | `debit` \| `credit` |
| `days` | — | 1–366, relative to today |
| `limit` | `20` | 1–100; `truncated` says whether anything was cut off |

The only route returning raw merchant text, and it stays as narrow as it can while
still answering "what was that €42": it omits `notifications`, `counterparty`,
`account`, `resulting_balance`, and every IBAN.

## Errors

Always `{"error": "<message>"}`, plus the valid values where there are any:

| Status | When |
|---|---|
| 400 | Bad month, unknown enum, out-of-range window, missing required filter |
| 401 | Missing or wrong key |
| 503 | `FINANCE_API_KEY` is not set |

Two conventions worth knowing:

- **Out-of-range is rejected, not clamped.** `months=500` is a 400 naming the
  bounds, because silently answering with 24 months would leave the caller quoting
  a window it never asked for.
- **A month with no data is not a 404.** It returns the empty period with
  `tx_in_period: 0` and lets `data_freshness` explain itself. An error would make
  a model retry pointlessly; an empty annotated answer describes itself.

Errors echo back what *is* valid — `valid_topics`, `valid_by`, `valid_range`,
`valid_direction`, `valid_categories`, `required_one_of`. This matters more here
than it looks: topic names are the user's own invented Dutch labels, which no
caller could reasonably guess.

## Deliberately absent

- **No `/context` endpoint.** The equivalent would be the AI digest path, which is
  goal-conditioned, cached in `ai_summaries`, and costs a Gemini call — an API
  *read* that spends money and mutates a cache table is no longer read-only. Serve
  the structured payload and let the caller phrase it.
- **Nothing touching Gemini or text-to-speech.** Read-only means no side effects.
- **No user or profile selector.** The data tables have no `user_id`; there is one
  dataset and nothing to disambiguate.
