import { metricLabel } from './domain'

const pct = value => value == null ? '—' : `${(Number(value) * 100).toFixed(2)}%`
const scenarios = ['pessimistic', 'base', 'optimistic']

export default function ValuationPlanReview({ plan, t }) {
  if (!plan) return null
  const assumptions = plan.assumptions || {}
  const financials = Object.entries(plan.financials || {}).filter(([, value]) => typeof value === 'string' && /^-?\d+(\.\d+)?$/.test(value))
  return <section className="valuation-plan-review" aria-label={t('待确认估值方案', 'Valuation plan for review')}>
    <p className="valuation-ready-state">{t(`系统准备检查已通过 · 本次集中确认 ${(plan.staged_fact_ids || []).length} 个输入`, `System readiness check passed · ${(plan.staged_fact_ids || []).length} input(s) in this combined review`)}</p>
    <dl className="scope-preview">
      <div><dt>{t('估值方法', 'Methods')}</dt><dd>{(plan.methods || []).join(' / ').toUpperCase()}</dd></div>
      <div><dt>{t('财务基期', 'Financial baseline')}</dt><dd>{plan.baseline_period || t('在线取数', 'Online data')}</dd></div>
      <div><dt>{t('估值日', 'Valuation date')}</dt><dd>{plan.valuation_date}</dd></div>
      <div><dt>WACC / g</dt><dd>{pct(assumptions.wacc)} / {pct(assumptions.terminal_growth)}</dd></div>
    </dl>
    {!!Object.keys(plan.excluded_methods || {}).length && <div className="valuation-method-fallback">
      <b>{t('数据缺失降级方案', 'Data-limited fallback plan')}</b>
      <p>{t('以下原选方法不会阻塞本次估值，也不会使用猜测数据；确认后仅计算上方可执行方法。', 'The methods below will not block this valuation and no guessed data will be used. Confirmation runs only the executable methods shown above.')}</p>
      <ul>{Object.entries(plan.excluded_methods).map(([method, reason]) => <li key={method}><strong>{method.toUpperCase()}</strong>：{reason}</li>)}</ul>
    </div>}
    <p>{t('预测是模型判断，不是历史事实。确认后直接进入计算；财务校验仍可能要求修正。', 'Forecasts are assumptions, not historical facts. Confirmation starts calculation; financial validation may still require corrections.')}</p>
    <p>{plan.forecast_rationale}</p>
    {['revenue_growth_scenarios', 'ebit_margin_scenarios'].map(key => assumptions[key] && <div className="table-scroll" key={key}><table>
      <caption>{key === 'revenue_growth_scenarios' ? t('十年收入增长假设', 'Ten-year revenue growth assumptions') : t('十年 EBIT 利润率假设', 'Ten-year EBIT margin assumptions')}</caption>
      <thead><tr><th>{t('预测年', 'Forecast year')}</th>{scenarios.map((name, i) => <th key={name}>{[t('悲观', 'Pessimistic'), t('基准', 'Base'), t('乐观', 'Optimistic')][i]}</th>)}</tr></thead>
      <tbody>{(assumptions[key].base || []).map((_, i) => <tr key={i}><th>{i + 1}</th>{scenarios.map(name => <td key={name}>{pct(assumptions[key][name]?.[i])}</td>)}</tr>)}</tbody>
    </table></div>)}
    {!!financials.length && <details><summary>{t('查看本次计算的基期输入（金额：元；股数：股；比例：小数）', 'Baseline inputs (CNY, shares and decimal ratios)')}</summary><dl className="scope-preview">{financials.map(([key, value]) => <div key={key}><dt>{metricLabel(key, t)}</dt><dd>{value}</dd></div>)}</dl></details>}
    {!!plan.risks?.length && <ul>{plan.risks.map((risk, i) => <li key={i}>{risk}</li>)}</ul>}
  </section>
}
