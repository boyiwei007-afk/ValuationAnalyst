import test from 'node:test'
import assert from 'node:assert/strict'
import { buildRequest, percentToDecimal, decimalToPercent, mergeEvents, stageStates, sensitivityGrid, valuationRanges } from '../src/domain.js'

const draft = overrides => ({ language: 'en-US', mode: 'snapshot', company: 'Example Company', date: '2026-09-12', source: 'structured', ticker: '', assumptionSource: 'manual', wacc: '8.0000000000001', growth: '2.5', revenueGrowth: '', years: '5', methods: ['dcf', 'pe'], fileIds: [], assumptionFileIds: [], ...overrides })
const base = { company: { name: 'Original', currency: 'CNY' }, financials: { revenue: '10000000000.123456789' }, assumptions: { revenue_growth: ['0.08','0.07','0.06','0.05','0.04'], ebit_margin: ['0.16'] }, peers: [{ name: 'Peer' }] }

test('task choices preserve decimal values and imported annual assumptions', () => {
  const request = buildRequest(draft(), base)
  assert.equal(request.language, 'en-US')
  assert.equal(request.assumptions.wacc, '0.080000000000001')
  assert.equal(request.financials.revenue, '10000000000.123456789')
  assert.deepEqual(request.assumptions.revenue_growth, base.assumptions.revenue_growth)
  assert.deepEqual(request.assumptions.ebit_margin, ['0.16'])
  assert.equal(base.company.name, 'Original')
  assert.equal(request.company.name, 'Example Company')
})
test('switching source clears incompatible data, and demo is explicit', () => {
  const uploaded = buildRequest(draft({ source: 'upload', fileIds: ['file_fin'], assumptionSource: 'upload', assumptionFileIds: ['file_assumptions'] }), base)
  assert.deepEqual(uploaded.file_ids, ['file_fin'])
  assert.deepEqual(uploaded.assumption_file_ids, ['file_assumptions'])
  assert.equal(uploaded.financials, undefined)
  assert.equal(uploaded.peers, undefined)
  assert.deepEqual(uploaded.assumptions, {})
  const demo = buildRequest(draft({ mode: 'demo' }), base)
  assert.equal(demo.mode, 'demo')
  assert.equal(demo.financials, undefined)
  assert.equal(demo.peers, undefined)
})
test('missing financials, assumptions and methods block submission', () => {
  assert.throws(() => buildRequest(draft(), {}), /Import a structured/)
  assert.throws(() => buildRequest(draft({ methods: [] }), base), /Select at least one/)
  assert.throws(() => buildRequest(draft({ source: 'upload' }), base), /Upload financial/)
  assert.throws(() => buildRequest(draft({ assumptionSource: 'upload' }), base), /Upload assumptions/)
})
test('percentage conversion is exact, including negative and sub-percent inputs', () => {
  assert.equal(percentToDecimal('0.00125'), '0.0000125')
  assert.equal(percentToDecimal('-2.5'), '-0.025')
  assert.equal(decimalToPercent('0.09500000000000001'), '9.500000000000001')
  assert.equal(percentToDecimal(decimalToPercent('0.09500000000000001')), '0.09500000000000001')
  assert.throws(() => percentToDecimal('abc'))
})
test('reconnected events are deduplicated and tool completion replaces its start', () => {
  const started = { sequence: 1, stage: 'dcf_valuation', type: 'tool.started', tool_call_id: 'call_a', tool: 'calculate_dcf', status: 'running' }
  const done = { ...started, sequence: 2, type: 'tool.completed', status: 'completed', duration_ms: 25 }
  const stage = { sequence: 3, stage: 'dcf_valuation', type: 'stage.completed', status: 'completed' }
  const events = mergeEvents([started, done], [done, stage])
  assert.deepEqual(events.map(e => e.sequence), [1,2,3])
  assert.equal(stageStates(events).dcf_valuation.status, 'completed')
  assert.equal(stageStates(events).dcf_valuation.tools.length, 1)
  assert.equal(stageStates(events).dcf_valuation.tools[0].duration_ms, 25)
})
test('heatmap axes are numeric and invalid cells never create values', () => {
  const grid = sensitivityGrid([{ wacc: '0.12', terminal_growth: '0.03', valid: false, per_share_value: null }, { wacc: '0.08', terminal_growth: '0.025', valid: true, per_share_value: '40.12' }])
  assert.deepEqual(grid.rows, ['0.08','0.12'])
  assert.deepEqual(grid.columns, ['0.025','0.03'])
  assert.equal(grid.valid.length, 1)
  assert.equal(grid.byKey.get('0.12:0.03').per_share_value, null)
})
test('range chart excludes unavailable methods and never combines valuations', () => {
  const rows = valuationRanges({ dcf: { status: 'success', per_share_value: '32', range_low: '20', range_high: '40' }, relative: [{ method: 'pe', status: 'not_applicable', per_share_value: null }] })
  assert.equal(rows.length, 1)
  assert.equal(rows[0].method, 'dcf')
})
