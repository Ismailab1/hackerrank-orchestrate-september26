"""Income description classification (ARCHITECTURE.md Stage 1; DECISIONS.md entries 4-5).

Built from the dataset's actual 164 unique (category, description) combinations
rather than inferred from occurrence statistics. Only the `salary` and
`windfall` categories carry income event_type rows in this dataset.
"""

from __future__ import annotations

CONTINUING = "continuing"
TRANSITIONAL_START = "transitional_start"
TRANSITIONAL_END = "transitional_end"
GIG = "gig"
ONE_TIME = "one_time"

# transitional_start behaves like CONTINUING for projection purposes (the new
# job's income is confirmed going forward) but is kept distinct for the
# decision_explanation text produced in a later stage.
PROJECTABLE = frozenset({CONTINUING, TRANSITIONAL_START})

_SALARY_DESCRIPTIONS: dict[str, str] = {
    # Standard continuing salary.
    "Base salary": CONTINUING,
    "Payroll credit": CONTINUING,
    "Primary household salary": CONTINUING,
    "Second household income": CONTINUING,
    "International employer payroll": CONTINUING,
    "Peak-season wages": CONTINUING,
    "August 2019 net salary": CONTINUING,
    "Payroll after returning from leave": CONTINUING,
    # This is the dataset's own "next confirmed salary" row (always status
    # scheduled): the definitive next occurrence, not an inference.
    "Next confirmed salary": CONTINUING,
    # A new income source just started; treated as confirmed going forward.
    "New employer payroll": TRANSITIONAL_START,
    "First-job payroll": TRANSITIONAL_START,
    "Prorated first salary": TRANSITIONAL_START,
    "Temporary assignment pay": TRANSITIONAL_START,
    # An income source that ended; no future occurrence should be assumed
    # when this is the most recent salary-category record (DECISIONS.md #4).
    "Final employer payroll": TRANSITIONAL_END,
    "Previous employer payroll": TRANSITIONAL_END,
    "Payroll before leave": TRANSITIONAL_END,
    # Inherently variable gig/freelance/commission income (DECISIONS.md #5):
    # project using a conservative low estimate, never a mean or latest payout.
    "Freelance milestone payment": GIG,
    "Consulting invoice payment": GIG,
    "Client retainer payment": GIG,
    "Content contract payment": GIG,
    "Design contract payment": GIG,
    "Delivery platform payout": GIG,
    "Driver platform payout": GIG,
    "Task marketplace payout": GIG,
    "Weekly app earnings": GIG,
    "Application project payment": GIG,
    "Website project payment": GIG,
    "Independent work payment": GIG,
    "Seasonal contract payment": GIG,
    "Account commission payment": GIG,
    "Monthly sales commission": GIG,
    "Performance commission": GIG,
    # Occasional top-ups on top of base salary; real once settled but never
    # projected forward as a recurring commitment.
    "Quarterly performance bonus": ONE_TIME,
    "Promotion arrears payment": ONE_TIME,
}

_WINDFALL_DESCRIPTIONS: dict[str, str] = {
    "Prize proceeds": ONE_TIME,
}

_BY_CATEGORY: dict[str, dict[str, str]] = {
    "salary": _SALARY_DESCRIPTIONS,
    "windfall": _WINDFALL_DESCRIPTIONS,
}


def classify_income_description(category: str, description: str) -> str:
    """Classify one income-bearing (category, description) pair.

    Falls back to ONE_TIME for anything unrecognized: never invent confirmed
    future income for a description this lookup was not built from.
    """
    return _BY_CATEGORY.get(category, {}).get(description, ONE_TIME)
