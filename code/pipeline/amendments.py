"""Stage 2 -> Stage 1 integration: turn sanity-checked extracted facts into
amendments Stage 1 actually sees.

Grounded in what the real, sanity-checked facts across all 275 users
actually look like (see code/ARCHITECTURE.md Stage 2): every user has at
most one target-less income-lifecycle fact (never a conflicting pair to
merge), and every target-less `amount_change` comes from either an
`employer` message (a base-salary change) or a `service_provider` message
(a gig/invoice confirmation) -- never anything else.

Design: rather than patching the derived UserState after the fact, amend
the raw Event list *before* Stage 1 runs, so every downstream computation
(recurrence detection, income classification, confirmed-future-event
collection) sees the amendment through the exact same, already-tested code
path as any other structured row. Two amendment shapes:

1. A fact with a `target_event_id` patches that event's `amount` directly
   (this is the blank-amount-fill case, and the rare direct-target
   amount_change).
2. A target-less income fact synthesizes a new Event, reusing one of the
   income.py lookup's own known descriptions so Stage 1's existing
   classification logic (continuing vs transitional_end vs gig) applies to
   it unchanged -- `"Next confirmed salary"` for an employer amount/date
   change, `"Independent work payment"` (a GIG description) for a
   service_provider one.

`no_confirmed_income` facts are deliberately never synthesized into
anything (DECISIONS.md #11): every real one in this dataset is a vague,
unlinked message with no target_event_id telling us which income source it
is actually about, and a synthetic TRANSITIONAL_END row would zero the
user's entire continuing_salary stream regardless of which source ended --
wrong whenever more than one exists, which most of the real cases have.
"""

from __future__ import annotations

from dataclasses import replace

from .extraction import ExtractedFact, ExtractionCache, passes_sanity_check
from .income import PROJECTABLE, classify_income_description
from .loader import Event
from .state import UserState, build_user_state

_SYNTHETIC_PREFIX = "amend_"


def _fx_normalize(fact: ExtractedFact, profile, fx, target_event: Event | None) -> ExtractedFact:
    """passes_sanity_check rejects any cross-currency fact outright (it has
    no FX table to act on). A targeted patch does have one -- the target
    event's own settlement date is the correct rate lookup date -- so
    convert here instead of leaving the fact permanently stranded (this is
    what closed the one blank-amount event, image_12/event_7307, a USD
    receipt for an INR user, that sanity-check alone left unresolved)."""
    if fact.currency is None or fact.currency == profile.home_currency:
        return fact
    convert_date = (target_event.settlement_date if target_event else None) or fact.date
    if convert_date is None:
        return fact
    try:
        converted_amount = fx.convert(fact.amount, fact.currency, profile.home_currency, convert_date)
    except KeyError:
        return fact
    return replace(fact, amount=converted_amount, currency=profile.home_currency)


def _patch_targeted_events(events: list[Event], facts: list[ExtractedFact], profile, fx) -> list[Event]:
    events_by_id = {e.event_id: e for e in events}
    patches: dict[str, ExtractedFact] = {}
    for fact in facts:
        if fact.target_event_id is None or fact.amount is None:
            continue
        fact = _fx_normalize(fact, profile, fx, events_by_id.get(fact.target_event_id))
        if not passes_sanity_check(fact, profile):
            continue
        existing = patches.get(fact.target_event_id)
        if existing is None or fact.confidence >= existing.confidence:
            patches[fact.target_event_id] = fact

    if not patches:
        return events
    return [
        replace(e, amount=patches[e.event_id].amount, currency=patches[e.event_id].currency or e.currency)
        if e.event_id in patches
        else e
        for e in events
    ]


def _most_recent_continuing_amount(events: list[Event]) -> float | None:
    candidates = sorted(
        (
            e
            for e in events
            if e.category == "salary"
            and e.amount is not None
            and e.settlement_date is not None
            and classify_income_description(e.category, e.description) in PROJECTABLE
        ),
        key=lambda e: e.settlement_date,
    )
    return candidates[-1].amount if candidates else None


def _synthesize_income_events(
    events: list[Event], facts: list[ExtractedFact], profile, fx, messages_by_id
) -> list[Event]:
    synthetic: list[Event] = []
    for fact in facts:
        if fact.target_event_id is not None or fact.source_type != "message":
            continue
        # No target event to pull a settlement date from here, unlike
        # _patch_targeted_events -- fact.date is the only date available,
        # and it's what _fx_normalize falls back to when given no target_event.
        fact = _fx_normalize(fact, profile, fx, None)
        if not passes_sanity_check(fact, profile):
            continue
        message = messages_by_id.get(fact.source_id)
        if message is None:
            continue
        source = message["source_type"]

        # no_confirmed_income is deliberately never synthesized here (DECISIONS.md
        # #11): this function only ever sees target-less facts (the filter at the
        # top of this loop), and every real no_confirmed_income fact in this
        # dataset is a vague, unlinked message ("one household income source has
        # ended") with no structured link to which income source it's actually
        # about. Synthesizing a TRANSITIONAL_END wipe from it zeroes the user's
        # *entire* continuing_salary stream regardless of which source ended --
        # wrong whenever the user has more than one, which 12 of the 16 real
        # cases do (two of them, user_42 and user_58, even state a "remaining
        # confirmed" salary in the same message that the wipe then discards).
        # Same principle entry 9 established for base salary vs. commission: a
        # stream stands on its own evidence, and something with no link to a
        # specific source doesn't get to unsupportedly discard one that has
        # perfectly good evidence of its own.

        if fact.fact_type not in ("amount_change", "date_change") or fact.date is None:
            continue
        if source not in ("employer", "service_provider"):
            continue

        amount = fact.amount if fact.amount is not None else _most_recent_continuing_amount(events)
        if amount is None:
            continue  # a date_change with nothing to anchor an amount to; don't invent one

        description = "Next confirmed salary" if source == "employer" else "Independent work payment"
        synthetic.append(
            Event(
                event_id=f"{_SYNTHETIC_PREFIX}{fact.source_id}",
                user_id=fact.user_id,
                event_type="income",
                description=description,
                category="salary",
                direction="credit",
                amount=amount,
                currency=fact.currency or profile.home_currency,
                event_date=fact.date,
                settlement_date=fact.date,
                status="scheduled",
                linked_event_id=None,
                flexibility="fixed",
                minimum_allowed_amount=None,
            )
        )
    return events + synthetic


def apply_amendments(events: list[Event], facts: list[ExtractedFact], profile, fx, messages_by_id) -> list[Event]:
    """Returns an amended event list ready to pass into build_user_state.
    `facts` should already be filtered to this user (any order, any
    sanity-check status -- this function re-checks per fact)."""
    events = _patch_targeted_events(events, facts, profile, fx)
    events = _synthesize_income_events(events, facts, profile, fx, messages_by_id)
    return events


def build_amended_user_state(
    profile, user_events: list[Event], as_of_date, fx, cache: ExtractionCache, messages_by_id
) -> UserState:
    """Stage 1 + Stage 2 combined: the reconstructed state Stage 3 should
    actually forecast from."""
    facts = cache.facts_for_user(profile.user_id)
    amended_events = apply_amendments(user_events, facts, profile, fx, messages_by_id)
    return build_user_state(profile, amended_events, as_of_date, fx)
