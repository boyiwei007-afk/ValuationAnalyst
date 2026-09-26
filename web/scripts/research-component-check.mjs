import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { Window } from 'happy-dom'

// Virtual DOM only: no browser, backend, external resource or model is contacted.
const window = new Window({ url: 'http://valuation.test/', settings: {
  disableJavaScriptEvaluation: true,
  disableJavaScriptFileLoading: true,
  disableCSSFileLoading: true,
  disableIframePageLoading: true,
} })
const originalGlobals = new Map()
for (const key of ['window', 'document', 'navigator', 'sessionStorage', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'Node', 'Event', 'KeyboardEvent', 'MouseEvent', 'File', 'Blob', 'FormData']) {
  originalGlobals.set(key, Object.getOwnPropertyDescriptor(globalThis, key))
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value: key === 'window' ? window : window[key] })
}
originalGlobals.set('IS_REACT_ACT_ENVIRONMENT', Object.getOwnPropertyDescriptor(globalThis, 'IS_REACT_ACT_ENVIRONMENT'))
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const originalFetch = globalThis.fetch
const { createElement, act } = await import('react')
const { createRoot } = await import('react-dom/client')
const { renderToStaticMarkup } = await import('react-dom/server')
const vite = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)), server: { middlewareMode: true }, appType: 'custom', logLevel: 'error' })
const roots = new Set()
const t = (_zh, en) => en
const noop = () => {}
let checks = 0
const pass = label => { checks++; console.log(`PASS: ${label}`) }

function mount() {
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  roots.add(root)
  return { host, root }
}
async function render(root, component, props) {
  await act(async () => { root.render(createElement(component, props)) })
}
async function click(element) {
  assert.ok(element, 'Expected clickable element')
  await act(async () => { element.click() })
}
async function type(element, value) {
  assert.ok(element, 'Expected input element')
  const prototype = element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype
  await act(async () => {
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, value)
    element.dispatchEvent(new window.Event('input', { bubbles: true }))
  })
}
async function attach(host, name = 'draft-financials.txt') {
  const field = host.querySelector('input[type="file"]')
  Object.defineProperty(field, 'files', { configurable: true, value: [new window.File(['Revenue 100'], name, { type: 'text/plain' })] })
  await act(async () => { field.dispatchEvent(new window.Event('change', { bubbles: true })) })
  assert.match(host.querySelector('.research-attachments')?.textContent || '', new RegExp(name.replaceAll('.', '\\.')))
}
const question = {
  question_id: 'question_scope', kind: 'task', title: 'Confirm the research scope',
  options: [{ id: 'accept', label: 'Use these settings' }, { id: 'revise', label: 'Change the scope' }, { id: 'defer', label: 'Decide later' }],
  proposed_draft: { company: 'Example Company', ticker: '600519', methods: ['dcf'] },
}
const baseProps = { language: 'en-US', t, connection: null, onConnect: noop, onConnectionLost: noop, onWizard: noop, onBusyChange: noop, onRestoreLanguage: noop, onHistoryChange: noop, onSessionChange: noop }

