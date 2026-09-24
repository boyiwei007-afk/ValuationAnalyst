from __future__ import annotations

import os

from valuationagent.finance.reference import ReferenceFinancialModel
from valuationagent.finance.team_model import FinanceTeamModel


def create_financial_model():
    """Select the production finance-team model; reference remains opt-in."""
    selected = os.getenv("VALUATION_FINANCE_MODEL", "team").strip().lower()
    if selected in {"reference", "demo", "legacy"}:
        return ReferenceFinancialModel()
    if selected in {"team", "finance_team", "production"}:
        return FinanceTeamModel()
    raise ValueError(
        "VALUATION_FINANCE_MODEL must be team or reference"
    )
