from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from valuationagent.core.data import DataBundle, LocalDataProvider
from valuationagent.finance.industry import (
    FINANCIAL_KEYWORDS,
    IndustryParameterRegistry,
)
from valuationagent.schemas.models import (
    CompanyInput,
    EvidenceRef,
    FinancialSnapshot,
    PeerCompany,
    ValuationRequest,
)

D = Decimal
TUSHARE_DOC = "https://tushare.pro/document/2"

TUSHARE_INDUSTRY_MAP = (
    (("软件", "IT", "互联网", "元器件", "半导体", "通信设备", "电脑设备"), "电子 / 计算机 / 半导体"),
    (("医药", "医疗", "生物", "制药"), "医药 / 生物"),
    (("航空", "船舶", "军工"), "军工 / 航空航天"),
    (("电气设备", "光伏", "电池", "新能源", "储能"), "电力设备 / 新能源"),
    (("机械", "专用设备", "通用设备", "工程机械"), "机械设备"),
    (("汽车", "汽配", "摩托车"), "汽车"),
    (("化工", "化纤", "塑料", "橡胶", "农药化肥"), "化工"),
    (("造纸", "家具", "文教", "家居用品", "日用化工", "家用电器", "食品", "饮料", "白酒", "乳制品"), "轻工制造"),
    (("纺织", "服饰", "服装", "鞋"), "纺织服饰制鞋"),
    (("建筑", "建材", "水泥", "陶瓷", "玻璃"), "建筑业"),
    (("铝", "铜", "铅锌", "小金属", "黄金", "有色", "金属新材料"), "有色金属"),
    (("钢", "普钢", "特种钢"), "钢铁"),
    (("农业", "种植", "饲料", "畜牧", "渔业", "林业", "农林牧渔"), "农林牧渔"),
    (("煤", "石油", "天然气", "油气"), "煤炭 / 石油"),
    (("电力", "供气供热", "水务", "环境保护"), "公用事业"),
    (("百货", "零售", "超市", "商贸", "批发", "商品城"), "商贸零售"),
    (("旅游", "酒店", "餐饮", "景点"), "餐饮旅游"),
    (("房地产", "房产"), "房地产"),
    (("运输", "物流", "港口", "航运", "机场", "公路", "铁路", "仓储"), "交通运输"),
    (("传媒", "广告", "影视", "教育", "专业服务", "通信服务"), "服务业（兜底）"),
)


def map_tushare_industry(label: str) -> tuple[str, str | None]:
    """Map detailed vendor labels to the versioned finance-team registry."""
    raw = (label or "").strip()
    if any(word in raw for word in FINANCIAL_KEYWORDS):
        raise ValueError("当前项目不研究金融行业，不能为该公司运行通用FCFF估值。")
    registry = IndustryParameterRegistry()
    try:
        return registry.resolve(raw).name, None
    except ValueError:
        pass
    for keywords, target in TUSHARE_INDUSTRY_MAP:
        if any(keyword.casefold() in raw.casefold() for keyword in keywords):
            return target, f"Tushare行业“{raw}”按显式映射进入参数库“{target}”，请复核主营业务。"
    return "工业（兜底）", f"Tushare行业“{raw or '空'}”未命中细分类，暂用工业兜底参数，必须人工复核。"


def normalize_a_share_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if ticker.endswith((".SH", ".SZ", ".BJ")):
        return ticker
    symbol = ticker.split(".")[0]
    if not (len(symbol) == 6 and symbol.isdigit()):
        raise ValueError("A股代码应为6位数字，或带.SH/.SZ/.BJ后缀。")
    if symbol.startswith(("4", "8", "9")):
        return symbol + ".BJ"
    if symbol.startswith(("5", "6", "9")):
        return symbol + ".SH"
    return symbol + ".SZ"


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _record_date(value: Any) -> date | None:
    raw = str(value or "").replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        return None
    return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))


