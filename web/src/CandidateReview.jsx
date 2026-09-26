import { useState } from 'react'
import { metricLabel } from './domain'

export default function CandidateReview({ facts, t, onSource, onCorrect, busy }) {
  const [selected, setSelected] = useState(null)
  const [period, setPeriod] = useState('all')
  const [pendingOnly, setPendingOnly] = useState(false)
  const periods = [...new Set(facts.filter(f => f.status !== 'rejected').map(f => f.period))].sort().reverse()
  const visible = facts.filter(f => f.status !== 'rejected' && (!pendingOnly || f.status === 'proposed') && (period === 'all' || f.period === period))
  const columns = period === 'all' ? periods : [period]
  const rowKey = f => `${f.peer_ticker || f.peer_name || ''}:${f.metric}`
  const rowTitle = f => [f.peer_name, f.peer_ticker, metricLabel(f.metric, t)].filter(Boolean).join(' ')
  const unitTitle = f => f.unit === 'ratio' ? (f.role === 'comparable' ? t('倍', '×') : t('比例', 'ratio')) : f.unit
  const rows = [...new Map(visible.map(f => [rowKey(f), f])).values()]
  const chosen = visible.find(f => f.fact_id === selected) || visible.find(f => f.warnings.length) || visible[0]
  if (!facts.length) return <p className="research-empty">{t('上传资料后，Agent 将提取有原文依据的候选字段。确认前不会进入计算。', 'Attach sources for evidence-backed extraction. Proposed values are not used in calculations.')}</p>
  return <div className="candidate-review">
    <div className="candidate-filters"><select aria-label={t('按期间筛选字段', 'Filter candidates by period')} value={period} onChange={e => setPeriod(e.target.value)}><option value="all">{t('全部期间', 'All periods')}</option>{periods.map(p => <option key={p}>{p}</option>)}</select><label><input type="checkbox" checked={pendingOnly} onChange={e => setPendingOnly(e.target.checked)}/>{t('仅待复核', 'Pending only')}</label></div>
    <p className="field-hint">{t('点击数值核对原文。警告字段不能批量确认，修改会保留旧记录。', 'Select a value to inspect its evidence. Warned values cannot be batch-approved; corrections preserve history.')}</p>
    <div className="table-scroll"><table className="candidate-grid"><thead><tr><th>{t('字段 / 公司', 'Metric / peer')}</th>{columns.map(p => <th key={p}>{p}</th>)}</tr></thead><tbody>{rows.map(row => <tr key={rowKey(row)}><th>{rowTitle(row)}</th>{columns.map(p => <td key={p}>{visible.filter(f => rowKey(f) === rowKey(row) && f.period === p).map(f => <button className={`${f.status} ${f.warnings.length ? 'has-warning' : ''} ${chosen?.fact_id === f.fact_id ? 'selected' : ''}`} key={f.fact_id} onClick={() => setSelected(f.fact_id)} aria-label={`${rowTitle(f)} ${p}: ${f.raw_value} ${unitTitle(f)}`}><b>{f.raw_value}</b><small>{unitTitle(f)} {f.warnings.length ? '⚠' : f.status === 'confirmed' ? '✓' : ''}</small></button>)}</td>)}</tr>)}</tbody></table></div>
    {!visible.length && <p className="field-hint">{t('当前筛选下没有候选字段。', 'No candidates match these filters.')}</p>}
    {chosen && <section className={`research-fact ${chosen.status}`}><div><b>{rowTitle(chosen)}</b><span>{chosen.status === 'confirmed' ? t('已确认', 'Confirmed') : t('待复核', 'Proposed')}</span></div><strong>{chosen.raw_value} <small>{unitTitle(chosen)}</small></strong><p>{chosen.period} · {chosen.scope === 'consolidated' ? t('合并口径', 'Consolidated') : chosen.scope === 'parent' ? t('母公司口径', 'Parent') : chosen.scope === 'issuer' ? t('发行人股数', 'Issuer shares') : t('口径待确认', 'Unconfirmed scope')}</p><blockquote>{chosen.quote}</blockquote>{chosen.source_location?.page && <p>{t('原文页码', 'Source page')} {chosen.source_location.page}</p>}{chosen.source_location?.sheet && <p>{chosen.source_location.sheet} · {chosen.source_location.cell || chosen.source_location.row}</p>}{chosen.warnings.map(w => <p className="research-warning" key={w}>{w}</p>)}<div className="inline-actions">{chosen.source_type === 'document' && <button onClick={() => onSource(chosen)}>{t('查看原文', 'View source')}</button>}<button disabled={busy} onClick={() => onCorrect(chosen)}>{t('提出更正', 'Request correction')}</button></div></section>}
  </div>
}
