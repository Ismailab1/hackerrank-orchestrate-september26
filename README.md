# Buy or Wait? — Submission

An AI-powered financial agent that decides whether a user can safely afford a requested expense — pay in full, pay partially, use installments, wait, or not proceed — personalized to that user's recurring commitments, priorities, payment preferences, and willingness to adjust flexible spending.

Built for the **HackerRank Orchestrate** hackathon (September 2026). Full task spec: [`problem_statement.md`](./problem_statement.md).

---

## Setup Instructions

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip install -r requirements.txt
cp .env.example .env                              # fill in ANTHROPIC_API_KEY
```

**Run the full pipeline** (writes `output.csv` to the repo root and `evaluation/usage_report.md`):

```bash
.venv/bin/python code/main.py --write-output
```

This step alone makes **zero API calls**. All message/image extraction (Stage 2) was already run once against the real Anthropic API and its results are committed at `cache/extracted_facts.json`, keyed by message/image id — `--write-output` reads that cache, so the submitted `code.zip` reproduces the submitted `output.csv` exactly, deterministically, without needing anyone else's API key.

To re-run extraction from scratch instead of trusting the committed cache (optional, costs a small amount of real API spend — see `evaluation/usage_report.md`):

```bash
.venv/bin/python code/main.py --extract
```

**Other entry points**, documented in full in `code/main.py`'s own module docstring:

```bash
.venv/bin/python code/main.py                # Stage 0/1 summary across all 250 requests
.venv/bin/python code/main.py --user user_01 # full reconstructed state for one user
.venv/bin/python code/main.py --sample       # against sample_requests.csv's 25 examples
.venv/bin/python code/main.py --validate     # score Stages 1-4 against sample_requests.csv's known-correct answers
```

---

## Approach Overview

**Determinism first.** The scoring criteria and the challenge rules reward exact, checkable numbers: hard bounds on `amount_safe_to_pay`, an exact 90-day forecast safety check, installment plans that must match a supplied option exactly, and a fully specified tie-break order between competing plans. None of that is left to LLM judgment — it's computed and then validated. The LLM's role is narrowed to the two places the data is genuinely unstructured: extracting facts from message/image evidence, and phrasing the final explanation.

### Pipeline

| Stage | What it does | Code |
| --- | --- | --- |
| 0 | Load and normalize every CSV; exact-match FX conversion | `pipeline/loader.py` |
| 1 | Reconstruct each user's financial state: detect recurring obligations, model irregular essential spending as a rate, classify income streams | `pipeline/income.py`, `pipeline/state.py` |
| 2 | Extract structured facts from messages and images via the real Anthropic API (Claude Haiku 4.5 for text, Claude Sonnet 5 for vision), cached by source id | `pipeline/extraction.py` |
| — | Apply Stage 2's extracted facts back into Stage 1 as amendments | `pipeline/amendments.py` |
| 3 | Simulate the 90-day balance forward and compute `amount_safe_to_pay` / `earliest_date_for_full_payment` | `pipeline/forecast.py` |
| 4 | Generate and rank candidate payment plans against the spec's tie-break order | `pipeline/plans.py` |
| 5 | Build `decision_explanation` from a deterministic template, grounded in the plan's own numbers | `pipeline/explain.py` |
| 6 | Validate every row before writing — the row fails open, the recommendation fails closed | `pipeline/output.py` |

Full design rationale for each stage, including the empirical analysis behind every threshold, is in [`code/ARCHITECTURE.md`](./code/ARCHITECTURE.md).

### Decisions worth knowing about

The full reasoning for each of these — including the data that drove it — is logged in [`code/DECISIONS.md`](./code/DECISIONS.md) (12 entries). The ones that most shaped the output:

- **Recurring vs. irregular spending is modeled differently, and the split is empirical, not assumed.** Fixed-cadence categories (rent, utilities, subscriptions, insurance, and — found only by testing every category rather than an assumed subset — entertainment, shopping, healthcare, family_support) show day-of-month spread of exactly 0 across 1,305+ real occurrence groups. Groceries, transport, and dining never land on a clean schedule, so they're modeled as a daily spending rate instead (entries 1, 3).
- **Income is one or more independent streams, never a single "most recent row wins."** A base salary and a later commission are both real, concurrent income in this dataset; treating the newest row as the only truth let a commission payment silently erase a well-evidenced base salary for 9 users, and let an unrelated, unlinked "one income source ended" message wipe out a stream it was never actually about for 12 more. Both fixed by evaluating each stream on its own evidence (entries 9, 11).
- **Gig/freelance income is deliberately excluded from the hard 90-day safety floor**, even though it's tracked and used for other purposes. This was re-tested, not just assumed, in the last accuracy pass: removing the exclusion, and a "credit only the next occurrence" middle ground, were both tried against real validation data and both made results worse (entry 10).
- **Never invent unsupported income, expenses, or corrections.** A message that's ambiguous about which income source it refers to doesn't get to guess; a fact that fails its confidence or currency-conversion check is excluded entirely rather than approximated (entries 7, 9, 11).
- **No generic industry benchmarks layered on top of the user's own numbers.** `minimum_balance_to_keep` and `max_installment_months` already are this user's personalized version of a debt-to-income ceiling or an emergency-fund buffer; adding a population-level rule on top would silently override a preference the user already gave (entry 6).
- **The row always gets written; the recommendation never guesses past its own uncertainty.** Every request produces exactly one schema-valid row (a catch-all prevents a crash from ever dropping one), but when the pipeline's own estimate lands within a few percent of the safety threshold, it declines rather than gambles — verified in the final pass by tracing the tightest near-miss cases by hand and finding no locatable defect, only genuine forecast variance (entries 8, 12).

### Where it landed

Validated against `sample_requests.csv`'s 25 known-correct rows (`--validate`) before trusting the full run — per the challenge's own note, these are a style guide, not evaluation labels, so this checks plausibility rather than being optimized against directly:

| Metric | Result |
| --- | --- |
| `amount_safe_to_pay` within 2% | 8/25 |
| `earliest_date_for_full_payment` exact | 19/25 |
| `affordability_status` exact | 18/25 |
| `recommended_payment_method` exact | 19/25 |
| `payment_plan` exact | 18/25 |

Full 250-row run (`--write-output`): zero errors, zero fail-open fallback rows. `full_payment` 65, `wait` 49, `installments` 34, `partial_payment` 9, `not_recommended` 93.

---

## Repository Layout

```text
.
├── README.md                    # this file
├── problem_statement.md         # full challenge spec
├── AGENTS.md                    # AI-agent working rules + chat-transcript logging
├── requirements.txt
├── .env.example
├── output.csv                   # generated predictions (submission deliverable)
├── cache/
│   └── extracted_facts.json     # Stage 2's cached extraction results
├── code/
│   ├── main.py                  # entry point — see its module docstring for every CLI flag
│   ├── ARCHITECTURE.md           # pipeline design, derivation of every threshold
│   ├── DECISIONS.md              # 12 logged edge-case decisions, with the data behind each
│   ├── pipeline/                 # loader, income, state, extraction, amendments, forecast, plans, explain, output
│   └── evaluation/
│       └── usage_report.md      # token usage and cost for the Stage 2 extraction run
└── dataset/                      # organizer-provided input data (not modified)
```

---

## Submission Checklist

- [x] `output.csv` — one row per row in `dataset/requests.csv` (250 rows + header), exact required columns and order, `0 <= amount_safe_to_pay <= requested_amount` on every row.
- [x] `code.zip` — this README, `requirements.txt`, `.env.example`, `code/` (including `evaluation/usage_report.md`), and `cache/extracted_facts.json` so the package is runnable and reproducible without a fresh API key.
- [x] `chat_transcript` — `log.txt` at the repo root, maintained per `AGENTS.md` across the whole build. It's gitignored (per `AGENTS.md`'s own rule), so grab it directly from the working directory at submission time rather than looking for it in `code.zip`.

Before submitting, regenerate `output.csv` one more time to confirm it's current:

```bash
.venv/bin/python code/main.py --write-output
```