class TushareApiClient:
    """Small dependency-free client for the official Tushare Pro JSON API."""

    version = "tushare-pro-json-v1"

    def __init__(
        self,
        token: str,
        *,
        endpoint: str = "https://api.tushare.pro",
        timeout_seconds: float = 40,
        transport=None,
    ):
        if not token.strip():
            raise ValueError("TUSHARE_TOKEN不能为空。")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Tushare接口必须是无账号信息的HTTPS地址。")
        self._token = token.strip()
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    def query(
        self, api_name: str, *, params: dict[str, Any] | None = None, fields: list[str] | None = None
    ) -> list[dict[str, Any]]:
        payload = {
            "api_name": api_name,
            "token": self._token,
            "params": params or {},
            "fields": ",".join(fields or []),
        }
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.post(self.endpoint, json=payload)
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f"（HTTP {code}）" if code else ""
            raise ValueError(f"Tushare数据请求失败{suffix}，请检查网络与数据源配置。") from None
        if int(body.get("code", -1)) != 0:
            raw = str(body.get("msg") or "").lower()
            message = (
                "Token无效或权限不足" if "token" in raw
                else "接口权限不足" if "权限" in raw or "permission" in raw
                else f"供应商错误代码 {body.get('code')}"
            )
            raise ValueError(f"Tushare接口 {api_name} 失败：{message}")
        data = body.get("data") or {}
        names = data.get("fields") or []
        return [dict(zip(names, row)) for row in data.get("items") or []]


