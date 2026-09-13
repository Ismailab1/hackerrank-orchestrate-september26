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

**Stage 5, explanation generation.** Build `decision_explanation` from a
template populated with the deterministic engine's own numbers, optionally
smoothed by an LLM pass for natural phrasing matching
`sample_requests.csv`'s style. A post generation check re parses every
number and date the LLM output contains and asserts it matches the
computed values; reject and regenerate from the template if not, so prose
can never silently drift from the decision.

**Stage 6, deterministic validation before write.** Two stacked guarantees
with two different failure directions, see `DECISIONS.md` entry 8 for the
full reasoning. The row fails open: every request_id gets exactly one row,
guaranteed by wrapping the per request pipeline in a catch all that falls
back to a schema valid `not_recommended` row rather than crashing or
skipping a row. The recommendation inside the row fails closed: hard
constraints (`0 <= amount_safe_to_pay <= requested_amount`, `payment_plan`
sums and chronology, `partial_payment` exactly two payments summing to
`requested_amount`, installment plans matching a real `payment_option_id`,
`spending_changes_needed` referencing only flexible non protected events
with `stop` and `reduce_to` never on the same event) are enforced with a
fallback ladder (`full_payment` then `partial_payment` then the best safe
matching installment option then `wait` then `not_recommended`) rather
than a reject and drop, so stepping down always lands on something valid.
Output row order and `request_id` set are guaranteed by iterating
`requests.csv` in order and always writing one row per iteration.

**Stage 7, evaluation harness.** Score the pipeline against
`dataset/sample_requests.csv` (25 solved rows) before the full run: exact
match on categorical fields, tolerance based comparison on amounts and
dates, per field accuracy, and an aggregate distribution audit (status and
method mix) to catch systemic bias. Only after this looks solid, run the
full 250 row dataset and write `output.csv`, with a pre write assertion
that the output `request_id` sequence exactly matches the input sequence.

**Token usage tracking.** Wrap every LLM call with input and output token
capture; aggregate into `evaluation/usage_report.md` at the end of the
final full dataset run: providers, models, call counts, total and average
tokens, estimated cost, per the submission requirement.

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

Stages 0 through 4 implemented and run against the real dataset:

- Stage 0/1 (`code/pipeline/loader.py`, `code/pipeline/income.py`,
  `code/pipeline/state.py`): zero errors across all 275 users
  (250 `requests.csv` + 25 `sample_requests.csv`).
- Stage 2 (`code/pipeline/extraction.py`): 231/231 sources (215 messages +
  16 images) extracted successfully via the real Anthropic API; results
  cached at `cache/extracted_facts.json`. Actual spend for the runs so far
  (including one iteration re-run after a prompt fix): ~$0.78 across Claude
  Haiku 4.5 (text) and Claude Sonnet 5 (vision).
- Stage 2 -> Stage 1 amendments (`code/pipeline/amendments.py`): 0/16
  unresolved blank-amount events dataset-wide (that 16 is the true
  population, per DECISIONS.md #7 -- a prior report of this number as
  "0/231" mislabeled it against Stage 2's unrelated source count instead),
  zero errors.
- Stage 3 (`code/pipeline/forecast.py`): three real bugs found and fixed via
  `--validate` against `sample_requests.csv`'s known-correct values (see
  Stage 3's own section above); currently 7/25 `amount_safe_to_pay` within
  2%, 18/25 `earliest_date_for_full_payment` exact, 0.265 mean relative
  amount error. Two open questions logged there.
- Stage 4 (`code/pipeline/plans.py`): one real formatting bug found and
  fixed via `--validate` (see Stage 4's own section above); currently 18/25
  `recommended_payment_method` exact, 17/25 `affordability_status`, 17/25
  `payment_plan` exact. Full 250-row smoke run: zero errors, all five
  methods represented, but a ~40% `not_recommended` rate that traces back to
  Stage 3's open accuracy gaps rather than a new Stage 4 bug -- flagged
  there, not hidden.
- `python code/main.py`, `--sample`, `--user <id>`, `--extract [--limit N]`,
  and `--validate` are all working CLI entry points; see `code/main.py`'s
  module docstring for the full list.
- Setup: `python -m venv .venv`, `.venv/Scripts/pip install -r
  requirements.txt` (Windows; `.venv/bin/pip` elsewhere), copy `.env.example`
  to `.env` and fill in `ANTHROPIC_API_KEY` before using `--extract`.

Not yet built: Stage 5 (explanation), Stage 6 (validation), and the final
`output.csv` / `evaluation/usage_report.md` writers.
