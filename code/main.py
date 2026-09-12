"""Buy or Wait? -- entry point.

Currently wires up Stage 0 (load/normalize) and Stage 1 (per-user financial
state reconstruction) from ARCHITECTURE.md. Stages 2-6 (multimodal
extraction, forecast, plan ranking, explanation, validation) are not yet
implemented, so this does not write dataset/output.csv yet.

Run from the repo root:

    python code/main.py                 # build state for every request, print a summary
    python code/main.py --user user_01  # print the full reconstructed state for one user
    python code/main.py --sample        # use dataset/sample_requests.csv instead of requests.csv
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from pipeline.loader import Dataset, load_dataset
from pipeline.state import UserState, build_user_state


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def build_all_states(dataset: Dataset, request_user_ids: list[tuple[str, str]]) -> tuple[dict[str, UserState], list[tuple[str, str, Exception]]]:
    """request_user_ids: list of (request_id, user_id). Returns states keyed by
    request_id, plus a list of (request_id, user_id, error) for failures."""
    states: dict[str, UserState] = {}
    errors: list[tuple[str, str, Exception]] = []
    request_dates = {r.request_id: r.request_date for r in dataset.requests}
    for request_id, user_id in request_user_ids:
        as_of_date = request_dates.get(request_id)
        if as_of_date is None:
            # sample_requests.csv rows aren't in dataset.requests; look it up there instead.
            continue
        profile = dataset.profiles.get(user_id)
        if profile is None:
            errors.append((request_id, user_id, ValueError("no financial_profiles.csv row for user")))
            continue
        try:
            states[request_id] = build_user_state(
                profile, dataset.events_by_user.get(user_id, []), as_of_date, dataset.fx
            )
        except Exception as exc:  # noqa: BLE001 -- summarized for the report, not swallowed silently
            errors.append((request_id, user_id, exc))
    return states, errors


def print_summary(states: dict[str, UserState], errors: list[tuple[str, str, Exception]]) -> None:
    print(f"Built state for {len(states)} requests, {len(errors)} errors.")
    if errors:
        print("Errors:")
        for request_id, user_id, exc in errors[:20]:
            print(f"  {request_id} ({user_id}): {exc!r}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")

    fixed_counts = Counter()
    irregular_counts = Counter()
    income_status_counts = Counter()
    unresolved_total = 0
    confirmed_future_total = 0
    for state in states.values():
        fixed_counts[len(state.recurring_fixed)] += 1
        irregular_counts[len(state.irregular_rates)] += 1
        income_status_counts[state.income.status] += 1
        unresolved_total += len(state.unresolved_events)
        confirmed_future_total += len(state.confirmed_future_events)

    print()
    print("Recurring fixed obligations detected per user (count -> num users):", dict(sorted(fixed_counts.items())))
    print("Irregular essential rates detected per user (count -> num users):", dict(sorted(irregular_counts.items())))
    print("Income model status distribution:", dict(income_status_counts))
    print(f"Total unresolved (blank-amount) events across all users: {unresolved_total}")
    print(f"Total confirmed future events (reserved pending debits + scheduled) across all users: {confirmed_future_total}")


def print_user_detail(state: UserState) -> None:
    p = state.profile
    print(f"user_id={state.user_id}  as_of_date={state.as_of_date}  home_currency={p.home_currency}")
    print(f"  current_available_balance={p.current_available_balance:,.2f}  minimum_balance_to_keep={p.minimum_balance_to_keep:,.2f}")
    print(f"  priorities={list(p.financial_priorities)}  payment_methods={sorted(p.payment_methods_user_will_consider)}  max_installment_months={p.max_installment_months}")
    print(f"  protect={sorted(p.expense_categories_to_protect)}  reduce={sorted(p.expense_categories_user_is_willing_to_reduce)}  stop={sorted(p.expense_categories_user_is_willing_to_stop)}")
    print(f"  history window: {state.history_window_start} .. {state.history_window_end}")

    print(f"  Recurring fixed obligations ({len(state.recurring_fixed)}):")
    for r in sorted(state.recurring_fixed, key=lambda r: r.category):
        print(f"    {r.category:20s} {r.description:35s} day={r.day_of_month:2d}  amount={r.amount:,.2f}  flex={r.flexibility}  n={len(r.event_ids)}")

    print(f"  Irregular essential rates ({len(state.irregular_rates)}):")
    for r in state.irregular_rates:
        print(f"    {r.category:20s} daily_rate={r.daily_rate:,.2f}  flex={r.flexibility}  window_days={r.window_days}  total={r.total_amount:,.2f}")

    inc = state.income
    print(f"  Income: status={inc.status}  class={inc.classification}  description={inc.description}")
    if inc.status != "none":
        print(f"    amount={inc.amount:,.2f}  cadence_days={inc.cadence_days}  next_date={inc.next_date}")

    print(f"  Confirmed future events ({len(state.confirmed_future_events)}):")
    for c in state.confirmed_future_events[:15]:
        print(f"    {c.date} {c.status:10s} {c.direction:6s} {c.category:15s} {c.amount:>12,.2f}  {c.description}")
    if len(state.confirmed_future_events) > 15:
        print(f"    ... and {len(state.confirmed_future_events) - 15} more")

    if state.unresolved_events:
        print(f"  Unresolved events awaiting Stage 2 image/message extraction ({len(state.unresolved_events)}):")
        for u in state.unresolved_events:
            print(f"    {u.event_id} {u.category}/{u.description}: {u.reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-dir", type=Path, default=repo_root() / "dataset")
    parser.add_argument("--user", help="print full reconstructed state for one user_id instead of the summary")
    parser.add_argument("--sample", action="store_true", help="use dataset/sample_requests.csv instead of dataset/requests.csv")
    args = parser.parse_args(argv)

    dataset = load_dataset(args.dataset_dir)

    if args.sample:
        request_user_ids = [(r["request_id"], r["user_id"]) for r in dataset.sample_requests]
        request_dates = {r["request_id"]: r["request_date"] for r in dataset.sample_requests}
        # sample_requests.csv rows aren't in dataset.requests, so build states directly here.
        from pipeline.loader import parse_date

        states: dict[str, UserState] = {}
        errors: list[tuple[str, str, Exception]] = []
        for request_id, user_id in request_user_ids:
            profile = dataset.profiles.get(user_id)
            if profile is None:
                errors.append((request_id, user_id, ValueError("no financial_profiles.csv row for user")))
                continue
            try:
                states[request_id] = build_user_state(
                    profile,
                    dataset.events_by_user.get(user_id, []),
                    parse_date(request_dates[request_id]),
                    dataset.fx,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append((request_id, user_id, exc))
    else:
        request_user_ids = [(r.request_id, r.user_id) for r in dataset.requests]
        states, errors = build_all_states(dataset, request_user_ids)

    if args.user:
        matches = [rid for rid, uid in request_user_ids if uid == args.user]
        if not matches:
            print(f"No request found for user_id={args.user}", file=sys.stderr)
            return 1
        for request_id in matches:
            if request_id in states:
                print_user_detail(states[request_id])
            else:
                print(f"{request_id} ({args.user}) failed to build state; see errors above.")
        return 0

    print_summary(states, errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
