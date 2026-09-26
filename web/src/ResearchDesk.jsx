import { useCallback, useEffect, useRef, useState } from 'react'
import Icon from './Icons'
import { api, post, uploadFile, downloadResearch } from './api'
import { ErrorNotice } from './ui'
import './research.css'
import { COMPANY_PRESETS } from './presets'
import MessageBody from './MessageBody'
import CandidateReview from './CandidateReview'
import ValuationPlanReview from './ValuationPlanReview'
import OutcomePanel from './OutcomePanel'

const roles = [['historical_financials', '历史财务', 'Financials'], ['assumptions', '经营假设', 'Assumptions'], ['comparables', '可比公司', 'Comparables'], ['evidence', '政策 / 其他证据', 'Policy / evidence']]
const toolNames = { propose_forecast: ['构建预测假设', 'Build forecast assumptions'], request_formal_valuation: ['准备计算方案', 'Prepare valuation plan'], parse_document: ['解析文件', 'Parse file'], read_document: ['读取原文', 'Read source'], inspect_context: ['查看研究上下文', 'Inspect context'], propose_task: ['整理研究范围', 'Propose scope'], propose_facts: ['提取候选字段', 'Extract candidates'], search_sources: ['检索公开资料', 'Search public sources'], fetch_search_source: ['下载官方原文', 'Download official source'], update_memory: ['更新长期上下文', 'Update durable context'], check_preparation: ['检查资料缺口', 'Check preparation'], finish_response: ['综合研究说明', 'Compose response'] }
const activityNames = { 'report.generated': ['生成结果说明报告', 'Generate outcome report'], 'report.failed': ['报告生成需重试', 'Report generation needs retry'], 'intent.classified': ['识别用户意图', 'Classify intent'], 'agent.started': ['Agent 开始规划', 'Agent planning'], 'agent.completed': ['Agent 完成本轮', 'Agent completed'], 'agent.failed': ['Agent 执行失败', 'Agent failed'], 'agent.recovery_required': ['等待恢复选择', 'Recovery required'], 'agent.recovery_resolved': ['恢复问题已处理', 'Recovery resolved'], 'facts.quarantined': ['隔离无效候选', 'Quarantine invalid candidates'], 'memory.updated': ['更新长期上下文', 'Context updated'], 'security.credential_redacted': ['疑似凭证已脱敏', 'Credential redacted'], 'valuation.goal_recorded': ['记住估值目标', 'Remember valuation goal'], 'valuation.submitted': ['提交正式估值', 'Submit valuation'] }

function readDrafts() { try { return JSON.parse(sessionStorage.getItem('valuation-research-drafts') || '{}') } catch { return {} } }
function mergeEvents(previous, incoming) { return [...new Map([...previous, ...incoming].map(item => [item.sequence, item])).values()].sort((a, b) => a.sequence - b.sequence).slice(-200) }

