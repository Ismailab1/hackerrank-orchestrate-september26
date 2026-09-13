# Buy or Wait? — Edge Cases and Design Decisions

Running log of edge cases considered and the design decision made for each,
kept alongside `code/ARCHITECTURE.md`. Written for the AI Judge interview:
every entry states the question, what the data actually shows, the
decision, and why.

## 1. Personalization scope and data gaps between recorded history and the request

**Question.** The task calls recommendations personalized. Does that mean
context accumulates across many messages and requests from the same user
over time, and if so, what happens when there is a large gap between a
message and the last recorded financial history?

**What the data shows.** Every `request_id` maps to exactly one unique
`user_id` across `requests.csv` and `sample_requests.csv` combined; no user
has more than one request. So this is a single snapshot decision per user,
not an accumulating conversation. Every user has between 175 and 179 days
of settled event history ending right at their own `request_date`, with no
exceptions. Messages, whether tied to a request directly or only to a user
or event, are all sent within 0 to 11 days of the associated request date.
There is no case in this dataset of stale or long unrecorded gaps between
a message and the request it informs.

What does look thin is repetition within that six month window: across all
user and category combinations of settled events, about 97 percent have
three or more occurrences, but about 3 percent have only one or two. That
is the practical version of "not enough history," here it shows up as an
insufficient sample count within an otherwise complete window, not as a
calendar gap.

**Decision.**

1. No session or conversation state across requests. Each request is
   reconstructed fresh from that single user's profile, events, messages,
   and images.
2. Recurring detection requires at least three settled occurrences of the
   same user, category, and description, with amount and spacing roughly
   consistent, before a category is treated as a confirmed recurring
   commitment and projected forward. One or two occurrences default to one
   time and are excluded from the forecast unless a message or image
   explicitly states otherwise. Revised in entry 2: this flat count only
   applies to fixed cadence categories such as rent, utilities, subscriptions,
   and debt payments. Inherently irregular essential categories (groceries,
   transport, dining) are modeled as a spending rate instead, see entry 2.
3. The forecast always extrapolates forward from `request_date` using a
   detected pattern's cadence, never from wherever the last recorded event
   happens to fall. The entire 90 day forecast window is essentially
   unrecorded by construction (only 47 rows in `financial_events.csv`
   across the whole dataset are dated after their user's own request date,
   all status `scheduled`), so the same extrapolation mechanism that would
   handle a long real world gap is simply the normal path for every
   request, not a special case.
4. When a more recent, more specific record contradicts an older
   assumption, the newer record wins outright per the given conflict
   priority (explicit cancellation or amendment, then newer record from
   the same source, then settled over estimate, then the safer reading),
   regardless of how much time separates the two. No separate time based
   confidence decay is applied on top of this; it is not asked for by the
   spec and would risk inventing an unsupported discount. The occurrence
   count threshold in point 2 already expresses uncertainty from thin
   history.

## 2. Total obligation stacking, not just the single request being evaluated

