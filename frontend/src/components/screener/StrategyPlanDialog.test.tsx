// @vitest-environment jsdom
/**
 * 「生成交易计划」弹窗的行为测试。
 *
 * 重点覆盖三件事 (都来自真实反馈):
 *  1. 预览由后端算: 某一行算不出来时, 界面必须**显示后端给的中文原因**,
 *     而不是把整行画成 `--` 让用户完全看不懂;
 *  2. 「大盘择时 ×N」必须用后端 trend 口径 (risk_on/weak/risk_off),
 *     绝不能拿情绪周期倍率表 (labels.exposure) 冒充 —— 两者不是一回事;
 *  3. 权益不足时要有明确提示, 且不可选的行不能被勾选进「生成」里。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrategyPlanDialog } from './StrategyPlanDialog'

const mockAccounts = vi.fn()
const mockConfig = vi.fn()
const mockPreview = vi.fn()
const mockFromRows = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    paperAccounts: () => mockAccounts(),
  },
}))

vi.mock('@/custom/paper/planClient', () => ({
  planAllKey: ['custom', 'paper-plan'],
  planConfigKey: (a: string) => ['custom', 'paper-plan', 'config', a],
  planApi: {
    config: (a: string) => mockConfig(a),
    preview: (a: string, b: unknown) => mockPreview(a, b),
    fromRows: (a: string, b: unknown) => mockFromRows(a, b),
  },
  defaultEntryMode: () => 'zone',
  entryModeOptions: () => [{ value: 'zone', label: '按买入区间 (价格进区间才买)' }],
  pickPlanScale: (args: any) => ({
    limit: args.limit > 0 ? args.limit : Math.max(args.candidates ?? 1, 1),
    rankKey: args.rankKey ?? 'score',
    timingExposure: args.timingExposure ?? 1,
    phaseExposure: args.phaseExposure ?? 1,
    holdingsLimit: args.limit > 0 ? args.limit : Math.max(args.candidates ?? 1, 1),
    fromServer: false,
  }),
  pickPlanSummary: (scale: any, unlimited: boolean) =>
    `按评分降序${unlimited ? '不限只数' : `取前 ${scale.limit} 只`} · 大盘择时 ×${scale.timingExposure} · 情绪周期 ×${scale.phaseExposure} → 本期最多 ${scale.holdingsLimit} 只`,
}))

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const CFG = {
  risk_budget_pct: 1,
  max_pos_pct: 20,
  min_pos_pct: 2,
  atr_stop_mult: 2,
  max_stop_pct: 12,
  entry_zone_pct: 2,
  target1_r: 1.5,
  target2_r: 3,
  max_hold_days: 20,
  max_symbols: 0,
  min_holdings: 1,
  top_n: 10,
  max_total_exposure_pct: 100,
  regime_scale_enabled: true,
  regime_stop_tighten_enabled: true,
  default_entry_mode: 'zone',
}

const LABELS = {
  state_labels: { lean_strong: '偏强' },
  // 情绪周期倍率表: lean_strong = 1.00。**大盘择时是另一套口径** (risk_off = 0.35)。
  exposure: { lean_strong: 1 },
  stop_k: { lean_strong: 1 },
}

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)

const ROWS = [
  { symbol: '600000.SH', name: '浦发银行', close: 10, atr_14: 0.4, low: 9.6, score: 88 },
  { symbol: '600519.SH', name: '贵州茅台', close: 1500, atr_14: 30, low: 1440, score: 70 },
]

/** 等查询落地: 账户 → 配置 → 预览 是三级依赖, 每级都要一个 tick */
async function settle(predicate: () => boolean, tries = 40) {
  for (let i = 0; i < tries; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    if (predicate()) return
  }
}

async function render() {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  root = createRoot(container)
  await act(async () => {
    root!.render(
      <QueryClientProvider client={qc}>
        <StrategyPlanDialog open onClose={() => {}} rows={ROWS} selected={new Set(['600000.SH', '600519.SH'])} />
      </QueryClientProvider>,
    )
  })
  await settle(() => mockPreview.mock.calls.length > 0)
  await settle(() => !container.textContent?.includes('正在计算交易计划'))
}