class TushareDataProvider:
    """A-share facts and comparable companies with point-in-time controls."""

    version = "tushare-a-share-2026-09"

    INCOME_FIELDS: ClassVar[list[str]] = [
        "ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type",
        "revenue", "total_revenue", "operate_profit", "total_profit", "n_income_attr_p",
        "income_tax", "oper_cost", "biz_tax_surchg", "sell_exp", "admin_exp", "rd_exp",
        "assets_impair_loss", "oth_income",
    ]
    BALANCE_FIELDS: ClassVar[list[str]] = [
        "ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type",
        "money_cap", "total_hldr_eqy_exc_min_int", "total_assets", "total_liab",
        "fix_assets", "intan_assets", "lt_amor_exp", "use_right_assets", "lease_liab",
        "accounts_receiv", "inventories", "acct_payable",
    ]
    CASHFLOW_FIELDS: ClassVar[list[str]] = [
        "ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type",
        "c_pay_acq_const_fiolta", "depr_fa_coga_dpba", "amort_intang_assets",
        "lt_amort_deferred_exp", "use_right_asset_dep",
    ]
    INDICATOR_FIELDS: ClassVar[list[str]] = [
        "ts_code", "ann_date", "end_date", "ebit", "ebitda", "daa", "interestdebt",
        "networking_capital", "working_capital", "fixed_assets", "or_yoy", "ebit_of_gr", "roe",
    ]

    def __init__(self, client: TushareApiClient, *, peer_limit: int = 8):
        self.client = client
        self.peer_limit = max(3, min(peer_limit, 12))
        self.local = LocalDataProvider()

    @staticmethod
    def _available_annual(rows: list[dict[str, Any]], cutoff: date) -> dict[str, dict[str, Any]]:
        selected: dict[str, dict[str, Any]] = {}
        for row in rows:
            end = str(row.get("end_date") or "")
            if not end.endswith("1231"):
                continue
            period_end = _record_date(end)
            if period_end is None or period_end > cutoff:
                continue
            if row.get("report_type") not in (None, "", "1", 1):
                continue
            announced = _record_date(row.get("f_ann_date") or row.get("ann_date"))
            if announced and announced > cutoff:
                continue
            old = selected.get(end)
            old_date = _record_date((old or {}).get("f_ann_date") or (old or {}).get("ann_date"))
            if old is None or (announced or date.min) >= (old_date or date.min):
                selected[end] = row
        return selected

    def _query_financial_rows(self, ticker: str, cutoff: date):
        start = date(cutoff.year - 12, 1, 1).strftime("%Y%m%d")
        end = cutoff.strftime("%Y%m%d")
        params = {"ts_code": ticker, "start_date": start, "end_date": end}
        income = self._available_annual(
            self.client.query("income", params=params, fields=self.INCOME_FIELDS), cutoff
        )
        balance = self._available_annual(
            self.client.query("balancesheet", params=params, fields=self.BALANCE_FIELDS), cutoff
        )
        cashflow = self._available_annual(
            self.client.query("cashflow", params=params, fields=self.CASHFLOW_FIELDS), cutoff
        )
        indicator = self._available_annual(
            self.client.query("fina_indicator", params=params, fields=self.INDICATOR_FIELDS), cutoff
        )
        return income, balance, cashflow, indicator

    def _latest_daily(self, ticker: str, cutoff: date) -> dict[str, Any]:
        rows = self.client.query(
            "daily_basic",
            params={
                "ts_code": ticker,
                "start_date": (cutoff - timedelta(days=20)).strftime("%Y%m%d"),
                "end_date": cutoff.strftime("%Y%m%d"),
            },
            fields=[
                "ts_code", "trade_date", "close", "pe_ttm", "ps_ttm", "total_share", "total_mv"
            ],
        )
        eligible = [row for row in rows if (_record_date(row.get("trade_date")) or date.max) <= cutoff]
        if not eligible:
            raise ValueError(f"{ticker}在估值日前没有可用每日指标。")
        return max(eligible, key=lambda row: str(row.get("trade_date") or ""))

    def _market_cap_statistics(
        self, ticker: str, cutoff: date
    ) -> dict[str, Decimal]:
        rows = self.client.query(
            "daily_basic",
            params={
                "ts_code": ticker,
                "start_date": (cutoff - timedelta(days=370)).strftime("%Y%m%d"),
                "end_date": cutoff.strftime("%Y%m%d"),
            },
            fields=["ts_code", "trade_date", "total_mv"],
        )
        observations = [
            (_record_date(row.get("trade_date")), _decimal(row.get("total_mv")))
            for row in rows
            if (_record_date(row.get("trade_date")) or date.max) <= cutoff
            and _decimal(row.get("total_mv")) is not None
        ]
        observations = [
            (trade_date, value * D("10000"))
            for trade_date, value in observations
            if trade_date is not None and value is not None and value > 0
        ]
        values = [value for _, value in observations]
        if len(values) < 60:
            return {}
        quarterly: dict[tuple[int, int], list[Decimal]] = {}
        for trade_date, value in observations:
            key = (trade_date.year, (trade_date.month - 1) // 3 + 1)
            quarterly.setdefault(key, []).append(value)
        quarterly_means = [
            sum(group, D(0)) / D(len(group))
            for _, group in sorted(quarterly.items())[-4:]
        ]
        return {
            "quarterly_average_market_cap": (
                sum(quarterly_means, D(0)) / D(len(quarterly_means))
            ),
            "annual_average_market_cap": sum(values, D(0)) / D(len(values)),
            "market_cap_period_low": min(values),
            "market_cap_period_high": max(values),
        }

    def _quarterly_average_market_cap(
        self, ticker: str, cutoff: date
    ) -> Decimal | None:
        """Compatibility wrapper for integrations that still consume one value."""

        return self._market_cap_statistics(ticker, cutoff).get(
            "quarterly_average_market_cap"
        )

    @staticmethod
    def _evidence(ticker: str, period: str, api: str, field: str, ann_date=None) -> list[EvidenceRef]:
        return [
            EvidenceRef(
                evidence_id=f"tushare:{ticker}:{period}:{api}:{field}",
                source=f"Tushare Pro · {api}",
                published_at=_record_date(ann_date),
                note=f"field={field}; ts_code={ticker}; period={period}; docs={TUSHARE_DOC}",
            )
        ]

    def _financial_snapshots(
        self,
        ticker: str,
        cutoff: date,
        daily: dict[str, Any],
        market_cap_statistics: dict[str, Decimal] | None = None,
    ) -> tuple[list[FinancialSnapshot], list[str]]:
        income, balance, cashflow, indicator = self._query_financial_rows(ticker, cutoff)
        periods = sorted(set(income) & set(balance) & set(cashflow) & set(indicator))[-10:]
        if len(periods) < 4:
            raise ValueError(f"{ticker}仅取得{len(periods)}个完整年报期，无法运行自动收入模型。")
        warnings: list[str] = []
        rows = []
        previous_nwc: Decimal | None = None
        latest_shares = (_decimal(daily.get("total_share")) or D(0)) * D("10000")
        latest_market_cap = (_decimal(daily.get("total_mv")) or D(0)) * D("10000")
        if latest_shares <= 0:
            raise ValueError("每日指标缺少总股本，无法形成每股估值。")
        for period in periods:
            inc, bal, cash, ind = income[period], balance[period], cashflow[period], indicator[period]
            revenue = _decimal(inc.get("revenue") or inc.get("total_revenue"))
            ebit = _decimal(ind.get("ebit")) or _decimal(inc.get("operate_profit"))
            net_income = _decimal(inc.get("n_income_attr_p"))
            if not revenue or revenue <= 0 or ebit is None or net_income is None:
                raise ValueError(f"{ticker} {period} 缺少收入、EBIT或归母净利润。")
            da = _decimal(ind.get("daa"))
            ebitda = _decimal(ind.get("ebitda"))
            if da is None and ebitda is not None:
                da = max(D(0), ebitda - ebit)
            if da is None:
                components = [
                    _decimal(cash.get("depr_fa_coga_dpba")),
                    _decimal(cash.get("amort_intang_assets")),
                    _decimal(cash.get("lt_amort_deferred_exp")),
                    _decimal(cash.get("use_right_asset_dep")),
                ]
                da = sum((item for item in components if item is not None), D(0))
            if ebitda is None:
                ebitda = ebit + da
            capex = abs(_decimal(cash.get("c_pay_acq_const_fiolta")) or D(0))
            receivable = _decimal(bal.get("accounts_receiv"))
            inventory = _decimal(bal.get("inventories"))
            payable = _decimal(bal.get("acct_payable"))
            direct_nwc = (
                receivable + inventory - payable
                if receivable is not None and inventory is not None and payable is not None
                else None
            )
            nwc = _decimal(ind.get("networking_capital") or ind.get("working_capital"))
            if nwc is None:
                nwc = direct_nwc
            change_nwc = D(0) if nwc is None or previous_nwc is None else nwc - previous_nwc
            if nwc is None:
                warnings.append(f"{period}缺少经营营运资本，ΔNWC暂为0并要求复核。")
            total_profit = _decimal(inc.get("total_profit"))
            tax_expense = _decimal(inc.get("income_tax"))
            if total_profit is not None and total_profit > 0 and tax_expense is not None:
                tax_rate = max(D(0), min(D("0.6"), tax_expense / total_profit))
            else:
                tax_rate = D("0.25")
                warnings.append(f"{period}无法由所得税费用/利润总额计算税率，暂用25%并要求复核。")
            period_date = _record_date(period)
            announced = inc.get("f_ann_date") or inc.get("ann_date")
            evidence = {
                "revenue": self._evidence(ticker, period, "income", "revenue", announced),
                "ebit_margin": self._evidence(ticker, period, "fina_indicator", "ebit", announced),
                "net_income_parent": self._evidence(ticker, period, "income", "n_income_attr_p", announced),
                "depreciation_amortization": self._evidence(ticker, period, "fina_indicator", "daa", announced),
                "capital_expenditure": self._evidence(ticker, period, "cashflow", "c_pay_acq_const_fiolta", announced),
            }
            statement_items = {
                key: value
                for key, value in {
                    "market_cap": latest_market_cap if period == periods[-1] else None,
                    **(
                        market_cap_statistics
                        if period == periods[-1] and market_cap_statistics
                        else {}
                    ),
                    "total_equity": _decimal(bal.get("total_hldr_eqy_exc_min_int")),
                    "operating_nwc": nwc,
                    "fixed_assets_net": _decimal(bal.get("fix_assets") or ind.get("fixed_assets")),
                    "intangible_assets": _decimal(bal.get("intan_assets")),
                    "long_term_deferred_expenses": _decimal(bal.get("lt_amor_exp")),
                    "right_of_use_assets": _decimal(bal.get("use_right_assets")),
                    "lease_liabilities": _decimal(bal.get("lease_liab")),
                    "depreciation_fixed_assets": _decimal(cash.get("depr_fa_coga_dpba")),
                    "amortization_intangibles": _decimal(cash.get("amort_intang_assets")),
                    "amortization_long_term_deferred": _decimal(cash.get("lt_amort_deferred_exp")),
                    "depreciation_right_of_use": _decimal(cash.get("use_right_asset_dep")),
                    "accounts_receivable": receivable,
                    "inventory": inventory,
                    "accounts_payable": payable,
                    "operating_cost": _decimal(inc.get("oper_cost")),
                    "taxes_and_surcharges": _decimal(inc.get("biz_tax_surchg")),
                    "selling_expense": _decimal(inc.get("sell_exp")),
                    "administrative_expense": _decimal(inc.get("admin_exp")),
                    "research_expense": _decimal(inc.get("rd_exp")),
                    "impairment_loss": _decimal(inc.get("assets_impair_loss")),
                    "other_income": _decimal(inc.get("oth_income")),
                }.items()
                if value is not None
            }
            rows.append(
                FinancialSnapshot(
                    period_end=period_date,
                    published_at=_record_date(announced),
                    revenue=revenue,
                    ebit_margin=ebit / revenue,
                    tax_rate=tax_rate,
                    depreciation_amortization=max(D(0), da),
                    capital_expenditure=capex,
                    change_operating_nwc=change_nwc,
                    cash_and_non_operating_assets=max(D(0), _decimal(bal.get("money_cap")) or D(0)),
                    interest_bearing_debt=max(D(0), _decimal(ind.get("interestdebt")) or D(0)),
                    common_shares=latest_shares,
                    net_income_parent=net_income,
                    ebitda=ebitda,
                    source_label=f"Tushare Pro · {ticker} · annual statements",
                    evidence=evidence,
                    statement_items=statement_items,
                )
            )
            previous_nwc = nwc
        return rows, list(dict.fromkeys(warnings))

    def _company(self, ticker: str) -> CompanyInput:
        rows = self.client.query(
            "stock_basic",
            params={"ts_code": ticker, "list_status": "L"},
            fields=["ts_code", "symbol", "name", "industry", "market", "exchange", "curr_type", "list_status"],
        )
        if not rows:
            raise ValueError(f"未找到正常上市的A股代码 {ticker}。")
        row = rows[0]
        if any(word in str(row.get("industry") or "") for word in FINANCIAL_KEYWORDS):
            raise ValueError("当前项目不研究金融行业，不能为该公司运行通用FCFF估值。")
        return CompanyInput(
            ticker=ticker,
            name=row.get("name"),
            exchange=row.get("exchange"),
            industry=row.get("industry"),
            currency="CNY",
        )

    def _peer_indicator(self, ticker: str, cutoff: date) -> dict[str, Any] | None:
        start = date(cutoff.year - 2, 1, 1).strftime("%Y%m%d")
        rows = self.client.query(
            "fina_indicator",
            params={"ts_code": ticker, "start_date": start, "end_date": cutoff.strftime("%Y%m%d")},
            fields=self.INDICATOR_FIELDS,
        )
        annual = self._available_annual(rows, cutoff)
        return annual[max(annual)] if annual else None

    def _peer_cash(self, ticker: str, cutoff: date) -> Decimal:
        start = date(cutoff.year - 2, 1, 1).strftime("%Y%m%d")
        rows = self.client.query(
            "balancesheet",
            params={"ts_code": ticker, "start_date": start, "end_date": cutoff.strftime("%Y%m%d")},
            fields=["ts_code", "ann_date", "f_ann_date", "end_date", "money_cap"],
        )
        annual = self._available_annual(rows, cutoff)
        if not annual:
            return D(0)
        return max(D(0), _decimal(annual[max(annual)].get("money_cap")) or D(0))

    def _select_peers(
        self,
        company: CompanyInput,
        target_daily: dict[str, Any],
        cutoff: date,
    ) -> tuple[list[PeerCompany], list[str]]:
        universe = self.client.query(
            "stock_basic",
            params={"list_status": "L"},
            fields=["ts_code", "name", "industry", "market", "exchange", "list_status"],
        )
        same = [
            row for row in universe
            if row.get("industry") == company.industry
            and row.get("ts_code") != company.ticker
            and "ST" not in str(row.get("name") or "").upper()
            and not any(word in str(row.get("industry") or "") for word in FINANCIAL_KEYWORDS)
        ]
        if not same:
            return [], ["同一Tushare行业下没有可用非金融候选公司。"]
        market_rows = self.client.query(
            "daily_basic",
            params={"trade_date": str(target_daily["trade_date"])},
            fields=["ts_code", "trade_date", "pe_ttm", "ps_ttm", "total_mv"],
        )
        market = {row["ts_code"]: row for row in market_rows}
        target_mv = (_decimal(target_daily.get("total_mv")) or D(0)) * D("10000")
        target_indicator = self._peer_indicator(company.ticker, cutoff) or {}
        target_growth = _decimal(target_indicator.get("or_yoy")) or D(0)
        target_margin = _decimal(target_indicator.get("ebit_of_gr")) or D(0)
        ranked = []
        for row in same:
            daily = market.get(row["ts_code"])
            mv = (_decimal((daily or {}).get("total_mv")) or D(0)) * D("10000")
            if not daily or mv <= 0 or target_mv <= 0:
                continue
            size_score = abs(math.log(float(mv / target_mv)))
            ranked.append((size_score, row, daily, mv))
        ranked.sort(key=lambda item: item[0])
        peers = []
        warnings = []
        for size_score, row, daily, market_cap in ranked[: max(self.peer_limit * 2, 12)]:
            indicator = self._peer_indicator(row["ts_code"], cutoff)
            if not indicator:
                continue
            growth = _decimal(indicator.get("or_yoy")) or D(0)
            margin = _decimal(indicator.get("ebit_of_gr")) or D(0)
            quality_score = size_score + abs(float(growth - target_growth)) / 20 + abs(float(margin - target_margin)) / 10
            pe = _decimal(daily.get("pe_ttm"))
            ps = _decimal(daily.get("ps_ttm"))
            ebitda = _decimal(indicator.get("ebitda"))
            debt = _decimal(indicator.get("interestdebt")) or D(0)
            cash = self._peer_cash(row["ts_code"], cutoff)
            ev_ebitda = (market_cap + debt - cash) / ebitda if ebitda and ebitda > 0 else None
            if not any(value and value > 0 for value in (pe, ps, ev_ebitda)):
                continue
            peers.append((quality_score, PeerCompany(
                ticker=row["ts_code"],
                name=str(row.get("name") or row["ts_code"]),
                pe=pe if pe and pe > 0 else None,
                ps=ps if ps and ps > 0 else None,
                ev_ebitda=ev_ebitda if ev_ebitda and ev_ebitda > 0 else None,
                market_cap=market_cap,
                revenue_growth=growth / D("100"),
                ebit_margin=margin / D("100"),
                selection_score=D(str(quality_score)),
                peer_tier="broad",
                rationale=(
                    f"Tushare同一行业={company.industry}；规模/增长/EBIT率距离得分={quality_score:.4f}；"
                    f"市场数据日={daily.get('trade_date')}；EV=市值+有息负债-货币资金"
                ),
            )))
        peers.sort(key=lambda item: item[0])
        selected = [
            item[1].model_copy(update={"peer_tier": "core" if index < 5 else "broad"})
            for index, item in enumerate(peers[: self.peer_limit])
        ]
        if len(selected) < 3:
            warnings.append(f"生产筛选后仅{len(selected)}家可比公司，倍数结果将标记样本不足。")
        return selected, warnings

    def resolve(self, request: ValuationRequest, store) -> DataBundle:
        if request.mode == "demo" or request.data_source != "ticker":
            return self.local.resolve(request, store)
        ticker = normalize_a_share_ticker(request.company.ticker or "")
        company = self._company(ticker)
        daily = self._latest_daily(ticker, request.valuation_date)
        market_warnings: list[str] = []
        try:
            market_cap_statistics = self._market_cap_statistics(
                ticker, request.valuation_date
            )
        except ValueError as exc:
            market_cap_statistics = {}
            market_warnings.append(
                f"四季度平均市值取数失败（{exc}），WACC将降级使用基准日前最近市值。"
            )
        if not market_cap_statistics:
            market_warnings.append(
                "估值日前一年有效市值观测不足60个交易日，WACC降级使用基准日前最近市值。"
            )
        financials, warnings = self._financial_snapshots(
            ticker, request.valuation_date, daily, market_cap_statistics
        )
        warnings = market_warnings + warnings
        peers = list(request.peers)
        if any(method != "dcf" for method in request.methods) and not peers:
            peers, peer_warnings = self._select_peers(company, daily, request.valuation_date)
            warnings.extend(peer_warnings)
        model_industry, mapping_warning = map_tushare_industry(company.industry or "")
        company = company.model_copy(update={"industry": model_industry})
        if mapping_warning:
            warnings.append(mapping_warning)
        return DataBundle(
            company=company,
            financials=financials[-1],
            historical_financials=financials[:-1],
            peers=peers,
            assumptions=request.assumptions,
            assumption_evidence=request.assumption_evidence,
            warnings=list(dict.fromkeys(warnings)),
        )
