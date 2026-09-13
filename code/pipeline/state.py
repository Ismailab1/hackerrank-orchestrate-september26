"""Stage 1: per-user financial state reconstruction, deterministic.

Builds, for one user as of one snapshot date, everything a later forecast
stage needs: detected recurring fixed obligations, a spending-rate model for
irregular essentials, an income model, and the already-known future cash
events (reserved pending debits, scheduled debits/credits). See
ARCHITECTURE.md Stage 1 and DECISIONS.md entries 1-6.

Thresholds below come from ARCHITECTURE.md's resolved "Open questions"
section, measured against the real dataset rather than guessed:
fixed-cadence categories have day-of-month spread of exactly 0 and amount CV
under 9.7% across 1,305 real 3+-occurrence groups.
"""

from __future__ import annotations

import calendar
import statistics
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from .income import GIG, PROJECTABLE, TRANSITIONAL_END, classify_income_description
from .loader import Event, FXTable, Profile

FIXED_CADENCE_CATEGORIES = frozenset(
    {
        "rent",
        "utilities",
        "debt_repayment",
        "cloud_storage",
        "streaming",
        "music_subscription",
        "delivery_membership",
        "gym",
        "insurance",
        "education",
        "housing",
        # Added after re-running the day-of-month-spread/CV analysis across
        # every category, not just an assumed subset (caught via
        # sample_requests.csv validation -- see ARCHITECTURE.md Stage 3):
        # these four show the identical fixed-cadence signature (day-of-month
        # spread ~0, CV well under the 12% tolerance) as rent/utilities.
        "entertainment",
        "shopping",
        "healthcare",
        "family_support",
    }
)
IRREGULAR_ESSENTIAL_CATEGORIES = frozenset({"groceries", "transport", "dining"})

RECURRING_MIN_OCCURRENCES = 3
DAY_OF_MONTH_SPREAD_TOLERANCE = 2  # observed max in real data: 0
AMOUNT_CV_TOLERANCE = 0.12  # observed max in real data: 0.097
GAP_MIN_DAYS = 25
GAP_MAX_DAYS = 35
DEFAULT_HISTORY_DAYS = 180  # observed real window: 175-179 days

# Minimum total occurrences pooled across every GIG-classified description for
# a user before a gig income stream is confirmed at all (DECISIONS.md #9):
# pooled, not per-description, since this dataset bills the same freelancer
# under a different description for nearly every engagement.
GIG_POOL_MIN_OCCURRENCES = 3


@dataclass(frozen=True, slots=True)
class RecurringFixed:
    category: str
    description: str
    day_of_month: int
    amount: float  # home currency, most recent occurrence
    flexibility: str
    event_ids: tuple[str, ...]
    last_date: date


@dataclass(frozen=True, slots=True)
class IrregularRate:
    category: str
    daily_rate: float  # home currency per day
    flexibility: str
    window_days: int
    total_amount: float


@dataclass(frozen=True, slots=True)
class ConfirmedFutureEvent:
    """A pending debit or a scheduled debit/credit already on record, not a projection."""

    event_id: str
    date: date
    amount: float
    direction: str
    category: str
    description: str
    flexibility: str
    status: str


@dataclass(frozen=True, slots=True)
class IncomeModel:
    """One confirmed, independently-evidenced income stream.

    A user can have more than one at once (DECISIONS.md #9: a base salary and
    a later commission are both real, concurrent income in this dataset).
    Only constructed for a stream that actually has confirmed future income;
    a stream with no evidence simply does not appear in UserState.income_streams.
    """

    stream: str  # "continuing_salary" | "gig"
    description: str
    classification: str
    amount: float
    cadence_days: int
    next_date: date
    source_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UnresolvedEvent:
    event_id: str
    category: str
    description: str
    reason: str


@dataclass(slots=True)
class UserState:
    user_id: str
    as_of_date: date
    profile: Profile
    recurring_fixed: list[RecurringFixed]
    irregular_rates: list[IrregularRate]
    confirmed_future_events: list[ConfirmedFutureEvent]
    income_streams: list[IncomeModel]
    unresolved_events: list[UnresolvedEvent]
    history_window_start: date
    history_window_end: date


def add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_month_on_day(last_date: date, day_of_month: int) -> date:
    candidate = add_months(last_date, 1)
    day = min(day_of_month, calendar.monthrange(candidate.year, candidate.month)[1])
    return candidate.replace(day=day)


