# Buy or Wait? — Architecture

Proposed 2026-09-12, before implementation began. This is the design the
pipeline in this `code/` directory should follow; update it in place as
decisions change during the build.

## Task shape

For each of 250 requests in `dataset/requests.csv`, decide
`amount_safe_to_pay`, `affordability_status`, `recommended_payment_method`,
`payment_plan`, `earliest_date_for_full_payment`, `spending_changes_needed`,
and `decision_explanation`, and write them to `output.csv`. 25 solved
examples are in `dataset/sample_requests.csv` — the style and correctness
guide, not evaluation labels.

Dataset: `financial_profiles.csv` (276 rows, one per user: currency,
balance, minimum balance, priorities, protected/reducible/stoppable
categories, accepted payment methods, max installment months),
`financial_events.csv` (25,343 rows: historical/pending/scheduled/settled/
failed/cancelled/unrealized transactions and non-cash investments, with
`linked_event_id` chains), `request_payment_options.csv` (2 to 4
seller-offered options per request: full payment or installments with fee
and schedule), `exchange_rates.csv` (135 sparse, roughly monthly dated FX
snapshots), `messages.csv` (216 rows, multilingual, some employer/service
provider messages amend or confirm financial facts), `images.csv` (16 rows
linking payroll letters, bills, and receipts to a `related_event_id`), and
`dataset/media/images/*.png`.

## Why a hybrid, determinism first design

The scoring criteria and `AGENTS.md` both explicitly reward determinism and
grounded numbers: hard bounds (`0 <= amount_safe_to_pay <= requested_amount`),
an exact 90 day forecast safety check, installment plans that must exactly
match a supplied `payment_option_id`, spending changes restricted to
flexible events, and a fully specified six level tie break order between
competing safe plans. None of that should be left to LLM judgment, it
should be computed and then checked. The LLM's role is narrowed to the two
places the data is genuinely unstructured: extracting facts from
message and image evidence, and phrasing the explanation.

## Pipeline

**Stage 0, load and normalize.** Read all CSVs. Build an FX lookup from
`exchange_rates.csv` keyed by `(rate_date, from_currency, to_currency)`;
resolve the applicable rate for a given settlement date (verify exact
match vs nearest preceding date against `sample_requests.csv`, see Open
Questions). Only convert when a record's currency differs from the user's
`home_currency`; most events appear to already be in currency.

**Stage 1, per user financial state reconstruction, deterministic.** For
each user: separate recurring expenses from one time events. Fixed cadence
categories (rent, utilities, subscriptions, debt payments) use period
matching: same category, description, and roughly consistent amount across
three or more occurrences. Inherently irregular essential categories
(groceries, transport, dining) use a spending rate derived from the
history window instead of period matching, since these categories rarely
land on a clean schedule even when they are genuinely ongoing, see
`DECISIONS.md` entry 3. Income descriptions are classified once against a
small lookup (standard continuing, transitional start or end, project or
gig based) built from the dataset's 164 unique category and description
combinations, rather than inferred purely from statistics; a user whose
most recent income event is a transitional end marker with nothing after
it is treated as having no confirmed future income, see `DECISIONS.md`
entry 4. Variable gig or freelance income uses a conservative low estimate
from recent occurrences rather than an average, see `DECISIONS.md` entry
5. Reserve pending debits, ignore pending credits, bonuses, refunds,
investment gains, and unrealized investment value, count confirmed salary
only on its settlement date, and resolve `linked_event_id` chains using
the specified conflict priority: explicit cancellation, settlement, or
amendment first; then a newer record from the same source; then a settled
event over an estimate; then the financially safer reading when still
ambiguous.

