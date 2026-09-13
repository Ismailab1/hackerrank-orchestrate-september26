"""Buy or Wait? -- entry point.

Currently wires up Stage 0 (load/normalize), Stage 1 (per-user financial
state reconstruction), and Stage 2 (message/image extraction) from
ARCHITECTURE.md. Stages 3-6 (forecast, plan ranking, explanation,
validation) are not yet implemented, so this does not write
dataset/output.csv yet.

Run from the repo root (activate .venv first, or call .venv/Scripts/python.exe / .venv/bin/python directly):

    python code/main.py                     # build state for every request, print a summary
    python code/main.py --user user_01      # print the full reconstructed state for one user
    python code/main.py --sample            # use dataset/sample_requests.csv instead of requests.csv

    python code/main.py --extract --limit 5 # Stage 2 smoke test: a few real API calls (needs .env)
    python code/main.py --extract           # Stage 2 for real: every message and image in the dataset

    python code/main.py --validate          # Stage 3 vs sample_requests.csv's 25 known-correct answers
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from pipeline.amendments import build_amended_user_state
from pipeline.extraction import ExtractionCache
from pipeline.loader import Dataset, load_dataset
from pipeline.state import UserState


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_extraction_cache() -> ExtractionCache:
    return ExtractionCache(repo_root() / "cache" / "extracted_facts.json")


def build_state_for_user(dataset: Dataset, profile, as_of_date, cache: ExtractionCache) -> UserState:
    """Stage 1 + Stage 2 combined: cache.facts_for_user() is simply empty for
    a user with no extracted evidence (or if cache/extracted_facts.json
    doesn't exist yet), which degrades this to plain Stage 1 automatically."""
    user_events = dataset.events_by_user.get(profile.user_id, [])
    return build_amended_user_state(profile, user_events, as_of_date, dataset.fx, cache, dataset.messages_by_id)


def build_all_states(
    dataset: Dataset, request_user_ids: list[tuple[str, str]], cache: ExtractionCache
) -> tuple[dict[str, UserState], list[tuple[str, str, Exception]]]:
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
            states[request_id] = build_state_for_user(dataset, profile, as_of_date, cache)
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
    income_combo_counts = Counter()
    unresolved_total = 0
    confirmed_future_total = 0
    for state in states.values():
        fixed_counts[len(state.recurring_fixed)] += 1
        irregular_counts[len(state.irregular_rates)] += 1
        streams = {m.stream for m in state.income_streams}
        if not streams:
            income_combo_counts["none"] += 1
        else:
            income_combo_counts["+".join(sorted(streams))] += 1
        unresolved_total += len(state.unresolved_events)
        confirmed_future_total += len(state.confirmed_future_events)

    print()
    print("Recurring fixed obligations detected per user (count -> num users):", dict(sorted(fixed_counts.items())))
    print("Irregular essential rates detected per user (count -> num users):", dict(sorted(irregular_counts.items())))
    print("Income stream combination distribution:", dict(income_combo_counts))
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

    print(f"  Income streams ({len(state.income_streams)}):")
    if not state.income_streams:
        print("    none")
    for inc in state.income_streams:
        print(f"    {inc.stream:18s} class={inc.classification:12s} description={inc.description}")
        print(f"      amount={inc.amount:,.2f}  cadence_days={inc.cadence_days}  next_date={inc.next_date}")

    print(f"  Confirmed future events ({len(state.confirmed_future_events)}):")
    for c in state.confirmed_future_events[:15]:
        print(f"    {c.date} {c.status:10s} {c.direction:6s} {c.category:15s} {c.amount:>12,.2f}  {c.description}")
    if len(state.confirmed_future_events) > 15:
        print(f"    ... and {len(state.confirmed_future_events) - 15} more")

    if state.unresolved_events:
        print(f"  Unresolved events awaiting Stage 2 image/message extraction ({len(state.unresolved_events)}):")
        for u in state.unresolved_events:
            print(f"    {u.event_id} {u.category}/{u.description}: {u.reason}")


def run_extraction(dataset: Dataset, dataset_dir: Path, user_ids: list[str] | None, limit: int | None) -> int:
    """Stage 2: run message/image extraction for real against the Anthropic API.

    Requires ANTHROPIC_API_KEY (see .env.example). Every call is metered via
    UsageTracker so the totals below map directly onto evaluation/usage_report.md.
    """
    import os

    from dotenv import load_dotenv

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env in the repo root and fill in your key.",
            file=sys.stderr,
        )
        return 1

    import anthropic

    from pipeline.extraction import (
        MIN_CONFIDENCE,
        UsageTracker,
        extract_facts_from_image,
        extract_facts_from_message,
        gather_sources_for_user,
        passes_sanity_check,
    )

    client = anthropic.Anthropic()
    tracker = UsageTracker()
    images_dir = dataset_dir / "media" / "images"
    cache = load_extraction_cache()
    cache_hits = 0

    target_users = user_ids or sorted(dataset.profiles.keys())
    seen_message_ids: set[str] = set()
    seen_image_ids: set[str] = set()
    all_facts = []

    for uid in target_users:
        if limit is not None and len(all_facts) >= limit:
            break
        messages, images = gather_sources_for_user(dataset, uid)
        for m in messages:
            if limit is not None and len(all_facts) >= limit:
                break
            if m["message_id"] in seen_message_ids:
                continue
            seen_message_ids.add(m["message_id"])
            cached = cache.get(m["message_id"])
            if cached is not None:
                cache_hits += 1
                all_facts.append(cached)
                continue
            target_event = dataset.events_by_id.get(m["related_event_id"].strip())
            fact = extract_facts_from_message(client, m, tracker, target_event)
            cache.set(fact)
            all_facts.append(fact)
        for img in images:
            if limit is not None and len(all_facts) >= limit:
                break
            if img["image_id"] in seen_image_ids:
                continue
            seen_image_ids.add(img["image_id"])
            cached = cache.get(img["image_id"])
            if cached is not None:
                cache_hits += 1
                all_facts.append(cached)
                continue
            target_event = dataset.events_by_id.get(img["related_event_id"].strip())
            fact = extract_facts_from_image(client, img, images_dir, tracker, target_event)
            cache.set(fact)
            all_facts.append(fact)

    cache.save()
    print(f"Cache: {cache_hits} reused, {len(all_facts) - cache_hits} new calls ({cache.path}).")
    n_messages = sum(1 for f in all_facts if f.source_type == "message")
    n_images = sum(1 for f in all_facts if f.source_type == "image")
    print(f"Extracted from {len(all_facts)} sources ({n_messages} messages, {n_images} images).")
    print("fact_type distribution:", dict(Counter(f.fact_type for f in all_facts)))

    passed = [f for f in all_facts if passes_sanity_check(f, dataset.profiles[f.user_id])]
    print(f"Passed sanity check (confidence >= {MIN_CONFIDENCE} and plausible): {len(passed)}/{len(all_facts)}")

    print()
    print("Token usage by model:")
    for model, totals in tracker.totals_by_model().items():
        print(f"  {model}: {totals}")

    return 0


