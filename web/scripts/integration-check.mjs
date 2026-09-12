import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { buildRequest, initialDraft } from '../src/domain.js'

// Explicit local integration check. Creates an isolated, clearly named demo study.
const origin = process.env.VALUATION_TEST_URL || 'http://127.0.0.1:8001'
async function request(path, body) {
  const response = await fetch(origin + path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {})
  assert.ok(response.ok, `${path}: ${response.status} ${response.ok ? '' : await response.text()}`)
  return response.json()
}
async function waitForResult(id) {
  for (let count = 0; count < 50; count++) {
    const record = await request(`/api/runs/${id}`)
    if (record.result) return record
    assert.notEqual(record.status, 'failed')
    assert.notEqual(record.status, 'waiting_review')
    await new Promise(resolve => setTimeout(resolve, 150))
  }
  throw new Error('Run did not finish within the integration check timeout')
}

const page = await fetch(origin + '/')
assert.equal(page.status, 200)
const html = await page.text()
const bundle = html.match(/src="(\/assets\/[^"]+\.js)"/)
assert.ok(bundle, 'Production page must reference the built application')
assert.equal((await fetch(origin + bundle[1])).status, 200)
assert.equal((await request('/health')).status, 'ok')
const draft = { ...initialDraft('en-US'), company: 'Web integration demo', date: '2026-09-12' }
const accepted = await request('/api/runs', { request: buildRequest(draft) })
const original = await waitForResult(accepted.run_id)
const explanation = await request(`/api/runs/${original.run_id}/messages`, { content: 'What assumptions were used?' })
assert.match(explanation.content, /WACC/)
const revision = await request(`/api/runs/${original.run_id}/messages`, { content: 'Set WACC to 8%' })
const record = await waitForResult(revision.related_run_id)
assert.equal(record.request.language, 'en-US')
assert.equal(Number(record.result.assumptions.wacc), .08)
assert.notEqual(record.result.dcf.per_share_value, original.result.dcf.per_share_value)
assert.equal((await request(`/api/runs/${original.run_id}`)).result.dcf.per_share_value, original.result.dcf.per_share_value)
const events = await request(`/api/runs/${record.run_id}/events/history`)
const artifacts = await request(`/api/runs/${record.run_id}/artifacts`)
const stream = await (await fetch(`${origin}/api/runs/${record.run_id}/events?after=0`)).text()
assert.match(stream, /event: tool.cached/)
assert.match(stream, /event: run.completed/)
assert.ok(events.some(event => event.type === 'tool.cached'))
assert.ok(artifacts.some(artifact => artifact.tool === 'calculate_dcf'))

// Server rendering exercises component code with actual backend data; no browser UI automation.
const vite = await createServer({ server: { middlewareMode: true }, appType: 'custom', logLevel: 'error' })
try {
  globalThis.window = { location: { hash: '', pathname: '/' } }
  const t = (_zh, en) => en
  const { default: App } = await vite.ssrLoadModule('/src/App.jsx')
  assert.match(renderToString(createElement(App)), /ValuationAgent/)
  const { Results, Evidence } = await vite.ssrLoadModule('/src/Analysis.jsx')
  const { Workflow } = await vite.ssrLoadModule('/src/Inspector.jsx')
  const results = renderToString(createElement(Results, { record, t }))
  assert.match(results, /Sensitivity analysis/)
  assert.match(results, /Revenue &amp; free cash flow/)
  assert.match(renderToString(createElement(Evidence, { record, events, artifacts, t })), /Financial snapshot/)
  assert.match(renderToString(createElement(Workflow, { record, events, artifacts, t })), /calculate_dcf/)
  console.log('PASS: production assets, task creation, conversation, revision, persisted history, tool evidence, SSE, and component rendering with real results.')
  console.log(`Demo study: ${record.run_id} (v${record.revision})`)
} finally { await vite.close() }