def _normalize_currency(events: list[Event], home_currency: str, fx: FXTable) -> list[Event]:
    normalized = []
    for e in events:
        if e.amount is None or e.currency == home_currency:
            normalized.append(e)
            continue
        convert_date = e.settlement_date or e.event_date
        converted = fx.convert(e.amount, e.currency, home_currency, convert_date)
        normalized.append(replace(e, amount=converted, currency=home_currency))
    return normalized


def _group_by_category_description(events: list[Event]) -> dict[tuple[str, str], list[Event]]:
    groups: dict[tuple[str, str], list[Event]] = {}
    for e in events:
        groups.setdefault((e.category, e.description), []).append(e)
    return groups


def _detect_fixed_recurring(rows: list[Event]) -> RecurringFixed | None:
    """Period-match a (category, description) series: 3+ occurrences, ~monthly
    gap, day-of-month within tolerance, amount within tolerance."""
    usable = sorted(
        (r for r in rows if r.amount is not None and r.settlement_date is not None),
        key=lambda r: r.settlement_date,
    )
    if len(usable) < RECURRING_MIN_OCCURRENCES:
        return None
    dates = [r.settlement_date for r in usable]
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    if not (GAP_MIN_DAYS <= statistics.median(gaps) <= GAP_MAX_DAYS):
        return None
    days_of_month = [d.day for d in dates]
    if max(days_of_month) - min(days_of_month) > DAY_OF_MONTH_SPREAD_TOLERANCE:
        return None
    amounts = [r.amount for r in usable]
    mean_amount = statistics.mean(amounts)
    if mean_amount == 0:
        return None
    if statistics.pstdev(amounts) / mean_amount > AMOUNT_CV_TOLERANCE:
        return None
    last = usable[-1]
    return RecurringFixed(
        category=last.category,
        description=last.description,
        day_of_month=last.settlement_date.day,
        amount=last.amount,
        flexibility=last.flexibility,
        event_ids=tuple(r.event_id for r in usable),
        last_date=last.settlement_date,
    )


def _compute_irregular_rate(rows: list[Event], window_start: date, window_end: date) -> IrregularRate | None:
    """Rate model for inherently irregular essentials (DECISIONS.md #3):
    total settled spend / days covered, no period matching required."""
    usable = [
        r
        for r in rows
        if r.status == "settled"
        and r.amount is not None
        and r.settlement_date is not None
        and window_start <= r.settlement_date <= window_end
    ]
    if not usable:
        return None
    total = sum(r.amount for r in usable)
    days = max((window_end - window_start).days, 1)
    flexibility = Counter(r.flexibility for r in usable).most_common(1)[0][0]
    return IrregularRate(
        category=usable[0].category,
        daily_rate=total / days,
        flexibility=flexibility,
        window_days=days,
        total_amount=total,
    )


def _build_continuing_salary_stream(salary_rows: list[Event], as_of_date: date) -> IncomeModel | None:
    """The base-salary / employment lifecycle stream, evaluated on its own
    evidence only (DECISIONS.md #9). GIG and ONE_TIME rows never enter this
    stream's history at all, so a later commission or bonus payment cannot
    make an established base salary look like it stopped."""
    usable = sorted(
        (
            r
            for r in salary_rows
            if r.amount is not None
            and r.settlement_date is not None
            and r.status in ("settled", "scheduled")
            and classify_income_description(r.category, r.description) in PROJECTABLE | {TRANSITIONAL_END}
        ),
        key=lambda r: r.settlement_date,
    )
    if not usable:
        return None

    last_row = usable[-1]
    cls = classify_income_description(last_row.category, last_row.description)
    if cls == TRANSITIONAL_END:
        # This stream's own most recent record is a job-ending marker with
        # nothing after it: no confirmed future salary (DECISIONS.md #4).
        return None

    same_desc = [r for r in usable if r.description == last_row.description]
    pattern = _detect_fixed_recurring(same_desc)
    if pattern is not None:
        amount = pattern.amount
        day_of_month = pattern.day_of_month
    else:
        # Too few occurrences to period-match (e.g. a job just started);
        # the newest record is still the highest-priority confirmed fact.
        amount = last_row.amount
        day_of_month = last_row.settlement_date.day
    next_date = (
        last_row.settlement_date
        if last_row.status == "scheduled"
        else next_month_on_day(last_row.settlement_date, day_of_month)
    )
    return IncomeModel(
        stream="continuing_salary",
        description=last_row.description,
        classification=cls,
        amount=amount,
        cadence_days=30,
        next_date=next_date,
        source_event_ids=tuple(r.event_id for r in same_desc),
    )


