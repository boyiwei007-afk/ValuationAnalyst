export function initialDraft(language) {
  const now = new Date()
  const date = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  return { language, mode: 'demo', company: language === 'en-US' ? 'Example Manufacturing Co.' : '示例制造企业', date, source: 'structured', ticker: '', assumptionSource: 'automatic', wacc: '9.5', growth: '3', revenueGrowth: '', years: '10', methods: ['dcf', 'pe', 'ev_ebitda'], fileIds: [], assumptionFileIds: [] }
}

export const STAGES = [
  ['data_intake', '资料与来源', 'Data & sources'],
  ['agent_planning', 'Agent 规划', 'Agent planning'],
  ['financial_validation', '财务审核', 'Financial review'],
  ['industry_parameters', '行业识别与参数', 'Industry parameters'],
  ['assumption_resolution', '经营假设', 'Assumptions'],
  ['financial_forecast', '现金流预测', 'Cash flow forecast'],
  ['dcf_valuation', 'DCF 估值', 'DCF valuation'],
  ['relative_valuation', '相对估值', 'Relative valuation'],
  ['sensitivity', '敏感性分析', 'Sensitivity'],
  ['reconciliation', '区间验证', 'Cross-validation'],
  ['reporting', '结果与依据', 'Results & evidence'],
]
export const TERMINAL = new Set(['completed', 'completed_with_warnings', 'waiting_review', 'failed', 'cancelled'])
export const statusLabel = (status, t) => ({
  created: t('等待执行', 'Queued'), running: t('执行中', 'Running'),
  completed: t('已完成', 'Completed'), completed_with_warnings: t('完成 · 有提示', 'Completed · Notes'),
  waiting_review: t('需要复核', 'Needs review'), failed: t('执行失败', 'Failed'),
  cancelled: t('已取消', 'Cancelled'), cached: t('已复用', 'Cached'),
}[status] || status || t('未开始', 'Not started'))
export const methodLabel = method => ({ dcf: 'DCF', pe: 'P/E', ps: 'P/S', ev_ebitda: 'EV/EBITDA' }[method] || method)
export const money = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits })
export const percent = value => value == null ? '—' : `${money(Number(value) * 100)}%`

// Preserve entered decimal precision. Number() is only used for chart/display values.
export function percentToDecimal(value) {
  const match = String(value).trim().match(/^(-?)(\d+)(?:\.(\d+))?$/)
  if (!match) throw new Error('请输入有效百分数 / Enter a valid percentage')
  const [, sign, integer, fraction = ''] = match
  const digits = (integer + fraction).padStart(fraction.length + 3, '0')
  const split = digits.length - fraction.length - 2
  return `${sign}${digits.slice(0, split).replace(/^0+(?=\d)/, '')}.${digits.slice(split)}`
}

export function decimalToPercent(value) {
  const match = String(value).match(/^(-?)(\d*)(?:\.(\d*))?$/)
  if (!match) return String(Number(value) * 100)
  const [, sign, whole, fraction = ''] = match
  const padded = fraction.padEnd(2, '0')
  return sign + (whole + padded.slice(0, 2)).replace(/^0+(?=\d)/, '') + (padded.slice(2) ? '.' + padded.slice(2) : '')
}

export function mergeEvents(previous, incoming) {
  return [...new Map([...previous, ...incoming].map(event => [event.sequence, event])).values()].sort((a, b) => a.sequence - b.sequence)
}

export function stageStates(events) {
  const states = Object.fromEntries(STAGES.map(([id]) => [id, { status: 'created', tools: [] }]))
  for (const event of events) {
    const stage = states[event.stage]
    if (!stage) continue
    if (event.type.startsWith('stage.')) stage.status = event.status
    if (event.type === 'review.required') stage.status = 'waiting_review'
    if (event.type === 'tool.failed') stage.status = 'failed'
    if (event.type.startsWith('tool.')) {
      const key = event.tool_call_id || `${event.tool}-${event.sequence}`
      const index = stage.tools.findIndex(tool => tool.key === key)
      const tool = { ...event, key }
      if (index >= 0) stage.tools[index] = tool
      else stage.tools.push(tool)
    }
  }
  return states
}

