# AI Feature Enhancement Plan

Turning the Diagnostics AI summary from a one-shot digest into three side-by-side cards — **Digest**, **Forecast**, **Advice** — generated together from one button, with goal-aware advice and optional auto-generation.

## Context

The current AI panel sends a scrubbed, single-period aggregate to Gemini and returns a 4–7 sentence prose recap. The prompt explicitly forbids generic advice, which is the right call but leaves the output feeling shallow. Goal: keep the same privacy posture, add real value.

## Privacy guardrails (non-negotiable)

The payload sent to Gemini must **never** include:

- Transaction `name`
- `account` (own IBAN or account label)
- `counterparty` (merchant or sender name)
- Free-text `notifications`

What is allowed: topic names, category names (user-defined), aggregated amounts, period metadata, dates at the day/month level, balance snapshots, and user-defined goal text.

Every new payload field must be reviewed against this list before shipping.

---

## Phase 1 — Expand the payload

The single biggest unlock. Without more time depth, advice and forecasts stay weak.

Add to `_build_ai_summary_payload`:

- **6–12 months of topic-level history** — month-by-month spend per topic, not just current vs. prior average.
- **Day-of-period spend distribution** — share of spend in week 1 / week 2 / week 3 / week 4 after salary. Surfaces "I always overspend right after payday" patterns.
- **Salary cadence** — typical day-of-month, amount range. Lets the model reason about runway.
- **Recurring load estimate** — sum of Subscriptions topic plus any topic flagged `is_fixed`. Used for next-period preview.
- **Savings rate history** — last 6 months of monthly savings rate.

The Advice mode additionally receives the user's selected goal (title + description) — see Phase 4.

---

## Phase 2 — Three cards, one generate button

Replace the single AI panel with three cards stacked or in a row, each with the same rounded-border styling as the existing diagnostics panels:

- **Digest** — what happened this period
- **Forecast** — where things are heading
- **Advice** — highest-leverage change to make

A single **Generate** button at the top fires all three in parallel. Each card renders its result independently as it returns, so a slow call on one mode doesn't block the others.

UI behavior:

- Single button label changes to "Regenerate" once any of the three is cached.
- Each card shows its own meta line (generated timestamp, model name, stale flag) below its prose, matching the current style.
- If one of the three calls fails, that card shows an error inline and the other two still render normally.
- Optional per-card regenerate icon button (small, top-right of each card) for refreshing just one mode without burning calls on the others. Lower priority — can ship without it.

Caching:

- Each mode caches independently per period.
- Schema change on `ai_summaries`: add a `mode` column (`digest` / `forecast` / `advice`), drop the existing unique index on `period_key`, replace with unique `(period_key, mode)`.
- `_ai_summary_period_key` extended to include mode.

---

## Phase 3 — The three prompts

### Digest

Keep the current prompt. Default behavior, descriptive recap of the period.

### Forecast

Answers: *where am I heading?*

Coverage:

- End-of-period landing zone for expenses, with the topic(s) driving variance from prior average
- Per-topic pace alerts (only for topics meaningfully off-trend, max 2)
- Savings rate trajectory across recent months
- Runway estimate if recurring load + current pace would push balance toward a low point

Tone: descriptive and quantified, not prescriptive. No "you should."

### Advice

Answers: *what's the highest-leverage thing to change, given my goal?*

Coverage:

- The single biggest mover vs. trend, with **impact framing** — how much it shifts the savings rate or net.
- At most one reallocation suggestion with explicit math ("trimming X by €30 and Y by €50 moves savings rate from 14% to 22%").
- One habit observation if the day-of-period data shows a clear pattern.
- All framed against the user's selected goal when one is set; otherwise general advice oriented toward improving savings rate.

Hard rule in the prompt: every recommendation must cite a number from the payload. No "consider cutting back" without a euro figure attached.

---

## Phase 4 — Goals on the Profile page

A new section on the profile page below the Gemini API key, titled **Goals**. The user picks one of four hardcoded goals to give Advice direction.

### The four goals

| ID | Title | Description |
|---|---|---|
| `general` | General advice | No specific target — surface the highest-leverage opportunity to improve overall financial health and savings rate. |
| `emergency_fund` | Build an emergency fund | Set aside 3–6 months of essential expenses as a safety buffer, prioritizing consistent monthly contributions over one-off large transfers. |
| `big_purchase` | Save for a big purchase | Work toward a concrete one-off target like a house deposit, car, or renovation, with advice framed around time-to-goal math. |
| `cut_spending` | Cut monthly spending | Reduce recurring and discretionary outflow — subscriptions, eating out, lifestyle creep — without changing income. |

Hardcoded list. No add / edit / delete UI needed. Descriptions are user-facing and also sent to Gemini as part of the Advice payload to shape the prompt's framing.

### Data model

No new table. A single `active_goal` column on the `users` table (or equivalent single-row settings store), TEXT, defaulting to `general`. Stores the chosen goal ID from the list above.

