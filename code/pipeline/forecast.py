"""Stage 3: ninety day forecast simulation, deterministic.

From `as_of_date` (the request's `request_date`), project the balance
forward through the user's *complete* financial position (DECISIONS.md #2:
every other recurring, pending, and scheduled obligation already on record,
not only the request being evaluated) -- recurring fixed obligations,
irregular essential rates, confirmed future events (reserved pending debits
and scheduled debits/credits), and income streams -- and compute:

- `amount_safe_to_pay`: the largest lump sum payable at `as_of_date`, before
  optional spending changes, such that the balance never dips below
  `minimum_balance_to_keep` at any point in the forecast window.
- `earliest_date_for_full_payment`: the first date a full `requested_amount`
  lump sum payment passes that same check, or None if none exists within
  the window.

Both reduce to a single min-balance computation because a lump sum paid at
one date shifts every subsequent day's balance down by exactly that amount
(no compounding, no interest) -- see `compute_amount_safe_to_pay` and
`compute_earliest_full_payment_date` for the derivation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .state import UserState, next_month_on_day

FORECAST_DAYS = 90

# Confirmed-event descriptions that textually signal the same underlying bill
# a recurring fixed pattern already projects, not just any confirmed debit in
# the same category and month (DECISIONS.md #13): a generic "Pending
# merchant debit" or "Pending online order charge" could just as easily be a
# separate one-time charge, with no textual evidence it's the same bill --
# only these five say so explicitly.
DUPLICATE_BILL_DESCRIPTIONS = frozenset(
    {
        "Scheduled bill payment retry",
        "Scheduled utility debit",
        "Possible duplicate card charge",
        "Scheduled insurance payment",
        "Scheduled school fee",
    }
)


@dataclass(frozen=True, slots=True)
class CashEvent:
    date: date
    amount: float  # always positive; direction says which way
    direction: str  # "credit" | "debit"
    category: str
    description: str
    flexibility: str | None
    event_id: str | None  # set only for an already-confirmed event, never a projection


def _confirmed_duplicate_bill_months(state: UserState) -> set[tuple[str, int, int]]:
    """(category, year, month) already covered by a confirmed debit whose
    description textually signals it's the same bill a recurring pattern
    would otherwise also project -- see DUPLICATE_BILL_DESCRIPTIONS."""
    return {
        (c.category, c.date.year, c.date.month)
        for c in state.confirmed_future_events
        if c.direction == "debit" and c.description in DUPLICATE_BILL_DESCRIPTIONS
    }


def _project_fixed_occurrences(state: UserState, end: date) -> list[CashEvent]:
    confirmed_months = _confirmed_duplicate_bill_months(state)
    events: list[CashEvent] = []
    for r in state.recurring_fixed:
        occ = next_month_on_day(r.last_date, r.day_of_month)
        while occ < state.as_of_date:
            occ = next_month_on_day(occ, r.day_of_month)
        while occ <= end:
            if (r.category, occ.year, occ.month) not in confirmed_months:
                events.append(CashEvent(occ, r.amount, "debit", r.category, r.description, r.flexibility, None))
            occ = next_month_on_day(occ, r.day_of_month)
    return events


def _project_income_occurrences(state: UserState, end: date, already_confirmed: set[tuple]) -> list[CashEvent]:
    """Only continuing_salary is ever projected here; gig streams are
    deliberately excluded from the hard 90-day safety floor (see
    ARCHITECTURE.md Stage 3's own section for the full re-investigation:
    re-tested by actually removing this exclusion and by crediting only a
    stream's single next occurrence, both against the real sample data, not
    reasoned about in the abstract -- both make the aggregate result worse,
    and the two clearest counterexamples, user_09 and user_10, are both
    gig-only-income users with well-evidenced streams, ruling out "only
    exclude gig when other income exists" too. This project forward
    calendar-correct on the same day of month (a flat cadence_days=30 step
    drifts a full day off the real pattern within a couple of months --
    caught against sample_requests.csv)."""
    events: list[CashEvent] = []
    for inc in state.income_streams:
        if inc.stream != "continuing_salary":
            continue
        occ = inc.next_date
        while occ < state.as_of_date:
            occ = next_month_on_day(occ, inc.next_date.day)
        while occ <= end:
            key = (occ, round(inc.amount, 2), "salary", "credit")
            if key not in already_confirmed:
                events.append(CashEvent(occ, inc.amount, "credit", "salary", inc.description, None, None))
            occ = next_month_on_day(occ, inc.next_date.day)
    return events


def build_forecast_events(state: UserState, forecast_days: int = FORECAST_DAYS) -> list[CashEvent]:
    end = state.as_of_date + timedelta(days=forecast_days)
    confirmed = [
        CashEvent(c.date, c.amount, c.direction, c.category, c.description, c.flexibility, c.event_id)
        for c in state.confirmed_future_events
        if c.date <= end
    ]
    already_confirmed = {(c.date, round(c.amount, 2), c.category, c.direction) for c in confirmed}

    events = confirmed + _project_fixed_occurrences(state, end) + _project_income_occurrences(state, end, already_confirmed)
    events.sort(key=lambda e: e.date)
    return events


def simulate_balance(
    state: UserState, forecast_days: int = FORECAST_DAYS, extra_payments: tuple[tuple[date, float], ...] = ()
) -> list[tuple[date, float]]:
    """(date, balance_at_end_of_day) for every day from as_of_date to
    as_of_date+forecast_days inclusive, starting from current_available_balance.
    `extra_payments` layers a candidate plan's own debits (e.g. an
    installment schedule) on top of the user's complete existing financial
    position -- DECISIONS.md #2's obligation-stacking requirement applied to
    Stage 4's plan safety checks, not just Stage 3's baseline lump sum."""
    events = build_forecast_events(state, forecast_days)
    events_by_date: dict[date, list[CashEvent]] = {}
    for e in events:
        events_by_date.setdefault(e.date, []).append(e)
    extra_by_date: dict[date, float] = {}
    for d, amount in extra_payments:
        extra_by_date[d] = extra_by_date.get(d, 0.0) + amount

    daily_irregular_drain = sum(r.daily_rate for r in state.irregular_rates)

    balance = state.profile.current_available_balance
    timeline: list[tuple[date, float]] = []
    for offset in range(forecast_days + 1):
        d = state.as_of_date + timedelta(days=offset)
        if offset > 0:
            # Today's own irregular spending is already reflected in
            # current_available_balance; the continuous drain models future days only.
            balance -= daily_irregular_drain
        for e in events_by_date.get(d, []):
            balance += e.amount if e.direction == "credit" else -e.amount
        balance -= extra_by_date.get(d, 0.0)
        timeline.append((d, balance))
    return timeline


def compute_amount_safe_to_pay(timeline: list[tuple[date, float]], minimum_balance_to_keep: float, requested_amount: float) -> float:
    """Paying X today shifts every day's balance down by exactly X (a single
    debit at the start of the window, no compounding), so the constraint
    balance[t] - X >= minimum_balance_to_keep for all t reduces to
    X <= min(balance) - minimum_balance_to_keep."""
    min_balance = min(b for _, b in timeline)
    return max(0.0, min(requested_amount, min_balance - minimum_balance_to_keep))


def compute_earliest_full_payment_date(
    timeline: list[tuple[date, float]], minimum_balance_to_keep: float, requested_amount: float
) -> date | None:
    """Paying requested_amount at day tau only affects balance from tau
    onward, so tau is safe iff min(balance[t] for t >= tau) - requested_amount
    >= minimum_balance_to_keep. That suffix-min is non-decreasing in tau, so
    the first tau clearing the threshold is the answer."""
    threshold = minimum_balance_to_keep + requested_amount
    suffix_min = float("inf")
    earliest = None
    for d, balance in reversed(timeline):
        suffix_min = min(suffix_min, balance)
        if suffix_min >= threshold:
            earliest = d
    return earliest


@dataclass(frozen=True, slots=True)
class ForecastResult:
    as_of_date: date
    requested_amount: float
    amount_safe_to_pay: float
    earliest_date_for_full_payment: date | None
    min_balance: float
    timeline: list[tuple[date, float]]


def run_forecast(state: UserState, requested_amount: float, forecast_days: int = FORECAST_DAYS) -> ForecastResult:
    timeline = simulate_balance(state, forecast_days)
    return ForecastResult(
        as_of_date=state.as_of_date,
        requested_amount=requested_amount,
        amount_safe_to_pay=compute_amount_safe_to_pay(timeline, state.profile.minimum_balance_to_keep, requested_amount),
        earliest_date_for_full_payment=compute_earliest_full_payment_date(
            timeline, state.profile.minimum_balance_to_keep, requested_amount
        ),
        min_balance=min(b for _, b in timeline),
        timeline=timeline,
    )
