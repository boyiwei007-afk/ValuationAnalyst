from __future__ import annotations

import os

from valuationagent.core.data import LocalDataProvider
from valuationagent.market.tushare import TushareApiClient, TushareDataProvider


def create_data_provider():
    token = os.getenv("TUSHARE_TOKEN") or os.getenv("VALUATION_MARKET_DATA_TOKEN")
    if token:
        return TushareDataProvider(TushareApiClient(token))
    return LocalDataProvider()
