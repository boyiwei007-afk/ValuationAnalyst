from __future__ import annotations

from pydantic import Field

from valuationagent.core.tools import NoArguments, ToolSpec
from valuationagent.finance.industry import IndustryParameterRegistry
from valuationagent.schemas.models import ApiModel


class IndustryLookup(ApiModel):
    industry: str = Field(
        min_length=1,
        max_length=120,
        description="已从公司资料识别出的申万行业或主营业务标签；有歧义时先向用户确认。",
    )


class FinanceResearchToolProvider:
    provider_id = "finance_team_model_tools"
    version = "1.0.0"

    def __init__(self, registry: IndustryParameterRegistry | None = None):
        self.registry = registry or IndustryParameterRegistry()

    def tool_specs(self, session):
        def lookup(args: IndustryLookup):
            row = self.registry.resolve(args.industry)
            return {
                "ok": True,
                "industry": row.as_dict(),
                "rule": "LLM只负责提出行业标签；数值由版本化参数库确定性返回。",
                "requires_confirmation": False,
            }

        def describe(_):
            return {
                "ok": True,
                "model_scope": "A股非金融行业",
                "forecast_years": 10,
                "discount_policy": "year_end",
                "parameter_registry": self.registry.describe(),
                "financial_industry_supported": False,
            }

        return [
            ToolSpec(
                "lookup_industry_parameters",
                "在已确认行业后查询金融小组参数库，返回G、景气窗口、T、工资系数、beta与来源版本；禁止猜测行业。",
                IndustryLookup,
                lookup,
            ),
            ToolSpec(
                "inspect_finance_model_scope",
                "读取正式金融模型的适用范围、预测期、折现政策和参数库版本。",
                NoArguments,
                describe,
            ),
        ]
