// @vitest-environment jsdom
/**
 * 大盘择时卡片的渲染契约测试。
 *
 * 这一屏最容易回归的是两类东西:
 *   · 单位口径 —— pct_vs_ma20 / pct_vs_ma60 / suggested_exposure_pct 都是**百分数数值**,
 *     渲染时不能再乘 100 (-4.69 → "-4.69%", 而不是 "-469.00%");
 *     exposure / parts[].value / suggested_exposure 是**仓位系数**, 显示成 ×0.35。
 *   · 缺失降级 —— 取不到日K 时显示「数据缺失」而不是画 0; 某一路因子 missing 时
 *     显示「数据缺失(按 1.0 计)」, 因为后端是拿 1.0 中性参与合成的。
 */
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type RiskAdvice, type RiskTiming } from '@/lib/api'
import { MarketTimingCard } from './MarketTimingCard'

/** 真实数据 (2026-11-03 从 3099 后端取的, 000300.SH 只到 2026-10-30)。 */
const timingRiskOff: RiskTiming = {
  ok: true,
  symbol: '000300.SH',
  as_of: '2026-10-30',
  close: 3985.501,
  ma20: 4181.6791,
  ma60: 4450.6257,
  pct_vs_ma20: -4.69,
  pct_vs_ma60: -10.45,
  state: 'risk_off',
  state_label: '空头趋势',
  exposure: 0.35,
  bars: 120,
  stale_days: 4,
  stale: false,
  advice: '同时跌破 MA20/MA60, 系统性风险期, 建议空仓或仅留底仓。',
}

const timingRiskOn: RiskTiming = {
  ...timingRiskOff,
  state: 'risk_on',
  state_label: '趋势偏多',
  exposure: 1.0,
  pct_vs_ma20: 2.31,
  pct_vs_ma60: 5.08,
  close: 4300.123,
  advice: '站上 MA20/MA60, 趋势偏多, 可按选股结果正常建仓。',
}

/** 数据缺失: 本地日K 读不到 → 后端 ok=false / unknown, 全部数字为 null。 */
const timingUnknown: RiskTiming = {
  ok: false,
  symbol: '000300.SH',
  as_of: null,
  close: null,
  ma20: null,
  ma60: null,
  pct_vs_ma20: null,
  pct_vs_ma60: null,
  state: 'unknown',
  state_label: '数据缺失',
  exposure: 0.5,
  bars: 0,
  stale_days: null,
  stale: false,
  advice: '本地没有沪深300 的日K, 无法计算 MA20/MA60, 择时按中性 0.5 处理。',
}

/** 真实数据: 0.35 × 1.00 × 0.762 = 0.2667 → 26.7%。 */
const adviceReal: RiskAdvice = {
  ok: true,
  suggested_exposure: 0.267,
  suggested_exposure_pct: 26.7,
  label: '轻仓试单',
  advice: '建议仓位 27% (轻仓试单)。主要压制来自大盘择时 ×0.35 (空头趋势); 另 情绪周期 ×1.00, 外围风险 ×0.76。 可轻仓试单, 止损收紧。',
  parts: {
    timing: { key: 'timing', label: '大盘择时', value: 0.35, missing: false, state: 'risk_off', state_label: '空头趋势', detail: '收盘 3985.501 / MA20 4181.6791 / MA60 4450.6257' },
    phase: { key: 'phase', label: '情绪周期', value: 1.0, missing: false, state: 'lean_strong', state_label: '偏强', detail: '偏强 (2026-11-02)' },
    risk: { key: 'risk', label: '外围风险', value: 0.762, missing: false, state: 'elevated', state_label: '偏高', detail: '风险分 68.3 (偏高)' },
  },
  detail: {
    raw: 0.2667,
    clamped: false,
    floor: 0.15,
    cap: 1.2,
    paper: { active_plans: 0, last_adj: null, last_adj_reason: null, timing_scale: null, note: '该建议将在你新建交易计划时生效。' },
    timing: timingRiskOff,
  },
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'riskTiming').mockResolvedValue(timingRiskOff)
  vi.spyOn(api, 'riskAdvice').mockResolvedValue(adviceReal)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
})

async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) }) }

async function render(node: ReactNode) {
  await act(async () => root.render(<QueryClientProvider client={client}>{node}</QueryClientProvider>))
  await settle()
}

const text = () => host.textContent ?? ''

it('空头趋势: 四态/系数/收盘/两条均线偏离都按「百分数数值」渲染, 不重复乘 100', async () => {
  await render(<MarketTimingCard />)
  const t = text()
  expect(host.querySelector('[aria-label="大盘择时"]')).toBeTruthy()
  expect(t).toContain('空头趋势')
  expect(t).toContain('×0.35')
  expect(t).toContain('3985.501')
  expect(t).toContain('4181.679')
  expect(t).toContain('-4.69%')
  expect(t).toContain('4450.626')
  expect(t).toContain('-10.45%')
  // 回归保护: 百分数不能再乘 100
  expect(t).not.toContain('-469.00')
  expect(t).not.toContain('-1045.00')
  expect(t).toContain('截至 2026-10-30')
  expect(t).toContain('120 根日K')
  expect(t).toContain('同时跌破 MA20/MA60')
})

