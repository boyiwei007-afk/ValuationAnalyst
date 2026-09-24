"""Formal, traceable XLSX and PDF exports for completed valuation runs."""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from pathlib import Path

from valuationagent.schemas.models import RunRecord


def _number(value):
    return float(value) if isinstance(value, Decimal) else value


def _completed(record: RunRecord):
    if record.result is None:
        raise ValueError("估值尚未完成，不能导出正式报告。")
    return record.result


class ValuationReportExporter:
    """Generate reviewer-friendly reports without changing model results."""

    def xlsx(self, record: RunRecord) -> bytes:
        try:
            from openpyxl import Workbook
            from openpyxl.chart import BarChart, Reference
            from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
            from openpyxl.utils import get_column_letter
        except ImportError as exc:
            raise ValueError("Excel报告组件未安装，请安装 valuationagent[reports]。") from exc

        result = _completed(record)
        wb = Workbook()
        wb.remove(wb.active)
        navy, teal, pale, amber = "14273D", "0F8B8D", "EAF4F4", "FFF3CD"
        thin = Side(style="thin", color="D7DEE7")

        def sheet(name, widths=(24, 24, 48)):
            ws = wb.create_sheet(name)
            ws.sheet_view.showGridLines = False
            ws.freeze_panes = "A2"
            for index, width in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(index)].width = width
            return ws

        def header(ws, row=1):
            for cell in ws[row]:
                cell.fill = PatternFill("solid", fgColor=navy)
                cell.font = Font(color="FFFFFF", bold=True)
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border = Border(bottom=thin)
            ws.row_dimensions[row].height = 28

        def rows(ws, values):
            for value in values:
                ws.append([_number(item) for item in value])
            for row in ws.iter_rows():
                for cell in row:
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
                    cell.border = Border(bottom=thin)

        summary = sheet("估值摘要", (24, 28, 55))
        rows(summary, [
            ["项目", "结果", "说明"],
            ["公司", result.company.name or result.company.ticker, result.company.ticker or ""],
            ["估值基准日", str(result.valuation_date), "所有市场与公开信息不得晚于该日"],
            ["模型版本", result.model_version, f"运行ID：{record.run_id} · 修订v{record.revision}"],
            ["数据质量置信度", result.data_quality.confidence,
             f"可比历史 {result.data_quality.comparable_years}/{result.data_quality.historical_years} 年"],
            ["证据覆盖率", result.data_quality.evidence_coverage,
             "核心字段中带逐项证据的比例"],
            ["可比样本质量", result.data_quality.peer_sample_quality,
             f"行业参数等级 {result.data_quality.industry_parameter_quality} · 元数据 {result.data_quality.industry_metadata_completeness}"],
            ["DCF每股价值", result.dcf.per_share_value if result.dcf else None,
             f"区间 {result.dcf.range_low} – {result.dcf.range_high}" if result.dcf else "未采用"],
            ["退出倍数交叉校验", result.dcf.exit_multiple_per_share if result.dcf else None,
             (
                 f"相对Gordon差异 {result.dcf.terminal_method_gap:.2%}；仅作校验"
                 if result.dcf and result.dcf.terminal_method_gap is not None
                 else "无文档支持的行业倍数或未采用DCF"
             )],
            ["相对估值区间", result.reconciliation.relative_range[0] if result.reconciliation.relative_range else None,
             (
                 f"{result.reconciliation.relative_range[0]:.2f} – "
                 f"{result.reconciliation.relative_range[1]:.2f}"
                 if result.reconciliation.relative_range
                 else "没有成功方法"
             )],
            ["交叉验证", result.reconciliation.conclusion, "不同方法保持独立，不强行平均"],
            ["结论", result.executive_summary, ""],
        ])
        header(summary)
        summary["B6"].number_format = "0.00%"
        summary["B8"].number_format = '¥#,##0.00'

        assumptions = sheet("关键假设", (26, 22, 70))
        assumption_rows = [
            ["参数", "数值", "依据/口径"],
            ["WACC", result.assumptions.wacc, result.assumptions.rationale.get("wacc", "")],
            ["永续增长率", result.assumptions.terminal_growth, result.assumptions.rationale.get("terminal_growth", "")],
            ["假设来源", result.assumptions.source, ""],
        ]
        assumption_rows += [[f"WACC组成 · {key}", value, ""] for key, value in result.assumptions.wacc_components.items()]
        assumption_rows += [[f"行业参数 · {key}", str(value), ""] for key, value in result.assumptions.industry_parameters.items()]
        assumption_rows += [[f"经营驱动 · {key}", value, ""] for key, value in result.assumptions.operating_drivers.items()]
        assumption_rows += [[f"计算方法 · {key}", value, ""] for key, value in result.assumptions.calculation_methods.items()]
        assumption_rows += [["模型决定", item, ""] for item in result.assumptions.model_decisions]
        rows(assumptions, assumption_rows)
        header(assumptions)
        for cell in (assumptions["B2"], assumptions["B3"]):
            cell.number_format = "0.00%"
            cell.font = Font(color="1F4E78")

        historical = sheet("历史财务", (16, 20, 20, 20, 20, 20, 18, 45, 32))
        rows(historical, [["报告期", "营业收入", "EBIT率", "归母净利润", "EBITDA", "资本开支", "可比状态", "可比说明", "来源"]] + [
            [item.period_end, item.revenue, item.ebit_margin, item.net_income_parent,
             item.ebitda, item.capital_expenditure, item.comparability_status,
             item.comparability_note, item.source_label]
            for item in [*record.request.historical_financials,
                         *([result.effective_financials] if result.effective_financials else [])]
        ])
        header(historical)
        for row in range(2, historical.max_row + 1):
            historical.cell(row, 3).number_format = "0.00%"
            for col in (2, 4, 5, 6):
                historical.cell(row, col).number_format = '#,##0.00'

        forecast = sheet("预测与FCFF", (12, 16, 16, 16, 16, 16, 16, 16, 42, 16, 16, 48))
        rows(forecast, [["年度", "收入增长率", "营业收入", "EBIT率", "EBIT", "NOPAT", "折旧摊销", "FCFF", "审计复算公式", "资本开支", "Δ经营营运资本", "计算口径"]] + [
            [item.year, item.revenue_growth, item.revenue, item.ebit_margin, item.ebit,
             item.nopat, item.depreciation_amortization, item.fcff,
             f"=F{row}+G{row}-J{row}-K{row}", item.capital_expenditure,
             item.change_operating_nwc,
             "；".join(f"{key}={value}" for key, value in item.calculation_methods.items())]
            for row, item in enumerate(result.forecast, 2)
        ])
        header(forecast)
        for row in range(2, forecast.max_row + 1):
            forecast.cell(row, 2).number_format = forecast.cell(row, 4).number_format = "0.00%"
            for col in (*range(3, 9), 10, 11):
                forecast.cell(row, col).number_format = '#,##0.00'
            forecast.cell(row, 9).font = Font(color="008000")

        relative = sheet("可比公司", (16, 24, 16, 16, 16, 18, 16, 16, 16, 70))
        rows(relative, [["代码", "公司", "P/E", "P/S", "EV/EBITDA", "层级", "筛选得分", "收入增长", "EBIT率", "筛选依据"]] + [
            [peer.ticker, peer.name, peer.pe, peer.ps, peer.ev_ebitda,
             peer.peer_tier, peer.selection_score, peer.revenue_growth,
             peer.ebit_margin, peer.rationale]
            for peer in result.effective_peers
        ] + [["估值结果", item.method.upper(), item.per_share_value,
               f"{item.range_low} – {item.range_high}" if item.range_low is not None else "",
               item.sample_size, item.sample_quality, item.outlier_count,
               item.statistic, ", ".join(item.peer_tickers), item.reason or ""]
              for item in result.relative])
        header(relative)

        sensitivity = sheet("敏感性分析", (16, 18, 18, 18, 18, 18))
        growths = sorted({cell.terminal_growth for cell in result.sensitivity})
        waccs = sorted({cell.wacc for cell in result.sensitivity})
        table = [["WACC / 永续增长率", *growths]]
        lookup = {(cell.wacc, cell.terminal_growth): cell for cell in result.sensitivity}
        for wacc in waccs:
            table.append([wacc, *[
                lookup[(wacc, growth)].per_share_value if lookup[(wacc, growth)].valid else "无效"
                for growth in growths
            ]])
        rows(sensitivity, table)
        header(sensitivity)
        for row in range(2, sensitivity.max_row + 1):
            sensitivity.cell(row, 1).number_format = "0.00%"
        for col in range(2, sensitivity.max_column + 1):
            sensitivity.cell(1, col).number_format = "0.00%"
            for row in range(2, sensitivity.max_row + 1):
                sensitivity.cell(row, col).number_format = '¥#,##0.00'

        if result.sensitivity_studies:
            sensitivity.append([])
            sensitivity.append([
                "编号", "参数", "基准输入", "低值输入", "高值输入", "基准每股",
                "低值每股", "高值每股", "最大相对变动", "影响等级", "状态", "说明",
            ])
            detail_header = sensitivity.max_row
            for study in result.sensitivity_studies:
                sensitivity.append([
                    study.study_id, study.parameter, study.baseline_input,
                    study.low_input, study.high_input, study.baseline_per_share,
                    study.low_per_share, study.high_per_share,
                    study.max_relative_change, study.classification,
                    study.status, study.rationale,
                ])
            header(sensitivity, detail_header)
            for row in range(detail_header + 1, sensitivity.max_row + 1):
                for col in (6, 7, 8):
                    sensitivity.cell(row, col).number_format = '¥#,##0.00'
                sensitivity.cell(row, 9).number_format = "0.00%"
            sensitivity.column_dimensions["L"].width = 70

            completed_rows = [
                (study.study_id + " " + study.parameter, study.max_relative_change)
                for study in result.sensitivity_studies
                if study.status == "completed" and study.max_relative_change is not None
            ]
            completed_rows.sort(key=lambda item: item[1], reverse=True)
            if completed_rows:
                chart_start = sensitivity.max_row + 3
                sensitivity.cell(chart_start, 1, "敏感性因素")
                sensitivity.cell(chart_start, 2, "最大相对变动")
                for offset, (label, impact) in enumerate(completed_rows, 1):
                    sensitivity.cell(chart_start + offset, 1, label)
                    sensitivity.cell(chart_start + offset, 2, _number(impact))
                    sensitivity.cell(chart_start + offset, 2).number_format = "0.00%"
                chart = BarChart()
                chart.type = "bar"
                chart.style = 10
                chart.title = "敏感性龙卷风图（按最大相对变动排序）"
                chart.y_axis.title = "参数"
                chart.x_axis.title = "相对估值变动"
                chart.height = 8
                chart.width = 15
                chart.add_data(
                    Reference(sensitivity, min_col=2, min_row=chart_start,
                              max_row=chart_start + len(completed_rows)),
                    titles_from_data=True,
                )
                chart.set_categories(
                    Reference(sensitivity, min_col=1, min_row=chart_start + 1,
                              max_row=chart_start + len(completed_rows))
                )
                sensitivity.add_chart(chart, "N2")

        sources = sheet("来源与风险", (22, 28, 78))
        source_rows = [["类型", "字段/编号", "来源、依据或提醒"]]
        for field, refs in result.effective_financials.evidence.items() if result.effective_financials else []:
            for ref in refs:
                source_rows.append(["财务证据", field, f"{ref.source} · {ref.note}"])
        for field, refs in result.assumption_evidence.items():
            for ref in refs:
                source_rows.append(["假设证据", field, f"{ref.source} · {ref.note}"])
        source_rows += [
            ["数据质量", f"Q{index}", note]
            for index, note in enumerate(result.data_quality.notes, 1)
        ]
        source_rows += [["风险/限制", f"W{index}", warning] for index, warning in enumerate(result.warnings, 1)]
        if not result.warnings:
            source_rows.append(["风险/限制", "", "本次运行未产生系统警告；仍需人工复核关键假设与同业口径。"])
        rows(sources, source_rows)
        header(sources)

        for ws in wb.worksheets:
            ws.auto_filter.ref = ws.dimensions
            for row in range(2, ws.max_row + 1):
                if row % 2 == 0:
                    for cell in ws[row]:
                        cell.fill = PatternFill("solid", fgColor=pale)
            ws.sheet_properties.pageSetUpPr.fitToPage = True
            ws.page_setup.fitToWidth = 1
            ws.page_setup.fitToHeight = 0
        summary["A1"].fill = PatternFill("solid", fgColor=teal)
        sources["A2"].fill = PatternFill("solid", fgColor=amber)
        wb.calculation.fullCalcOnLoad = True
        wb.calculation.forceFullCalc = True
        output = BytesIO()
        wb.save(output)
        return output.getvalue()

    def pdf(self, record: RunRecord) -> bytes:
        try:
            from reportlab.lib import colors
            from reportlab.lib.enums import TA_CENTER
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
            from reportlab.lib.units import mm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFError, TTFont
            from reportlab.platypus import (
                KeepTogether,
                PageBreak,
                Paragraph,
                SimpleDocTemplate,
                Spacer,
                Table,
                TableStyle,
            )
        except ImportError as exc:
            raise ValueError("PDF报告组件未安装，请安装 valuationagent[reports]。") from exc

        result = _completed(record)
        font_path = next((path for path in [
            Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")
        ] if path.is_file()), None)
        font = "Helvetica"
        if font_path:
            try:
                pdfmetrics.registerFont(TTFont("ValuationCN", str(font_path), subfontIndex=0))
                font = "ValuationCN"
            except (OSError, TTFError, ValueError):
                font = "Helvetica"
        styles = getSampleStyleSheet()
        title = ParagraphStyle("TitleCN", parent=styles["Title"], fontName=font,
                               fontSize=22, leading=30, textColor=colors.HexColor("#14273D"),
                               alignment=TA_CENTER, spaceAfter=12)
        h2 = ParagraphStyle("H2CN", parent=styles["Heading2"], fontName=font,
                            fontSize=14, textColor=colors.HexColor("#0F6F72"), spaceBefore=10, spaceAfter=7)
        body = ParagraphStyle("BodyCN", parent=styles["BodyText"], fontName=font,
                              fontSize=9, leading=14, textColor=colors.HexColor("#24364B"))
        small = ParagraphStyle("SmallCN", parent=body, fontSize=7.5, leading=11)
        table_header = ParagraphStyle(
            "TableHeaderCN", parent=small, textColor=colors.white
        )

        def p(value, style=body):
            text = str(value if value is not None else "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return Paragraph(text, style)

        def table(data, widths=None, header=True):
            rendered = []
            for row_index, row in enumerate(data):
                style = table_header if header and row_index == 0 else small
                rendered.append([p(cell, style) for cell in row])
            t = Table(rendered, colWidths=widths, repeatRows=1 if header else 0)
            commands = [
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#D7DEE7")),
                ("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [colors.white, colors.HexColor("#F2F7F8")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
            if header:
                commands += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#14273D")),
                             ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
            t.setStyle(TableStyle(commands))
            return t

        output = BytesIO()
        doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=16*mm, leftMargin=16*mm,
                                topMargin=16*mm, bottomMargin=16*mm,
                                title=f"{result.company.name or result.company.ticker}估值报告")
        story = [p("ValuationAgent 正式估值报告", title),
                 p(f"{result.company.name or ''} · {result.company.ticker or ''} · 基准日 {result.valuation_date}"),
                 Spacer(1, 5*mm), p("估值摘要", h2)]
        story.append(table([
            ["方法", "每股价值", "估值区间/状态"],
            ["DCF", result.dcf.per_share_value if result.dcf else "—",
             f"{result.dcf.range_low} – {result.dcf.range_high}" if result.dcf else "未采用"],
            *([["退出倍数校验", result.dcf.exit_multiple_per_share,
                f"相对Gordon差异 {result.dcf.terminal_method_gap:.2%}；不替代主估值"]]
              if result.dcf and result.dcf.exit_multiple_per_share is not None else []),
            *[[item.method.upper(), item.per_share_value or "—",
               f"{item.range_low} – {item.range_high}" if item.status == "success" else item.reason]
              for item in result.relative],
        ], [35*mm, 38*mm, 85*mm]))
        story += [Spacer(1, 3*mm), p(result.executive_summary), p("数据质量", h2), table([
            ["指标", "结论", "说明"],
            ["总体置信度", result.data_quality.confidence,
             f"可比历史 {result.data_quality.comparable_years}/{result.data_quality.historical_years} 年"],
            ["证据覆盖率", f"{result.data_quality.evidence_coverage:.2%}",
             f"行业参数 {result.data_quality.industry_parameter_quality} · 元数据 {result.data_quality.industry_metadata_completeness}"],
            ["可比样本", result.data_quality.peer_sample_quality,
             "；".join(result.data_quality.notes) or "未产生额外质量提示"],
        ], [38*mm, 34*mm, 86*mm]), p("关键假设", h2), table([
            ["参数", "数值", "依据"],
            ["WACC", f"{result.assumptions.wacc:.2%}", result.assumptions.rationale.get("wacc", "")],
            ["永续增长率", f"{result.assumptions.terminal_growth:.2%}", result.assumptions.rationale.get("terminal_growth", "")],
            ["假设来源", result.assumptions.source, "模型与输入审计轨迹保存在系统中"],
        ], [38*mm, 34*mm, 86*mm]), p("十年预测", h2)]
        story.append(table([["年", "收入增长", "EBIT率", "收入", "FCFF"]] + [
            [item.year, f"{item.revenue_growth:.2%}", f"{item.ebit_margin:.2%}",
             f"{item.revenue:,.0f}", f"{item.fcff:,.0f}"] for item in result.forecast
        ], [19*mm, 29*mm, 26*mm, 43*mm, 41*mm]))
        story += [PageBreak(), p("可比公司与相对估值", h2)]
        story.append(table([["代码", "公司", "P/E", "P/S", "EV/EBITDA", "层级/得分"]] + [
            [peer.ticker, peer.name, peer.pe or "—", peer.ps or "—", peer.ev_ebitda or "—",
             f"{peer.peer_tier} / {peer.selection_score if peer.selection_score is not None else '—'}"]
            for peer in result.effective_peers
        ], [24*mm, 38*mm, 22*mm, 22*mm, 27*mm, 31*mm]))
        story += [p("敏感性分析", h2)]
        growths = sorted({cell.terminal_growth for cell in result.sensitivity})
        waccs = sorted({cell.wacc for cell in result.sensitivity})
        lookup = {(cell.wacc, cell.terminal_growth): cell for cell in result.sensitivity}
        sensitivity_rows = [["WACC / g", *[f"{g:.2%}" for g in growths]]]
        for wacc in waccs:
            sensitivity_rows.append([f"{wacc:.2%}", *[
                f"{lookup[(wacc, growth)].per_share_value:.2f}" if lookup[(wacc, growth)].valid else "无效"
                for growth in growths]])
        story.append(table(sensitivity_rows))
        if result.sensitivity_studies:
            story += [p("S1–S20 敏感性目录", h2)]
            impact_rows = [["编号", "参数", "低值", "基准", "高值", "影响", "状态"]]
            for study in result.sensitivity_studies:
                impact_rows.append([
                    study.study_id,
                    study.parameter,
                    f"{study.low_per_share:.2f}" if study.low_per_share is not None else "—",
                    f"{study.baseline_per_share:.2f}" if study.baseline_per_share is not None else "—",
                    f"{study.high_per_share:.2f}" if study.high_per_share is not None else "—",
                    (
                        f"{study.max_relative_change:.2%} / {study.classification}"
                        if study.max_relative_change is not None else "未测试"
                    ),
                    study.status,
                ])
            story.append(table(
                impact_rows,
                [14*mm, 37*mm, 21*mm, 21*mm, 21*mm, 28*mm, 22*mm],
            ))
            unavailable_notes = [
                f"{study.study_id} {study.parameter}：{study.rationale}"
                for study in result.sensitivity_studies
                if study.status == "not_available"
            ]
            if unavailable_notes:
                story.append(KeepTogether([
                    p("未完成敏感性项及原因", h2),
                    p(f"• {unavailable_notes[0]}", small),
                ]))
                story += [p(f"• {note}", small) for note in unavailable_notes[1:]]
        story += [p("风险、限制与追溯", h2)]
        notes = result.warnings or ["系统未产生运行警告；关键假设、同业口径与业务判断仍需人工复核。"]
        story += [p(f"• {note}") for note in notes]
        story += [Spacer(1, 3*mm), p(
            f"模型版本：{result.model_version}　运行ID：{record.run_id}　输入哈希：{result.effective_input_hash or result.input_hash}", small
        )]
        doc.build(story)
        return output.getvalue()

    def export(self, record: RunRecord, format: str) -> tuple[bytes, str, str]:
        selected = format.lower().lstrip(".")
        if selected == "xlsx":
            return self.xlsx(record), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
        if selected == "pdf":
            return self.pdf(record), "application/pdf", "pdf"
        if selected == "json":
            return record.result.model_dump_json(indent=2).encode("utf-8"), "application/json", "json"
        raise ValueError("导出格式仅支持 json、xlsx 或 pdf。")