beforeEach(() => {
  mockAccounts.mockReset()
  mockConfig.mockReset()
  mockPreview.mockReset()
  mockFromRows.mockReset()
  mockAccounts.mockResolvedValue({ accounts: [{ id: 'acc_1', name: '突破新高', status: 'active' }] })
  mockConfig.mockResolvedValue({
    cfg: CFG,
    state: { date: '2026-11-02', state: 'lean_strong', label: '偏强', score: 67 },
    labels: LABELS,
    holdings: { limit: null, unlimited: true, reason: '未设置只数上限 (max_symbols=0, 不限制持仓)' },
    timing: { state: 'risk_off', label: '空头趋势', exposure: 0.35, ok: true },
  })
})

afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})

it('不可生成的行显示后端给的中文原因, 并给出汇总说明', async () => {
  mockPreview.mockResolvedValue({
    ok: true,
    account_id: 'acc_1',
    equity: 20000,
    previews: [
      {
        symbol: '600000.SH', name: '浦发银行', ok: true, reason: '', note: '无 ATR 数据, 按 3% 波动率估算止损',
        entry_ref: 10, entry_low: 9.8, entry_high: 10.2, stop: 9.6, stop_pct: 4,
        target1: 10.6, target2: 11.2, pos_pct: 20, qty_planned: 4000, risk_amount: 1600,
        gain_at_t1: 4000, gain_at_t2: 8000, rr: 1.5,
      },
      {
        symbol: '600519.SH', name: '贵州茅台', ok: false,
        reason: '计划股数不足一手 (按仓位 20% 只能买 2 股), 一手 150000 元; 提高仓位上限、换成低价标的, 或给账户加资金',
        note: '', entry_ref: 1500, entry_low: 1470, entry_high: 1530, stop: 1440, stop_pct: 4,
      },
    ],
    summary: { total: 2, ok: 1, blocked: 1, total_pos_pct: 20, need_cash: 40000 },
    state: { state: 'lean_strong' },
  })

  await render()
  const text = container.textContent || ''

  // 原因必须逐字讲清楚, 而不是一串 '--'
  expect(text).toContain('计划股数不足一手')
  expect(text).toContain('提高仓位上限')
  // 汇总块: 告诉用户「1 只无法生成计划」
  expect(text).toContain('1 只无法生成计划')
  // 算得出的字段照样显示 (不是全 --)
  expect(text).toContain('9.80 ~ 10.20')
  expect(text).toContain('1440.00')
})

it('大盘择时徽标用后端 trend 口径, 不能拿情绪周期倍率冒充', async () => {
  mockPreview.mockResolvedValue({
    ok: true, account_id: 'acc_1', equity: 200000,
    previews: [{ symbol: '600000.SH', name: '浦发银行', ok: true, reason: '', note: '', entry_ref: 10, entry_low: 9.8, entry_high: 10.2, stop: 9.6, target1: 10.6, target2: 11.2, pos_pct: 20, qty_planned: 4000, rr: 1.5 }],
    summary: { total: 1, ok: 1, blocked: 0, total_pos_pct: 20, need_cash: 40000 },
    state: { state: 'lean_strong' },
  })

  await render()
  const text = container.textContent || ''

  // 大盘择时 = 空头趋势 ×0.35 (后端 trend 口径), **不是** 情绪周期的 ×1
  expect(text).toContain('大盘择时')
  expect(text).toContain('空头趋势')
  expect(text).toContain('×0.35')
  // 情绪周期另起一个徽标, 展示 ×1
  expect(text).toContain('情绪周期')
  expect(text).toContain('偏强')
  // 绝不能出现「大盘 … ×1」这种错值
  expect(text).not.toMatch(/大盘[^·]*×1\b/)
})

it('权益过少时给出提示', async () => {
  mockPreview.mockResolvedValue({
    ok: true, account_id: 'acc_1', equity: 20,
    previews: [{ symbol: '600000.SH', name: '浦发银行', ok: false, reason: '计划股数不足一手', note: '' }],
    summary: { total: 1, ok: 0, blocked: 1, total_pos_pct: 0, need_cash: 0 },
    state: { state: 'lean_strong' },
  })

  await render()
  expect(container.textContent || '').toContain('资金偏少')
})