try {
  const { default: MessageBody } = await vite.ssrLoadModule('/src/MessageBody.jsx')
  const { default: ResearchDesk, ConfirmationCard } = await vite.ssrLoadModule('/src/ResearchDesk.jsx')
  const readable = document.createElement('div')
  readable.innerHTML = renderToStaticMarkup(createElement(MessageBody, { content: '## Review\n\n- **Revenue:** 100\n- Confirm the source\n\n| Year | Revenue |\n| --- | ---: |\n| 2024 | 100 |\n\n`source-1`' }))
  assert.equal(readable.querySelector('h2')?.textContent, 'Review')
  assert.equal(readable.querySelectorAll('ul > li').length, 2)
  assert.equal(readable.querySelector('strong')?.textContent, 'Revenue:')
  assert.equal(readable.querySelector('.message-table table tbody td')?.textContent, '2024')
  assert.equal(readable.querySelector('code')?.textContent, 'source-1')
  pass('Markdown headings, lists, emphasis, tables and code render as readable semantic content')

  const unsafe = document.createElement('div')
  unsafe.innerHTML = renderToStaticMarkup(createElement(MessageBody, { content: '<script>globalThis.__messageScriptRan = true</script>\n\n<img src="https://untrusted.example/raw.png" onerror="alert(1)">\n\n[unsafe](javascript:alert%281%29) [data](data:text/html,hello) [local](file:///private) [source](https://example.com/report)\n\n![Revenue chart](https://untrusted.example/chart.png)' }))
  assert.equal(unsafe.querySelectorAll('script,img,iframe,object,embed').length, 0)
  assert.equal(unsafe.querySelectorAll('a').length, 1)
  assert.equal(unsafe.querySelector('a').href, 'https://example.com/report')
  assert.equal(unsafe.querySelector('a').rel, 'noopener noreferrer')
  assert.equal(unsafe.querySelector('a').target, '_blank')
  assert.match(unsafe.textContent, /Revenue chart/)
  assert.equal(globalThis.__messageScriptRan, undefined)
  pass('Untrusted HTML, dangerous links and remote images cannot become executable/loading elements')

  const card = mount()
  const choices = [], freeText = []
  await render(card.root, ConfirmationCard, { question, t, busy: false, onAnswer: answer => choices.push(answer), onFreeText: questionId => freeText.push(questionId) })
  const group = card.host.querySelector('[role="radiogroup"]')
  assert.equal(group.getAttribute('aria-label'), question.title)
  const radios = [...group.querySelectorAll('[role="radio"]')]
  assert.equal(radios.length, 4)
  assert.equal(radios.at(-1).textContent.includes('Chat'), true)
  assert.deepEqual(radios.map(radio => radio.getAttribute('aria-checked')), ['false', 'false', 'false', 'false'])
  assert.deepEqual(radios.map(radio => radio.tabIndex), [0, -1, -1, -1])
  assert.equal(card.host.querySelector('.button.primary').disabled, true)
  await act(async () => { radios[0].dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })) })
  assert.equal(radios[1].getAttribute('aria-checked'), 'true')
  assert.equal(radios[1].tabIndex, 0)
  assert.equal(document.activeElement, radios[1])
  await click(card.host.querySelector('.button.primary'))
  assert.deepEqual(choices, [{ question_id: question.question_id, option_id: 'revise' }])
  await click(radios.at(-1))
  assert.deepEqual(freeText, [question.question_id])
  await render(card.root, ConfirmationCard, { question, t, busy: true, onAnswer: noop, onFreeText: noop })
  assert.ok([...card.host.querySelectorAll('button')].every(button => button.disabled))
  pass('Confirmation radios expose state, support arrow-key focus and block submission while busy')

  const valuationCard = mount()
  const plan = { methods: ['dcf'], baseline_period: '2025-12-31', valuation_date: '2026-09-24',
    requested_methods: ['dcf', 'pe'], excluded_methods: { pe: 'Only two verified FY peers were available.' }, degraded: true,
    assumptions: { wacc: '0.10', terminal_growth: '0.02', revenue_growth_scenarios: {
      pessimistic: Array(10).fill('0.01'), base: Array(10).fill('0.05'), optimistic: Array(10).fill('0.08') } },
    financials: { revenue: '10000000', common_shares: '1000000' },
    forecast_rationale: '<script>bad()</script> Evidence-based model assumptions', risks: ['Only one year of history'],
  }
  const valuationQuestion = { ...question, kind: 'facts', proposed_draft: null, valuation_review: plan,
    options: [{ id: 'accept', label: 'Confirm plan and calculate' }, { id: 'defer', label: 'Revise the plan first' }] }
  await render(valuationCard.root, ConfirmationCard, { question: valuationQuestion, t, busy: false, onAnswer: noop, onFreeText: noop })
  assert.equal(valuationCard.host.querySelectorAll('.valuation-plan-review tbody tr').length, 10)
  assert.match(valuationCard.host.textContent, /10\.00% \/ 2\.00%/)
  assert.match(valuationCard.host.textContent, /Forecasts are assumptions, not historical facts/)
  assert.match(valuationCard.host.textContent, /Only one year of history/)
  assert.match(valuationCard.host.textContent, /Data-limited fallback plan/)
  assert.match(valuationCard.host.textContent, /Only two verified FY peers/)
  assert.equal(valuationCard.host.querySelectorAll('script').length, 0)
  assert.equal(valuationCard.host.querySelector('.button.primary').disabled, true)
  pass('Valuation review shows all ten forecast years, WACC/g, baseline and risks without pre-approval or executable text')

  const calls = [], unexpected = []
  let state = {
    session: { session_id: 'research_abcdef', revision: 1, language: 'en-US', draft: { company: 'Example Company', ticker: '600519' }, documents: [], facts: [], memory: [], gaps: [], question },
    messages: [], events: [],
  }
  globalThis.fetch = async (path, options = {}) => {
    const method = options.method || 'GET'
    const body = options.body instanceof window.FormData ? options.body : options.body ? JSON.parse(options.body) : null
    calls.push({ path, method, body })
    let result
    if (path === '/api/research-sessions' && method === 'GET') result = []
    else if (path.startsWith('/api/research-sessions/research_abcdef?compact=true') && method === 'GET') result = state
    else if (path === '/api/research-sessions/research_abcdef/model-session' && method === 'POST') result = state
    else if (path === '/api/research-sessions/research_abcdef/turns' && method === 'POST') {
      state = { ...state, session: { ...state.session, revision: state.session.revision + 1, question: null, ...(body?.content?.startsWith('Start the formal valuation') ? { pending_action: 'valuation' } : {}) } }
      result = { request_id: body.request_id, execution: { status: 'completed', active: false } }
    } else if (path === '/api/files' && method === 'POST') result = { file_id: 'file_draft' }
    else { unexpected.push(`${method} ${path}`); throw new Error(`Unexpected request: ${method} ${path}`) }
    return { ok: true, status: 200, json: async () => structuredClone(result) }
  }
  window.fetch = globalThis.fetch

  const fresh = mount()
  await render(fresh.root, ResearchDesk, { ...baseProps, initialCompany: '' })
  await type(fresh.host.querySelector('textarea'), 'Keep this unsent research request')
  await attach(fresh.host)
  const updatedProps = { ...baseProps, connection: { session_id: 'model_offline_test', model: 'Mock model' }, initialCompany: 'New Company' }
  await render(fresh.root, ResearchDesk, updatedProps)
  assert.equal(fresh.host.querySelector('textarea').value, 'Keep this unsent research request')
  assert.equal(fresh.host.querySelector('#research-target-input').value, 'New Company')
  assert.match(fresh.host.querySelector('.research-attachments').textContent, /draft-financials\.txt/)
  assert.equal(calls.filter(call => call.method === 'POST').length, 0)
  pass('Model connection and company prop changes preserve the unsent composer and attachments')

  const study = mount()
  await render(study.root, ResearchDesk, { ...updatedProps, initialResearchId: 'research_abcdef' })
  await type(study.host.querySelector('textarea'), 'Draft for my next separate message')
  await attach(study.host, 'next-message.txt')
  await click(study.host.querySelector('[role="radio"]'))
  await click(study.host.querySelector('.confirmation-actions .button.primary'))
  const confirmation = calls.find(call => call.path.endsWith('/turns'))
  assert.ok(confirmation.body.request_id)
  assert.deepEqual(confirmation.body, { request_id: confirmation.body.request_id, question_id: question.question_id, option_id: 'accept', language: 'en-US', file_ids: [] })
  assert.equal(calls.filter(call => call.path === '/api/files').length, 0)
  assert.equal(study.host.querySelector('textarea').value, 'Draft for my next separate message')
  assert.match(study.host.querySelector('.research-attachments').textContent, /next-message\.txt/)
  assert.equal(study.host.querySelector('.research-confirm'), null)
  pass('Submitting a confirmation sends only that choice, preserving the separate draft and unuploaded attachments')

  await act(async () => { study.host.querySelector('form.research-composer').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })) })
  const messages = calls.filter(call => call.path.endsWith('/turns'))
  assert.equal(calls.filter(call => call.path === '/api/files').length, 1)
  assert.deepEqual(messages.at(-1).body, { request_id: messages.at(-1).body.request_id, content: 'Draft for my next separate message', language: 'en-US', file_ids: ['file_draft'] })
  assert.equal(study.host.querySelector('textarea').value, '')
  assert.equal(study.host.querySelector('.research-attachments'), null)
  assert.equal(calls.filter(call => call.path.endsWith('/model-session')).length, 1)
  assert.deepEqual(unexpected, [])
  pass('The preserved draft uploads and clears only when its own message is submitted')

  await click(study.host.querySelector('.research-valuation-button'))
  const valuationRequest = calls.filter(call => call.path.endsWith('/turns')).at(-1)
  assert.match(valuationRequest.body.content, /^Start the formal valuation/)
  assert.equal(calls.some(call => call.path.endsWith('/valuation')), false)
  assert.match(study.host.querySelector('.research-goal')?.textContent || '', /Valuation goal saved/)
  pass('The valuation button starts the resumable Agent goal instead of bypassing it with a raw submission')
  state = { ...state, session: { ...state.session, revision: 8, pending_action: '' }, execution: { active: true, status: 'running', stage: 'parse_document', elapsed_seconds: 4 } }
  const restored = mount()
  await render(restored.root, ResearchDesk, { ...baseProps, initialResearchId: 'research_abcdef' })
  assert.equal(restored.host.querySelector('textarea').disabled, true)
  assert.match(restored.host.querySelector('.working-message').textContent, /Parse file/)
  state = { ...state, session: { ...state.session, revision: 9, documents: [{ file_id: 'file_restored', name: 'Restored source', block_count: 2, warnings: [] }] }, execution: { active: false, status: 'completed' } }
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 1450)) })
  assert.equal(restored.host.querySelector('textarea').disabled, false)
  await click(restored.host.querySelector('#research-tab-sources'))
  assert.match(restored.host.querySelector('.research-source').textContent, /Restored source/)
  assert.equal(restored.host.querySelector('.working-message'), null)
  pass('Reopening an active task restores busy state and autonomously receives completion without sending another message')
  await type(restored.host.querySelector('textarea'), 'Persistent draft after remount')
  await act(async () => { restored.root.unmount() }); roots.delete(restored.root)
  const again = mount()
  await render(again.root, ResearchDesk, { ...baseProps, initialResearchId: 'research_abcdef' })
  assert.equal(again.host.querySelector('textarea').value, 'Persistent draft after remount')
  pass('Session-scoped draft survives unmount and remount')
  const { default: OutcomePanel } = await vite.ssrLoadModule('/src/OutcomePanel.jsx')
  const outcome = mount(), downloads = []
  await render(outcome.root, OutcomePanel, { t, session: { revision: 3, draft: { company: 'Synthetic' }, documents: [], facts: [] }, busy: false, connected: true,
    report: { source_revision: 3, numeric_result_available: false, counts: { verified: 0, sources: 0, searches: 3 }, conclusion: 'No verified inputs; no price estimate.' },
    onDownload: format => downloads.push(format) })
  assert.match(outcome.host.textContent, /Outcome report available/)
  assert.match(outcome.host.textContent, /not a price estimate/)
  await click(outcome.host.querySelector('.outcome-downloads .primary'))
  assert.deepEqual(downloads, ['pdf'])
  assert.equal(outcome.host.querySelectorAll('.outcome-stages li').length, 4)
  pass('Zero-data outcome clearly discloses no price estimate and offers a direct PDF download')
  console.log(`${checks} research component checks passed (virtual DOM, mocked API, no real model).`)
} finally {
  for (const root of roots) await act(async () => { root.unmount() })
  await vite.close()
  await window.happyDOM.close()
  globalThis.fetch = originalFetch
  for (const [key, descriptor] of originalGlobals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else delete globalThis[key]
  }
}
