import { useEffect, useRef, useState, useCallback } from 'react'
import Icon from './Icons'
import Wizard from './Wizard'
import ResearchDesk from './ResearchDesk'
import Inspector from './Inspector'
import { Results, Evidence } from './Analysis'
import { Modal, Status, ErrorNotice, Empty } from './ui'
import useResearch from './useResearch'
import { api, post, downloadJson } from './api'
import { TERMINAL, statusLabel, initialDraft } from './domain'
import { MODEL_PRESETS, COMPANY_PRESETS } from './presets'

function ConnectionModal({ t, onClose, onConnect, session, onDisconnect }) {
  const [config, setConfig] = useState({ provider: session?.provider || 'openai_compatible', base_url: session?.base_url || 'https://api.openai.com/v1', model: session?.model || '', api_key: '', thinking: 'auto' })
  const [company, setCompany] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const update = (key, value) => setConfig(previous => ({ ...previous, [key]: value }))
  const updateModel = value => {
    const preset = MODEL_PRESETS.find(item => item.value === value)
    setConfig(previous => ({ ...previous, model: value, ...(preset ? { provider: preset.provider, base_url: preset.base_url } : {}) }))
  }
  const connect = async event => {
    event.preventDefault(); setBusy(true); setError('')
    try {
      await post('/api/model-connections/test', config)
      const next = await post('/api/model-sessions', config)
      if (session) await api(`/api/model-sessions/${session.session_id}`, { method: 'DELETE' }).catch(() => {})
      onConnect(next, company.trim())
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return <Modal title={t('连接研究模型', 'Connect a research model')} onClose={busy ? () => {} : onClose}>
    <p className="modal-intro">{t('先选择或输入模型，再选填研究对象。API Key 只在当前后端会话使用。', 'Choose or type a model, then optionally set a research subject. The API key stays in the current backend session.')}</p>
    {session && <div className="connected-banner"><Icon name="check" size={17}/><span>{session.model}</span><button type="button" onClick={onDisconnect} disabled={busy}>{t('断开', 'Disconnect')}</button></div>}
    <form onSubmit={connect}>
      <label className="field">{t('模型名称', 'Model name')}<input required list="valuation-model-presets" value={config.model} onChange={e => updateModel(e.target.value)} placeholder={t('输入模型名，或从建议中选择', 'Type a model name or choose a suggestion')} maxLength={200}/></label>
      <datalist id="valuation-model-presets">{MODEL_PRESETS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</datalist>
      <label className="field">{t('研究对象（可选）', 'Research subject (optional)')}<input list="valuation-company-presets" value={company} onChange={e => setCompany(e.target.value)} placeholder={t('公司名或 A 股代码，例如 600519', 'Company or A-share ticker, e.g. 600519')} maxLength={120}/></label>
      <datalist id="valuation-company-presets">{COMPANY_PRESETS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</datalist>
      <label className="field">{t('接口类型', 'Provider protocol')}<select value={config.provider} onChange={e => update('provider', e.target.value)}><option value="openai_compatible">OpenAI Compatible</option><option value="openai">OpenAI</option></select></label>
      <label className="field">Base URL<input type="url" required value={config.base_url} onChange={e => update('base_url', e.target.value)}/></label>
      <label className="field">{t('思考模式', 'Thinking mode')}<select value={config.thinking} onChange={e => update('thinking', e.target.value)}><option value="auto">Auto</option><option value="enabled">Enabled</option><option value="disabled">Disabled</option></select></label>
      <label className="field">API Key<input type="password" autoComplete="off" required value={config.api_key} onChange={e => update('api_key', e.target.value)} placeholder="sk-…"/></label>
      <p className="privacy-note"><Icon name="shield" size={15}/>{t('密钥只用于当前后端会话，不写入浏览器存储、任务或报告。连接时会验证工具调用能力。', 'Keys stay in the current backend session, outside browser storage, tasks and reports. Connections verify tool calling.')}</p>
      <ErrorNotice message={error}/><div className="modal-actions"><button className="button secondary" type="button" onClick={onClose} disabled={busy}>{t('取消', 'Cancel')}</button><button className="button primary" disabled={busy}>{busy ? t('正在验证连接…', 'Verifying connection…') : t('验证并连接', 'Verify & connect')}<Icon name="link" size={17}/></button></div>
    </form>
  </Modal>
}

function ReviewModal({ record, t, onClose, onSubmit, submitting }) {
  const [text, setText] = useState(JSON.stringify({ reason: t('补充与更正数据', 'Correct and supplement inputs'), changes: { assumptions: { wacc: record.result?.assumptions.wacc || '0.095' } } }, null, 2))
  const [error, setError] = useState('')
  const submit = async event => { event.preventDefault(); setError(''); try { await onSubmit(JSON.parse(text)) } catch (err) { setError(err.message) } }
  return <Modal title={t('更正数据并创建新版本', 'Correct data in a new revision')} onClose={submitting ? () => {} : onClose} className="wide-modal"><p className="modal-intro">{t('修改 changes 中的财务、同业或假设字段，填写更正原因。旧结果会保留。', 'Edit financials, peers or assumptions in changes, and give a reason. Previous results are preserved.')}</p><form onSubmit={submit}><label className="field">{t('更正文件 JSON', 'Revision JSON')}<textarea className="code-editor" value={text} onChange={e => setText(e.target.value)} rows={12} spellCheck={false} required/></label><label className="import-inline"><Icon name="upload" size={16}/>{t('从 JSON 文件导入', 'Import JSON file')}<input type="file" accept=".json" onChange={async e => { const file = e.target.files?.[0]; if (!file) return; if (file.size > 50 * 1024 * 1024) { setError(t('文件过大', 'File too large')); return } try { setText((await file.text()).replace(/^\uFEFF/, '')) } catch (err) { setError(err.message) } }}/></label><ErrorNotice message={error}/><div className="modal-actions"><button type="button" className="button secondary" onClick={onClose} disabled={submitting}>{t('取消', 'Cancel')}</button><button className="button primary" disabled={submitting}>{submitting ? t('正在提交…', 'Submitting…') : t('保存并重算', 'Save & recalculate')}</button></div></form></Modal>
}

function Conversation({ record, runId, messages, revisions, pending, sending, loading, t, onSend, onSelect, onReview, onResume, session, onConnect, wizard }) {
  const [input, setInput] = useState('')
  const scroll = useRef(null)
  const nearBottom = useRef(true)
  const running = record && !TERMINAL.has(record.status)
  const needsModel = record?.request.mode === 'live' && !session
  const disabled = !record || running || sending
  const lastMessageId = messages.at(-1)?.message_id
  useEffect(() => { if (nearBottom.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight }, [lastMessageId, sending, runId])
  const submit = async event => {
    event.preventDefault()
    if (!input.trim() || disabled) return
    if (needsModel) { onConnect(); return }
    try { await onSend(input.trim()); setInput('') } catch { /* Keep the draft for retry. */ }
  }
  return <section className="conversation-panel"><div className="panel-title"><span><Icon name="chat"/>{t('研究对话', 'Research conversation')}</span>{record ? <select className="version-select" aria-label={t('研究版本', 'Study revision')} value={record.run_id} disabled={sending} onChange={e => onSelect(revisions.find(r => r.run_id === e.target.value))}>{revisions.map(r => <option key={r.run_id} value={r.run_id}>v{r.revision} · {statusLabel(r.status, t)}</option>)}</select> : <span className="subtle">ValuationAgent</span>}</div><div className="chat-scroll" ref={scroll} onScroll={() => { const el = scroll.current; nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 90 }}>
    {!runId ? <><div className="agent-message"><span className="avatar"><Icon name="spark"/></span><div><b>ValuationAgent</b><p>{t('先确定研究对象。接下来，我会协助你确认数据、设置经营假设，并运行估值模型。', 'First, select your research subject. Then we will review data, set assumptions and run the valuation.')}</p></div></div>{wizard}</> : <>{loading && !record && <div className="loading-state"><span className="mini-spinner"/>{t('正在载入研究…', 'Loading study…')}</div>}{record && <div className="conversation-context"><Icon name="folder" size={16}/><span>{record.request.company.name || record.request.company.ticker}</span><small>{record.request.mode.toUpperCase()} · {record.request.valuation_date}</small></div>}{messages.filter(m => m.role === 'user' || m.role === 'assistant').map(message => <div key={message.message_id} className={`agent-message ${message.role === 'user' ? 'user-message' : ''}`}><span className="avatar">{message.role === 'user' ? t('我', 'Me') : <Icon name="spark"/>}</span><div className="message-content"><div className="message-meta"><b>{message.role === 'user' ? t('你', 'You') : 'ValuationAgent'}</b><span>v{message.revision} · {new Date(message.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span></div><p>{message.content}</p>{message.related_run_id && <button className="revision-link" onClick={() => onSelect(revisions.find(r => r.run_id === message.related_run_id))}>{t('查看重算版本', 'Open recalculated revision')}<Icon name="arrow" size={14}/></button>}</div></div>)}{pending && !messages.slice(-4).some(m => m.role === 'user' && m.content === pending) && <div className="agent-message user-message"><span className="avatar">{t('我', 'Me')}</span><div><b>{t('你', 'You')}</b><p>{pending}</p></div></div>}{sending && <div className="working-message" role="status"><span className="mini-spinner"/>{t('正在处理请求；执行进度显示在右侧。', 'Processing your request. Follow execution in the workflow panel.')}</div>}
      {record && ['waiting_review','failed'].includes(record.status) && <div className="review-card"><div><Icon name="alert"/><h3>{t('这一步需要确认', 'This step needs attention')}</h3></div><p>{(record.review || record.error)?.message}</p><div className="inline-actions"><button className="button primary" onClick={onReview} disabled={sending}>{t('更正数据', 'Correct inputs')}</button><button className="button secondary" onClick={onResume} disabled={sending}><Icon name="refresh" size={16}/>{t('恢复执行', 'Resume')}</button></div></div>}
      {record?.result && !sending && <div className="suggestions" aria-label={t('继续研究', 'Continue research')}>{[[t('解释核心假设','Explain assumptions'), t('本次用了哪些假设？','What assumptions were used?')], [t('查看敏感性','Explain sensitivity'), t('解释本次敏感性分析','Explain sensitivity')], ['WACC → 8%', 'Set WACC to 8%']].map(([label, content]) => <button key={label} disabled={running} onClick={() => needsModel ? onConnect() : void onSend(content).catch(() => {})}>{label}<Icon name="arrow" size={13}/></button>)}</div>}
    </>}
  </div><form className="composer" onSubmit={submit}>{needsModel && <button className="connect-prompt" type="button" onClick={onConnect}><Icon name="link" size={15}/>{t('连接模型以继续 Live Agent 对话', 'Connect a model to continue Live Agent conversation')}</button>}<textarea aria-label={t('发送研究问题', 'Send a research question')} maxLength={8000} value={input} disabled={disabled} onChange={e => setInput(e.target.value)} placeholder={!record ? t('完成配置后，继续提问或调整假设…', 'Complete setup to ask questions or revise assumptions…') : running ? t('正在运行研究，请等待当前流程完成…', 'Research is running. Please wait for completion…') : t('询问估值依据，或输入「把 WACC 改为 8%」', 'Ask about the valuation, or type “Set WACC to 8%”')} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); e.currentTarget.form.requestSubmit() } }}/><div><small>{record?.request.mode === 'live' ? 'Live Agent' : record ? t('确定性结果问答', 'Deterministic results Q&A') : t('Enter 发送 · Shift + Enter 换行', 'Enter to send · Shift + Enter for a new line')}</small><button className="send-button" type="submit" disabled={disabled || !input.trim()} aria-label={t('发送消息', 'Send message')}><Icon name="send" size={18}/></button></div></form></section>
}

export default function App() {
  const [chosenLanguage, chooseLanguage] = useState(null)
  const [draft, setDraft] = useState(() => initialDraft('zh-CN'))
  const [draftVersion, setDraftVersion] = useState(0)
  const [showWizard, setShowWizard] = useState(false)
  const [runId, setRunId] = useState(() => window.location.hash.match(/^#(run_[a-f0-9]+)$/)?.[1] || null)
  const [view, setView] = useState('workspace')
  const [runs, setRuns] = useState([])
  const [health, setHealth] = useState('loading')
  const [session, setSession] = useState(null)
  const [researchSeed, setResearchSeed] = useState('')
  const [modal, setModal] = useState(null)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [sending, setSending] = useState(false)
  const [pending, setPending] = useState('')
  const [mobileNav, setMobileNav] = useState(false)
  const pendingStart = useRef(null)
  const sendingLock = useRef(false)
  const mounted = useRef(true)
  const research = useResearch(runId)
  const record = research.record?.run_id === runId ? research.record : null
  const events = record ? research.events : []
  const language = chosenLanguage || record?.request.language || 'zh-CN'
  const setLanguage = value => { chooseLanguage(value); setDraft(previous => ({ ...previous, language: value })) }
  const t = (zh, en) => language === 'en-US' ? en : zh
  const syncRuns = useCallback(async () => {
    try { const [check, items] = await Promise.all([api('/health'), api('/api/runs')]); if (!mounted.current) return; setHealth(check.status === 'ok' ? 'online' : 'offline'); setRuns(items) } catch { if (mounted.current) setHealth('offline') }
  }, [])
  useEffect(() => { mounted.current = true; const initial = setTimeout(syncRuns, 0); const timer = setInterval(syncRuns, 8000); return () => { mounted.current = false; clearTimeout(initial); clearInterval(timer) } }, [syncRuns])
  useEffect(() => { document.documentElement.lang = language }, [language])
  const companyName = record?.request.company.name
  useEffect(() => { document.title = companyName ? `${companyName} · ValuationAgent` : 'ValuationAgent · 估值研究工作台' }, [companyName])
  const persistedId = record?.run_id, persistedStatus = record?.status
  useEffect(() => { if (persistedId && TERMINAL.has(persistedStatus)) { const timer = setTimeout(syncRuns, 100); return () => clearTimeout(timer) } }, [persistedId, persistedStatus, syncRuns])
  const activate = id => { setRunId(id); window.history.replaceState(null, '', id ? `#${id}` : window.location.pathname); setMobileNav(false) }
  const selectRun = selected => { if (!selected || sending || submitting) return; setLanguage(selected.request.language); activate(selected.run_id); setError(''); setModal(null) }
  const newTask = () => { if (sending || submitting) return; activate(null); setShowWizard(false); setResearchSeed(''); setDraft(initialDraft(language)); setDraftVersion(value => value + 1); setView('workspace'); setError('') }
  const create = async (request, connected = session) => {
    if (request.mode === 'live' && !connected) { pendingStart.current = request; setModal('connection'); return }
    setSubmitting(true); setError('')
    try { const accepted = await post('/api/runs', { request, ...(request.mode === 'live' ? { model_session_id: connected.session_id } : {}) }); activate(accepted.run_id); setView('workspace'); void syncRuns() } catch (err) { setError(err.message) } finally { setSubmitting(false) }
  }
  const attach = async id => {
    if (record?.request.mode !== 'live') return
    if (!session) { setModal('connection'); throw new Error(t('请先连接研究模型。', 'Connect a research model first.')) }
    try { await post(`/api/runs/${id}/model-session`, { model_session_id: session.session_id }) }
    catch (err) { if (err.status === 404 && err.message === 'model session not found') { setSession(null); setModal('connection') } throw err }
  }
  const send = async content => {
    if (sendingLock.current || !record || !content.trim() || !TERMINAL.has(record.status)) throw new Error(t('请等待当前任务结束。', 'Wait for the current run.'))
    sendingLock.current = true
    setSending(true)
    try { await attach(runId) } catch (err) { sendingLock.current = false; setSending(false); setError(err.message); throw err }
    setSending(true); setPending(content); setError('')
    const origin = runId, originalRevision = record.revision
    let polling = false, finished = false
    const timer = setInterval(async () => {
      if (polling) return
      polling = true
      try {
        const revisions = await api(`/api/runs/${origin}/revisions`)
        const child = revisions.filter(r => r.parent_run_id === origin && r.revision > originalRevision && r.revision_reason === content).at(-1)
        if (child && mounted.current && !finished) activate(child.run_id)
      } catch { /* The final message response remains authoritative. */ } finally { polling = false }
    }, 900)
    try { const reply = await post(`/api/runs/${origin}/messages`, { content }); if (reply.related_run_id) activate(reply.related_run_id); research.refresh(); void syncRuns(); return reply }
    catch (err) { setError(err.message); research.refresh(); throw err }
    finally { finished = true; clearInterval(timer); sendingLock.current = false; setSending(false); setPending('') }
  }
  const resume = async () => {
    setSubmitting(true); setError('')
    try { await attach(runId); await post(`/api/runs/${runId}/resume`, session ? { model_session_id: session.session_id } : {}); research.refresh() } catch (err) { setError(err.message) } finally { setSubmitting(false) }
  }
  const revise = async revision => {
    setSubmitting(true)
    try { await attach(runId); const accepted = await post(`/api/runs/${runId}/revisions`, revision); activate(accepted.run_id); setModal(null); void syncRuns() } finally { setSubmitting(false) }
  }
  const exportResult = () => downloadJson(`valuation-${record.run_id}-v${record.revision}.json`, { run: record, artifacts: research.artifacts, events })
  const connect = (connected, initialCompany = '') => { setSession(connected); setResearchSeed(initialCompany); setModal(null); setError(''); if (pendingStart.current) { const request = pendingStart.current; pendingStart.current = null; void create(request, connected) } }
  const closeModal = () => { setModal(null); pendingStart.current = null }
  const navigation = [['workspace','chat','研究工作台','Workspace'],['results','chart','估值分析','Valuation'],['evidence','shield','数据与依据','Evidence']]
  const sidebar = <><a className="brand" href="#" onClick={e => { e.preventDefault(); newTask() }}><span className="brand-mark">V<span>/</span>A</span><div><strong>ValuationAgent</strong><small>VALUATION RESEARCH</small></div></a><button className="new-task" onClick={newTask} disabled={sending || submitting} title={t('新建估值研究', 'New valuation')}><Icon name="plus"/><span>{t('新建估值研究', 'New valuation')}</span></button><div className="nav-label">WORKSPACE</div><nav>{navigation.map(([id,icon,zh,en]) => <button className={view === id ? 'active' : ''} key={id} disabled={!runId && id !== 'workspace'} onClick={() => { setView(id); setMobileNav(false) }} title={t(zh,en)} aria-current={view === id ? 'page' : undefined}><Icon name={icon}/><span>{t(zh,en)}</span></button>)}</nav><div className="history-section"><div className="nav-label">{t('最近研究', 'RECENT STUDIES')}<button onClick={() => setModal('history')} title={t('全部任务', 'All studies')}><Icon name="clock" size={14}/></button></div>{runs.length ? runs.slice(0,6).map(run => <button className={`history-item ${run.run_id === runId ? 'selected' : ''}`} key={run.run_id} onClick={() => selectRun(run)} disabled={sending || submitting}><Icon name="chat" size={15}/><span>{run.request.company.name || run.request.company.ticker || t('未命名', 'Untitled')}<small>v{run.revision} · {run.request.mode.toUpperCase()}</small></span></button>) : <p>{t('你的研究会保存在这里', 'Your studies will appear here')}</p>}</div><button className="sidebar-bottom" onClick={() => setModal('connection')}><Icon name={session ? 'link' : 'layers'}/><span>{session?.model || t('连接研究模型', 'Connect research model')}<small>{session ? t('工具调用已验证', 'Tool calling verified') : 'ValuationAgent · 0.2'}</small></span></button></>
  const wizard = <Wizard key={`draft-${draftVersion}`} draft={draft} setDraft={setDraft} t={t} onStart={create} submitting={submitting} onLanguage={setLanguage}/>
  const conversation = <Conversation key={runId || `draft-${draftVersion}`} {...{record, runId, t, sending, pending, session, wizard}} messages={record ? research.messages : []} revisions={record ? research.revisions : []} loading={research.loading} onSend={send} onSelect={selectRun} onReview={() => setModal('review')} onResume={resume} onConnect={() => setModal('connection')}/>
  const activeNav = navigation.find(([id]) => id === view)
  return <div className="app-shell"><aside className="sidebar">{sidebar}</aside>{mobileNav && <div className="mobile-navigation"><button className="mobile-backdrop" onClick={() => setMobileNav(false)} aria-label={t('关闭导航', 'Close navigation')}/><aside className="sidebar mobile-sidebar">{sidebar}</aside></div>}<div className="main-shell"><header className="topbar"><div className="breadcrumbs"><button className="mobile-menu" onClick={() => setMobileNav(true)} aria-label={t('打开导航', 'Open navigation')}><Icon name="menu"/></button>{t('项目空间', 'Project space')}<span>/</span><b>{t(activeNav[2], activeNav[3])}</b></div><div className="top-actions"><span className={`service-state ${health}`} title={t('后端连接状态', 'Backend connection')}><i/>{health === 'online' ? t('服务在线', 'Online') : health === 'offline' ? t('服务未连接', 'Offline') : t('连接中', 'Connecting')}</span><select aria-label={t('界面语言', 'Interface language')} value={language} onChange={e => setLanguage(e.target.value)}><option value="zh-CN">简体中文</option><option value="en-US">English</option></select><button className="button secondary model-button" onClick={() => setModal('connection')}><Icon name="settings"/>{session ? t('模型已连接', 'Model connected') : t('模型连接', 'Connect model')}</button></div></header><main className="page"><div className="page-heading"><div><div className="eyebrow">RESEARCH WORKSPACE</div><h1>{record ? record.request.company.name || record.request.company.ticker : t('开始一项估值研究', 'Start a valuation study')}</h1><p>{record ? `${record.request.valuation_date} · ${record.request.company.currency} · ${record.request.mode === 'demo' ? t('合成数据演示', 'Synthetic data demo') : t('基于已提交资料', 'Based on submitted data')} · v${record.revision}` : t('确认数据和假设，与 Agent 一起形成可追溯的估值。', 'Choose your data and assumptions, then work with your Agent.')}</p></div><div className="heading-actions">{record ? <Status status={record.status} t={t}/> : <span className="tag">{t('新研究', 'New study')}</span>}{record?.result && <button className="button secondary export-button" onClick={exportResult} title={t('导出 JSON 复算包', 'Export JSON research package')}><Icon name="download" size={16}/><span>{t('导出研究', 'Export')}</span></button>}</div></div><ErrorNotice message={error || research.error} onDismiss={() => { setError(''); research.refresh() }}/>{health === 'offline' && <div className="offline-notice"><Icon name="link" size={16}/><span>{t('无法连接估值服务。请启动后端；你的当前配置会保留。', 'Cannot reach the valuation service. Start the backend; your current selections are preserved.')}</span><button onClick={syncRuns}>{t('重试', 'Retry')}</button></div>}
    {!runId && !showWizard ? <ResearchDesk key={`research-${draftVersion}-${researchSeed}`} language={language} t={t} connection={session} initialCompany={researchSeed} onConnect={() => setModal('connection')} onWizard={() => setShowWizard(true)} onBusyChange={setSending} onRestoreLanguage={chooseLanguage}/> : view === 'workspace' ? <div className="workspace-grid">{conversation}<Inspector record={record} events={events} artifacts={research.artifacts} t={t}/></div> : <div className="report-layout"><div className="report-surface">{view === 'results' ? <Results record={record} t={t}/> : <Evidence record={record} events={events} artifacts={research.artifacts} t={t}/>}</div><div className="report-conversation">{conversation}</div></div>}
  </main></div>
  {modal === 'connection' && <ConnectionModal t={t} session={session} onClose={closeModal} onConnect={connect} onDisconnect={async () => { try { await api(`/api/model-sessions/${session.session_id}`, { method: 'DELETE' }); setSession(null); setModal(null) } catch (err) { setError(err.message) } }}/>} {modal === 'review' && record && <ReviewModal {...{record,t,submitting}} onClose={closeModal} onSubmit={revise}/>} {modal === 'history' && <Modal title={t('研究历史', 'Study history')} onClose={closeModal} className="wide-modal">{runs.length ? <div className="history-table">{runs.map(run => <button key={run.run_id} onClick={() => selectRun(run)} disabled={sending || submitting}><span><b>{run.request.company.name || run.request.company.ticker}</b><small>{run.request.valuation_date} · {run.request.mode.toUpperCase()} · v{run.revision}</small></span><Status status={run.status} t={t}/><Icon name="chevron" size={16}/></button>)}</div> : <Empty title={t('还没有研究任务', 'No studies yet')}>{t('创建研究后，记录会自动保留。', 'New studies are saved automatically.')}</Empty>}</Modal>}
  </div>
}
