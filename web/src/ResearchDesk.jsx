import { useCallback, useEffect, useRef, useState } from 'react'
import Icon from './Icons'
import { api, post, uploadFile, downloadResearch } from './api'
import { ErrorNotice } from './ui'
import './research.css'
import { COMPANY_PRESETS } from './presets'

const roles = [['historical_financials', '历史财务', 'Financials'], ['assumptions', '经营假设', 'Assumptions'], ['comparables', '可比公司', 'Comparables'], ['evidence', '政策 / 其他证据', 'Policy / evidence']]
const toolNames = { parse_document: ['解析文件', 'Parse file'], read_document: ['读取原文', 'Read source'], inspect_context: ['查看研究上下文', 'Inspect context'], propose_task: ['整理研究范围', 'Propose scope'], propose_facts: ['提取候选字段', 'Extract candidates'], search_sources: ['检查检索能力', 'Check search capability'], check_preparation: ['检查资料缺口', 'Check preparation'], finish_response: ['综合研究说明', 'Compose response'] }

export function WelcomeHints({ t, onExample, onUpload, onConnect, target, onTargetChange, onTargetSubmit }) {
  return <div className="research-welcome">
    <div className="research-emblem">V<span>/</span>A</div><div className="eyebrow">WELCOME TO VALUATIONAGENT</div>
    <h2>{t('从一个问题，开始研究。', 'Start with a question.')}</h2>
    <p>{t('告诉我你想研究的公司，或提供手头的资料。我们一起整理信息、核对来源，逐步形成估值所需的数据。', 'Tell me about your company or share the material you have. We will organize information and sources to prepare for valuation.')}</p>
    <form className="research-target" onSubmit={onTargetSubmit}><label htmlFor="research-target-input">{t('先选一个研究对象（可选）', 'Choose a research subject (optional)')}</label><div><input id="research-target-input" list="research-company-presets" value={target} onChange={event => onTargetChange(event.target.value)} placeholder={t('输入公司名或 A 股代码，例如 600519', 'Type a company or A-share ticker, e.g. 600519')} maxLength={120}/><button type="submit" disabled={!target.trim()}>{t('带入对话', 'Use in chat')}<Icon name="arrow" size={14}/></button></div><datalist id="research-company-presets">{COMPANY_PRESETS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</datalist></form>
    <button className="research-model-setup" type="button" onClick={onConnect}><Icon name="settings" size={16}/><span><b>{t('配置研究模型', 'Configure a research model')}</b><small>{t('选择或输入模型，并在验证时输入 API Key', 'Choose or type a model, then enter an API key during verification')}</small></span><Icon name="arrow" size={15}/></button>
    <div className="research-hints"><div className="research-hint-title"><Icon name="spark" size={17}/>{t('你可以这样开始', 'Things you can ask')}</div>
      <button onClick={() => onExample(t('我想研究 600519，先帮我梳理需要哪些历史财务数据。', 'I want to research 600519. Help me identify the historical financial data needed.'))}><Icon name="chat" size={18}/><span><b>{t('描述研究目标', 'Describe your goal')}</b><small>{t('“我想研究 600519，先看看需要哪些数据。”', '“Help me prepare the data for 600519.”')}</small></span><Icon name="arrow" size={16}/></button>
      <button onClick={onUpload}><Icon name="upload" size={18}/><span><b>{t('提供现有资料', 'Share your material')}</b><small>{t('年报 PDF、财务 Excel、假设表或政策原文', 'Annual reports, spreadsheets, assumptions or policy text')}</small></span><Icon name="arrow" size={16}/></button>
      <button onClick={() => onExample(t('请分析我上传的政策原文，说明适用范围、影响渠道以及仍需核实的信息。', 'Review my uploaded policy text: scope, possible effects and remaining uncertainties.'))}><Icon name="shield" size={18}/><span><b>{t('理解政策与依据', 'Understand policy and evidence')}</b><small>{t('区分原文事实、影响推断与资料缺口', 'Separate source facts, inferences and missing evidence')}</small></span><Icon name="arrow" size={16}/></button>
      <p>{t('需要确认时，我会给出选项；你也可以直接输入自己的要求。', 'When confirmation is needed, choose an option or describe what you want.')}</p>
    </div>
  </div>
}

export function ConfirmationCard({ question, t, busy, onAnswer, onFreeText }) {
  const [selected, setSelected] = useState('')
  if (!question) return null
  return <section className="research-confirm" aria-label={t('需要确认', 'Confirmation needed')}>
    <div className="research-hint-title"><Icon name="layers" size={18}/>{t('这一步由你确认', 'Your choice')}</div><h3>{question.title}</h3>
    {question.proposed_draft && <dl className="scope-preview">{Object.entries(question.proposed_draft).filter(([, value]) => value && (!Array.isArray(value) || value.length)).map(([key, value]) => <div key={key}><dt>{({ company: t('公司', 'Company'), ticker: t('代码', 'Ticker'), valuation_date: t('估值日', 'Valuation date'), objective: t('目标', 'Goal'), methods: t('方法', 'Methods') })[key]}</dt><dd>{Array.isArray(value) ? value.join(' / ').toUpperCase() : value}</dd></div>)}</dl>}
    <div className="confirmation-options" role="radiogroup" aria-label={question.title}>{question.options.map((option, i) => <button key={option.id} type="button" role="radio" aria-checked={selected === option.id} disabled={busy} className={selected === option.id ? 'selected' : ''} onClick={() => setSelected(option.id)}><span className="option-index">{i + 1}</span><span><b>{option.label}</b>{option.description && <small>{option.description}</small>}</span><span className="option-check">{selected === option.id && <Icon name="check" size={14}/>}</span></button>)}</div>
    <div className="confirmation-actions"><button className="button secondary" disabled={busy} onClick={() => { setSelected(''); onFreeText() }}>{t('输入其他要求', 'Write your own requirements')}</button><button className="button primary" disabled={busy || !selected} onClick={() => onAnswer({ question_id: question.question_id, option_id: selected })}>{t('提交选择', 'Submit choice')}<Icon name="arrow" size={16}/></button></div>
  </section>
}

export default function ResearchDesk({ language, t, connection, initialCompany = '', onConnect, onWizard, onBusyChange, onRestoreLanguage }) {
  const [snapshot, setSnapshot] = useState(null)
  const [id, setId] = useState(() => window.location.hash.match(/^#(research_[a-f0-9]+)$/)?.[1] || null)
  const [input, setInput] = useState('')
  const [target, setTarget] = useState(initialCompany)
  const [files, setFiles] = useState([])
  const [role, setRole] = useState('historical_financials')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [history, setHistory] = useState([])
  const [tab, setTab] = useState('sources')
  const [source, setSource] = useState(null)
  const [detail, setDetail] = useState(null)
  const inputRef = useRef(null), fileRef = useRef(null), scroll = useRef(null)
  const lock = useRef(false), attached = useRef(''), alive = useRef(true), active = useRef(id)
  const session = snapshot?.session
  const messages = snapshot?.messages || [], events = snapshot?.events || []
  const refreshHistory = useCallback(() => api('/api/research-sessions').then(items => { if (alive.current) setHistory(items) }).catch(() => {}), [])
  const lastQuestion = useRef('')
  const applySnapshot = useCallback(data => {
    if (!alive.current || data.session.session_id !== active.current) return
    setSnapshot(previous => previous?.session.session_id === data.session.session_id && previous.session.revision > data.session.revision ? previous : data)
    const question = data.session.question
    if (question?.kind === 'facts' && question.question_id !== lastQuestion.current) {
      lastQuestion.current = question.question_id
      setTab('facts')
    }
  }, [])
  useEffect(() => { alive.current = true; void refreshHistory(); return () => { alive.current = false } }, [refreshHistory])
  useEffect(() => {
    active.current = id
    if (!id) return
    let cancelled = false
    api(`/api/research-sessions/${id}`).then(data => { if (!cancelled) { applySnapshot(data); onRestoreLanguage(data.session.language) } }).catch(err => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [id, onRestoreLanguage, applySnapshot])
  const messageCount = messages.length
  useEffect(() => { if (scroll.current && messageCount > 0) scroll.current.scrollTop = scroll.current.scrollHeight }, [messageCount, busy])
  useEffect(() => { onBusyChange(busy); return () => onBusyChange(false) }, [busy, onBusyChange])
  const activate = next => { active.current = next; setId(next); window.history.replaceState(null, '', `#${next}`) }
  const send = async payload => {
    if (lock.current) return
    lock.current = true; setBusy(true); setError('')
    let timer, workingId = id
    try {
      if (!workingId) {
        const created = await post('/api/research-sessions', { language, ...(connection ? { model_session_id: connection.session_id } : {}) })
        workingId = created.session.session_id; activate(workingId); setSnapshot(created)
        if (connection) attached.current = `${workingId}:${connection.session_id}`
      }
      if (connection && attached.current !== `${workingId}:${connection.session_id}`) {
        await post(`/api/research-sessions/${workingId}/model-session`, { model_session_id: connection.session_id })
        attached.current = `${workingId}:${connection.session_id}`
      }
      const uploadIds = []
      for (const file of files) uploadIds.push((await uploadFile(file, role)).file_id)
      let polling = false
      timer = setInterval(async () => { if (polling) return; polling = true; try { applySnapshot(await api(`/api/research-sessions/${workingId}`)) } catch { /* Next poll can recover. */ } finally { polling = false } }, 900)
      const data = await post(`/api/research-sessions/${workingId}/messages`, { ...payload, language, file_ids: uploadIds })
      applySnapshot(data); setInput(''); setFiles([]); void refreshHistory()
    } catch (err) { if (alive.current) setError(err.message) }
    finally { clearInterval(timer); lock.current = false; if (alive.current) setBusy(false) }
  }
  const submit = event => { event.preventDefault(); if (!input.trim() && !files.length) return; void send({ content: input.trim() || t('请整理上传的资料，提取候选信息并保留原文出处。', 'Review the attached material and extract candidates with source references.') }) }
  const submitTarget = event => { event.preventDefault(); if (!target.trim()) return; setInput(t(`我想研究 ${target.trim()}，先帮我梳理需要哪些历史财务数据。`, `I want to research ${target.trim()}. First identify the historical financial data needed.`)); inputRef.current?.focus() }
  const addFiles = list => { const chosen = Array.from(list || []); if (files.length + chosen.length > 8) { setError(t('每次最多上传 8 个文件。', 'Up to 8 files per message.')); return } setFiles(previous => [...previous, ...chosen]) }
  const selectHistory = next => { if (busy) return; setSnapshot(null); setSource(null); setDetail(null); setInput(''); setFiles([]); activate(next) }
  const viewSource = async (fileId, offset = 0) => { try { setSource({ fileId, offset, ...await api(`/api/research-sessions/${id}/sources/${fileId}?offset=${offset}`) }); setTab('sources') } catch (err) { setError(err.message) } }
  const tools = Object.values(events.filter(e => e.tool_call_id).reduce((all, event) => { all[event.tool_call_id] = { ...all[event.tool_call_id], ...event }; return all }, {}))
  return <div className="research-layout">
    <section className="conversation-panel research-conversation"><div className="panel-title"><span><Icon name="chat"/>{t('研究对话', 'Research conversation')}</span><span className="research-mode">{connection ? connection.model : t('资料整理模式', 'Local preparation')}</span></div>
      <div className="chat-scroll" ref={scroll}>
        {!session && <WelcomeHints t={t} target={target} onTargetChange={setTarget} onTargetSubmit={submitTarget} onConnect={onConnect} onExample={text => { setInput(text); inputRef.current?.focus() }} onUpload={() => fileRef.current?.click()}/>}
        {session && <div className="research-context"><span>{session.draft.company || session.draft.ticker || t('研究范围待确认', 'Scope to be confirmed')}</span><small>v{session.revision} · {session.language}</small></div>}
        {messages.map(message => <div key={message.message_id} className={`agent-message ${message.role === 'user' ? 'user-message' : ''}`}><span className="avatar">{message.role === 'user' ? t('我', 'Me') : <Icon name="spark"/>}</span><div className="message-content"><div className="message-meta"><b>{message.role === 'user' ? t('你', 'You') : 'ValuationAgent'}</b></div><p>{message.content}</p></div></div>)}
        {busy && <div className="working-message" role="status"><span className="mini-spinner"/>{t('正在整理资料与研究，右侧显示实际调用。', 'Researching. Follow actual tool calls on the right.')}</div>}
        {session?.question && <ConfirmationCard key={session.question.question_id} question={session.question} t={t} busy={busy} onAnswer={payload => void send(payload)} onFreeText={() => { inputRef.current?.focus() }}/>}<ErrorNotice message={error} onDismiss={() => setError('')}/>
      </div>
      <form className="composer research-composer" onSubmit={submit}>
        {files.length > 0 && <div className="research-attachments">{files.map((file, i) => <span key={`${file.name}-${i}`}><Icon name="file" size={14}/>{file.name}<button type="button" disabled={busy} aria-label={t('移除附件', 'Remove attachment')} onClick={() => setFiles(previous => previous.filter((_, index) => index !== i))}>×</button></span>)}</div>}
        {!connection && <button className="connect-prompt" type="button" onClick={onConnect}><Icon name="link" size={15}/>{t('连接模型，启用自然语言理解和字段提取', 'Connect a model for understanding and extraction')}</button>}
        <textarea ref={inputRef} aria-label={t('输入研究需求或修改要求', 'Research request or changes')} value={input} disabled={busy} maxLength={8000} placeholder={session?.question ? t('也可以在这里写修改要求；文字不会自动确认上面的选项…', 'Or describe changes here; text will not automatically accept a choice…') : t('说说你想研究什么，或上传手头的资料…', 'Describe your research or attach your material…')} onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form.requestSubmit() } }}/>
        <div className="research-composer-tools"><div><button type="button" className="attachment-button" disabled={busy} onClick={() => fileRef.current?.click()} aria-label={t('上传资料', 'Attach material')}><Icon name="plus" size={20}/></button><input ref={fileRef} type="file" multiple accept=".pdf,.xlsx,.xls,.csv,.json,.txt,.md" hidden onChange={event => { addFiles(event.target.files); event.target.value = '' }}/><select aria-label={t('文件用途', 'File role')} value={role} disabled={busy} onChange={event => setRole(event.target.value)}>{roles.map(([value, zh, en]) => <option key={value} value={value}>{t(zh, en)}</option>)}</select><small>{t('Enter 发送', 'Enter to send')}</small></div><button className="send-button" disabled={busy || (!input.trim() && !files.length)} aria-label={t('发送需求', 'Send request')}><Icon name="send" size={18}/></button></div>
      </form>
    </section>
    <aside className="research-inspector"><div className="research-inspector-top"><div className="eyebrow">RESEARCH NOTEBOOK</div><h2>{t('资料、依据与过程', 'Sources & research trail')}</h2><p>{t('先把资料整理清楚，再进入金融建模。', 'Prepare reliable inputs for financial modeling.')}</p><div className="research-counters"><span><b>{session?.documents.length || 0}</b>{t('份资料', 'sources')}</span><span><b>{session?.facts.filter(f => f.status === 'confirmed').length || 0}</b>{t('已确认', 'confirmed')}</span><span><b>{session?.facts.filter(f => f.status === 'proposed').length || 0}</b>{t('待复核', 'to review')}</span></div></div>
      <div className="inspector-tabs" role="tablist">{[['sources', '资料', 'Sources'], ['facts', '候选字段', 'Candidates'], ['tools', '执行记录', 'Activity']].map(([key, zh, en]) => <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}>{t(zh, en)}</button>)}</div>
      <div className="research-inspector-body">
        {tab === 'sources' && <>{session?.documents.map(doc => <div className="research-source" key={doc.file_id}><button onClick={() => void viewSource(doc.file_id)}><Icon name="file" size={18}/><span><b>{doc.name}</b><small>{doc.block_count} {t('个原文片段', 'source blocks')}</small></span><Icon name="chevron" size={15}/></button>{doc.warnings.map(w => <p className="research-warning" key={w}>{w}</p>)}</div>)}{!session?.documents.length && <p className="research-empty">{t('上传后，这里会显示文件及可定位的原文。文本型 PDF、Excel、CSV、JSON 和纯文本可读取；扫描件 OCR 待接入。', 'Uploaded material appears here with source locations. Text PDFs, Excel, CSV, JSON and plain text are supported; OCR is pending.')}</p>}{source && <div className="source-preview"><button className="research-text-button" onClick={() => setSource(null)}>{t('收起原文', 'Close preview')}</button>{source.blocks.map(block => <div key={block.block_id}><small>{JSON.stringify(block.location)} · {block.block_id}</small><pre>{block.text}</pre></div>)}<div className="inline-actions"><button disabled={source.offset === 0} onClick={() => void viewSource(source.fileId, Math.max(0, source.offset - 12))}>{t('上一页', 'Previous')}</button><button disabled={source.offset + 12 >= source.total} onClick={() => void viewSource(source.fileId, source.offset + 12)}>{t('下一页', 'Next')}</button></div></div>}</>}
        {tab === 'facts' && <>{!session?.facts.length && <p className="research-empty">{t('连接模型后，让 Agent 提取已上传资料中的财务字段。每个候选值都需要原文引用；确认前保持待复核状态。', 'Connect a model and ask it to extract financial fields. Every candidate needs a source quote and remains proposed until confirmed.')}</p>}{session?.facts.map(fact => <div className={`research-fact ${fact.status}`} key={fact.fact_id}><div><b>{fact.metric}</b><span>{({ confirmed: t('已确认', 'Confirmed'), proposed: t('待复核', 'Proposed'), rejected: t('已拒绝', 'Rejected') })[fact.status]}</span></div><strong>{fact.raw_value} <small>{fact.unit}</small></strong><p>{fact.period} · {fact.scope}</p><blockquote>{fact.quote}</blockquote><small>{fact.block_id}</small>{fact.warnings.map(w => <p className="research-warning" key={w}>{w}</p>)}</div>)}</>}
        {tab === 'tools' && <>{!tools.length && <p className="research-empty">{t('实际执行后显示工具、状态和耗时。', 'Tool calls, status and timing appear after execution.')}</p>}{tools.map(tool => <button className={`research-tool ${tool.status}`} key={tool.tool_call_id} onClick={() => setDetail(detail?.tool_call_id === tool.tool_call_id ? null : tool)}><span className="tool-dot"/><span><b>{toolNames[tool.tool] ? t(...toolNames[tool.tool]) : tool.tool}</b><small>{tool.status} {tool.duration_ms != null ? `· ${tool.duration_ms} ms` : ''}</small></span><Icon name="chevron" size={14}/></button>)}{detail && <pre className="tool-detail">{JSON.stringify(detail, null, 2)}</pre>}</>}
        {session?.gaps.length > 0 && <div className="research-gaps"><b>{t('资料缺口', 'Open gaps')}</b>{session.gaps.map(gap => <p key={gap}>· {gap}</p>)}</div>}
      </div>
      <div className="research-inspector-footer">{id && <div className="inline-actions"><button disabled={busy} onClick={() => void send({ content: '/prepare' })}>{t('检查准备情况', 'Check preparation')}</button><button disabled={busy} onClick={() => downloadResearch(id, 'json').catch(err => setError(err.message))}>JSON</button><button disabled={busy} onClick={() => downloadResearch(id, 'html').catch(err => setError(err.message))}>HTML</button></div>}<p>{t('正式金融模型与联网取数待接入，当前交付研究资料和复核记录。', 'Financial models and online data are pending. This workspace prepares sources and review records.')}</p><button className="research-text-button" onClick={onWizard} disabled={busy}>{t('打开原有分步向导 / 参考模型演示', 'Open the setup wizard / reference demo')}<Icon name="arrow" size={14}/></button>{history.length > 0 && <select aria-label={t('继续已有研究', 'Resume research')} value={id || ''} disabled={busy} onChange={event => event.target.value && selectHistory(event.target.value)}><option value="">{t('继续已有研究…', 'Resume a study…')}</option>{history.map(item => <option key={item.session_id} value={item.session_id}>{item.draft.company || item.draft.ticker || item.draft.objective || t('未命名研究', 'Untitled study')} · v{item.revision}</option>)}</select>}</div>
    </aside>
  </div>
}