def _build_gig_stream(salary_rows: list[Event], as_of_date: date) -> IncomeModel | None:
    """The variable/freelance stream, pooling every GIG-classified description
    together (DECISIONS.md #9). This dataset regularly bills the same
    freelancer under a different description per engagement, so requiring one
    exact description to repeat 3+ times undercounts real, ongoing gig income."""
    pool = sorted(
        (
            r
            for r in salary_rows
            if r.amount is not None
            and r.settlement_date is not None
            and r.status == "settled"
            and classify_income_description(r.category, r.description) == GIG
        ),
        key=lambda r: r.settlement_date,
    )
    if len(pool) < GIG_POOL_MIN_OCCURRENCES:
        # Not enough pooled evidence to confidently project variable income;
        # conservative default is no assumed future occurrence.
        return None
    dates = [r.settlement_date for r in pool]
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    cadence_days = round(statistics.median(gaps))
    conservative_amount = min(r.amount for r in pool)  # DECISIONS.md #5: minimum observed, not mean
    return IncomeModel(
        stream="gig",
        description=pool[-1].description,
        classification=GIG,
        amount=conservative_amount,
        cadence_days=cadence_days,
        next_date=dates[-1] + timedelta(days=cadence_days),
        source_event_ids=tuple(r.event_id for r in pool),
    )


def _build_income_streams(salary_rows: list[Event], as_of_date: date) -> list[IncomeModel]:
    """Each stream stands on its own evidence; a user can have both a
    continuing salary and a gig stream at once, and neither can clobber the
    other (DECISIONS.md #9)."""
    streams = []
    continuing = _build_continuing_salary_stream(salary_rows, as_of_date)
    if continuing is not None:
        streams.append(continuing)
    gig = _build_gig_stream(salary_rows, as_of_date)
    if gig is not None:
        streams.append(gig)
    return streams


def build_user_state(
    profile: Profile,
    user_events: list[Event],
    as_of_date: date,
    fx: FXTable,
    history_days: int = DEFAULT_HISTORY_DAYS,
) -> UserState:
    events = _normalize_currency(user_events, profile.home_currency, fx)
    history_start = as_of_date - timedelta(days=history_days)

    unresolved = [
        UnresolvedEvent(e.event_id, e.category, e.description, "blank_amount_awaiting_image_extraction")
        for e in events
        if e.amount is None
    ]

    historical_settled = [
        e
        for e in events
        if e.status == "settled"
        and e.amount is not None
        and e.settlement_date is not None
        and history_start <= e.settlement_date <= as_of_date
    ]

    confirmed_future: list[ConfirmedFutureEvent] = []
    for e in events:
        if e.amount is None or e.settlement_date is None or e.settlement_date < as_of_date:
            continue
        # Reserve pending debits; ignore pending credits (bonuses/refunds not yet
        # settled). Scheduled rows are confirmed regardless of direction.
        if (e.status == "pending" and e.direction == "debit") or e.status == "scheduled":
            confirmed_future.append(
                ConfirmedFutureEvent(e.event_id, e.settlement_date, e.amount, e.direction, e.category, e.description, e.flexibility, e.status)
            )
    confirmed_future.sort(key=lambda c: c.date)

    non_income_settled = [e for e in historical_settled if e.category != "salary" and e.event_type != "income"]

    recurring_fixed: list[RecurringFixed] = []
    fixed_groups = _group_by_category_description(
        [e for e in non_income_settled if e.category in FIXED_CADENCE_CATEGORIES]
    )
    for rows in fixed_groups.values():
        pattern = _detect_fixed_recurring(rows)
        if pattern is not None:
            recurring_fixed.append(pattern)

    irregular_rates: list[IrregularRate] = []
    for category in IRREGULAR_ESSENTIAL_CATEGORIES:
        rate = _compute_irregular_rate(
            [e for e in historical_settled if e.category == category], history_start, as_of_date
        )
        if rate is not None:
            irregular_rates.append(rate)

    salary_rows = [e for e in events if e.category == "salary"]
    income_streams = _build_income_streams(salary_rows, as_of_date)

    return UserState(
        user_id=profile.user_id,
        as_of_date=as_of_date,
        profile=profile,
        recurring_fixed=recurring_fixed,
        irregular_rates=irregular_rates,
        confirmed_future_events=confirmed_future,
        income_streams=income_streams,
        unresolved_events=unresolved,
        history_window_start=history_start,
        history_window_end=as_of_date,
    )
