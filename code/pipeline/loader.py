"""Stage 0: load and normalize the dataset CSVs.

Exact-date FX matching only: every cross-currency financial_events row in
this dataset has an exact (settlement_date, event_currency, home_currency)
row in exchange_rates.csv in the forward direction. See ARCHITECTURE.md,
resolved open questions 1-2. A missing exact match is treated as a data
problem, not something to interpolate around.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


def parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    y, m, d = value.split("-")
    return date(int(y), int(m), int(d))


def parse_optional_float(value: str) -> float | None:
    value = value.strip()
    return float(value) if value else None


def parse_pipe_set(value: str) -> frozenset[str]:
    value = value.strip()
    return frozenset(value.split("|")) if value else frozenset()


@dataclass(frozen=True, slots=True)
class Profile:
    user_id: str
    home_currency: str
    current_available_balance: float
    minimum_balance_to_keep: float
    financial_priorities: tuple[str, ...]
    expense_categories_to_protect: frozenset[str]
    expense_categories_user_is_willing_to_reduce: frozenset[str]
    expense_categories_user_is_willing_to_stop: frozenset[str]
    payment_methods_user_will_consider: frozenset[str]
    max_installment_months: int | None


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str  # debit | credit | non_cash
    amount: float | None  # None for the 16 rows awaiting image extraction
    currency: str
    event_date: date
    settlement_date: date | None  # only blank for status == unrealized
    status: str  # settled | pending | scheduled | cancelled | failed | unrealized
    linked_event_id: str | None
    flexibility: str  # fixed | reducible | stoppable | reducible_or_stoppable
    minimum_allowed_amount: float | None


@dataclass(frozen=True, slots=True)
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: float
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str


@dataclass(frozen=True, slots=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: float
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: int | None  # blank for single-payment full_payment options
    financing_fee: float
    total_payable_amount: float


class FXTable:
    """Exact-match FX lookup keyed by (rate_date, from_currency, to_currency)."""

    def __init__(self, rows: list[dict[str, str]]):
        self._rates: dict[tuple[str, str, str], float] = {
            (r["rate_date"], r["from_currency"], r["to_currency"]): float(r["rate"])
            for r in rows
        }

    def convert(self, amount: float, from_currency: str, to_currency: str, on_date: date) -> float:
        if from_currency == to_currency:
            return amount
        key = (on_date.isoformat(), from_currency, to_currency)
        rate = self._rates.get(key)
        if rate is None:
            raise KeyError(
                f"No exact FX rate for {from_currency}->{to_currency} on {on_date}. "
                "This dataset is expected to always have one; treat this as a bug."
            )
        return amount * rate


@dataclass(slots=True)
class Dataset:
    profiles: dict[str, Profile]
    events_by_user: dict[str, list[Event]]
    events_by_id: dict[str, Event]
    fx: FXTable
    requests: list[Request]
    sample_requests: list[dict[str, str]]
    payment_options_by_request: dict[str, list[PaymentOption]]
    messages_by_request: dict[str, list[dict[str, str]]]
    messages_by_event: dict[str, list[dict[str, str]]]
    messages_by_user: dict[str, list[dict[str, str]]]
    messages_by_id: dict[str, dict[str, str]]
    images_by_request: dict[str, list[dict[str, str]]]
    images_by_event: dict[str, list[dict[str, str]]]
    images_by_user: dict[str, list[dict[str, str]]]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_profiles(path: Path) -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    for row in _read_csv(path):
        max_months = row["max_installment_months"].strip()
        profiles[row["user_id"]] = Profile(
            user_id=row["user_id"],
            home_currency=row["home_currency"],
            current_available_balance=float(row["current_available_balance"]),
            minimum_balance_to_keep=float(row["minimum_balance_to_keep"]),
            financial_priorities=tuple(row["financial_priorities"].split("|")) if row["financial_priorities"].strip() else (),
            expense_categories_to_protect=parse_pipe_set(row["expense_categories_to_protect"]),
            expense_categories_user_is_willing_to_reduce=parse_pipe_set(row["expense_categories_user_is_willing_to_reduce"]),
            expense_categories_user_is_willing_to_stop=parse_pipe_set(row["expense_categories_user_is_willing_to_stop"]),
            payment_methods_user_will_consider=parse_pipe_set(row["payment_methods_user_will_consider"]),
            max_installment_months=int(max_months) if max_months else None,
        )
    return profiles


def load_events(path: Path) -> dict[str, list[Event]]:
    by_user: dict[str, list[Event]] = {}
    for row in _read_csv(path):
        event = Event(
            event_id=row["event_id"],
            user_id=row["user_id"],
            event_type=row["event_type"],
            description=row["description"],
            category=row["category"],
            direction=row["direction"],
            amount=parse_optional_float(row["amount"]),
            currency=row["currency"],
            event_date=parse_date(row["event_date"]),
            settlement_date=parse_date(row["settlement_date"]),
            status=row["status"],
            linked_event_id=row["linked_event_id"].strip() or None,
            flexibility=row["flexibility"],
            minimum_allowed_amount=parse_optional_float(row["minimum_allowed_amount"]),
        )
        by_user.setdefault(event.user_id, []).append(event)
    return by_user


def load_fx(path: Path) -> FXTable:
    return FXTable(_read_csv(path))


def load_requests(path: Path) -> list[Request]:
    requests = []
    for row in _read_csv(path):
        requests.append(
            Request(
                request_id=row["request_id"],
                user_id=row["user_id"],
                request_date=parse_date(row["request_date"]),
                request_type=row["request_type"],
                requested_amount=float(row["requested_amount"]),
                desired_completion_date=parse_date(row["desired_completion_date"]),
                allows_partial_payment=row["allows_partial_payment"].strip().lower() == "true",
                request_text=row["request_text"],
            )
        )
    return requests


def load_payment_options(path: Path) -> dict[str, list[PaymentOption]]:
    by_request: dict[str, list[PaymentOption]] = {}
    for row in _read_csv(path):
        opt = PaymentOption(
            payment_option_id=row["payment_option_id"],
            request_id=row["request_id"],
            payment_method=row["payment_method"],
            payment_amount=float(row["payment_amount"]),
            number_of_payments=int(row["number_of_payments"]),
            first_payment_date=parse_date(row["first_payment_date"]),
            payment_frequency_days=int(row["payment_frequency_days"]) if row["payment_frequency_days"].strip() else None,
            financing_fee=float(row["financing_fee"]) if row["financing_fee"].strip() else 0.0,
            total_payable_amount=float(row["total_payable_amount"]),
        )
        by_request.setdefault(opt.request_id, []).append(opt)
    return by_request


def load_messages(
    path: Path,
) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    """Returns (by_request, by_event, by_user).

    35% of messages.csv (76/215) have both request_id and related_event_id
    blank -- associated only with user_id (e.g. an employer payroll-change
    notice with no supplied target row). by_user is the complete index: since
    every user has exactly one request (DECISIONS.md #1), gathering evidence
    for a request means every message with that user_id, not just the ones
    that happen to carry request_id or related_event_id.
    """
    by_request: dict[str, list[dict[str, str]]] = {}
    by_event: dict[str, list[dict[str, str]]] = {}
    by_user: dict[str, list[dict[str, str]]] = {}
    for row in _read_csv(path):
        by_user.setdefault(row["user_id"], []).append(row)
        if row["request_id"].strip():
            by_request.setdefault(row["request_id"], []).append(row)
        if row["related_event_id"].strip():
            by_event.setdefault(row["related_event_id"], []).append(row)
    return by_request, by_event, by_user


def load_images(
    path: Path,
) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    """Returns (by_request, by_event, by_user). Every image row currently has
    a request_id, but by_user is built the same way as messages for
    consistency and to stay correct if that ever changes."""
    by_request: dict[str, list[dict[str, str]]] = {}
    by_event: dict[str, list[dict[str, str]]] = {}
    by_user: dict[str, list[dict[str, str]]] = {}
    for row in _read_csv(path):
        by_user.setdefault(row["user_id"], []).append(row)
        if row["request_id"].strip():
            by_request.setdefault(row["request_id"], []).append(row)
        if row["related_event_id"].strip():
            by_event.setdefault(row["related_event_id"], []).append(row)
    return by_request, by_event, by_user


def load_dataset(dataset_dir: Path) -> Dataset:
    messages_by_request, messages_by_event, messages_by_user = load_messages(dataset_dir / "messages.csv")
    images_by_request, images_by_event, images_by_user = load_images(dataset_dir / "images.csv")
    events_by_user = load_events(dataset_dir / "financial_events.csv")
    events_by_id = {e.event_id: e for events in events_by_user.values() for e in events}
    messages_by_id = {m["message_id"]: m for rows in messages_by_user.values() for m in rows}
    return Dataset(
        profiles=load_profiles(dataset_dir / "financial_profiles.csv"),
        events_by_user=events_by_user,
        events_by_id=events_by_id,
        fx=load_fx(dataset_dir / "exchange_rates.csv"),
        requests=load_requests(dataset_dir / "requests.csv"),
        sample_requests=_read_csv(dataset_dir / "sample_requests.csv"),
        payment_options_by_request=load_payment_options(dataset_dir / "request_payment_options.csv"),
        messages_by_request=messages_by_request,
        messages_by_event=messages_by_event,
        messages_by_user=messages_by_user,
        messages_by_id=messages_by_id,
        images_by_request=images_by_request,
        images_by_event=images_by_event,
        images_by_user=images_by_user,
    )
