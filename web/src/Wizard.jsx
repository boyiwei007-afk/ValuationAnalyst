import { useState } from 'react'
import Icon from './Icons'
import { uploadFile } from './api'
import { buildRequest, decimalToPercent, methodLabel } from './domain'
import { ErrorNotice } from './ui'


export default function Wizard({ draft, setDraft, t, onStart, submitting, onLanguage }) {
  const [step, setStep] = useState(0)
  const [base, setBase] = useState({})
  const [files, setFiles] = useState({})
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const update = (key, value) => setDraft(previous => ({ ...previous, [key]: value }))
  const loadStructured = value => {
    if (!value.company || (!value.financials && !value.historical_financials?.length)) throw new Error(t('需要包含 company 和 financials 的请求 JSON。', 'Request JSON must include company and financials.'))
    setBase(value)
    setDraft(previous => ({ ...previous, company: value.company.name || previous.company, date: value.valuation_date || previous.date, ticker: value.company.ticker || '',
      assumptionSource: Object.keys(value.assumptions || {}).length ? 'manual' : 'automatic',
      wacc: value.assumptions?.wacc == null ? previous.wacc : decimalToPercent(value.assumptions.wacc),
      growth: value.assumptions?.terminal_growth == null ? previous.growth : decimalToPercent(value.assumptions.terminal_growth),
      years: String(value.forecast_years || previous.years), methods: value.methods || previous.methods,
    }))
  }
  const readFile = async (file, kind) => {
    if (!file) return
    setUploading(true); setError('')
    try {
      if (kind === 'structured') {
        if (file.size > 50 * 1024 * 1024) throw new Error(t('文件不能超过 50 MB。', 'Maximum file size: 50 MB.'))
        loadStructured(JSON.parse((await file.text()).replace(/^\uFEFF/, '')))
      } else {
        const saved = await uploadFile(file, kind === 'financial' ? 'historical_financials' : 'assumptions')
        update(kind === 'financial' ? 'fileIds' : 'assumptionFileIds', [saved.file_id])
      }
      setFiles(previous => ({ ...previous, [kind]: file.name }))
    } catch (err) { setError(err.message) } finally { setUploading(false) }
  }
  const sample = async () => {
    setUploading(true); setError('')
    try {
      const response = await fetch('/example-request.json')
      if (!response.ok) throw new Error(t('示例加载失败，请重试。', 'Could not load example. Please retry.'))
      loadStructured(await response.json()); setFiles(previous => ({ ...previous, structured: 'example-request.json' }))
    } catch (err) { setError(err.message) } finally { setUploading(false) }
  }
  const filePicker = (kind, label, accept) => <label className={`file-drop ${files[kind] ? 'has-file' : ''}`}><Icon name={files[kind] ? 'check' : 'upload'} size={22}/><span><b>{files[kind] || label}</b><small>{files[kind] ? t('点击替换文件', 'Click to replace') : t('点击选择文件 · 最大 50 MB', 'Choose a file · Up to 50 MB')}</small></span><input type="file" accept={accept} disabled={uploading} aria-label={label} onChange={e => { void readFile(e.target.files?.[0], kind); e.target.value = '' }}/></label>
  const submit = event => {
    event.preventDefault(); setError('')
    if (step < 2) { setStep(step + 1); return }
    try { void onStart(buildRequest(draft, base)) } catch (err) { setError(err.message) }
  }
  return <form className="setup-card" onSubmit={submit}><div className="setup-steps">{[t('研究对象', 'Company'), t('数据与假设', 'Data'), t('方法与运行', 'Methods')].map((label, index) => <button key={label} type="button" className={step === index ? 'active' : step > index ? 'finished' : ''} onClick={() => setStep(index)} aria-current={step === index ? 'step' : undefined}><span>{step > index ? <Icon name="check" size={12}/> : `0${index + 1}`}</span>{label}</button>)}</div><ErrorNotice message={error}/>
    {step === 0 && <><h2>{t('配置本次研究', 'Configure your study')}</h2><div className="choice-grid">{[['demo', 'spark', '演示体验', 'Demo', '合成数据，无需密钥', 'Synthetic data, no key'], ['snapshot', 'file', '结构化估值', 'Snapshot', '用自己的数据计算', 'Calculate with your data'], ['live', 'layers', 'Live Agent', 'Live Agent', '连接模型，协作研究', 'Research with your model']].map(([value, icon, zh, en, subZh, subEn]) => <button type="button" key={value} aria-pressed={draft.mode === value} className={`choice ${draft.mode === value ? 'selected' : ''}`} onClick={() => update('mode', value)}><Icon name={icon}/><b>{t(zh, en)}</b><small>{t(subZh, subEn)}</small></button>)}</div><label className="field">{t('企业 / 项目名称', 'Company / project')}<input required maxLength={200} value={draft.company} onChange={e => update('company', e.target.value)} placeholder={t('输入企业名称', 'Enter company name')}/></label><div className="form-row"><label className="field">{t('估值基准日', 'Valuation date')}<input type="date" required value={draft.date} onChange={e => update('date', e.target.value)}/></label><label className="field">{t('对话与结果语言', 'Conversation language')}<select value={draft.language} onChange={e => { update('language', e.target.value); onLanguage(e.target.value) }}><option value="zh-CN">简体中文</option><option value="en-US">English</option></select></label></div></>}
    {step === 1 && <><h2>{t('为估值准备数据', 'Prepare your valuation data')}</h2>{draft.mode === 'demo' ? <div className="info-note demo-note"><Icon name="spark"/><p>{t('使用明确标记的合成财务与同业数据，体验完整估值流程。', 'Use explicitly labeled synthetic financials and peers to try the full workflow.')}</p></div> : <><label className="field">{t('历史财务来源', 'Historical financial source')}<select value={draft.source} onChange={e => update('source', e.target.value)}><option value="structured">{t('结构化请求 JSON', 'Structured request JSON')}</option><option value="ticker">{t('A 股代码', 'A-share ticker')}</option><option value="upload">{t('上传财务文件', 'Upload financial data')}</option></select></label>{draft.source === 'structured' && <>{filePicker('structured', t('导入结构化请求', 'Import structured request'), '.json')}<button type="button" className="text-button" disabled={uploading} onClick={sample}>{t('使用结构化示例', 'Load structured example')}<Icon name="arrow" size={14}/></button></>}{draft.source === 'ticker' && <><label className="field">{t('A 股代码', 'A-share ticker')}<input required value={draft.ticker} onChange={e => update('ticker', e.target.value)} placeholder="600519.SH" maxLength={24}/></label><p className="field-hint warning-text">{t('在线取数尚未接入。任务将进入复核，可补充结构化财务后继续。', 'Market data adapter pending. The run will request structured financial data for review.')}</p></>}{draft.source === 'upload' && <>{filePicker('financial', t('上传财务文件', 'Upload financial data'), '.json,.pdf,.xlsx,.xls,.csv')}<p className="field-hint">{t('JSON 可直接解析；PDF / Excel 目前接收并留存，解析待接入。', 'JSON is supported. PDF / Excel files are stored; extraction is pending.')}</p></>}</>}
    <div className="form-divider"/><label className="field">{t('经营假设来源', 'Operating assumptions')}<select value={draft.assumptionSource} onChange={e => update('assumptionSource', e.target.value)}><option value="automatic">{t('参考默认值', 'Reference defaults')}</option><option value="manual">{t('手工设定', 'Manual assumptions')}</option><option value="upload">{t('上传假设文件', 'Upload assumptions')}</option></select></label>{draft.assumptionSource === 'automatic' && <p className="field-hint">{t('参考政策用于框架测试，结果中保留假设来源，后续可在对话中修改。', 'Reference defaults are for framework testing. Sources are recorded; revise assumptions in the conversation.')}</p>}{draft.assumptionSource === 'manual' && <><div className="form-row"><label className="field">WACC (%)<input type="number" step="any" required min="0.01" max="99" value={draft.wacc} onChange={e => update('wacc', e.target.value)}/></label><label className="field">{t('永续增长率', 'Terminal growth')} (%)<input type="number" step="any" required value={draft.growth} onChange={e => update('growth', e.target.value)}/></label></div><label className="field">{t('年收入增长率 · 可选', 'Annual revenue growth · optional')} (%)<input type="number" step="any" value={draft.revenueGrowth} onChange={e => update('revenueGrowth', e.target.value)} placeholder={t('留空使用已有数据或参考政策', 'Leave blank to use existing data or defaults')}/></label><p className="field-hint">{t('单值应用到全部预测年；结构化文件中的逐年假设会保留。', 'A single value applies to every forecast year. Imported annual assumptions are preserved.')}</p></>}{draft.assumptionSource === 'upload' && <>{filePicker('assumptions', t('上传假设文件', 'Upload assumptions'), '.json,.pdf,.xlsx,.xls')}<p className="field-hint">{t('JSON 可直接解析；PDF / Excel 待接入。', 'JSON supported; PDF / Excel extraction pending.')}</p></>}</>}
    {step === 2 && <><h2>{t('确认方法，开始研究', 'Choose methods and start')}</h2><div className="method-choices">{[['dcf', '现金流折现', 'Discounted cash flow'], ['pe', '市盈率可比估值', 'Price / earnings multiples'], ['ps', '市销率可比估值', 'Price / sales multiples'], ['ev_ebitda', '企业价值倍数', 'Enterprise value multiples']].map(([method, zh, en]) => <label key={method} className={draft.methods.includes(method) ? 'checked' : ''}><input type="checkbox" checked={draft.methods.includes(method)} onChange={e => update('methods', e.target.checked ? [...draft.methods, method] : draft.methods.filter(m => m !== method))}/><span><b>{methodLabel(method)}</b><small>{t(zh, en)}</small></span><Icon name={method === 'dcf' ? 'chart' : 'layers'} size={20}/></label>)}</div><label className="field">{t('预测期', 'Forecast horizon')}<select value={draft.years} onChange={e => update('years', e.target.value)}>{[10, 5, 7, 3].map(year => <option key={year} value={year}>{year} {t('年', 'years')}</option>)}</select></label><div className="request-summary"><div><span>{t('研究对象', 'Company')}</span><b>{draft.company || '—'}</b></div><div><span>{t('基准日', 'Valuation date')}</span><b>{draft.date || '—'}</b></div><div><span>{t('运行模式', 'Run mode')}</span><b>{draft.mode.toUpperCase()} · {draft.language === 'zh-CN' ? '简体中文' : 'English'}</b></div><div><span>{t('数据', 'Data')}</span><b>{draft.mode === 'demo' ? t('合成演示资料', 'Synthetic demo data') : files[draft.source === 'upload' ? 'financial' : 'structured'] || draft.ticker || t('尚未导入', 'Not imported')}</b></div></div></>}
    <div className="setup-footer">{step > 0 ? <button className="text-button" type="button" onClick={() => setStep(step - 1)}>{t('上一步', 'Back')}</button> : <span><Icon name="shield" size={16}/>{t('数据与假设随任务保存', 'Inputs saved with your study')}</span>}<button className="button primary" disabled={uploading || submitting} type="submit">{uploading ? t('正在读取…', 'Reading…') : submitting ? t('正在创建…', 'Creating…') : step === 2 ? t('开始估值研究', 'Start valuation') : t('下一步', 'Continue')}<Icon name={step === 2 ? 'play' : 'arrow'} size={17}/></button></div>
  </form>
}
