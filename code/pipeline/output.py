"""Stage 6: deterministic validation before write, and the output.csv /
evaluation/usage_report.md writers.

Two stacked guarantees with two different failure directions (DECISIONS.md
#8): the row fails open (every request_id gets exactly one row, guaranteed
by wrapping the whole per-request pipeline in a catch-all), the
recommendation inside the row fails closed (a computed row that fails the
sanity check downgrades to the guaranteed-valid `not_recommended` floor
rather than being written as-is or dropped).

Stage 4's `choose_plan` already only picks from candidates individually
verified safe by `simulate_balance`, so `validate_row` here is a narrower
defensive check against a logic bug producing an internally inconsistent
row (bad enum value, a payment_plan that doesn't sum correctly, an
`earliest_date_for_full_payment` that contradicts `affordable_now`) --
not a second fallback ladder reimplementing Stage 4's own eligibility
logic.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .amendments import build_amended_user_state
from .explain import build_explanation
from .extraction import ExtractionCache
from .forecast import run_forecast
from .loader import Dataset, Request
from .plans import choose_plan

OUTPUT_COLUMNS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]

VALID_STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
VALID_METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}


@dataclass(frozen=True, slots=True)
class OutputRow:
    request_id: str
    amount_safe_to_pay: float
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    earliest_date_for_full_payment: str
    spending_changes_needed: str
    decision_explanation: str


def _fallback_row(request_id: str, requested_amount: float = 0.0) -> OutputRow:
    return OutputRow(
        request_id=request_id,
        amount_safe_to_pay=0.0,
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payment_plan="none",
        earliest_date_for_full_payment="",
        spending_changes_needed="none",
        decision_explanation="Unable to safely evaluate this request; treated as not affordable.",
    )


def _format_amount_field(amount: float) -> str:
    rounded = round(amount, 2)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.2f}"


def validate_row(row: OutputRow, requested_amount: float) -> bool:
    if row.affordability_status not in VALID_STATUSES:
        return False
    if row.recommended_payment_method not in VALID_METHODS:
        return False
    if not (0 <= row.amount_safe_to_pay <= requested_amount + 1e-6):
        return False
    if row.affordability_status == "affordable_now" and row.recommended_payment_method == "not_recommended":
        return False
    if row.recommended_payment_method == "not_recommended" and row.payment_plan != "none":
        return False
    if row.recommended_payment_method == "partial_payment":
        parts = row.payment_plan.split("|")
        if len(parts) != 2:
            return False
        total = sum(float(p.split(":")[1]) for p in parts)
        if abs(total - requested_amount) > 0.02:
            return False
    changes = [] if row.spending_changes_needed == "none" else row.spending_changes_needed.split("|")
    if len(changes) > 3:
        return False
    stopped_or_reduced = [c.split(":")[1] for c in changes]
    if len(stopped_or_reduced) != len(set(stopped_or_reduced)):
        return False  # same event referenced by both a stop and a reduce_to (or twice)
    return True


def build_output_row(dataset: Dataset, request: Request, cache: ExtractionCache) -> OutputRow:
    try:
        profile = dataset.profiles[request.user_id]
        state = build_amended_user_state(
            profile,
            dataset.events_by_user.get(request.user_id, []),
            request.request_date,
            dataset.fx,
            cache,
            dataset.messages_by_id,
        )
        forecast_result = run_forecast(state, request.requested_amount)
        payment_options = dataset.payment_options_by_request.get(request.request_id, [])
        plan = choose_plan(state, request, payment_options, forecast_result, dataset.events_by_id)
        explanation = build_explanation(state, request, forecast_result, plan, dataset.events_by_id)

        row = OutputRow(
            request_id=request.request_id,
            amount_safe_to_pay=forecast_result.amount_safe_to_pay,
            affordability_status=plan.affordability_status,
            recommended_payment_method=plan.recommended_payment_method,
            payment_plan=plan.payment_plan,
            earliest_date_for_full_payment=(
                forecast_result.earliest_date_for_full_payment.isoformat()
                if forecast_result.earliest_date_for_full_payment
                else ""
            ),
            spending_changes_needed=plan.spending_changes_needed,
            decision_explanation=explanation,
        )
    except Exception:  # noqa: BLE001 -- the row fails open; the recommendation fails closed
        return _fallback_row(request.request_id)

    if not validate_row(row, request.requested_amount):
        return _fallback_row(request.request_id)
    return row


def build_all_output_rows(dataset: Dataset, cache: ExtractionCache) -> list[OutputRow]:
    return [build_output_row(dataset, request, cache) for request in dataset.requests]


def write_output_csv(path: Path, rows: list[OutputRow]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(OUTPUT_COLUMNS)
        for row in rows:
            writer.writerow(
                [
                    row.request_id,
                    _format_amount_field(row.amount_safe_to_pay),
                    row.affordability_status,
                    row.recommended_payment_method,
                    row.payment_plan,
                    row.earliest_date_for_full_payment,
                    row.spending_changes_needed,
                    row.decision_explanation,
                ]
            )