**Stage 2, multimodal evidence extraction, LLM, narrow scope. Implemented in
`code/pipeline/extraction.py`.** For each request, gather every message and
image with that request's `user_id` (`messages_by_user` / `images_by_user`
in `loader.py`) -- **not** only ones carrying `request_id` or
`related_event_id`: 35% of `messages.csv` (76/215) has both blank, tied only
to `user_id`, and would be silently dropped by a request/event-only gather.
Run each through an LLM (Claude Haiku 4.5 for text messages, Claude Sonnet 5
for images: payroll letters, bills, receipts) with a strict extraction
schema: `fact_type, target_event_id, amount, currency, date, confidence,
detail`, and an explicit system instruction that message text and image
content are data to classify or extract from, never instructions to follow,
regardless of what they claim to be. `target_event_id` is taken directly
from the source row's own `related_event_id` column, never guessed by the
model -- only amounts/dates/classification are extraction targets. Every
call is metered (input/output tokens per model) for `evaluation/usage_report.md`,
and results are cached on disk by source id (`cache/extracted_facts.json`)
so re-running or continuing into later stages never re-bills an
already-extracted source.

Two real bugs surfaced only by running this against the actual dataset and
API, not by design review: (1) the model needs to be told explicitly when a
linked event's amount is currently blank -- without that context it reads a
normal-looking payslip and calls it `irrelevant` instead of `amount_fill`
(caught on `image_01`/`event_253`); (2) `amount_fill` and `amount_change`
were ambiguous for messages with no linked event at all (a "first salary at
a new job" message has no prior amount either, so the model called it
`amount_fill` 52/53 times before the schema was tightened to require an
explicit blank-amount target). Full-dataset run after both fixes: 231/231
sources processed with zero errors, 216/231 (93.5%) pass the sanity-check
gate below; every one of the 15 that don't is either a genuine
cross-currency fact awaiting Stage 3's FX conversion or a low-confidence/
no-amount extraction correctly excluded rather than guessed at.