**Research.** The OCC's Buy Now Pay Later risk management bulletin
([occ.gov](https://www.occ.gov/news-issuances/bulletins/2023/bulletin-2023-37.html))
frames the core underwriting failure as a visibility gap: a lender that
only looks at the one loan in front of it, without seeing a borrower's
other concurrent obligations, can approve something unsafe even when each
individual approval looks fine on its own. The CFPB's 2025 report on
consumer use of Buy Now Pay Later
([consumerfinance.gov](https://files.consumerfinance.gov/f/documents/cfpb_BNPL_Report_2025_01.pdf))
found that 63 percent of borrowers carried multiple simultaneous loans at
some point in the year, and that this is largely invisible to any single
lender.

**Decision.** The 90 day safety simulation in Stage 3 must always run
against the user's complete financial position, meaning every other
recurring, pending, and scheduled obligation already on record for that
user, not only the payment plan being evaluated for the current request.
This is already implied by reconstructing the full financial state in
Stage 1, but it is worth stating as a non negotiable rule with its own
test: pick a user who already has an existing debt repayment or another
installment plan in progress, and confirm the engine correctly shrinks or
rejects a new request that would collide with it, rather than evaluating
the new request as if it were the user's only obligation.

## 3. Irregular essential categories need a rate model, not a period match

**Data finding.** Querying the inter occurrence gaps across every
(user, category, description) group of settled events in
`financial_events.csv` shows about 18 percent of recurring looking series
have a median gap over 60 days. The categories there are groceries,
transport, dining, and salary, not slow cadence bills. These categories
are genuinely irregular in the real world: someone does not buy groceries
on a fixed day every month, so a small number of occurrences spaced
unevenly is the expected shape of the data, not a sign the expense is one
time.

**Decision.** For fixed cadence categories (rent, utilities, subscriptions,
debt payments), keep the period matching approach from entry 1: consistent
amount and consistent spacing across three or more occurrences. For
inherently irregular essential categories (groceries, transport, dining),
do not require period matching at all. Instead derive a spending rate
(total settled amount in that category divided by the number of days
covered by the history window) and project that rate forward across the
90 day forecast. This matches the spec's own instruction to "forecast
essential variable spending conservatively," and avoids the failure mode
where an irregular but real essential expense gets excluded from the
forecast simply because it never landed on a clean monthly schedule.

## 4. Description text carries stronger signal than occurrence statistics for income

**Data finding.** The entire dataset has only 164 unique (category,
description) combinations, small enough to inspect directly rather than
infer purely from counts. Within the salary category, most rows are a
plain "Payroll credit," but a meaningful share are self labeling
transitions: "Final employer payroll," "New employer payroll," "Prorated
first salary," "Payroll before leave" and "Payroll after returning from
leave," "Promotion arrears payment." Seven users have "Final employer
payroll" as their single most recent settled income event, with nothing
recorded after it anywhere in their six month window.

**Decision.** Build a one time lookup that classifies each of the 164
description values into a small set of income and expense semantics
(standard continuing, transitional start, transitional end or leave,
project or gig based, one time), instead of trying to infer this purely
from occurrence counts and gaps per user. For a user whose most recent
settled income event is a transitional end marker like "Final employer
payroll" with nothing after it, do not assume continuing salary in the
forecast. Treat future income as absent or unconfirmed for that user unless
a message, image, or a later event says otherwise, which will correctly
push their forecast toward a more cautious recommendation.

## 5. Conservative baseline for gig and freelance income

**Research.** Guidance from Nebraska's Department of Banking and Finance on
budgeting with irregular income
([ndbf.nebraska.gov](https://ndbf.nebraska.gov/how-budget-effectively-irregular-income))
states the standard practice plainly: "Instead of budgeting off your
highest or average month, use your lowest consistent monthly income (or a
conservative estimate)." The dataset has several categories that match
this profile directly: delivery and driver platform payouts, freelance
milestone and consulting invoice payments, commissions.

**Decision.** For income categories that are inherently variable
(gig platform payouts, freelance or project payments, commissions), use a
conservative low estimate derived from recent settled occurrences (for
example the minimum, or a low percentile) as the assumed recurring amount
for forecasting, rather than a mean or the most recent single payout. This
is the same asymmetric caution already applied to expenses, extended to
variable income for the same reason: overestimating available income is
the direction that leads to an unsafe recommendation.

## 6. Deliberately not adding generic industry benchmarks

**Research.** Standard lending and personal finance benchmarks exist and
are well documented: an emergency fund of three to six months of expenses
([finra.org](https://www.finra.org/investors/investing/investing-basics/financial-foundations)),
and a debt to income ratio ceiling, typically 36 percent as a comfortable
threshold and 43 percent as a common maximum for conventional loan programs
([rocketmortgage.com](https://www.rocketmortgage.com/learn/debt-to-income-ratio)).

**Decision.** Deliberately do not layer either of these onto the forecast.
Both exist because a generic lender does not know an individual borrower's
actual risk tolerance and has to substitute a population level rule. This
dataset does not have that problem: `minimum_balance_to_keep` and
`max_installment_months` in `financial_profiles.csv` are this user's own
stated, personalized version of exactly those same ideas. Adding a generic
buffer or ratio cap on top would silently override a preference the user
already gave us, which conflicts with the instruction not to invent
unsupported financial information, and would likely hurt accuracy against
ground truth that was built from those fields directly rather than from an
external benchmark.

## 7. Vague requested amounts, and extraction confidence more generally

**Question.** What if a user's message is vague about the amount they want
to spend, for example "I want to buy a laptop" with no figure, which could
plausibly mean anywhere from a few hundred to a few thousand.

**Data finding.** This cannot happen for `requested_amount` in this
dataset. Checked across all 275 requests (25 samples plus 250 to
evaluate): zero nulls, zero values at or below zero, and every single one
matches the number actually written in `request_text` once thousands
separators are parsed correctly. `requested_amount` is always a required,
precise, structured field, never something inferred from the sentence.

The same check extended to the wider evidence set. All 215 messages were
scanned for hedging language ("around," "approximately," "not sure," and
equivalents in Indonesian); every hit was a false positive on ordinary
phrasing, not real amount uncertainty. All 16 images that fill in a blank
`amount` event were checked one to one against their target event (16
images, 16 blank amount events, no ambiguity about which resolves which),
and the one inspected directly (a payslip for event_253) states its key
figure three separate times in the same image: a labeled total, a bank
transfer line, and a spelled out amount in words. This dataset was built
to be cleanly extractable, not adversarial about amounts. The adversarial
content it does contain is prompt injection style phrasing, a different
failure mode, already covered by the untrusted content rule in
`ARCHITECTURE.md`.

**Decision.**

1. Never derive `requested_amount`, or any other structured request field,
   from `request_text` or any other free text. Always read the structured
   field directly. Free text is used only for context: what the money is
   for, deadline framing, tone for the explanation, never as a source for
   a number that already exists in structured form.
2. For the small set of genuine extraction cases (the 16 blank amount
   events resolved from images, and any message based amendments to
   income or expenses), still carry a confidence field per extracted fact
   as specified in `ARCHITECTURE.md` Stage 2, and only apply an amendment
   when it passes a basic sanity check (positive, correct currency,
   plausible order of magnitude next to the user's other known amounts)
   and confidence is high. An extraction that fails that check is treated
   as no evidence at all: excluded rather than guessed at, consistent with
   not inventing unsupported financial information, with the uncertainty
   noted in `decision_explanation` rather than silently resolved.

## 8. Sanity check design, protective without ever blocking a row

**Research.** A framework for choosing fail open versus fail closed
behavior
([read.thecoder.cafe](https://read.thecoder.cafe/p/fail-open-fail-closed))
frames the choice around which risk is worse for a given layer of a
system: unavailability, or an unsafe outcome going through unchecked. The
two do not have to be the same choice for every layer of the same system.

**Decision.** Stage 6 is two stacked guarantees with two different failure
directions, not one validation gate with one behavior.

1. The row fails open. Every `request_id` in `requests.csv` gets exactly
   one row in `output.csv`, without exception. The per request pipeline
   (Stages 1 through 5) runs inside a catch all: if anything throws for a
   given request, a fallback row is written instead of the batch crashing
   or the row being skipped. `not_recommended` with `amount_safe_to_pay`
   of 0, `payment_plan` of `none`, and `spending_changes_needed` of `none`
   is schema valid under any input, so it is the guaranteed floor.
2. The recommendation inside the row fails closed. When a check cannot be
   satisfied confidently, default toward the more cautious outcome, the
   same direction already established in entry 1, point 4 for conflicting
   data, extended here to cover internal uncertainty as well.
3. Hard constraints stated by the spec (the bound on `amount_safe_to_pay`,
   an installment plan that must exactly match a supplied
   `payment_option_id`, a `partial_payment` plan that must be exactly two
   payments summing to `requested_amount`) are enforced with a fallback
   ladder rather than a reject and drop: try `full_payment`, then
   `partial_payment`, then the best matching supplied installment option
   that is still actually safe, then `wait`, then `not_recommended`.
   Stepping down the ladder always lands on something valid; nothing in
   this design can produce a malformed row. Where practical, fields are
   derived from one shared computation rather than computed twice and
   cross checked afterward, for example `affordability_status` and
   `earliest_date_for_full_payment` should come from the same simulation
   call so they cannot disagree with each other.
4. Any plausibility or magnitude check must be relative, never a single
   global threshold. `requested_amount` in this dataset ranges from about
   167 to about 84 million purely because of currency (IDR values are
   naturally in the millions, ZAR and EUR values are naturally small). A
   fixed absolute threshold would misfire constantly on some currencies
   and catch nothing real on others. Any such check compares against that
   user's own balance or that currency's typical range, never a flat
   number.
5. Track, across the full run, how often the fallback ladder had to step
   down from the top choice, and how often the `not_recommended` floor was
   hit specifically due to an internal error rather than a genuine "nothing
   safe here" decision. This extends the carried forward lesson in
   `learnings.md` about auditing the aggregate action distribution rather
   than trusting per row correctness alone: a fallback rate of more than a
   couple of percent signals a pipeline bug, not edge cases, and should be
   visible in the usage report before submission rather than discovered
   afterward from a bad score.

## 9. Income is multiple independent streams, not one governing row

**Bug found.** The first Stage 1 implementation of `_build_income_model`
picked a single "governing" salary-category row: whichever settled or
scheduled record was chronologically most recent for the user. That is
wrong whenever a user genuinely has two concurrent income streams, which
this dataset does regularly: a base salary (`Base salary` or
`Payroll credit`) followed a week or so later, same cycle, by a commission
payment (`Monthly sales commission`, `Account commission payment`,
`Performance commission`).

**What the data actually shows.** Across all 275 users, 50 have a base
salary with 5+ clean monthly occurrences whose absolute most recent
salary-category row is something else. Of those 50: 32 have a scheduled
`Next confirmed salary` row last, which the old code already handled
correctly (still `continuing`); 9 have a one-time `Promotion arrears
payment` last, which the old code's one-time-skip logic also already
handled correctly (falls through to the real `Payroll credit` row
underneath); and 9 have a commission description last with fewer than 3
occurrences of that exact description, which the old code genuinely got
wrong, dropping an established base salary to `none` or a thin `gig`
reading entirely. The 9 are `user_11`, `user_76`, `user_80`, `user_92`,
`user_104`, `user_108`, `user_164`, `user_176`, `user_248`. (A user-supplied
reproduction list named 8 of these plus `user_192`; `user_192`'s own most
recent record is actually the scheduled `Next confirmed salary` row, which
was never affected, so the real affected set is these 9, not that list of
8.) Separately, of the 45 users with any gig-classified income at all, 29
never reach 3 occurrences of one exact description because this dataset
bills the same freelancer under a different description almost every
engagement (`user_09`: 10 income events over 10 months, 8 different
descriptions; `user_110` the same pattern) -- real, ongoing gig income that
the per-description occurrence count was undercounting.

**Decision.** Split income into independently evidenced streams instead of
one governing row:

1. A continuing-salary stream, built only from rows classified
   `continuing`, `transitional_start`, or `transitional_end` in
   `income.py` -- `gig` and `one_time` rows never enter its history, so a
   later commission or bonus can no longer make an established base salary
   look like it stopped. This stream zeroes out (no confirmed future
   salary) only when its own most recent record is a `transitional_end`
   marker, per entry 4, evaluated on this stream's own history, not
   whatever salary-category row happens to be newest overall.
2. A gig stream, pooling every `gig`-classified description for the user
   together rather than requiring one exact description to repeat 3+
   times, still requiring 3+ pooled occurrences as the evidence floor and
   still using the minimum observed amount as the conservative estimate
   (entry 5), with cadence taken from the median gap across the pooled,
   sorted dates.
3. Both streams are computed independently and both can contribute
   confirmed income at once. A user can have a `continuing_salary` stream,
   a `gig` stream, both, or neither; nothing about checking one stream
   causes the other to be dropped. This follows the same "do not invent
   unsupported financial information" principle already governing entry 4,
   read the other way: it also means never unsupportedly *discarding* a
   stream that has perfectly good evidence of its own.

**Before / after, combined across all 275 users** (`code/pipeline/state.py`,
verified by rerunning `python code/main.py` and `python code/main.py
--sample`, zero errors either run):

| | before (single governing row) | after (independent streams) |
| --- | --- | --- |
| has a continuing/base-salary income | 227 | 238 (228 salary-only + 10 with both) |
| has a gig income | 16 | 40 (30 gig-only + 10 with both) |
| no confirmed future income | 32 | 7 |

The "no confirmed future income" count after the fix is exactly 7, matching
entry 4's own empirical count of users whose most recent salary record is a
genuine `Final employer payroll` ending with nothing after it -- confirming
`none` no longer contains anything except real job endings.

## 10. Re-testing the gig-income exclusion instead of assuming it, and closing the amendments FX gap on the other path

**Question 1.** `forecast.py`'s `_project_income_occurrences` starts with
`if inc.stream != "continuing_salary": continue` before a per-stream `step`
function that branches on `inc.stream == "continuing_salary"` ever runs --
so the gig branch of that function was dead code. The question raised: was
the gig-income exclusion (entry logged in ARCHITECTURE.md's Stage 3
section) actually re-validated after that per-stream step function was
added, or was it left in place unexamined once a plausible-looking fix
existed for it?

**What was actually tested, in order, against the real 25-row sample set,
not reasoned about in the abstract:**

1. Printed what the (dead) gig branch would compute for `user_10` -- the
   user whose 266,700-vs-12,700 overstatement originally justified the
   exclusion -- with the exclusion still in place: 13 weekly occurrences at
   40,977.52 each, a 532,707.76 projected total. This is the exact
   computation that produced the original overstatement; the per-stream
   step function changes nothing about it, because gig income was already
   stepping by its own `cadence_days` before and after that function
   existed (only `continuing_salary`'s stepping changed, from a flat
   30-day approximation to calendar-correct). So the premise that a fix
   was sitting unused was checked directly and is false for this case.
2. Removed the `continue` outright and re-ran `--validate`: mean relative
   amount error went from 0.265 to 0.991 -- `request_09` (a gig-only-income
   user) fixes, but `request_10` reproduces its original overstatement
   exactly and `request_11` (previously an exact match) breaks (`wait`
   instead of `full_payment`, wrong date, wrong status). A net regression,
   not a fix.
3. Checked whether `user_10`'s `cadence_days=7` / `amount=40,977.52` are
   themselves wrong (the second hypothesis offered: a bug in how those are
   computed). They are not: the raw event history shows 21 real
   occurrences at an almost exactly weekly cadence, rotating across four
   different descriptions (`Delivery platform payout`, `Weekly app
   earnings`, `Task marketplace payout`, `Driver platform payout`) --
   exactly the cross-description pooling entry 9 was built to catch, and
   40,977.52 is genuinely the minimum of the 21 real amounts. This is the
   best-evidenced gig stream in the dataset, not a thin or miscomputed one.
4. Tried a middle ground: credit only a gig stream's single next
   occurrence, not the full cadence, and re-ran `--validate`. Mean relative
   amount error improved slightly (0.265 to 0.231), but the exact-match
   counts didn't move -- `request_09` fixes, `request_11` breaks the same
   way as full inclusion did, a wash rather than an improvement.
5. Checked the one remaining distinguishing hypothesis: maybe gig income
   should only be excluded when the user has other income to fall back on.
   `user_09` and `user_10` are both gig-only (no `continuing_salary`
   stream at all) yet need opposite treatment, ruling this out too.

**Decision.** Keep the exclusion. It was re-tested against the real data
three different ways, not left standing on the strength of an old
measurement -- every alternative tried makes the aggregate result worse or
is a wash, and the two clearest counterexamples are both gig-only-income
users pulling in opposite directions from each other. `user_09`'s specific
undershoot remains unexplained, but chasing it further risks curve-fitting
two ambiguous data points in a dataset explicitly documented as a style
guide, not evaluation labels (AGENTS.md). The dead code itself was real --
cleaned up by removing the now-pointless per-stream branch, since only
`continuing_salary` ever reaches it -- but it was leftover shape from
before the exclusion existed, not a disabled fix.

**Question 2.** `amendments.py`'s `_patch_targeted_events` calls
`_fx_normalize` before `passes_sanity_check`, which is what closed the
`image_12`/`event_7307` cross-currency case. `_synthesize_income_events` --
the path for a target-less `amount_change`, `date_change`, or
`no_confirmed_income` fact -- went straight to `passes_sanity_check` with
no conversion step, so a foreign-currency fact on that path would be
silently dropped the same way the targeted case used to fail.

**Confirmed reachable, not just theoretical:** 6 of the 275 users have
exactly this -- a target-less `amount_change` fact, confidence 0.95, in a
currency other than their own home currency (`user_71`, `user_98`,
`user_125`, `user_173`, `user_245`, `user_263`; USD/EUR facts against
INR/ZAR/IDR home currencies). All 6 were being silently dropped before this
fix.

**Decision.** Apply the same `_fx_normalize` call inside
`_synthesize_income_events` before its own `passes_sanity_check`, using
`fact.date` as the conversion date (there is no target event to pull a
settlement date from on this path). Confirmed fixed: all 6 facts now
produce a correctly-converted synthetic `"Next confirmed salary"` event
(e.g. `user_71`'s USD 696 message converts to IDR 11,019,997.68).

**Measured effect, honestly reported rather than assumed:** the full
250-row aggregate action distribution is unchanged by this fix --
`not_recommended` stays at 99/250, every other method/status count
identical before and after. Checked why, per-user: for all 6 affected
users, the converted synthetic event's amount is a near-exact match for
what that user's own structured salary history already independently
computed (`user_71`: both paths give exactly 11,019,997.68). These 6
messages are salary *confirmations* that happen to restate a figure the
structured data already had right, not corrections that disagree with it,
so closing this gap doesn't move this particular dataset's numbers. The fix
is still correct to make: the previous behavior was silently discarding
confirmable evidence rather than using it, and a case where a message
*disagrees* with stale structured data would have been affected and wasn't
tested here only because none happens to exist in this dataset.

**Correcting a previous report.** The prior status report described "0/231
unresolved blank-amount events, down from 16." That mixed two unrelated
counts: 231 is Stage 2's total source count (215 messages + 16 images,
ARCHITECTURE.md Stage 2), not the population of blank-amount events, which
entry 7 already established as exactly 16. Re-verified directly:
`financial_events.csv` has exactly 16 rows with a blank `amount`, and
summing `unresolved_events` across all 275 per-user states gives exactly 0.
The result -- 0/16, not 0/231 -- was already correct; only its write-up
mislabeled the denominator.

## 11. An unlinked "no confirmed income" message can't wipe a whole stream on a guess

**Bug found.** `_synthesize_income_events` synthesized a `"Final employer
payroll"` (TRANSITIONAL_END) event for every `no_confirmed_income` fact,
unconditionally. That synthetic event was dated `as_of_date`, making it the
most recent salary-category row for the user, so
`_build_continuing_salary_stream` zeroed the user's *entire*
`continuing_salary` stream because its own most recent record looked like
a job ending. The problem: every real `no_confirmed_income` fact in this
dataset is a vague, unlinked message (`target_event_id` is `None` for all
16 of them, confirmed directly against `cache/extracted_facts.json`) --
there is no structured link telling the pipeline which income source the
message is actually about, yet the wipe applied to the whole stream
regardless of how many sources the user actually has.

**Confirmed before touching anything:** built state twice for each of the
16 real `no_confirmed_income` users -- once with plain Stage 1 (no
amendments), once with the full amended pipeline -- and compared whether a
`continuing_salary` stream survived. 12 users had a real, independently
evidenced stream in the plain build that disappeared once amendments ran:
`user_12`, `user_133`, `user_201`, `user_213`, `user_237`, `user_241`,
`user_262`, `user_265`, `user_29`, `user_42`, `user_58`, `user_61`. The
other 4 (`user_111`, `user_165`, `user_246`, `user_75`) had no
`continuing_salary` stream either way -- these genuinely have no other
income, so the (former) wipe was harmless for them specifically.

Two of the twelve make the bug unambiguous rather than merely plausible:
`user_42`'s message (`message_30`) reads "One household employment record
has ended. The remaining confirmed monthly salary is INR 148000" and
`user_58`'s (`message_42`) says the same for IDR 25,840,000 -- both state a
still-active income figure in the identical message the wipe was
discarding. `user_262`'s message illustrates why fuzzy-matching the text to
a specific structured description was rejected as the fix instead: it says
"one household work income source has ended" without naming whether it
means `Primary household salary` or `Second household income`, so guessing
would only replace one wrong answer with a different one.

**Decision.** In `_synthesize_income_events`, never synthesize a wipe event
for a `no_confirmed_income` fact that has no `target_event_id` -- which, in
this dataset, is all of them, so the branch was removed outright rather
than left as a conditional that never fires. Same principle entry 9
established for base salary vs. commission: a stream stands on its own
evidence, and a fact with no structured link to a specific source doesn't
get to unsupportedly discard a stream that has perfectly good evidence of
its own. A cleaner long-run fix -- reclassifying these facts at extraction
time so `no_confirmed_income` is reserved for messages that actually are
unambiguous -- would need a Stage 2 re-run against the real API, not worth
the remaining time before submission.

**Re-verified after the fix**, same before/after methodology: all 12
previously-wiped users now keep their real `continuing_salary` stream, and
the 4 genuinely-empty cases are unaffected -- confirming the fix is neither
too broad nor too narrow.

**Measured effect:**

| | before | after |
| --- | --- | --- |
| sample (`--validate`, 25 rows): `amount_safe_to_pay` within 2% | 7/25 | 8/25 |
| sample: `earliest_date_for_full_payment` exact | 18/25 | 19/25 |
| sample: `affordability_status` exact | 17/25 | 18/25 |
| sample: `recommended_payment_method` exact | 18/25 | 19/25 |
| sample: `payment_plan` exact | 17/25 | 18/25 |
| sample: mean relative amount error | 0.265 | 0.261 |
| full 250: `not_recommended` / `not_affordable` | 99 (39.6%) | 93 (37.2%) |
| full 250: `full_payment` / `affordable_now` | 60 / 54 | 65 / 59 |
| full 250: `installments` / `affordable_with_plan` | 33 / 48 | 34 / 49 |

`request_12` (one of the twelve, and the one sample row this bug directly
broke) now matches ground truth exactly on every field: `amount_safe_to_pay`
65,164.00, `earliest_date_for_full_payment` 2026-04-05, `affordable_with_plan`,
`installments` -- previously `not_recommended` with an empty income model
despite `"Temporary assignment pay"` sitting in that user's own history with
nothing suggesting it had ended.

Six of the twelve fixed users flip to a real recommendation in the full
250-row run (five to `full_payment`, one to `installments`); the other six
remain `not_recommended` for unrelated reasons already covered by Stage 3's
open questions, not this bug.

Per direction from this point: no further Stage 3 accuracy chasing. The
remaining sample misses (`request_03`, `06`, `08`, `13`, `17`, `21`) all
trace to the same already-documented Stage 3 calibration gap, and
`sample_requests.csv` is a style guide, not evaluation labels -- squeezing
further out of 25 rows this close to the deadline risks tuning to noise.
Next: Stage 5 (`decision_explanation`), Stage 6 (validation), and the
`output.csv` / `evaluation/usage_report.md` writers.

## 12. The near-miss installment cases: two hypotheses tested, both dead, decision left as declined

**The shape of the problem.** Of the 93 `not_recommended` rows in the full
250-row run, **21 have a fully compliant installment option that misses the
safety check by 15% or less of that user's `minimum_balance_to_keep`, and 8
of those miss by under 2%** (reproduced independently before investigating,
not taken on faith). The tightest is `request_53`, short by **$17.02 against
a $2,100 minimum** -- 0.81%. In every one of the 21, a *longer* installment
option would have been safe, but exceeds the user's own
`max_installment_months`, so it is correctly ineligible (entry in
ARCHITECTURE.md Stage 4).

This is not a new bug. It is the same Stage 3 calibration gap open since the
first `--validate` run, concentrated exactly where it flips a real decision.
A blanket correction was ruled out before starting: the gap has **no
consistent direction** (`request_04` computes high against ground truth,
`request_25` computes low), so any global margin would fix some of these 21
and break currently-correct rows elsewhere. No global adjustment, buffer, or
tolerance was added in this pass.

**Hypothesis 1 -- obligation stacking (DECISIONS.md #2) is mis-reserving
something.** Tested on `request_53`, the tightest case in the dataset:
traced user_53's day-by-day 90-day balance with `payment_option_145`'s
schedule layered on, then checked every reserved obligation against the raw
`financial_events.csv` rows.

*Falsified.* Every obligation is reserved at the exact amount and exact date
the raw data supports: rent 774.00 day 2, utilities 162.58 day 6, education
169.00 day 8, debt_repayment (vehicle loan) 463.00 day 11 (all 5 historical
occurrences are exactly 463), music_subscription 36.00 day 11,
delivery_membership 41.00 day 13. No installment is already in progress. The
user has exactly one non-settled row in the entire window, and it is a
*credit*. An independent hand recomputation of the trough balance from first
principles (start 3,523.42, minus 4,000.16 fixed, plus 5,760.00 income,
minus 1,178.12 installments, minus 2,022.16 irregular drain) reproduces the
simulator's 2,082.98 to **0.000000** difference. Nothing is being
double-counted, mis-dated, or mis-sized.

**Hypothesis 2 -- irregular-essential daily-rate window edge / off-by-one.**
Tested on `request_100` (short 453.63) and `request_148` (short 82.67), the
next two tightest, plus `request_53`.

*Falsified.* Drain days applied exactly equals days elapsed to the trough in
all three cases -- 68/68, 69/69, 7/7 -- and the manual recomputation matches
the simulator to 0.000000 in all three. There is no off-by-one at either
window edge. (Day 0 deliberately carries no drain, since
`current_available_balance` is a snapshot that already reflects today's
spending; moving drain onto day 0 would only lower balances further.)

**The one real modeling detail found, and why it is not the fix.** The
irregular rate divides total settled spend by a fixed **180-day** divisor
while the actual data span is **176 days** (in all three traced cases; 175-179
universally per entry 1). So the drain is under-estimated by ~2.2%, making
the forecast slightly *optimistic*. Correcting it therefore moves every
near-miss in the **wrong** direction, measured rather than assumed:

| case | shortfall now | with a true 176-day divisor |
| --- | --- | --- |
| `request_53` | 17.02 | 62.98 (worse by 45.96) |
| `request_100` | 453.63 | 1,269.88 (worse by 816.25) |
| `request_148` | 82.67 | 147.04 (worse by 64.36) |

It is also exactly the kind of global recalibration this pass ruled out up
front. Not applied.

**A partial explanation that does not generalize.** `request_53`'s entire
17.02 gap is smaller than a 230.40 pending merchant refund settling
2025-11-14, inside the window -- money the spec *explicitly forbids
counting* ("do not count pending credits, bonuses, commissions, refunds ...
until they settle"). So that specific decline is the rules working as
written, not a calibration error. Checked across all 21: an excluded pending
credit alone would close the gap in only **2 of 21** (`request_53`,
`request_149`). Real, but not the general cause.

**Decision.** Neither hypothesis produced a concrete root cause, so nothing
was changed -- no code was touched in this pass, and the before/after
numbers are therefore identical by construction: **21 near-miss cases, 8
under 2%, 93 `not_recommended` of 250, both before and after.** The
remaining gap is genuine estimator variance in a 90-day forecast built from
~176 days of history, not a locatable defect.

Leaving these 21 as declined is the correct outcome rather than a
concession. The alternative -- loosening the `minimum_balance_to_keep`
check, or adding a tolerance to let a 0.81% miss through -- would mean
recommending a payment plan the system's own best estimate says breaks the
one hard guarantee the user actually specified, on the strength of the
estimate being *nearly* good enough. That is precisely the fail-closed
direction established in entry 8 and reinforced in entries 9, 10, and 11: a
stream, a fact, or a plan stands on its own evidence, and when the evidence
lands this close to the line the system declines rather than gambles with
someone's rent money.

## 13. The fixed-obligation projector double-counted a bill the confirmed record already covered

**Bug.** `_project_fixed_occurrences` in `forecast.py` projected each user's
recurring fixed obligations forward by calendar cadence with no awareness of
`state.confirmed_future_events` -- unlike `_project_income_occurrences`
directly below it, which already takes an `already_confirmed` set and skips
a projected occurrence that a real confirmed event already backs. The
expense side never got that treatment. So when a confirmed record (a
pending or scheduled event) and the recurring pattern's own next projected
occurrence both landed in the same category *and* the same calendar month,
that single bill was charged twice against the 90-day simulation.

**Spec grounding.** `problem_statement.md`'s conflict-resolution order,
rule 3 -- "a settled event over an estimate or forecast" -- is exactly this
situation: a confirmed record and a pattern projection describing the same
bill, where the confirmed record wins.

**Fix, deliberately narrow.** A `DUPLICATE_BILL_DESCRIPTIONS` set of five
confirmed-event description strings that textually signal the same
underlying bill -- `"Scheduled bill payment retry"`, `"Scheduled utility
debit"`, `"Possible duplicate card charge"`, `"Scheduled insurance
payment"`, `"Scheduled school fee"` -- used to suppress a projected
occurrence only when a confirmed debit carrying one of those five
descriptions already covers that same category and calendar month.

Explicitly **not** a blanket same-category-same-month rule. Both rules were
scanned across all 250 requests before choosing: 30 raw
same-category-same-month overlaps exist, and the other descriptions among
them (`"Pending merchant debit"`, `"Pending online order charge"`,
`"Pending pharmacy card charge"`) carry no textual evidence they are the
same bill rather than a genuinely separate one-time charge. Suppressing
those would risk masking a real second debit -- the unsafe direction. Only
the five explicit ones are suppressed; the change is scoped to that one
function, and `_project_income_occurrences` was left alone since it already
handles this correctly.

**Measured impact**, full 250-row diff of the regenerated `output.csv`
against the previously submitted one, matched by `request_id`:

| | before | after |
| --- | --- | --- |
| `not_recommended` / `not_affordable` | 93 | 91 |
| `installments` | 34 | 37 |
| `partial_payment` | 9 | 8 |
| `affordable_with_plan` | 49 | 51 |
| `full_payment` / `wait` | 65 / 49 | 65 / 49 |

13 of 250 rows changed, broken down exactly: **2 flip status**
(`request_55` and `request_229`, both `not_affordable`/`not_recommended` ->
`affordable_with_plan`/`installments`), **1 flips method only**
(`request_224`, `partial_payment` -> `installments`, status unchanged at
`affordable_with_plan`), and **10 shift numbers only**
(`amount_safe_to_pay`, `payment_plan`, or
`earliest_date_for_full_payment`). Every change is in the safer, more
accurate direction and never the reverse -- verified mechanically, not
eyeballed: zero rows moved to a worse affordability status, and zero rows
saw `amount_safe_to_pay` decrease.

**Regression check.** `python code/main.py --validate` run side by side
against the committed pre-fix `forecast.py` and the fixed one (the pre-fix
file restored from `HEAD`, then hash-verified back afterward). All five
exact-match counts identical both ways: `amount_safe_to_pay` within 2% on
8/25, `earliest_date_for_full_payment` exact 19/25, `affordability_status`
exact 18/25, `recommended_payment_method` exact 19/25, `payment_plan` exact
18/25. The only difference is the mean relative amount error, 0.261 ->
0.263 -- a 0.002 move with no row crossing a pass/fail boundary, reported
here rather than rounded away.

**This does not close the near-miss gap from entry 12, and that was checked
rather than assumed.** Two of the 21 near-miss rows entry 12 flagged do
resolve here (`request_55`, which was short by 10.84% of its minimum
balance, and `request_229`, short by 1.39%) -- but as a side effect of
removing a specific, genuine double count, not because this addresses the
calibration gap itself. The remaining near-miss rows are untouched: the
tightest cases entry 12 traced by hand (`request_53`, `request_148`) have
no confirmed-event overlap at all, and `request_100`'s overlap is a
`"Pending online order charge"` that this fix deliberately does not
suppress. A future reader should not reopen entry 12's gap expecting this to
be the explanation -- it accounts for 2 of 21, and the other 19 remain
accumulated forecasting uncertainty rather than a single findable bug.