def run_validation(dataset: Dataset, cache: ExtractionCache) -> int:
    """Stage 7 (early): validate Stage 1+2+3 against sample_requests.csv's 25
    known-correct amount_safe_to_pay / earliest_date_for_full_payment values,
    per ARCHITECTURE.md's own plan to check here before the full 250-row run."""
    from pipeline.forecast import run_forecast
    from pipeline.loader import parse_date

    AMOUNT_TOLERANCE = 0.02  # relative
    rows = dataset.sample_requests
    amount_hits = 0
    date_hits = 0
    amount_errors = []

    for r in rows:
        user_id = r["user_id"]
        profile = dataset.profiles[user_id]
        as_of_date = parse_date(r["request_date"])
        requested_amount = float(r["requested_amount"])
        state = build_state_for_user(dataset, profile, as_of_date, cache)
        result = run_forecast(state, requested_amount)

        expected_amount = float(r["amount_safe_to_pay"])
        expected_date = r["earliest_date_for_full_payment"].strip() or None

        rel_err = abs(result.amount_safe_to_pay - expected_amount) / max(expected_amount, 1.0)
        amount_ok = rel_err <= AMOUNT_TOLERANCE
        amount_hits += amount_ok
        amount_errors.append(rel_err)

        got_date_str = result.earliest_date_for_full_payment.isoformat() if result.earliest_date_for_full_payment else None
        date_ok = got_date_str == expected_date
        date_hits += date_ok

        marker = "OK" if (amount_ok and date_ok) else "MISS"
        print(
            f"{marker:4s} {r['request_id']:12s} user={user_id:9s} "
            f"amount_safe_to_pay: got={result.amount_safe_to_pay:,.2f} expected={expected_amount:,.2f} "
            f"| earliest_date: got={got_date_str} expected={expected_date}"
        )

    n = len(rows)
    print()
    print(f"amount_safe_to_pay within {AMOUNT_TOLERANCE:.0%}: {amount_hits}/{n}")
    print(f"earliest_date_for_full_payment exact match: {date_hits}/{n}")
    print(f"mean relative amount error: {sum(amount_errors) / n:.3f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-dir", type=Path, default=repo_root() / "dataset")
    parser.add_argument("--user", help="print full reconstructed state for one user_id instead of the summary")
    parser.add_argument("--sample", action="store_true", help="use dataset/sample_requests.csv instead of dataset/requests.csv")
    parser.add_argument("--extract", action="store_true", help="run Stage 2 (message/image extraction) against the real Anthropic API instead of the Stage 0/1 summary")
    parser.add_argument("--limit", type=int, default=None, help="with --extract, stop after this many sources (for a cheap smoke test)")
    parser.add_argument("--validate", action="store_true", help="run Stage 3 against sample_requests.csv's known-correct values instead of the Stage 0/1 summary")
    args = parser.parse_args(argv)

    dataset = load_dataset(args.dataset_dir)

    if args.extract:
        return run_extraction(dataset, args.dataset_dir, [args.user] if args.user else None, args.limit)

    cache = load_extraction_cache()

    if args.validate:
        return run_validation(dataset, cache)

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
                states[request_id] = build_state_for_user(dataset, profile, parse_date(request_dates[request_id]), cache)
            except Exception as exc:  # noqa: BLE001
                errors.append((request_id, user_id, exc))
    else:
        request_user_ids = [(r.request_id, r.user_id) for r in dataset.requests]
        states, errors = build_all_states(dataset, request_user_ids, cache)

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