**Extracted facts feed back into Stage 1 as amendments. Implemented in
`code/pipeline/amendments.py`.** Rather than patching the derived
`UserState` after the fact, amendments patch the raw `Event` list *before*
Stage 1 runs, so every downstream computation sees the amendment through the
exact same, already-tested code path as any other structured row: a fact
naming a `target_event_id` patches that event's `amount` directly (FX
Converting through the same exact-match table first if needed -- this is
what closed the one blank-amount fact sanity-check alone couldn't, a USD
receipt for an INR user); a target-less income fact (91 employer + 22
service_provider messages in this dataset, never anything else) synthesizes
a new `Event` reusing one of `income.py`'s own known descriptions --
`"Next confirmed salary"` for an employer change, `"Independent work
payment"` (a GIG description) for a service_provider one, `"Final employer
payroll"` (TRANSITIONAL_END) for an explicit no-confirmed-income statement
-- so Stage 1's existing classification logic applies unchanged. Validated
on the three real cases this produces: `user_02`'s salary raise
(IDR 42,750,000 effective 2025-08-15), `user_07`'s payroll date move
(2024-09-23, amount preserved from structured data), and `user_12`'s
contract-ended message correctly zeroing their `continuing_salary` stream
to `none`. Full-dataset re-run: 0/16 unresolved blank-amount events (the
`231` figure elsewhere in this doc is Stage 2's total *source* count --
215 messages + 16 images -- a different population; DECISIONS.md #7
independently established 16 as the exact number of blank-amount rows in
`financial_events.csv`, and that's the population this number is out of),
zero errors across all 275 users.

**Stage 3, ninety day forecast simulation, deterministic. Implemented in
`code/pipeline/forecast.py`.** From `request_date`, project the balance
forward through settled recurring expenses, reserved pending debits,
confirmed salary, and Stage 2 amendments. `amount_safe_to_pay` is the
largest amount payable at `request_date`, before optional spending changes,
such that the balance never dips below `minimum_balance_to_keep` at any
forecast point, capped at `requested_amount`. `earliest_date_for_full_payment`
is the first date a
full lump sum payment passes the same check, empty if none within the
forecast window.

Both numbers reduce to a single min-balance computation: a lump sum paid at
one date shifts every subsequent day's balance down by exactly that amount,
so `amount_safe_to_pay = min(balance over the window) - minimum_balance_to_keep`,
and `earliest_date_for_full_payment` is the first date whose *suffix* min
(monotonic, so a single forward scan suffices) clears
`minimum_balance_to_keep + requested_amount`.

Validated against `sample_requests.csv`'s 25 known-correct values
(`python code/main.py --validate`) rather than trusted from design review
alone -- this caught three real bugs no amount of re-reading the code would
have:

1. Continuing-salary income was projected with a flat `cadence_days=30`
   step. The real pattern is "same calendar day every month" (28-31 days),
   so a flat 30-day step drifts a full day off it within a couple of
   projected months -- visible as a consistent `-1 day` error in the
   validation output (2025-01-14 vs the correct 2025-01-15, etc.). Fixed by
   stepping with `next_month_on_day` (the same calendar-correct helper
   `RecurringFixed` already used) instead of a flat timedelta.
2. Gig/variable income, projected at its full detected cadence for the
   entire 90-day window, wildly overstated safety for a driver-platform-payout
   user (computed `amount_safe_to_pay` came out at the full requested amount
   of 266,700 against a true value of 12,700). Excluded gig income from the
   hard safety-floor simulation entirely: ARCHITECTURE.md's own Stage 3 text
   only ever named "confirmed salary" as a category to project, and an
   inherently variable income source has no place backing a hard guarantee
   that the balance never drops below `minimum_balance_to_keep`. This is a
   judgment call, not a fully closed question -- see Open Questions below.
3. `FIXED_CADENCE_CATEGORIES` only covered 11 of the categories that
   actually show a fixed-cadence signature. Re-running the day-of-month-
   spread/CV analysis across *every* category (not the originally assumed
   subset) found `entertainment`, `shopping`, `healthcare`, and
   `family_support` all show the identical signature (day-of-month spread
   ~0, CV well under the 12% tolerance) as rent/utilities -- they were
   simply never tested. Adding them was the single largest accuracy
   improvement of the three (mean relative amount error dropped from 1.14 to
   0.27 across the fixes combined).

Current validation state after all three fixes: 7/25 `amount_safe_to_pay`
within 2%, 18/25 `earliest_date_for_full_payment` exact matches, 0.265 mean
relative amount error, zero errors building state for any of the 275 real
users. Remaining gaps are open, not silently accepted -- see below.

### Stage 3 open questions

1. **Gig income's role in the safety floor is still unresolved.** Excluding
   it entirely undershoots for at least one user (`user_09`, whose only
   income is gig-classified: computed 17-94 depending on the exact fix
   state, against a true value of at least 166.61, since that sample is
   capped at `requested_amount` and doesn't reveal the true ceiling).
   Crediting it at full cadence overshoots by an order of magnitude for
   another (`user_10`: 266,700 computed vs 12,700 true). A "credit exactly
   one occurrence" middle ground was checked by hand for `user_10` and still
   didn't land on 12,700, so the true rule isn't a simple occurrence count.
   `sample_requests.csv` is documented as a style guide, not evaluation
   labels, so this hasn't been chased into curve-fitting the two points --
   flagging it here for whoever picks Stage 4 up next.
2. **Remaining `amount_safe_to_pay` gaps for continuing-salary users**
   (e.g. `request_04`: 10,211,200 computed vs 8,401,800 true, `request_25`:
   1,223,887 vs 1,425,000) are smaller than before the category fix but not
   closed. Candidates not yet checked: whether an existing debt-repayment or
   installment obligation should reserve more than its own recurring line
   (DECISIONS.md #2's "obligation stacking" concern, which Stage 4's
   installment-plan reservation logic may need to feed back into Stage 3
   rather than Stage 3 fully closing on its own), and edge effects in the
   irregular-essential daily-rate window.

**Stage 4, plan candidate generation and ranking, deterministic.
Implemented in `code/pipeline/plans.py`.** Enumerate eligible candidates:
`full_payment`, `partial_payment`, and `installments` (each gated on
appearing in `payment_methods_user_will_consider`, and for installments,
exactly matching a supplied `payment_option_id` and `max_installment_months`
-- `number_of_payments` doubles as the month count here since every real
installment option in this dataset has a ~28-31 day frequency), `wait`
(full payment becomes safe later and `full_payment` is accepted), a
spending-change-adjusted `full_payment` variant (`stop` or `reduce_to` on
flexible, non-protected events the user is willing to adjust -- `reduce_to`
always floors at the event's own `minimum_allowed_amount`, which is
populated for exactly every reducible-flagged row in the dataset -- never
combining a stop and a reduce on the same event), and `not_recommended` as
the fallback when nothing safe qualifies. `amount_safe_to_pay` and
`earliest_date_for_full_payment` are already fully determined by Stage 3,
independent of whichever plan wins here.

Deadline handling is a hard gate here, not just the ranking tie-break the
spec's own ordering implies: every candidate (partial_payment explicitly,
and installments/`wait` by the same reading) is only generated if it
completes by `desired_completion_date`, because `problem_statement.md`
states as a requirement, not a preference, that "the plan must complete the
request by desired_completion_date" -- and empirically, every `wait` row in
`sample_requests.csv` has `earliest_date_for_full_payment` exactly equal to
`desired_completion_date`, never later. This makes ranking tier 1 in the
spec's own order ("completes by desired_completion_date") a no-op among the
candidates generated here, since they've already all been filtered on it.

The spending-change search (`find_spending_changes`) tries combinations of
up to 3 adjustable recurring obligations (largest cash freed first),
re-simulating each combination's actual effect on the min-balance -- not
trusting a naive sum of freed cash, since an event dated after the
balance's trough doesn't help it at all regardless of amount.

Filter to plans that pass the Stage 3 safety check throughout the forecast
(installments layer their own payment schedule on top of the user's
complete existing position via `simulate_balance`'s `extra_payments`
parameter, per DECISIONS.md #2), then rank by the specified order:
completes by `desired_completion_date` (see above -- already satisfied by
construction), then requires no spending changes, then minimizes total paid
(this is where `installments`' financing fee naturally loses to a fee-free
`full_payment`/`partial_payment` when both are safe), then starts earlier,
then uses fewer payments, then lowest `payment_option_id`.

Validated against `sample_requests.csv` (`python code/main.py --validate`,
now checking `affordability_status`, `recommended_payment_method`, and
`payment_plan` alongside Stage 3's two numbers). This caught one real Stage
4 bug: `payment_plan` amounts were formatted with Python's `:g`, which
flips to scientific notation for large amounts and strips the trailing
zero the dataset's own convention keeps (`3246.10`, never `3246.1`) --
fixed with a formatter matching that exact convention (whole numbers get no
decimal point, everything else gets exactly two). After that fix: 18/25
`recommended_payment_method` exact matches, 17/25 `affordability_status`,
17/25 `payment_plan` (up from 12/25 pre-fix). Full 250-row smoke run: zero
errors, all five methods represented (`full_payment` 60, `wait` 49,
`installments` 33, `partial_payment` 9, `not_recommended` 99).

That `not_recommended` rate (99/250, ~40%) is well above DECISIONS.md #8
point 5's "a couple of percent" red-flag threshold. Tracing specific cases
(e.g. `request_17`: an installment option matching the ground truth exactly
misses the safety check by a small margin, `158,268` computed against a
`166,100` requirement) shows this is inherited from Stage 3's own
documented residual accuracy gaps flipping the categorical decision, not a
new Stage 4 bug -- of the 6 sample rows where my status disagrees with
ground truth, every one traces to a Stage 3 amount that was too pessimistic
by roughly the same margin already logged in Stage 3's open questions.
Resolving those (particularly the gig-income question) should pull this
rate down; it is flagged here rather than left to look like a quietly
accepted 40% failure rate.

**Stage 5, explanation generation. Implemented in `code/pipeline/explain.py`.**
`decision_explanation` is a deterministic template, not an LLM narrative
pass -- the optional LLM-smoothing idea in the original design is skipped
outright rather than guarded against, since a template can't drift from the
decision it describes and a same-day deadline isn't the time to add a new
LLM dependency to the row-writing path. The template was reverse-engineered
directly from `sample_requests.csv`: every one of the 25 sample
explanations' "leaves at least X available" / "keeps the X minimum" figure
is *exactly* that user's own `minimum_balance_to_keep`, verified by direct
comparison against `financial_profiles.csv` row by row, in all 25 cases
regardless of payment method -- never a separately computed remaining-
balance number. One template per method (`full_payment`, with and without
spending changes; `installments`; `partial_payment`; `wait`;
`not_recommended`), each grounded in the plan's own computed numbers, with
dates rendered in the sample's "D Month YYYY" long form (distinct from
`payment_plan`'s ISO format) and amounts comma-separated with the same
whole-number-drops-decimals rule as `payment_plan`.

**Stage 6, deterministic validation before write. Implemented in
`code/pipeline/output.py`.** Two stacked guarantees with two different
failure directions, see `DECISIONS.md` entry 8. The row fails open: every
request_id gets exactly one row, via a catch-all around the whole per-
request Stage 1-5 pipeline that falls back to a schema-valid
`not_recommended` row on any exception rather than crashing or skipping a
row. The recommendation inside the row fails closed: `validate_row` checks
the hard invariants (valid enum values, `0 <= amount_safe_to_pay <=
requested_amount`, a `not_recommended` row never carrying a plan,
`partial_payment`'s two payments summing to `requested_amount`, no event
referenced by more than one spending change) and downgrades to the
guaranteed-valid `not_recommended` floor if any fail. This is narrower than
a full fallback ladder by design: Stage 4's `choose_plan` already only
picks from candidates each individually verified safe by
`simulate_balance`, so `validate_row` guards against a logic bug producing
an internally inconsistent row, not against re-deriving eligibility from
scratch. Verified end to end on the real 250-row run: 0/250 rows hit the
fail-open path (caught one real bug this way during development --
`PlanResult` didn't carry the structured spending-change list
`build_explanation` needed, throwing on every `full_payment` row until
fixed). Output row order and `request_id` set are asserted to exactly
match `requests.csv` before writing.

**Stage 7, evaluation harness.** `python code/main.py --validate` scores
Stages 1-4 against `sample_requests.csv`'s 25 solved rows: exact match on
categorical fields, tolerance-based comparison on amounts and dates, and
(from Stage 4 onward) `affordability_status` / `recommended_payment_method`
/ `payment_plan` exact-match counts. Run before `--write-output`, which
writes the full 250-row `dataset/output.csv` with the pre-write assertion
above.

**Token usage tracking.** `python code/main.py --write-output` writes
`evaluation/usage_report.md` alongside `output.csv`. Producing `output.csv`
makes zero new LLM calls -- every extracted fact it consumes comes from
`cache/extracted_facts.json` -- so the report's numbers are the real,
measured totals from the Stage 2 extraction run that populated that cache
(215 Claude Haiku 4.5 calls for messages, 16 Claude Sonnet 5 calls for
images; ~$0.46 total), not an estimate or a placeholder.

## Trade offs

- Determinism first core vs an LLM driven decision layer: chosen
  determinism first because the rules are fully specified and checkable,
  and structural enforcement tends to score better than prompt cleverness.
  Cost is more upfront engineering to get recurrence detection and the
  forecast simulation right.
- Recurrence detection is a heuristic and the single biggest source of
  error risk, since it directly drives `amount_safe_to_pay`. Validate the
  heuristic against `sample_requests.csv` before trusting it on the full
  set.
- LLM extraction runs per request and message or image rather than once
  globally; cache per event extraction since some events are referenced by
  multiple messages, to control cost and latency across 250 requests.

## Open questions, resolved empirically before Stage 0 and 1

1. **FX date matching: exact match only, no fallback.** 140 events across
   the dataset have `currency != user.home_currency` (all five currencies,
   both directions of need). Every one has an exact
   `(settlement_date, event_currency, home_currency)` row in
   `exchange_rates.csv` in the forward direction (139 on the monthly 15th
   snapshots, one on an extra 2025-10-01 snapshot). Zero cases need
   nearest-preceding-date logic or rate inversion. A missing exact match at
   runtime should be treated as a pipeline bug, not interpolated around.
2. **Cross currency conversion is real but narrow.** The 140 events above
   are the only conversion surface; `request_payment_options.csv` has no
   currency column, so payment options and `requested_amount` are always
   already in the user's `home_currency`. Conversion only ever applies to
   financial events.
3. **Recurrence thresholds**, derived from the actual distributions:
   - Fixed cadence categories (rent, utilities, subscriptions,
     debt_repayment, insurance, education, housing, gym): among 1,305
     (user, category, description) groups with 3+ settled occurrences, gap
     is always ~30 to 31 days and day-of-month spread is exactly 0 in
     100% of groups; amount CV maxes at 9.7%. Use: recurring if 3+
     occurrences, day-of-month tolerance +/-1 day, amount within 12% of
     the group mean.
   - Irregular essentials (groceries, transport, dining): gap medians
     range 5 to 87.5 days and day-of-month spread reaches 30, confirming
     no clean schedule exists. No period matching; rate model only, per
     entry 3 in `DECISIONS.md`.
   - Salary: mostly day-of-month spread 0 and CV 0 for continuing salary,
     but a tail up to CV 0.44 and day spread 21 for gig/transitional
     income, exactly the split entries 4 and 5 in `DECISIONS.md` already
     handle via description classification rather than a new threshold.

## Status as of 2026-09-13

All 7 stages implemented, run end to end, and `dataset/output.csv` +
`evaluation/usage_report.md` written from the real dataset:

- Stage 0/1 (`code/pipeline/loader.py`, `code/pipeline/income.py`,
  `code/pipeline/state.py`): zero errors across all 275 users.
- Stage 2 (`code/pipeline/extraction.py`): 231/231 sources extracted via
  the real Anthropic API, cached at `cache/extracted_facts.json`.
- Stage 2 -> Stage 1 amendments (`code/pipeline/amendments.py`): 0/16
  unresolved blank-amount events; a real bug fixed where an unlinked
  `no_confirmed_income` fact could wipe an unrelated `continuing_salary`
  stream (DECISIONS.md #11, `not_recommended` 99->93/250).
- Stage 3 (`code/pipeline/forecast.py`): three bugs found and fixed via
  `--validate`, one (gig income's role in the safety floor) re-examined and
  confirmed correct as excluded via three further real experiments
  (DECISIONS.md #10) rather than left standing on an old assumption.
- Stage 4 (`code/pipeline/plans.py`): one formatting bug fixed via
  `--validate`.
- Stage 5 (`code/pipeline/explain.py`) and Stage 6
  (`code/pipeline/output.py`): built and wired together; caught and fixed
  one real integration bug (`PlanResult` didn't carry the structured
  spending-change data `explain.py` needed) via the fail-open count going
  from 65/250 to 0/250 on the actual full run, not assumed correct.
- Current `--validate` state: 8/25 `amount_safe_to_pay` within 2%, 19/25
  `earliest_date_for_full_payment` exact, 18/25 `affordability_status`,
  19/25 `recommended_payment_method`, 18/25 `payment_plan`, mean relative
  amount error 0.261. Full 250-row `output.csv`: `full_payment` 65,
  `wait` 49, `installments` 34, `partial_payment` 9, `not_recommended` 93
  (37.2%); 0/250 fail-open fallback rows.
- `python code/main.py`, `--sample`, `--user <id>`, `--extract [--limit N]`,
  `--validate`, and `--write-output` are all working CLI entry points; see
  `code/main.py`'s module docstring for the full list.
- Setup: `python -m venv .venv`, `.venv/Scripts/pip install -r
  requirements.txt` (Windows; `.venv/bin/pip` elsewhere), copy `.env.example`
  to `.env` and fill in `ANTHROPIC_API_KEY` before using `--extract`
  (`--write-output` needs no key -- it only reads the existing cache).

Remaining, documented rather than silently accepted: the Stage 3
calibration gap behind the 6 remaining sample misses (`request_03`, `06`,
`08`, `13`, `17`, `21`), all traced to the same root cause and intentionally
not chased further per explicit direction, since `sample_requests.csv` is a
style guide, not evaluation labels.