### UI

- Goal cards on the Profile page, stacked vertically. Same rounded-border styling as the diagnostics panels.
- Each card shows the title (bold) and description below it.
- The currently active goal is marked with a subtle indicator — Material Symbols `check_circle` icon, no heavy borders or background tints.
- Clicking any card sets it active and saves to the backend.
- `general` is the default for new users and acts as the "no specific goal" state.

### Integration with Advice

- The active goal's title and description are added to the Advice payload only (not Digest or Forecast).
- The Advice prompt instructs the model to frame the recommendation in the context of this goal — e.g. for `big_purchase`, recommendations should include time-to-goal math; for `cut_spending`, recommendations should focus on recurring outflow.
- For `general`, the prompt falls back to the broad savings-rate framing.

### Cache implication

Changing the active goal invalidates the cached Advice summary for the current period. Include the goal ID in the Advice cache key (e.g. `period_key + ":advice:" + goal_id`).

---

## Phase 5 — Auto-generation trigger

When the user lands on Diagnostics and the period looks "clean," generate all three summaries automatically without requiring a button click.

### Trigger conditions (all must hold)

- Uncategorized transactions for the period = 0
- No cached summary exists for the current period (any of the three modes missing)
- A Gemini API key is present in localStorage
- The user hasn't already auto-generated for this period in this session (prevents repeat fires on navigation)

### Behavior

- Fires once on page load after the above checks pass.
- Each card shows a generating state (Material Symbols spinner glyph + "Generating…") instead of the empty hint.
- Results stream in independently as each call returns.
- If a call fails silently in the background, the card shows the error and the user can manually retry — no toast or interruption.

### Why these guardrails

- Zero uncategorized means the data is in a state worth summarizing. Half-categorized periods produce misleading advice.
- The no-cache check prevents burning API calls on every page visit.
- The session flag avoids re-triggering if the user clears a card or manually navigates back.

---

## Prompt design principles

Apply across all three modes:

- **Quantification required.** Every claim names a number from the payload. No vague language.
- **Confidence calibration.** If history is thin (fewer than 3 prior periods), the model says so.
- **Cap recommendations at 1–2.** Three or more reads as a lecture.
- **Never invent numbers.** Existing rule, keep it.
- **No counterparty references.** Even if a category name happens to look like a merchant, talk in category terms.
- **Plain prose, no bullet lists or headings.** Match the current digest format for visual consistency.

---

## Schema and code touch points

Backend:

- `db.py`
  - Extend `ai_summaries` table with a `mode` column; replace unique constraint with `(period_key, mode)`.
  - Add `active_goal` column to `users` (TEXT, default `general`).
- `app.py`
  - `_build_ai_summary_payload` — add history fields from Phase 1; optionally accept a `goal` parameter for the Advice variant.
  - `_ai_summary_period_key` — include mode and (for Advice) goal ID.
  - `/api/diagnostics/ai_summary` — accept `mode` parameter; route to one of three prompt templates. Frontend calls this endpoint three times in parallel.
  - Add `AI_FORECAST_PROMPT` and `AI_ADVICE_PROMPT` constants alongside the existing `AI_SUMMARY_PROMPT`.
  - Hardcode the four goals as a constant (`GOALS = {...}`) — title and description live in Python, no DB rows.
  - New endpoint: `POST /api/profile/active_goal` to update the user's selection.

Frontend:

- `diagnostics.html`
  - Replace single AI panel with three cards.
  - Single Generate button at the top fires three parallel fetches.
  - Auto-generation logic gated on the conditions in Phase 5.
- `profile.html`
  - Add Goals section below the API key section.
  - Render the four hardcoded goal cards; clicking one calls the active-goal endpoint.
- `style.css` — minor additions for the three-card AI layout and the goal card list. Reuse existing rounded-border panel styling.

---

## Rollout

1. Ship Phase 1 (payload expansion) behind the existing single Digest prompt. Verify the new fields don't degrade the current output.
2. Add the three-card layout and parallel generation (Phase 2 + Phase 3). Goals not yet integrated — Advice runs in "general advice" mode for everyone.
3. Add Goals on Profile (Phase 4) and wire the active goal into the Advice payload.
4. Add auto-generation (Phase 5) last, once the manual flow is stable and cache behavior is verified.

---

## Open questions

- How much history is enough? 6 months minimum, 12 months ideal — but token cost grows linearly. Worth measuring.
- Three parallel API calls on every Generate click — acceptable on a per-user Gemini key, but worth confirming Gemini's rate limits at the free tier.
- Should the auto-generation trigger respect a user opt-out (e.g. a "don't auto-generate" toggle on Profile)? Probably yes for v1, default on.
- Are four goals enough coverage? The current set leans toward savers — no "track income growth" or "stabilize irregular income" angle. Easy to add later since they're hardcoded.
