from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files
from typing import Any

D = Decimal
FINANCIAL_KEYWORDS = (
    "银行",
    "券商",
    "证券",
    "保险",
    "非银金融",
    "金融业",
    "金融服务",
)
CONSUMER_STAPLES_KEYWORDS = (
    "食品饮料",
)


class IndustryResolutionError(ValueError):
    pass


class FinancialIndustryUnsupported(IndustryResolutionError):
    pass


class AmbiguousIndustry(IndustryResolutionError):
    pass


@dataclass(frozen=True)
class IndustryParameters:
    industry_id: str
    category: str
    name: str
    domestic_growth: Decimal
    cycle_label: str
    window_years: int
    global_growth_check: Decimal
    lifecycle: str
    decay_years: int
    rnd_wage_factor: Decimal
    admin_wage_factor: Decimal
    beta_default: Decimal
    quality: str
    version: str
    source_file: str
    source_sha256: str
    metadata_completeness: str
    required_metadata_missing: tuple[str, ...]
    provenance_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "industry_id": self.industry_id,
            "category": self.category,
            "name": self.name,
            "domestic_growth": self.domestic_growth,
            "cycle_label": self.cycle_label,
            "window_years": self.window_years,
            "global_growth_check": self.global_growth_check,
            "lifecycle": self.lifecycle,
            "decay_years": self.decay_years,
            "rnd_wage_factor": self.rnd_wage_factor,
            "admin_wage_factor": self.admin_wage_factor,
            "beta_default": self.beta_default,
            "quality": self.quality,
            "version": self.version,
            "source_file": self.source_file,
            "source_sha256": self.source_sha256,
            "metadata_completeness": self.metadata_completeness,
            "required_metadata_missing": list(self.required_metadata_missing),
            "provenance_note": self.provenance_note,
        }


def _normalize(value: str) -> str:
    return re.sub(r"[\s/／、·（）()_-]+", "", value).lower()


class IndustryParameterRegistry:
    """Versioned, deterministic lookup over the finance-team workbook snapshot."""

    def __init__(self, payload: dict[str, Any] | None = None):
        if payload is None:
            resource = files("valuationagent.finance").joinpath(
                "data/industry_parameters_v2.json"
            )
            payload = json.loads(resource.read_text(encoding="utf-8"), parse_float=str)
        self.version = str(payload["version"])
        self.source_file = str(payload["source_file"])
        self.source_sha256 = str(payload["source_sha256"])
        self.metadata_completeness = str(
            payload.get("metadata_completeness", "unknown")
        )
        self.required_metadata_missing = tuple(
            str(item) for item in payload.get("required_metadata_missing", [])
        )
        self._rows = list(payload["industries"])

    def resolve(self, label: str | None) -> IndustryParameters:
        raw = (label or "").strip()
        if not raw:
            raise IndustryResolutionError(
                "缺少行业。请提供申万一级行业或主营业务，确认后才能查询行业参数库。"
            )
        if any(word in raw for word in FINANCIAL_KEYWORDS):
            raise FinancialIndustryUnsupported(
                "当前项目不研究银行、券商、保险及其他金融行业，通用FCFF模型不适用。"
            )
        key = _normalize(raw)
        exact: list[dict[str, Any]] = []
        partial: list[dict[str, Any]] = []
        for row in self._rows:
            candidates = [row["id"], row["name"], *row.get("aliases", [])]
            normalized = [_normalize(str(item)) for item in candidates]
            if key in normalized:
                exact.append(row)
            elif any(item and (item in key or key in item) for item in normalized):
                partial.append(row)
        matches = exact or partial
        unique = {row["id"]: row for row in matches}
        if len(unique) > 1:
            names = "、".join(row["name"] for row in unique.values())
            raise AmbiguousIndustry(
                f"行业描述“{raw}”可对应多个参数行：{names}。请补充主营业务或明确选择。"
            )
        fallback = False
        if not unique and any(word in raw for word in CONSUMER_STAPLES_KEYWORDS):
            # The checked-in source workbook snapshot has no dedicated food or
            # beverage row. Reuse its industrial fallback mechanics instead of
            # inventing unsupported industry statistics, and downgrade the row
            # so every report makes the limitation explicit.
            row = next(item for item in self._rows if item["id"] == "industrial_fallback")
            unique = {row["id"]: row}
            fallback = True
        if not unique:
            raise IndustryResolutionError(
                f"行业参数库无法匹配“{raw}”。请在制造业/服务业兜底与具体行业之间确认。"
            )
        row = next(iter(unique.values()))
        fallback_note = (
            "参数库缺少食品饮料/白酒专属行；本次透明复用原始工业兜底参数，"
            "仅供情景起点，质量降为C，提交前应以可核验行业资料替换。"
            if fallback else ""
        )
        return IndustryParameters(
            industry_id="consumer_staples_fallback" if fallback else row["id"],
            category="消费品" if fallback else row["category"],
            name="食品饮料 / 白酒（保守兜底）" if fallback else row["name"],
            domestic_growth=D(str(row["domestic_growth"])),
            cycle_label=row["cycle_label"],
            window_years=int(row["window_years"]),
            global_growth_check=D(str(row["global_growth_check"])),
            lifecycle=row["lifecycle"],
            decay_years=int(row["decay_years"]),
            rnd_wage_factor=D(str(row["rnd_wage_factor"])),
            admin_wage_factor=D(str(row["admin_wage_factor"])),
            beta_default=D(str(row["beta_default"])),
            quality="C" if fallback else row["quality"],
            version=self.version,
            source_file=self.source_file,
            source_sha256=self.source_sha256,
            metadata_completeness="fallback" if fallback else self.metadata_completeness,
            required_metadata_missing=(
                (*self.required_metadata_missing, "dedicated_industry_parameters")
                if fallback else self.required_metadata_missing
            ),
            provenance_note=fallback_note,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_file": self.source_file,
            "source_sha256": self.source_sha256,
            "financial_industry_supported": False,
            "industry_count": len(self._rows),
            "metadata_completeness": self.metadata_completeness,
            "required_metadata_missing": list(self.required_metadata_missing),
        }