export function buildRequest(draft, base = {}) {
  const request = draft.mode === 'demo' ? {} : structuredClone(base)
  request.company = { ...(request.company || {}), name: draft.company.trim(), currency: request.company?.currency || 'CNY' }
  if (draft.source === 'ticker' && draft.mode !== 'demo') request.company.ticker = draft.ticker.trim()
  Object.assign(request, {
    valuation_date: draft.date, language: draft.language, mode: draft.mode,
    data_source: draft.mode === 'demo' ? 'structured' : draft.source,
    assumption_source: draft.assumptionSource, forecast_years: Number(draft.years),
    methods: draft.methods, discount_policy: draft.mode === 'demo' ? 'annual_midyear_remaining' : 'year_end', user_goal: draft.language === 'en-US' ? 'Build a traceable company valuation and explain the key assumptions.' : '完成可追溯的企业估值并解释关键假设',
    file_ids: draft.mode !== 'demo' && draft.source === 'upload' ? draft.fileIds : [],
    assumption_file_ids: draft.assumptionSource === 'upload' ? draft.assumptionFileIds : [],
  })
  request.assumptions = draft.assumptionSource === 'manual' ? {
    ...(request.assumptions || {}),
    wacc: percentToDecimal(draft.wacc), terminal_growth: percentToDecimal(draft.growth),
    ...(draft.revenueGrowth.trim() ? { revenue_growth: Array.from({ length: Number(draft.years) }, () => percentToDecimal(draft.revenueGrowth)) } : {}),
  } : {}
  if (draft.mode !== 'demo' && draft.source !== 'structured') {
    delete request.financials
    delete request.historical_financials
    delete request.peers
  }
  if (!request.company.name) throw new Error('请填写企业名称 / Company name is required')
  if (!request.methods.length) throw new Error('至少选择一种估值方法 / Select at least one method')
  if (request.data_source === 'ticker' && !request.company.ticker) throw new Error('请填写 A 股代码 / Enter an A-share ticker')
  if (request.data_source === 'upload' && !request.file_ids.length) throw new Error('请上传财务文件 / Upload financial data')
  if (draft.mode !== 'demo' && draft.source === 'structured' && !request.financials && !request.historical_financials?.length) throw new Error('请导入结构化请求文件 / Import a structured request')
  if (request.assumption_source === 'upload' && !request.assumption_file_ids.length) throw new Error('请上传假设文件 / Upload assumptions')
  return request
}

export function valuationRanges(result) {
  if (!result) return []
  return [ ...(result.dcf ? [{ method: 'dcf', ...result.dcf }] : []), ...result.relative ].filter(row => row.status === 'success' && row.range_low != null && row.range_high != null)
}

export function sensitivityGrid(cells) {
  return {
    rows: [...new Set(cells.map(c => c.wacc))].sort((a, b) => Number(a) - Number(b)),
    columns: [...new Set(cells.map(c => c.terminal_growth))].sort((a, b) => Number(a) - Number(b)),
    byKey: new Map(cells.map(c => [`${c.wacc}:${c.terminal_growth}`, c])),
    valid: cells.filter(c => c.valid && c.per_share_value != null),
  }
}

export function metricLabel(metric, t) {
  const names = {
    revenue: ['营业收入', 'Revenue'], ebit: ['息税前利润', 'EBIT'],
    ebit_margin: ['EBIT 利润率', 'EBIT margin'], tax_rate: ['有效所得税率', 'Effective tax rate'],
    depreciation_amortization: ['折旧与摊销', 'D&A'], capital_expenditure: ['资本开支', 'CapEx'],
    change_operating_nwc: ['经营性营运资本变动', 'Change in operating NWC'],
    cash_and_non_operating_assets: ['现金及非经营性资产', 'Cash & non-operating assets'],
    interest_bearing_debt: ['有息负债', 'Interest-bearing debt'], common_shares: ['普通股股数', 'Common shares'],
    net_income_parent: ['归母净利润', 'Parent net income'], ebitda: ['EBITDA', 'EBITDA'],
    pe: ['市盈率 P/E', 'P/E'], ps: ['市销率 P/S', 'P/S'], ev_ebitda: ['EV/EBITDA', 'EV/EBITDA'],
  }
  return names[metric] ? t(...names[metric]) : metric
}

export function studyLabel(parameter, t) {
  const parts = parameter.split(' · ')
  return parts.length === 2 ? `${parts[0]} · ${parts[1] === '可比倍数' ? t('可比倍数', 'Peer multiples') : metricLabel(parts[1], t)}` : parameter
}