it('趋势偏多: 徽标文案与系数 ×1.00 正确切换', async () => {
  vi.mocked(api.riskTiming).mockResolvedValue(timingRiskOn)
  // 合成建议里的 timing 那一路也要跟着切, 否则会出现「上半部偏多 / 下半部空头」的自相矛盾。
  vi.mocked(api.riskAdvice).mockResolvedValue({
    ...adviceReal,
    advice: '建议仓位 27% (轻仓试单)。主要压制来自外围风险 ×0.76 (偏高)。',
    parts: {
      ...adviceReal.parts,
      timing: { key: 'timing', label: '大盘择时', value: 1.0, missing: false, state: 'risk_on', state_label: '趋势偏多', detail: '收盘 4300.123 / MA20 / MA60' },
    },
    detail: { ...adviceReal.detail, timing: timingRiskOn },
  })
  await render(<MarketTimingCard />)
  const t = text()
  expect(t).toContain('趋势偏多')
  expect(t).toContain('×1.00')
  expect(t).toContain('4300.123')
  expect(t).toContain('+2.31%')
  expect(t).toContain('+5.08%')
  expect(t).not.toContain('空头趋势')
})

it('数据缺失降级: 显示「数据缺失」与 advice, 数字位置是「—」而不是 0', async () => {
  vi.mocked(api.riskTiming).mockResolvedValue(timingUnknown)
  await render(<MarketTimingCard />)
  const t = text()
  expect(t).toContain('数据缺失')
  expect(t).toContain('本地没有沪深300 的日K')
  // 三条价格位都是「—」
  expect(host.textContent?.match(/—/g)?.length ?? 0).toBeGreaterThanOrEqual(3)
  expect(t).not.toContain('0.000')
  // 上半部降级不影响下半部的合成建议
  expect(t).toContain('综合建议仓位')
})

it('综合建议仓位: 大号数字 + 档位 + 三路来源分列', async () => {
  await render(<MarketTimingCard />)
  const t = text()
  expect(t).toContain('综合建议仓位')
  expect(t).toContain('26.7%')        // 直接取后端 suggested_exposure_pct (百分数数值), 保留一位小数
  expect(t).toContain('轻仓试单')
  expect(t).toContain('大盘择时')
  expect(t).toContain('情绪周期')
  expect(t).toContain('外围风险')
  // 三路各自系数: 0.35 / 1.00 / 0.76
  expect(t).toContain('×0.35')
  expect(t).toContain('×1.00')
  expect(t).toContain('×0.76')
  expect(t).toContain('三路合成')
  expect(t).toContain('×0.267')
  expect(t).toContain('主要压制来自大盘择时')
})

it('某一路因子缺失: 显示「数据缺失(按 1.0 计)」, 不把缺失当成 0', async () => {
  vi.mocked(api.riskAdvice).mockResolvedValue({
    ...adviceReal,
    parts: {
      ...adviceReal.parts,
      phase: { key: 'phase', label: '情绪周期', value: 1.0, missing: true, state: null, state_label: null, detail: '情绪周期数据缺失 (按 1.0 计)' },
    },
  })
  await render(<MarketTimingCard />)
  const t = text()
  expect(t).toContain('数据缺失(按 1.0 计)')
  expect(t).toContain('×1.00')
})

it('乘积超界被夹: 明示「已夹到」区间', async () => {
  vi.mocked(api.riskAdvice).mockResolvedValue({
    ...adviceReal,
    suggested_exposure: 0.15,
    suggested_exposure_pct: 15,
    label: '建议空仓或仅留底仓',
    detail: { ...adviceReal.detail, raw: 0.0368, clamped: true },
  })
  await render(<MarketTimingCard />)
  const t = text()
  expect(t).toContain('建议空仓或仅留底仓')
  expect(t).toContain('15.0%')
  expect(t).toContain('已夹到')
  expect(t).toContain('0.15~1.20')
})

it('有进行中的交易计划时, 脚注给出计划数与最近生效系数', async () => {
  vi.mocked(api.riskAdvice).mockResolvedValue({
    ...adviceReal,
    detail: {
      ...adviceReal.detail,
      paper: { active_plans: 2, last_adj: 0.62, last_adj_reason: '偏强×1.00 外围×0.62 回撤×1.00 → 0.62', timing_scale: 0.35, note: '' },
    },
  })
  await render(<MarketTimingCard />)
  const t = text()
  expect(t).toContain('2')
  expect(t).toContain('×0.62')
  expect(t).toContain('偏强×1.00 外围×0.62 回撤×1.00 → 0.62')
  expect(t).toContain('×0.35')
  expect(t).toContain('卖出')
})

it('没有进行中的计划时, 脚注说明建议何时生效', async () => {
  await render(<MarketTimingCard />)
  const t = text()
  expect(t).toContain('该建议将在你新建交易计划时生效')
  expect(t).toContain('卖出')
})

it('大盘择时请求失败: 显示错误态与重试按钮, 不空白', async () => {
  vi.mocked(api.riskTiming).mockRejectedValue(new Error('本地日K 目录不可读'))
  await render(<MarketTimingCard />)
  const t = text()
  expect(host.querySelector('[role="alert"]')).toBeTruthy()
  expect(t).toContain('大盘择时加载失败')
  expect(t).toContain('本地日K 目录不可读')
  expect(t).toContain('重试')
})

it('只有合成建议失败时, 上半部照常渲染并提示「上半部不受影响」', async () => {
  vi.mocked(api.riskAdvice).mockRejectedValue(new Error('risk service down'))
  await render(<MarketTimingCard />)
  const t = text()
  expect(t).toContain('空头趋势')
  expect(t).toContain('3985.501')
  expect(t).toContain('综合建议仓位加载失败')
  expect(t).toContain('上半部的大盘择时不受影响')
})

it('两张卡用独立 query key, 不复用风险雷达的缓存', async () => {
  await render(<MarketTimingCard />)
  const keys = client.getQueryCache().getAll().map(q => JSON.stringify(q.queryKey))
  expect(keys).toContain(JSON.stringify(['risk', 'timing', 'default']))
  expect(keys).toContain(JSON.stringify(['risk', 'advice', 'default']))
})
