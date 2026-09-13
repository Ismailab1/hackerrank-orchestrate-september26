"""Stage 5: decision_explanation generation, deterministic template.

Built directly from the deterministic engine's own numbers -- never a
separate LLM narrative pass, so prose can never drift from the decision it
describes (the risk ARCHITECTURE.md's original LLM-smoothing idea existed
to guard against; skipping that pass avoids the risk instead of guarding
against it, which is the better trade with a same-day deadline).

Every sample_requests.csv explanation was checked directly against that
row's own financial_profiles.csv `minimum_balance_to_keep`: the "leaves at
least X available" / "keeps the X minimum" figure is always exactly that
value, in all 25 rows, regardless of payment method -- never a separately
computed remaining-balance number. That's what these templates report.
"""

from __future__ import annotations

from datetime import date

from .loader import Event
from .plans import PlanResult, SpendingChange
from .state import UserState


def _format_money(amount: float) -> str:
    rounded = round(amount, 2)
    if rounded == int(rounded):
        return f"{int(rounded):,}"
    return f"{rounded:,.2f}"


def _format_long_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B')} {d.year}"


def _change_phrase(change: SpendingChange, events_by_id: dict[str, Event], currency: str, capitalize: bool) -> str:
    event = events_by_id.get(change.event_id)
    label = event.description.lower() if event else change.event_id
    if change.kind == "stop":
        verb = "Stop" if capitalize else "stop"
        return f"{verb} the {label}"
    verb = "Reduce" if capitalize else "reduce"
    return f"{verb} the {label} to {currency} {_format_money(change.new_amount)}"


def build_explanation(
    state: UserState,
    request,
    forecast_result,
    plan: PlanResult,
    events_by_id: dict[str, Event],
) -> str:
    profile = state.profile
    ccy = profile.home_currency
    min_balance = _format_money(profile.minimum_balance_to_keep)
    method = plan.recommended_payment_method

    if method == "full_payment":
        amount = _format_money(request.requested_amount)
        if not plan.spending_changes:
            return f"Pay {ccy} {amount} today. This leaves at least {ccy} {min_balance} available over the next 90 days."
        phrases = [
            _change_phrase(c, events_by_id, ccy, capitalize=(i == 0)) for i, c in enumerate(plan.spending_changes)
        ]
        return f"{' and '.join(phrases)}, then pay {ccy} {amount} today. This leaves at least {ccy} {min_balance} available."

    if method == "installments":
        payments = [p.strip().split(":") for p in plan.payment_plan.split("|")]
        first_date = date.fromisoformat(payments[0][0])
        payment_amount = _format_money(float(payments[0][1]))
        return (
            f"Use {len(payments)} installments of {ccy} {payment_amount}, "
            f"starting {_format_long_date(first_date)}. This leaves at least {ccy} {min_balance} available."
        )

    if method == "partial_payment":
        second_date = date.fromisoformat(plan.payment_plan.split("|")[1].split(":")[0])
        remainder = request.requested_amount - forecast_result.amount_safe_to_pay
        return (
            f"Pay {ccy} {_format_money(forecast_result.amount_safe_to_pay)} today and the remaining "
            f"{ccy} {_format_money(remainder)} on {_format_long_date(second_date)}. "
            f"This completes the full request and keeps the {ccy} {min_balance} minimum protected."
        )

    if method == "wait":
        pay_date = date.fromisoformat(plan.payment_plan.split(":")[0])
        return (
            f"Pay {ccy} {_format_money(request.requested_amount)} in full on {_format_long_date(pay_date)}. "
            f"Paying earlier would take the balance below the {ccy} {min_balance} minimum."
        )

    return (
        f"Do not make this payment by {_format_long_date(request.desired_completion_date)}. "
        f"None of the available options keeps the {ccy} {min_balance} minimum protected."
    )
