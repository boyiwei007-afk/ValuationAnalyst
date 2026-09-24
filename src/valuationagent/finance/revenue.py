from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import ClassVar

from valuationagent.finance.industry import IndustryParameters
from valuationagent.schemas.models import FinancialSnapshot

D = Decimal


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        raise ValueError("平均值至少需要一个数据点。")
    return sum(values, D(0)) / D(len(values))


def _percentile(values: list[Decimal], q: Decimal) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("分位数至少需要一个数据点。")
    if len(ordered) == 1:
        return ordered[0]
    position = D(len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - D(lower)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


@dataclass(frozen=True)
class RevenueProjection:
    scenarios: dict[str, list[Decimal]]
    annual_growth_history: list[Decimal]
    cagr: Decimal
    a3: Decimal
    a5: Decimal
    center: Decimal
    position: str
    rho: Decimal
    raw_decay_years: int
    effective_decay_years: int
    cycle: str
    anchors: dict[str, Decimal]
    decisions: list[str]


class FinanceTeamRevenueModel:
    """Finance-team eight-step revenue model with explicit edge policies."""

    POSITION_RHO: ClassVar[dict[str, Decimal]] = {
        "high": D("0.60"),
        "mid": D("0.80"),
        "low": D("1.00"),
    }

    @staticmethod
    def _previous_completed_cycle(
        excess: list[Decimal], *, positive: bool
    ) -> Decimal | None:
        """Return the mean excess growth of the latest completed sign cycle.

        The finance note refers to the *previous* up/down cycle without defining
        an aggregation rule.  We therefore use the arithmetic mean of the most
        recent completed consecutive run and exclude the trailing run because it
        is the current, not yet completed, cycle.  The policy is emitted into the
        audit decisions below instead of being hidden in implementation code.
        """

        if not excess:
            return None
        runs: list[tuple[bool, list[Decimal]]] = []
        for value in excess:
            sign = value >= 0
            if not runs or runs[-1][0] != sign:
                runs.append((sign, [value]))
            else:
                runs[-1][1].append(value)
        completed = runs[:-1]
        for sign, values in reversed(completed):
            if sign == positive:
                return _mean(values)
        return None

    @staticmethod
    def _history(snapshots: list[FinancialSnapshot]) -> list[FinancialSnapshot]:
        by_year: dict[int, FinancialSnapshot] = {}
        for row in sorted(snapshots, key=lambda item: item.period_end):
            by_year[row.period_end.year] = row
        rows = list(by_year.values())[-10:]
        if len(rows) < 4:
            raise ValueError(
                "自动收入模型至少需要4个连续年度收入，才能形成3个年度增速并完成位置判断。"
            )
        years = [row.period_end.year for row in rows]
        if years != list(range(years[0], years[-1] + 1)):
            raise ValueError(
                "历史收入年份不连续，不能静默插值。请补充缺失年度或改用手工收入假设。"
            )
        return rows

    @staticmethod
    def _path(
        start: Decimal,
        industry_growth: Decimal,
        terminal_growth: Decimal,
        decay_years: int,
        years: int,
    ) -> list[Decimal]:
        result: list[Decimal] = []
        for year in range(1, years + 1):
            if year <= decay_years:
                weight = D(1) - D(year - 1) / D(max(decay_years - 1, 1))
                growth = industry_growth + (start - industry_growth) * weight
            else:
                growth = industry_growth + (terminal_growth - industry_growth) * (
                    D(year - decay_years) / D(years - decay_years)
                )
            result.append(growth)
        result[-1] = terminal_growth
        return result

    def project(
        self,
        snapshots: list[FinancialSnapshot],
        industry: IndustryParameters,
        terminal_growth: Decimal,
        forecast_years: int = 10,
    ) -> RevenueProjection:
        history = self._history(snapshots)
        revenue = [row.revenue for row in history]
        growth = [revenue[i] / revenue[i - 1] - D(1) for i in range(1, len(revenue))]
        excess = [item - industry.domestic_growth for item in growth]
        a3 = _mean(growth[-min(3, len(growth)):])
        a5 = _mean(growth[-min(5, len(growth)):])
        cagr = (revenue[-1] / revenue[0]) ** (D(1) / D(len(revenue) - 1)) - D(1)
        center = D(str(median(growth)))

        vote_level = (
            "high"
            if a3 - industry.domestic_growth > D("0.15")
            else "mid"
            if a3 - industry.domestic_growth >= 0
            else "low"
        )
        percentile = D(sum(1 for item in growth if item <= a3)) / D(len(growth))
        vote_percentile = (
            "high" if percentile >= D("0.70") else "mid" if percentile >= D("0.30") else "low"
        )
        trend_delta = a3 - a5
        trend = "high" if trend_delta > D("0.05") else "low" if trend_delta < D("-0.05") else "mid"
        position = vote_level if vote_level == vote_percentile else trend
        rho = self.POSITION_RHO[position]

        cycle = "up" if a3 >= cagr else "down"
        p25 = _percentile(excess, D("0.25"))
        p75 = _percentile(excess, D("0.75"))
        previous_down = self._previous_completed_cycle(excess, positive=False)
        previous_up = self._previous_completed_cycle(excess, positive=True)
        h_minus = industry.domestic_growth + (
            D("0.5") * (p25 + previous_down)
            if previous_down is not None
            else p25
        )
        h_plus = industry.domestic_growth + (
            D("0.5") * (p75 + previous_up)
            if previous_up is not None
            else p75
        )

        # In an up-cycle A3 is the optimistic anchor; in a down-cycle it is the
        # pessimistic anchor.  The opposite side comes from the historical cycle
        # anchor.  Clamp anchors around CAGR before applying the same rho formula,
        # so scenario identities are preserved without silently sorting outputs.
        pessimistic_anchor = a3 if cycle == "down" else h_minus
        optimistic_anchor = a3 if cycle == "up" else h_plus
        pessimistic_anchor = min(pessimistic_anchor, cagr)
        optimistic_anchor = max(optimistic_anchor, cagr)
        anchors = {
            "pessimistic": pessimistic_anchor,
            "base": cagr,
            "optimistic": optimistic_anchor,
        }
        pessimistic = center + rho * (pessimistic_anchor - center)
        neutral = center + rho * (cagr - center)
        optimistic = center + rho * (optimistic_anchor - center)
        pessimistic = max(pessimistic, min(growth))

        effective_decay = min(industry.decay_years, forecast_years - 1)
        decisions = [
            f"历史收入使用{len(history)}个连续年度，CAGR指数分母为N-1={len(history)-1}。",
            f"位置判断={position}，rho={rho}；水平票={vote_level}，分位票={vote_percentile}，趋势仲裁={trend}。",
            (
                f"当前周期={cycle}（A3{'≥' if cycle == 'up' else '<'}CAGR）；"
                "上行期以A3作为乐观锚、下行期以A3作为悲观锚，另一侧使用H+/H-。"
            ),
            (
                "历史周期口径=相对行业增速同号的连续区间；上一完整周期取该区间超额增速算术平均，"
                "当前尾部区间不纳入。"
            ),
        ]
        if previous_down is None:
            decisions.append("没有可识别的上一完整下行周期，H-按G+P25(超额增速)降级。")
        if previous_up is None:
            decisions.append("没有可识别的上一完整上行周期，H+按G+P75(超额增速)降级。")
        decisions.append(
            "三情景均使用start=center+rho×(anchor-center)；先约束悲观锚≤CAGR≤乐观锚，"
            "不通过排序改写情景身份。"
        )
        if effective_decay != industry.decay_years:
            decisions.append(
                f"参数库T={industry.decay_years}与N={forecast_years}相等；为保留终值收敛阶段，计算采用T_eff={effective_decay}，原值仍保留在审计记录。"
            )
        scenarios = {
            "pessimistic": self._path(
                pessimistic, industry.domestic_growth, terminal_growth, effective_decay, forecast_years
            ),
            "base": self._path(
                neutral, industry.domestic_growth, terminal_growth, effective_decay, forecast_years
            ),
            "optimistic": self._path(
                optimistic, industry.domestic_growth, terminal_growth, effective_decay, forecast_years
            ),
        }
        return RevenueProjection(
            scenarios=scenarios,
            annual_growth_history=growth,
            cagr=cagr,
            a3=a3,
            a5=a5,
            center=center,
            position=position,
            rho=rho,
            raw_decay_years=industry.decay_years,
            effective_decay_years=effective_decay,
            cycle=cycle,
            anchors=anchors,
            decisions=decisions,
        )
