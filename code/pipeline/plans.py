"""Stage 4: plan candidate generation and ranking, deterministic.

`amount_safe_to_pay` and `earliest_date_for_full_payment` are already fully
determined by Stage 3, independent of whatever plan gets recommended here
(both are explicitly defined "before optional spending changes" and
independent of payment-method preference). Stage 4's only job is to choose
`recommended_payment_method`, `affordability_status`, `payment_plan`, and
`spending_changes_needed`.

Deadline handling: `problem_statement.md` states as a requirement, not just
a ranking tie-break, that "the plan must complete the request by
desired_completion_date." Empirically, all 6 `wait` rows in
sample_requests.csv have `earliest_date_for_full_payment` exactly equal to
`desired_completion_date` (never later) -- so every candidate here is
generated already gated on completing by the deadline; ranking tier 1 in
the spec's own order is then a no-op tie-break among a candidate set that
already all satisfy it (it still matters if that empirical pattern doesn't
hold for every request in the full 250).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from itertools import combinations

from .forecast import ForecastResult, compute_amount_safe_to_pay, simulate_balance
from .loader import Event, PaymentOption, Request
from .state import RecurringFixed, UserState, next_month_on_day


@dataclass(frozen=True, slots=True)
class SpendingChange:
    kind: str  # "stop" | "reduce_to"
    event_id: str
    new_amount: float | None  # only for reduce_to


@dataclass(frozen=True, slots=True)
class PlanCandidate:
    method: str  # "full_payment" | "partial_payment" | "installments" | "wait"
    affordability_status: str
    payments: tuple[tuple[date, float], ...]  # chronological (date, amount)
    spending_changes: tuple[SpendingChange, ...]
    payment_option_id: str | None

    @property
    def total_paid(self) -> float:
        return sum(a for _, a in self.payments)

    @property
    def starts_on(self) -> date:
        return self.payments[0][0]

    @property
    def num_payments(self) -> int:
        return len(self.payments)


@dataclass(frozen=True, slots=True)
class PlanResult:
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    spending_changes_needed: str
    spending_changes: tuple[SpendingChange, ...] = ()


def _fixed_occurrence_dates(r: RecurringFixed, start: date, end: date) -> list[date]:
    occ = next_month_on_day(r.last_date, r.day_of_month)
    while occ < start:
        occ = next_month_on_day(occ, r.day_of_month)
    dates = []
    while occ <= end:
        dates.append(occ)
        occ = next_month_on_day(occ, r.day_of_month)
    return dates


def _eligible_spending_changes(
    state: UserState, events_by_id: dict[str, Event], forecast_days: int
) -> list[dict]:
    """One candidate change per adjustable recurring obligation: 'stop' for a
    stoppable category the user is willing to stop, 'reduce_to' (down to the
    event's own minimum_allowed_amount, always populated for reducible rows)
    for a reducible category the user is willing to reduce. Protected
    categories are never touched."""
    profile = state.profile
    end = state.as_of_date + timedelta(days=forecast_days)
    candidates = []
    for r in state.recurring_fixed:
        if r.category in profile.expense_categories_to_protect:
            continue
        n = len(_fixed_occurrence_dates(r, state.as_of_date, end))
        if n == 0:
            continue
        event_id = r.event_ids[-1]
        can_stop = (
            r.flexibility in ("stoppable", "reducible_or_stoppable")
            and r.category in profile.expense_categories_user_is_willing_to_stop
        )
        can_reduce = (
            r.flexibility in ("reducible", "reducible_or_stoppable")
            and r.category in profile.expense_categories_user_is_willing_to_reduce
        )
        if can_stop:
            candidates.append({"kind": "stop", "event_id": event_id, "new_amount": None, "freed": r.amount * n})
        elif can_reduce:
            last_event = events_by_id.get(event_id)
            floor = last_event.minimum_allowed_amount if last_event and last_event.minimum_allowed_amount else 0.0
            per_occurrence = max(0.0, r.amount - floor)
            if per_occurrence > 0:
                candidates.append(
                    {"kind": "reduce_to", "event_id": event_id, "new_amount": floor, "freed": per_occurrence * n}
                )
    candidates.sort(key=lambda c: -c["freed"])
    return candidates


def _apply_changes_to_state(state: UserState, changes: tuple[dict, ...]) -> UserState:
    by_event = {c["event_id"]: c for c in changes}
    modified = []
    for r in state.recurring_fixed:
        c = by_event.get(r.event_ids[-1])
        if c is None:
            modified.append(r)
        elif c["kind"] == "stop":
            continue
        else:
            modified.append(replace(r, amount=c["new_amount"]))
    return replace(state, recurring_fixed=modified)


def find_spending_changes(
    state: UserState, requested_amount: float, events_by_id: dict[str, Event], forecast_days: int = 90
) -> tuple[SpendingChange, ...] | None:
    """Smallest combination (up to 3, per the output contract) of stop/reduce_to
    changes that makes a full requested_amount payment on as_of_date safe.
    Actually re-simulates each candidate combination rather than trusting the
    naive sum of freed cash, since timing (an event after the balance trough)
    can make a nominally large freed amount not actually help."""
    pool = _eligible_spending_changes(state, events_by_id, forecast_days)
    for size in (1, 2, 3):
        for combo in combinations(pool, size):
            modified_state = _apply_changes_to_state(state, combo)
            timeline = simulate_balance(modified_state, forecast_days)
            safe_amount = compute_amount_safe_to_pay(timeline, state.profile.minimum_balance_to_keep, requested_amount)
            if safe_amount >= requested_amount:
                return tuple(SpendingChange(c["kind"], c["event_id"], c["new_amount"]) for c in combo)
    return None


def _installment_schedule(opt: PaymentOption) -> list[tuple[date, float]]:
    return [
        (opt.first_payment_date + timedelta(days=opt.payment_frequency_days * i), opt.payment_amount)
        for i in range(opt.number_of_payments)
    ]


def _eligible_installment_options(
    state: UserState, request: Request, payment_options: list[PaymentOption]
) -> list[tuple[PaymentOption, tuple[tuple[date, float], ...]]]:
    """Installment options passing every eligibility gate except safety: the
    user accepts installments at all, the option fits inside their own
    `max_installment_months`, and its last payment lands on or before
    `desired_completion_date`. Shared by both installment candidate paths so
    the gates can't drift apart between them."""
    profile = state.profile
    if "installments" not in profile.payment_methods_user_will_consider or profile.max_installment_months is None:
        return []
    eligible = []
    for opt in payment_options:
        if opt.payment_method != "installments":
            continue
        if opt.number_of_payments > profile.max_installment_months:
            continue
        schedule = tuple(_installment_schedule(opt))
        if schedule[-1][0] > request.desired_completion_date:
            continue
        eligible.append((opt, schedule))
    return eligible


def find_installment_with_spending_changes(
    state: UserState,
    eligible_options: list[tuple[PaymentOption, tuple[tuple[date, float], ...]]],
    events_by_id: dict[str, Event],
    forecast_days: int = 90,
) -> tuple[tuple[SpendingChange, ...], PaymentOption, tuple[tuple[date, float], ...]] | None:
    """Smallest combination (up to 3, per the output contract) of stop/reduce_to
    changes that makes some eligible installment schedule safe.

    problem_statement.md defines `affordable_with_plan` as the full requested
    amount being completed "using a partial payment schedule, installments, or
    permitted spending changes" -- it never restricts spending changes to a
    same-day full payment, which is all the original implementation combined
    them with (DECISIONS.md #14). Checks the installment schedule's own safety
    against the modified state, not a lump sum."""
    if not eligible_options:
        return None
    pool = _eligible_spending_changes(state, events_by_id, forecast_days)
    minimum = state.profile.minimum_balance_to_keep
    for size in (1, 2, 3):
        for combo in combinations(pool, size):
            modified_state = _apply_changes_to_state(state, combo)
            for opt, schedule in eligible_options:
                timeline = simulate_balance(modified_state, forecast_days, extra_payments=schedule)
                if min(b for _, b in timeline) >= minimum:
                    changes = tuple(SpendingChange(c["kind"], c["event_id"], c["new_amount"]) for c in combo)
                    return changes, opt, schedule
    return None


def generate_candidates(
    state: UserState,
    request: Request,
    payment_options: list[PaymentOption],
    forecast_result: ForecastResult,
    events_by_id: dict[str, Event],
    forecast_days: int = 90,
) -> list[PlanCandidate]:
    profile = state.profile
    methods = profile.payment_methods_user_will_consider
    req_date = request.request_date
    desired = request.desired_completion_date
    amt = request.requested_amount
    candidates: list[PlanCandidate] = []

    if "full_payment" in methods:
        if forecast_result.amount_safe_to_pay >= amt and req_date <= desired:
            candidates.append(
                PlanCandidate("full_payment", "affordable_now", ((req_date, amt),), (), None)
            )
        else:
            changes = find_spending_changes(state, amt, events_by_id, forecast_days)
            if changes is not None and req_date <= desired:
                candidates.append(
                    PlanCandidate("full_payment", "affordable_with_plan", ((req_date, amt),), changes, None)
                )

    if (
        request.allows_partial_payment
        and "partial_payment" in methods
        and 0 < forecast_result.amount_safe_to_pay < amt
        and forecast_result.earliest_date_for_full_payment is not None
        and forecast_result.earliest_date_for_full_payment <= desired
    ):
        remainder = amt - forecast_result.amount_safe_to_pay
        candidates.append(
            PlanCandidate(
                "partial_payment",
                "affordable_with_plan",
                ((req_date, forecast_result.amount_safe_to_pay), (forecast_result.earliest_date_for_full_payment, remainder)),
                (),
                None,
            )
        )

    eligible_installments = _eligible_installment_options(state, request, payment_options)
    for opt, schedule in eligible_installments:
        timeline = simulate_balance(state, forecast_days, extra_payments=schedule)
        if min(b for _, b in timeline) < profile.minimum_balance_to_keep:
            continue
        candidates.append(
            PlanCandidate("installments", "affordable_with_plan", schedule, (), opt.payment_option_id)
        )

    if (
        "full_payment" in methods
        and forecast_result.earliest_date_for_full_payment is not None
        and forecast_result.earliest_date_for_full_payment > req_date
        and forecast_result.earliest_date_for_full_payment <= desired
    ):
        candidates.append(
            PlanCandidate(
                "wait", "affordable_later", ((forecast_result.earliest_date_for_full_payment, amt),), (), None
            )
        )

    # Installments combined with spending changes (DECISIONS.md #14). Only
    # searched when nothing else is safe: `_rank_key` puts any change-requiring
    # candidate strictly behind every no-change one, so a candidate from here
    # could never outrank an existing one anyway -- gating on an empty candidate
    # list makes that guarantee structural instead of relying on the ranking,
    # and skips a 1-to-3-item combinatorial search that could not change the
    # answer.
    if not candidates:
        found = find_installment_with_spending_changes(state, eligible_installments, events_by_id, forecast_days)
        if found is not None:
            changes, opt, schedule = found
            candidates.append(
                PlanCandidate(
                    "installments", "affordable_with_plan", schedule, changes, opt.payment_option_id
                )
            )

    return candidates


def _rank_key(c: PlanCandidate):
    return (
        1 if c.spending_changes else 0,
        c.total_paid,
        c.starts_on,
        c.num_payments,
        c.payment_option_id or "",
    )


def _format_amount(amount: float) -> str:
    """Matches sample_requests.csv's own convention: a whole number has no
    decimal point (12693000), anything else gets exactly two decimals
    (3246.10, 941.60) -- never :g, which flips to scientific notation for
    large amounts and strips the trailing zero these keep."""
    rounded = round(amount, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}"


def _format_payment_plan(payments: tuple[tuple[date, float], ...]) -> str:
    if not payments:
        return "none"
    return "|".join(f"{d.isoformat()}:{_format_amount(amount)}" for d, amount in payments)


def _format_spending_changes(changes: tuple[SpendingChange, ...]) -> str:
    if not changes:
        return "none"
    parts = []
    for c in changes:
        if c.kind == "stop":
            parts.append(f"stop:{c.event_id}")
        else:
            parts.append(f"reduce_to:{c.event_id}:{_format_amount(c.new_amount)}")
    return "|".join(parts)


def choose_plan(
    state: UserState,
    request: Request,
    payment_options: list[PaymentOption],
    forecast_result: ForecastResult,
    events_by_id: dict[str, Event],
    forecast_days: int = 90,
) -> PlanResult:
    candidates = generate_candidates(state, request, payment_options, forecast_result, events_by_id, forecast_days)
    if not candidates:
        return PlanResult("not_affordable", "not_recommended", "none", "none")
    best = min(candidates, key=_rank_key)
    return PlanResult(
        affordability_status=best.affordability_status,
        recommended_payment_method=best.method,
        payment_plan=_format_payment_plan(best.payments),
        spending_changes_needed=_format_spending_changes(best.spending_changes),
        spending_changes=best.spending_changes,
    )