export function WelcomeHints({ t, onExample, onUpload, target, onTargetChange, onTargetSubmit }) {
  return <div className="research-welcome">
    <div className="research-emblem">V<span>/</span>A</div><div className="eyebrow">EVIDENCE TO VALUE</div>
    <h2>{t('从一家公司，到一份估值。', 'From a company to a valuation.')}</h2>
    <p>{t('只需告诉我们公司。Agent 自动查找公开资料、核对财务并建模；关键方案由你确认。数据不足，也有一份讲清原因的结果报告。', 'Just name the company. The Agent retrieves public sources, checks financials and prepares a model for your review. Limited data still produces an explanatory report.')}</p>
    <form className="research-target" onSubmit={onTargetSubmit}><label htmlFor="research-target-input">{t('你想估值哪家公司？', 'Which company would you like to value?')}</label><div><input id="research-target-input" list="research-company-presets" value={target} onChange={event => onTargetChange(event.target.value)} placeholder={t('输入公司名或 A 股代码，例如 600519', 'Type a company or A-share ticker, e.g. 600519')} maxLength={120}/><button type="submit" disabled={!target.trim()}>{t('开始估值', 'Start valuation')}<Icon name="arrow" size={14}/></button></div><datalist id="research-company-presets">{COMPANY_PRESETS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</datalist></form>
    <div className="research-hints"><div className="research-hint-title"><Icon name="spark" size={17}/>{t('你可以这样开始', 'Things you can ask')}</div>
      <button onClick={() => onExample(t('请对 600519 进行估值，自动补齐必要数据并提出预测假设，整套方案确认后计算估值区间、敏感性分析并生成报告。', 'Start a valuation of 600519: gather required inputs, propose forecast assumptions for one combined review, then calculate ranges, sensitivities and a report.'))}><Icon name="chart" size={18}/><span><b>{t('完成自动化估值', 'Complete a valuation')}</b><small>{t('“对 600519 进行估值，给出区间、敏感性和报告。”', '“Value 600519, including ranges, sensitivities and a report.”')}</small></span><Icon name="arrow" size={16}/></button>
      <button onClick={onUpload}><Icon name="upload" size={18}/><span><b>{t('提供现有资料', 'Share your material')}</b><small>{t('年报 PDF、财务 Excel、假设表或政策原文', 'Annual reports, spreadsheets, assumptions or policy text')}</small></span><Icon name="arrow" size={16}/></button>
      <button onClick={() => onExample(t('请分析我上传的政策原文，说明适用范围、影响渠道以及仍需核实的信息。', 'Review my uploaded policy text: scope, possible effects and remaining uncertainties.'))}><Icon name="shield" size={18}/><span><b>{t('理解政策与依据', 'Understand policy and evidence')}</b><small>{t('区分原文事实、影响推断与资料缺口', 'Separate source facts, inferences and missing evidence')}</small></span><Icon name="arrow" size={16}/></button>
      <p>{t('默认先读附件，再检索公开资料补缺；也可选择仅用上传资料。关键财务输入与假设集中确认。', 'Read attachments first and retrieve public sources for missing inputs, or choose uploads only. Review key inputs and assumptions together.')}</p>
    </div>
  </div>
}

export function ConfirmationCard({ question, t, busy, onAnswer, onFreeText, onDataServices }) {
  const [selected, setSelected] = useState('')
  if (!question) return null
  const connectChoice = ['search_unavailable', 'search_failed'].includes(question.kind)
    ? [{ id: '__data__', label: t('连接 Tavily 搜索', 'Connect Tavily Search'), description: t('凭证仅用于当前研究会话', 'Credential stays in this research session') }]
    : []
  const choices = [...connectChoice, ...question.options, { id: '__chat__', label: 'Chat', description: t('输入自己的判断或修改要求', 'Write your own decision or changes') }]
  return <section className="research-confirm" aria-label={t('需要确认', 'Confirmation needed')}>
    <div className="research-hint-title"><Icon name="layers" size={18}/>{t('这一步由你确认', 'Your choice')}</div><h3>{question.title}</h3>
    <ValuationPlanReview plan={question.valuation_review} t={t}/>
    {question.proposed_draft && <dl className="scope-preview">{Object.entries(question.proposed_draft).filter(([, value]) => value && (!Array.isArray(value) || value.length)).map(([key, value]) => <div key={key}><dt>{({ company: t('公司', 'Company'), ticker: t('代码', 'Ticker'), industry: t('行业', 'Industry'), valuation_date: t('估值日', 'Valuation date'), objective: t('目标', 'Goal'), methods: t('方法', 'Methods') })[key] || key}</dt><dd>{Array.isArray(value) ? value.join(' / ').toUpperCase() : value}</dd></div>)}</dl>}
    <div className="confirmation-options" role="radiogroup" aria-label={question.title}>{choices.map((option, i) => <button key={option.id} type="button" role="radio" aria-checked={selected === option.id} tabIndex={selected ? (selected === option.id ? 0 : -1) : (i === 0 ? 0 : -1)} disabled={busy} className={`${selected === option.id ? 'selected' : ''} ${option.id === '__chat__' ? 'chat-option' : ''}`} onClick={() => { if (option.id === '__chat__') { setSelected(''); onFreeText(question.question_id) } else if (option.id === '__data__') { setSelected(''); onDataServices?.() } else setSelected(option.id) }} onKeyDown={event => { if (['ArrowDown', 'ArrowRight', 'ArrowUp', 'ArrowLeft'].includes(event.key)) { event.preventDefault(); const next = (i + (['ArrowDown', 'ArrowRight'].includes(event.key) ? 1 : -1) + choices.length) % choices.length; setSelected(choices[next].id); event.currentTarget.parentElement.children[next].focus() } else if (event.key === 'Enter' && option.id === '__chat__') { event.preventDefault(); setSelected(''); onFreeText(question.question_id) } else if (event.key === 'Enter' && option.id === '__data__') { event.preventDefault(); setSelected(''); onDataServices?.() } }}><span className="option-index">{option.id === '__chat__' ? <Icon name="chat" size={14}/> : option.id === '__data__' ? <Icon name="link" size={14}/> : i + 1}</span><span><b>{option.label}</b>{option.description && <small>{option.description}</small>}</span><span className="option-check">{selected === option.id && <Icon name="check" size={14}/>}</span></button>)}</div>
    <div className="confirmation-actions"><button className="button primary" disabled={busy || !selected || selected.startsWith('__')} onClick={() => onAnswer({ question_id: question.question_id, option_id: selected })}>{t('确认', 'Confirm')}<Icon name="arrow" size={16}/></button></div>
  </section>
}

export default function ResearchDesk({ language, t, connection, initialCompany = '', initialResearchId = null, refreshToken = 0, onConnect, onDataServices, onConnectionLost, onWizard, onValuation, onBusyChange, onRestoreLanguage, onHistoryChange, onSessionChange }) {
  const [snapshot, setSnapshot] = useState(null)
  const [id, setId] = useState(() => initialResearchId || window.location.hash.match(/^#(research_[a-f0-9]+)$/)?.[1] || null)
  const [drafts, setDrafts] = useState(readDrafts)
  const input = drafts[id || 'new'] || ''
  const setInput = value => setDrafts(previous => ({ ...previous, [id || 'new']: value }))
  const [replyQuestionId, setReplyQuestionId] = useState('')
  const [targetChoice, setTargetChoice] = useState(null)
  const target = targetChoice?.seed === initialCompany ? targetChoice.value : initialCompany
  const setTarget = value => setTargetChoice({ seed: initialCompany, value })
  const [files, setFiles] = useState([])
  const [role, setRole] = useState('historical_financials')
  const [sourcePolicy, setSourcePolicy] = useState('web')
  const [submitting, setBusy] = useState(false)
  const [loading, setLoading] = useState(Boolean(id))
  const [error, setError] = useState('')
  const [history, setHistory] = useState([])
  const [tab, setTab] = useState('outcome')
  const [exporting, setExporting] = useState('')
  const [source, setSource] = useState(null)
  const [detail, setDetail] = useState(null)
  const [mobilePane, setMobilePane] = useState('chat')
  const [pollEpoch, setPollEpoch] = useState(0)
  const [syncError, setSyncError] = useState('')
  const inputRef = useRef(null), fileRef = useRef(null), scroll = useRef(null)
  const lock = useRef(false), attached = useRef(''), alive = useRef(true), active = useRef(id)
  const nearBottom = useRef(true), sourceRequest = useRef(0), uploadCache = useRef(new WeakMap())
  const cursor = useRef(null), running = useRef(false), retryTurn = useRef(null), previousRun = useRef(null)
  const session = snapshot?.session
  const busy = submitting || Boolean(snapshot?.execution?.active)
  const reportAvailable = ['insufficient_data', 'review_required'].includes(snapshot?.result_document?.status)
  const messages = snapshot?.messages || [], events = snapshot?.events || []
  const refreshHistory = useCallback(() => api('/api/research-sessions').then(items => { if (alive.current) { setHistory(items); onHistoryChange?.(items) } }).catch(() => {}), [onHistoryChange])
  const lastQuestion = useRef('')
  const lastReport = useRef('')
  const applySnapshot = useCallback(data => {
    if (!alive.current || data.session.session_id !== active.current) return
    cursor.current = data.cursor ?? cursor.current
    running.current = Boolean(data.execution?.active)
    setSnapshot(previous => {
      if (previous?.session.session_id !== data.session.session_id) return data
      if (previous.session.revision > data.session.revision) return previous
      return { ...data, events: mergeEvents(previous.events || [], data.events || []) }
    })
    const question = data.session.question
    if (data.result_document && data.result_document.report_id !== lastReport.current && !data.execution?.active) {
      lastReport.current = data.result_document.report_id
      setTab('outcome')
    }
    if (question?.kind === 'facts' && question.question_id !== lastQuestion.current) {
      lastQuestion.current = question.question_id
      setTab('facts')
    }
  }, [])
  useEffect(() => { try { const safe = Object.fromEntries(Object.entries(drafts).filter(([, value]) => !/(?:sk-|tvly-)[a-zA-Z0-9_-]{12,}/.test(value))); sessionStorage.setItem('valuation-research-drafts', JSON.stringify(safe)) } catch { /* Draft stays in memory if storage is unavailable. */ } }, [drafts])
  useEffect(() => { alive.current = true; void refreshHistory(); return () => { alive.current = false } }, [refreshHistory])
  useEffect(() => {
    active.current = id
    if (!id) return
    let cancelled = false, timer, failures = 0, first = true
    const controller = new AbortController()
    const sync = async () => {
      try {
        const data = await api(`/api/research-sessions/${id}?compact=true${cursor.current == null ? '' : `&after=${cursor.current}`}`, { signal: controller.signal })
        if (cancelled) return
        applySnapshot(data); failures = 0; setSyncError('')
        if (first) { onRestoreLanguage(data.session.language); first = false }
        if (data.has_more) { timer = setTimeout(sync, 50); return }
      } catch (err) { if (!cancelled) { failures++; setSyncError(err.message) } }
      finally { if (!cancelled) setLoading(false) }
      if (!cancelled) timer = setTimeout(sync, failures ? Math.min(2000 * 2 ** failures, 15000) : running.current ? (document.hidden ? 5000 : 1200) : 30000)
    }
    void sync()
    return () => { cancelled = true; clearTimeout(timer); controller.abort() }
  }, [id, onRestoreLanguage, applySnapshot, refreshToken, pollEpoch])
  useEffect(() => {
    if (session?.valuation_run_id && previousRun.current === 'pending') onValuation?.({ run_id: session.valuation_run_id })
    previousRun.current = session?.valuation_run_id || (session?.pending_action === 'valuation' ? 'pending' : null)
  }, [session?.valuation_run_id, session?.pending_action, onValuation])
  const messageCount = messages.length
  useEffect(() => { if (nearBottom.current && scroll.current && messageCount > 0) scroll.current.scrollTop = scroll.current.scrollHeight }, [messageCount, busy, session?.question?.question_id])
  useEffect(() => { onBusyChange(busy); return () => onBusyChange(false) }, [busy, onBusyChange])
  const activate = next => { cursor.current = null; previousRun.current = null; active.current = next; setId(next); window.history.replaceState(null, '', `#${next}`); onSessionChange?.(next) }
  const send = async (payload, includeDraft = false) => {
    if (lock.current || busy || loading || (id && !session)) return
    lock.current = true; setBusy(true); setError('')
    nearBottom.current = true
    let workingId = id
    try {
      if (!workingId) {
        const created = await post('/api/research-sessions', { language, data_source_preference: sourcePolicy, ...(connection ? { model_session_id: connection.session_id } : {}) })
        workingId = created.session.session_id; activate(workingId); setSnapshot(created); setDrafts(previous => ({ ...previous, [workingId]: input }))
        if (connection) attached.current = `${workingId}:${connection.session_id}`
      }
      if (connection && attached.current !== `${workingId}:${connection.session_id}`) {
        await post(`/api/research-sessions/${workingId}/model-session`, { model_session_id: connection.session_id })
        attached.current = `${workingId}:${connection.session_id}`
      }
      const uploadIds = []
      if (includeDraft) for (const file of files) {
        const cached = uploadCache.current.get(file)
        const fileId = cached?.role === role ? cached.id : (await uploadFile(file, role)).file_id
        uploadCache.current.set(file, { role, id: fileId }); uploadIds.push(fileId)
      }
      const body = { ...payload, language, file_ids: uploadIds }, signature = `${workingId}:${JSON.stringify(body)}`
      if (retryTurn.current?.signature !== signature) retryTurn.current = { signature, request_id: crypto.randomUUID() }
      const accepted = await post(`/api/research-sessions/${workingId}/turns`, { ...body, request_id: retryTurn.current.request_id })
      retryTurn.current = null
      if (alive.current && active.current === workingId) setSnapshot(previous => ({ ...previous, execution: accepted.execution }))
      setPollEpoch(value => value + 1); setReplyQuestionId(''); if (includeDraft) { setDrafts(previous => ({ ...previous, [workingId]: '', new: '' })); setFiles([]) } void refreshHistory()
    } catch (err) {
      if (alive.current) {
        if (err.status === 404 && err.message === 'model session not found') {
          attached.current = ''; onConnectionLost?.()
          setError(t('模型连接已过期，请重新连接；输入和附件已保留。', 'Your model connection expired. Reconnect to continue; your draft and attachments are preserved.'))
        } else setError(err.message)
        if (workingId) setPollEpoch(value => value + 1)
        void refreshHistory()
      }
    }
    finally { lock.current = false; if (alive.current) setBusy(false) }
  }
  const submit = event => { event.preventDefault(); if (!input.trim() && !files.length) return; void send({ content: input.trim() || t('请整理上传的资料，提取候选信息并保留原文出处。', 'Review the attached material and extract candidates with source references.'), ...(replyQuestionId ? { question_id: replyQuestionId } : {}) }, true) }
  const submitTarget = event => {
    event.preventDefault()
    if (!target.trim() || busy || loading) return
    const message = t(`开始自动化估值。研究对象：${target.trim()}。自动查找官方公开资料，核对财务数据，选择适用的方法，给出估值区间、敏感性分析和报告；缺数据也请交付说明报告，关键方案集中确认。`, `Start the formal valuation of ${target.trim()}. Retrieve official public sources, verify inputs, propose suitable methods and deliver a report with ranges and sensitivity. If data is missing, deliver an explanatory report. Review the combined plan before calculation.`)
    if (!connection) { setInput(message); onConnect(); return }
    void send({ content: message })
  }
  const addFiles = list => {
    const chosen = Array.from(list || [])
    if (files.length + chosen.length > 8) { setError(t('每次最多上传 8 个文件。', 'Up to 8 files per message.')); return }
    if (chosen.some(file => file.size > 50 * 1024 * 1024)) { setError(t('单个文件不能超过 50 MB。', 'Each file must be 50 MB or less.')); return }
    if (chosen.some(file => !/\.(pdf|docx|xlsx|csv|tsv|html?|json|txt|md)$/i.test(file.name))) { setError(t('支持 PDF、DOCX、XLSX、CSV、TSV、HTML、JSON、TXT、MD；旧版 XLS 请先另存为 XLSX。', 'Use PDF, DOCX, XLSX, CSV, TSV, HTML, JSON, TXT or MD. Save legacy XLS as XLSX first.')); return }
    setError(''); setFiles(previous => [...previous, ...chosen])
  }
  const submitValuation = async () => {
    if (!id || busy || lock.current || !session) return
    if (session.valuation_run_id) {
      onValuation?.({ run_id: session.valuation_run_id })
      return
    }
    await send({ content: t('开始正式估值；如果资料还不完整，请自动继续查找、读取和提取，只有需要我确认时再暂停。', 'Start the formal valuation. If inputs are incomplete, continue searching, reading and extracting automatically, and pause only when my confirmation is required.') })
  }
  const downloadReport = async format => {
    if (!id || exporting) return
    setExporting(format); setError('')
    try { await downloadResearch(id, format); setPollEpoch(value => value + 1) }
    catch (err) { setError(err.message) }
    finally { if (alive.current) setExporting('') }
  }
  const selectHistory = next => { if (busy || next === id) return; sourceRequest.current++; setLoading(true); setError(''); setSnapshot(null); setSource(null); setDetail(null); setReplyQuestionId(''); setFiles([]); nearBottom.current = true; activate(next) }
  const controlTurn = async action => { try { setError(''); await post(`/api/research-sessions/${id}/${action}`, {}); setPollEpoch(value => value + 1) } catch (err) { setError(err.message) } }
  const viewEvent = async item => { if (detail?.sequence === item.sequence) { setDetail(null); return } try { setDetail(item.payload ? item : await api(`/api/research-sessions/${id}/events/${item.sequence}`)) } catch (err) { setError(err.message) } }
  const loadEarlier = async () => { try { const page = await api(`/api/research-sessions/${id}/messages?before=${snapshot.message_before}`); setDetail({ message: t('早期对话记录', 'Earlier messages'), messages: page.messages }); if (page.has_more) setSnapshot(previous => ({ ...previous, message_before: page.before })); else setSnapshot(previous => ({ ...previous, older_messages: false })); setTab('tools'); setMobilePane('notebook') } catch (err) { setError(err.message) } }
  const viewSource = async (fileId, offset = 0) => {
    const request = ++sourceRequest.current, origin = id
    try { const data = await api(`/api/research-sessions/${origin}/sources/${fileId}?offset=${offset}`); if (alive.current && active.current === origin && request === sourceRequest.current) { setSource({ fileId, offset, ...data }); setTab('sources') } }
    catch (err) { if (alive.current && active.current === origin && request === sourceRequest.current) setError(err.message) }
  }
  const toolEvents = Object.values(events.filter(e => e.tool_call_id).reduce((all, event) => { all[event.tool_call_id] = { ...all[event.tool_call_id], ...event }; return all }, {}))
  const activity = [...toolEvents, ...events.filter(event => !event.tool_call_id && activityNames[event.type])].sort((a, b) => a.sequence - b.sequence)
  const factCounts = {
    confirmed: session?.facts.filter(f => f.status === 'confirmed').length || 0,
    staged: session?.facts.filter(f => f.status === 'proposed' && !f.warnings?.length).length || 0,
    repair: session?.facts.filter(f => f.status === 'proposed' && f.warnings?.length).length || 0,
  }
  return <div className={`research-layout mobile-${mobilePane}`}>
    <div className="research-mobile-tabs"><button className={mobilePane === 'chat' ? 'active' : ''} onClick={() => setMobilePane('chat')}>{t('对话与进度', 'Chat & progress')}</button><button className={mobilePane === 'notebook' ? 'active' : ''} onClick={() => setMobilePane('notebook')}>{t('结果与资料', 'Results & sources')}{session?.facts.some(f => f.status === 'proposed') && ' ●'}</button></div>
    <section className="conversation-panel research-conversation"><div className="panel-title"><span><Icon name="chat"/>{t('研究对话', 'Research conversation')}</span><span className={`research-mode ${connection ? 'connected' : ''}`}><i/>{connection ? connection.model : t('未连接推理模型', 'Model not connected')}</span></div>
      <div className="chat-scroll" ref={scroll} onScroll={() => { const el = scroll.current; nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100 }}>
        {loading && !session && <div className="loading-state" role="status"><span className="mini-spinner"/>{t('正在恢复研究记录…', 'Restoring your research…')}</div>}
        {!id && !session && <WelcomeHints t={t} target={target} onTargetChange={setTarget} onTargetSubmit={submitTarget} onExample={text => { setInput(text); inputRef.current?.focus() }} onUpload={() => fileRef.current?.click()}/>}
        {id && !loading && !session && <div className="research-empty"><p>{t('研究记录暂时无法载入，输入已保留。', 'The study could not be loaded. Your draft is preserved.')}</p><button className="button secondary" onClick={() => { setLoading(true); api(`/api/research-sessions/${id}`).then(applySnapshot).catch(err => setError(err.message)).finally(() => setLoading(false)) }}>{t('重新载入', 'Reload study')}</button></div>}
        {session && <div className="research-context"><span>{session.draft.company || session.draft.ticker || t('研究范围待确认', 'Scope to be confirmed')}</span><small>v{session.revision} · {session.language}</small></div>}
        {session?.pending_action === 'valuation' && !session?.valuation_run_id && <div className="research-goal"><Icon name="chart" size={16}/><span><b>{busy ? t('正在推进正式估值', 'Formal valuation in progress') : snapshot?.result_document && !session?.question ? t('已保存结果说明', 'Outcome report saved') : t('估值目标已记录', 'Valuation goal saved')}</b><small>{session?.question ? t('处理当前确认后将自动继续，不必再次点击开始。', 'It will resume after this confirmation; no need to start again.') : t('就绪后确认整套方案；关键数据不可得时可直接下载说明报告。', 'Review the combined plan when ready; download an explanatory report if essential data is unavailable.')} {t(`已确认 ${factCounts.confirmed} · 待集中确认 ${factCounts.staged} · 需补证 ${factCounts.repair}`, `Confirmed ${factCounts.confirmed} · staged ${factCounts.staged} · needs repair ${factCounts.repair}`)}</small></span></div>}
        {snapshot?.older_messages && <button className="research-text-button" onClick={() => void loadEarlier()}>{t('查看更早的对话', 'View earlier messages')}</button>}
        {messages.map(message => <div key={message.message_id} className={`agent-message ${message.role === 'user' ? 'user-message' : ''}`}><span className="avatar">{message.role === 'user' ? t('我', 'Me') : <Icon name="spark"/>}</span><div className="message-content"><div className="message-meta"><b>{message.role === 'user' ? t('你', 'You') : 'ValuationAgent'}</b></div>{message.role === 'user' ? <p>{message.content}</p> : <MessageBody content={message.content}/>}</div></div>)}
        {busy && <div className="working-message" role="status"><span className="mini-spinner"/><span>{toolNames[snapshot?.execution?.stage] ? t(...toolNames[snapshot.execution.stage]) : t('正在分析估值所需数据', 'Analyzing valuation inputs')} · {Math.round(snapshot?.execution?.elapsed_seconds || 0)}s<small>{snapshot?.execution?.cancel_requested ? t('已请求停止，将在当前网络调用结束后保存进度。', 'Stopping after the current network call; progress will be saved.') : t('可以刷新页面，任务和进度不会丢失。', 'You can refresh safely. Your progress is saved.')}</small></span><button type="button" disabled={!snapshot?.execution?.active || snapshot?.execution?.cancel_requested} onClick={() => void controlTurn('cancel')}>{t('停止', 'Stop')}</button></div>}
        {!busy && ['failed', 'interrupted', 'cancelled'].includes(snapshot?.execution?.status) && <div className="research-recovery"><p>{t('任务已暂停，已完成的资料和候选值已保存。', 'Task paused. Completed sources and candidates are saved.')}</p><button className="button secondary" onClick={() => void controlTurn('resume-turn')}>{t('继续上次任务', 'Resume task')}</button></div>}
        {session?.question && <ConfirmationCard key={session.question.question_id} question={session.question} t={t} busy={busy} onAnswer={payload => void send(payload)} onFreeText={questionId => { setReplyQuestionId(questionId); inputRef.current?.focus() }} onDataServices={onDataServices}/>}
      </div>
      {syncError && <div className="sync-warning" role="status">{t('进度连接暂时中断，正在自动重连；后台任务可能仍在执行。', 'Progress connection interrupted. Reconnecting; the background task may still be running.')}</div>}
      <ErrorNotice message={error} onDismiss={() => setError('')}/>
      <form className="composer research-composer" onSubmit={submit}>
        {!id && <label className="research-intake-policy"><span>{t('资料策略', 'Source policy')}</span><select aria-label={t('资料策略', 'Source policy')} value={sourcePolicy} disabled={busy} onChange={event => setSourcePolicy(event.target.value)}><option value="web">{t('附件优先 + 公开检索补缺', 'Attachments first + public research')}</option><option value="upload">{t('仅用上传资料（不联网）', 'Uploads only (no web requests)')}</option></select></label>}
        {replyQuestionId && <div className="reply-context"><span>{t('正在补充确认要求', 'Replying to the confirmation')}</span><button type="button" aria-label={t('取消补充', 'Cancel reply')} onClick={() => setReplyQuestionId('')}><Icon name="close" size={14}/></button></div>}
        {files.length > 0 && <div className="research-attachments">{files.map((file, i) => <span key={`${file.name}-${i}`}><Icon name="file" size={14}/>{file.name}<button type="button" disabled={busy} aria-label={t('移除附件', 'Remove attachment')} onClick={() => setFiles(previous => previous.filter((_, index) => index !== i))}>×</button></span>)}</div>}
        {!connection && <button className="connect-prompt" type="button" onClick={onConnect}><Icon name="link" size={15}/>{t('连接模型，启用自然语言理解和字段提取', 'Connect a model for understanding and extraction')}</button>}
        <textarea ref={inputRef} aria-label={t('输入研究需求或修改要求', 'Research request or changes')} value={input} disabled={busy} maxLength={8000} placeholder={session?.question ? t('也可以在这里写修改要求；文字不会自动确认上面的选项…', 'Or describe changes here; text will not automatically accept a choice…') : t('说说你想研究什么，或上传手头的资料…', 'Describe your research or attach your material…')} onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form.requestSubmit() } }}/>
        <div className="research-composer-tools"><div><button type="button" className="attachment-button" disabled={busy} onClick={() => fileRef.current?.click()} aria-label={t('上传资料', 'Attach material')} title={t('添加资料 · 每个文件最多 50 MB', 'Attach files · up to 50 MB each')}><Icon name="plus" size={20}/></button><input ref={fileRef} type="file" multiple accept=".pdf,.docx,.xlsx,.csv,.tsv,.html,.htm,.json,.txt,.md" hidden onChange={event => { addFiles(event.target.files); event.target.value = '' }}/><select aria-label={t('文件用途', 'File role')} value={role} disabled={busy} onChange={event => setRole(event.target.value)}>{roles.map(([value, zh, en]) => <option key={value} value={value}>{t(zh, en)}</option>)}</select><small>{t('Enter 发送 · Shift + Enter 换行', 'Enter to send · Shift + Enter for a new line')}</small></div><button className="send-button" disabled={busy || loading || (id && !session) || (!input.trim() && !files.length)} aria-label={t('发送需求', 'Send request')}><Icon name="send" size={18}/></button></div>
      </form>
    </section>
    <aside className="research-inspector"><div className="research-inspector-top"><div className="eyebrow">VALUATION WORKSPACE</div><h2>{t('结果与估值依据', 'Results & evidence')}</h2><p>{t('围绕可执行的估值方法推进，交付有依据的结果。', 'Work toward a supported valuation and a documented outcome.')}</p>{tab !== 'outcome' && <div className="research-counters"><span><b>{session?.documents.length || 0}</b>{t('份资料', 'sources')}</span><span><b>{factCounts.confirmed}</b>{t('已确认', 'confirmed')}</span><span><b>{factCounts.staged}</b>{t('待集中确认', 'staged')}</span><span className={factCounts.repair ? 'has-warning' : ''}><b>{factCounts.repair}</b>{t('需补证', 'needs repair')}</span></div>}</div>
      <div className="inspector-tabs" role="tablist">{[['outcome', '结果', 'Outcome'], ['sources', '资料', 'Sources'], ['facts', '候选字段', 'Candidates'], ['memory', '上下文', 'Context'], ['tools', '执行记录', 'Activity']].map(([key, zh, en], index, tabs) => <button key={key} id={`research-tab-${key}`} role="tab" aria-controls="research-notebook-panel" tabIndex={tab === key ? 0 : -1} aria-selected={tab === key} onClick={() => setTab(key)} onKeyDown={event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length; setTab(tabs[next][0]); event.currentTarget.parentElement.children[next].focus() } }}>{t(zh, en)}</button>)}</div>
      <div className="research-inspector-body" id="research-notebook-panel" role="tabpanel" aria-labelledby={`research-tab-${tab}`} tabIndex={0}>
        {tab === 'outcome' && <OutcomePanel session={session} report={snapshot?.result_document} busy={busy} exporting={exporting} t={t} onDownload={format => void downloadReport(format)} onStart={() => connection ? void submitValuation() : onConnect()} onConnect={onConnect} connected={Boolean(connection)}/>}
        {tab === 'sources' && <>{session?.documents.map(doc => <div className="research-source" key={doc.file_id}><button onClick={() => void viewSource(doc.file_id)}><Icon name="file" size={18}/><span><b>{doc.name}</b><small>{doc.block_count} {t('个原文片段', 'source blocks')} · SHA-256 {doc.sha256?.slice(0, 12) || t('待记录', 'pending')}</small></span><Icon name="chevron" size={15}/></button>{doc.warnings.map(w => <p className="research-warning" key={w}>{w}</p>)}</div>)}{!session?.documents.length && <div className="research-empty source-empty"><span className="empty-source-icon"><Icon name="folder" size={27}/></span><h3>{t('让每个结论，都有出处', 'A source for every finding')}</h3><p>{t('无需先上传文件。自动检索取得的原文和你的附件都会保留在这里。', 'No upload required. Retrieved original sources and your attachments will appear here.')}</p><button className="button secondary" disabled={busy} onClick={() => fileRef.current?.click()}><Icon name="upload" size={16}/>{t('补充手头资料（可选）', 'Add optional sources')}</button><small>PDF · DOCX · XLSX · CSV · TSV · HTML · JSON · TXT · MD</small><small>{t('文本型 PDF 可读取，扫描件 OCR 待接入', 'Text PDFs supported; scanned PDFs need OCR')}</small></div>}{source && <div className="source-preview"><button className="research-text-button" onClick={() => setSource(null)}>{t('收起原文', 'Close preview')}</button>{source.blocks.map(block => <div key={block.block_id}><small>{JSON.stringify(block.location)} · {block.block_id}</small><pre>{block.text}</pre></div>)}<div className="inline-actions"><button disabled={source.offset === 0} onClick={() => void viewSource(source.fileId, Math.max(0, source.offset - 12))}>{t('上一页', 'Previous')}</button><button disabled={source.offset + 12 >= source.total} onClick={() => void viewSource(source.fileId, source.offset + 12)}>{t('下一页', 'Next')}</button></div></div>}</>}
        {tab === 'facts' && <CandidateReview facts={session?.facts || []} t={t} busy={busy} onSource={fact => { const [fileId, block] = fact.block_id.split(':'); void viewSource(fileId, Math.floor(Math.max(0, Number(block) - 1) / 12) * 12) }} onCorrect={fact => { setInput(input + (input ? '\n' : '') + t(`请重新核对 ${fact.fact_id}（${fact.metric}，${fact.period}，原值 ${fact.raw_value} ${fact.unit}）。我的更正理由：`, `Please recheck ${fact.fact_id} (${fact.metric}, ${fact.period}, ${fact.raw_value} ${fact.unit}). My correction: `)); setReplyQuestionId(session?.question?.question_id || ''); setMobilePane('chat'); inputRef.current?.focus() }}/ >}
        {tab === 'memory' && <>{!session?.memory?.length && <p className="research-empty">{t('长期有效的目标、偏好、约束和决定会显示在这里；财务数值仍进入候选字段，不会混入记忆。', 'Durable goals, preferences, constraints and decisions appear here. Financial values remain separate candidates.')}</p>}{session?.memory?.map(item => <div className="research-fact confirmed" key={item.key}><div><b>{item.key}</b><span>{({goal:t('目标','Goal'),preference:t('偏好','Preference'),constraint:t('约束','Constraint'),decision:t('决定','Decision'),definition:t('定义','Definition')})[item.kind] || item.kind}</span></div><p>{item.content}</p><small>{item.source_message_id} · {item.updated_at}</small></div>)}</>}
        {tab === 'tools' && <>{!activity.length && <p className="research-empty">{t('实际执行后显示 Agent 决策、工具、恢复和耗时。', 'Agent decisions, tool calls, recovery and timing appear after execution.')}</p>}{activity.map(item => { const key = item.tool_call_id || `${item.type}-${item.sequence}`; const label = item.tool ? (toolNames[item.tool] ? t(...toolNames[item.tool]) : item.tool) : t(...activityNames[item.type]); return <button className={`research-tool ${item.status}`} key={key} onClick={() => void viewEvent(item)}><span className="tool-dot"/><span><b>{label}</b><small>{({completed:t('已完成','Completed'),running:t('执行中','Running'),failed:t('未完成','Failed'),waiting:t('待处理','Waiting'),cached:t('已复用','Reused')})[item.status] || (item.type === 'agent.recovery_required' ? t('待你确认','Your choice') : t('已记录','Recorded'))} {item.duration_ms != null ? `· ${item.duration_ms} ms` : ''}</small></span><Icon name="chevron" size={14}/></button> })}{detail && <div className="activity-detail"><b>{detail.message || t('执行证据', 'Execution evidence')}</b><details><summary>{t('查看调用参数与记录', 'View call parameters and record')}</summary><pre className="tool-detail">{JSON.stringify(detail, null, 2)}</pre></details></div>}</>}
        {session?.gaps.length > 0 && <div className="research-gaps"><b>{t('资料缺口', 'Open gaps')}</b>{session.gaps.map(gap => <p key={gap}>· {gap}</p>)}</div>}
      </div>
      <div className="research-inspector-footer">{id && <><button className="research-valuation-button" disabled={busy || loading || !session || Boolean(exporting) || (Boolean(session?.question) && !reportAvailable)} onClick={() => reportAvailable ? void downloadReport('pdf') : void submitValuation()}><Icon name="chart" size={16}/>{reportAvailable ? t('下载 PDF 结果说明', 'Download outcome PDF') : session?.valuation_run_id ? t('打开已提交估值', 'Open submitted valuation') : session?.pending_action === 'valuation' ? t('继续完成估值', 'Continue valuation') : t('开始正式估值', 'Start formal valuation')}<Icon name="arrow" size={14}/></button><div className="inline-actions"><button disabled={busy || loading || !session} onClick={() => void send({ content: '/prepare' })}>{t('检查准备情况', 'Check preparation')}</button><button disabled={Boolean(exporting)} onClick={() => void downloadReport('pdf')}>{exporting ? t('生成中…', 'Generating…') : t('下载报告', 'Download report')}</button><button onClick={() => { setTab('outcome'); setMobilePane('notebook') }}>{t('查看结果', 'View outcome')}</button></div></>}<details className="research-source-policy"><summary>{t('数据来源说明', 'About data sources')}</summary><p>{t('A 股年报优先从巨潮官方公告目录定位，无需 API Key；Tavily 用于业务、政策和补充检索，Tushare 用于结构化取数。候选确认前不会进入计算。', 'A-share annual reports are located through CNINFO’s official catalogue without an API key. Tavily supports broader business and policy research, while Tushare provides structured data. Candidates stay out of calculations until confirmed.')}</p></details><button className="research-text-button" onClick={onWizard} disabled={busy}>{t('打开结构化估值', 'Open structured valuation')}<Icon name="arrow" size={14}/></button>{history.length > 0 && <select aria-label={t('继续已有研究', 'Resume research')} value={id || ''} disabled={busy} onChange={event => event.target.value && selectHistory(event.target.value)}><option value="">{t('继续已有研究…', 'Resume a study…')}</option>{history.map(item => <option key={item.session_id} value={item.session_id}>{item.draft.company || item.draft.ticker || item.draft.objective || t('未命名研究', 'Untitled study')} · v{item.revision}</option>)}</select>}</div>
    </aside>
  </div>
}
