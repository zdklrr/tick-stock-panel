// @vitest-environment jsdom
/**
 * 风险雷达 / 盘前简报 两张卡片的渲染契约测试。
 *
 * 重点保护「单位口径」和「空值降级」两类最容易回归的地方:
 *   · 百分数数值不再乘 100 (-0.09 → "-0.09%" 而不是 "-9.00%");
 *   · probs 是 0~100 的百分数, 直接当堆叠条宽度 (-1 → 100% 会撑爆);
 *   · dn_ratio 是 0~1 小数, 只有这里要 ×100;
 *   · price / score / regions.* 为 null 时显示「缺失」, stale 行打「数据过期」并降透明度。
 */
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type Premarket, type RiskRadar } from '@/lib/api'
import { GlobalRiskCard } from './GlobalRiskCard'
import { PremarketCard } from './PremarketCard'

const radar: RiskRadar = {
  ok: true,
  score: 68.0,
  level: 'elevated',
  level_text: '偏高',
  exposure: 0.771,
  global_exposure: 0.771,
  regions: {
    us: { score: 67.1, exposure: 0.778 },
    asia: { score: 81.2, exposure: 0.594 },
    // hk 故意留空, 验证「区域缺失」不画 0
  },
  indexes: [
    { code: 'IXIC', name: '纳斯达克', region: 'us', price: 25164.3631, chg_pct: -1.07, ma20_pct: -5.25, score: 79.3, as_of: '2026-11-03', stale: false, source: 'sina_gb+em' },
    { code: 'DJIA', name: '道琼斯', region: 'us', price: 51983.9805, chg_pct: 0.41, ma20_pct: -0.09, score: 41.9, as_of: '2026-11-03', stale: false, source: 'sina_gb+em' },
    // stale 行: 数据源停更被丢弃, price/chg/score 全为 null
    { code: 'TWII', name: '台湾加权', region: 'asia', price: null, chg_pct: null, ma20_pct: null, score: null, as_of: '2025-01-02', stale: true, source: 'missing' },
  ],
  breadth: { ok: true, score: 14.7, dn_ratio: 0.1466, up: 4680, down: 816, total: 5568 },
  equity: { ok: false, factor: 1.0, drawdown_pct: null, window_days: 60 },
  missing: ['N225'],
  advice: '综合风险 68 分 (偏高), 仓位系数 ×0.77; A 股仅 15% 在跌, 宽度健康。',
  fetched_at: '2026-11-03 09:23:02',
}

const premarket: Premarket = {
  ok: true,
  bias: '偏空',
  bias_key: 'bear',
  strength: '中',
  strength_key: 'mid',
  probs: { up: 21.0, flat: 26.1, down: 52.9 },
  gap_pct: { low: -0.76, high: 0.35 },
  amplitude: 1.1,
  signal: -0.554,
  a50: { price: 12808.6, chg_pct: -0.29 },
  us: { chg_pct: -0.606, rows: [{ code: 'DJIA', name: '道琼斯', chg_pct: 0.41 }, { code: 'IXIC', name: '纳斯达克', chg_pct: -1.07 }] },
  asia: { chg_pct: -1.28, rows: [{ code: 'N225', name: '日经225', chg_pct: -0.33 }] },
  cnh: { price: 6.6632, chg_pct: 0.257, note: '人民币贬值, 对外资流入偏不利 (仅提示, 不参与加权)' },
  drivers: ['A50 期货 -0.29% (权重 65%, 隔夜与亚太同步)'],
  advice: '预计 偏空 (中), 开盘缺口 -0.8% ~ +0.4%, 预计振幅 1.1%; 高开 21% / 平开 26% / 低开 53%。',
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'riskRadar').mockResolvedValue(radar)
  vi.spyOn(api, 'riskPremarket').mockResolvedValue(premarket)
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
const card = (label: string) => host.querySelector<HTMLElement>(`section[aria-label="${label}"]`)!

it('风险雷达: 仓位系数/等级/综合分按真实口径渲染, 百分数不重复乘 100', async () => {
  await render(<GlobalRiskCard />)
  expect(text()).toContain('×0.77')
  expect(text()).toContain('偏高')
  expect(text()).toContain('68.0')
  // chg_pct = -1.07 是百分数数值 → "-1.07%"; 若误用 fmtPct 会变成 "-107.00%"
  expect(text()).toContain('-1.07%')
  expect(text()).not.toContain('-107.00%')
  // ma20_pct = -0.09 → "-0.09%"
  expect(text()).toContain('-0.09%')
  expect(text()).toContain('+0.41%')
  expect(host.querySelector('[aria-label="综合风险分 68.0，满分 100，越高越危险"]')).not.toBeNull()
  // 抓取时间
  expect(text()).toContain('2026-11-03 09:23:02')
})

it('风险雷达: 缺失/过期/样本不足/无净值 各自降级, 不画 0', async () => {
  await render(<GlobalRiskCard />)
  // stale 行: 打「数据过期」标 + price 缺失
  expect(text()).toContain('数据过期')
  expect(text()).toContain('缺失')
  // 该行应有降透明度的 class, 且不能出现 0.00 冒充真实价格
  const staleRow = host.querySelector<HTMLElement>('[title*="(TWII)"]')
  expect(staleRow).not.toBeNull()
  expect(staleRow!.className).toContain('opacity-50')
  expect(staleRow!.title).toContain('数据源已过期')
  // hk 区域缺失 → 「缺失」而不是 0 分
  const hkCard = [...host.querySelectorAll<HTMLElement>('div')]
    .filter(el => el.className.includes('rounded-[6px]') && el.textContent?.includes('恒生'))
    .pop()
  expect(hkCard?.textContent).toContain('缺失')
  expect(hkCard?.textContent).toContain('本区域指数数据缺失')
  // equity.ok === false → 无净值数据
  expect(text()).toContain('无净值数据')
  // breadth: dn_ratio 0.1466 是小数 → 14.66%
  expect(text()).toContain('14.66%')
  expect(text()).toContain('4680')
  expect(text()).toContain('816')
  expect(text()).toContain('5568')
  // missing 列表
  expect(text()).toContain('缺失: N225')
  expect(text()).toContain('宽度健康')
})

it('风险雷达: ok=false 时整卡降级并提供重试', async () => {
  vi.spyOn(api, 'riskRadar').mockResolvedValue({
    ...radar, ok: false, score: null, level: 'unknown', level_text: '未知',
    exposure: 1.0, global_exposure: null, regions: {}, indexes: [], missing: [],
    breadth: { ok: false, score: null, dn_ratio: null, up: 0, down: 0, total: 0 },
    advice: '风险雷达组装失败 (外围行情全部超时), 仓位按无风险信号处理。',
  })
  await render(<GlobalRiskCard />)
  expect(text()).toContain('风险雷达暂无数据（外围行情获取失败）')
  expect(text()).toContain('仓位按无风险信号处理')
  const retry = [...host.querySelectorAll<HTMLButtonElement>('button')].find(b => b.textContent?.includes('重试'))
  expect(retry).toBeTruthy()
  await act(async () => retry!.click())
  await settle()
  // invalidate 会重新拉一次
  expect(api.riskRadar).toHaveBeenCalledTimes(2)
})

it('风险雷达: 请求失败显示错误态而不是空白', async () => {
  vi.spyOn(api, 'riskRadar').mockRejectedValue(new Error('请求超时 (20s)'))
  await render(<GlobalRiskCard />)
  expect(host.querySelector('[role="alert"]')).not.toBeNull()
  expect(text()).toContain('风险雷达加载失败')
  expect(text()).toContain('请求超时 (20s)')
})

it('盘前简报: 方向/强度/信号/缺口与三段概率按 0~100 口径渲染', async () => {
  await render(<PremarketCard />)
  expect(text()).toContain('偏空')
  expect(text()).toContain('S=-0.55')
  expect(text()).toContain('-0.8% ~ +0.4%')
  expect(text()).toContain('1.1%')
  expect(text()).toContain('21.0%')
  expect(text()).toContain('26.1%')
  expect(text()).toContain('52.9%')
  // probs 是 0~100, 堆叠条宽度必须直接用 (再 ×100 会溢出到 2100%)
  const bar = host.querySelector<HTMLElement>('[aria-label^="开盘概率"]')!
  expect(bar.getAttribute('aria-label')).toContain('高开 21.0%')
  const segments = [...bar.querySelectorAll<HTMLElement>('div')]
  expect(segments.map(el => el.style.width)).toEqual(['21%', '26.1%', '52.9%'])
  // A50 / 美股 / 亚太 三行驱动 + 逐指数明细
  expect(text()).toContain('-0.29%')
  expect(text()).toContain('-0.61%')
  expect(text()).toContain('-1.28%')
  expect(text()).toContain('纳斯达克')
  // 离岸人民币单独一行 + 仅提示标注
  expect(text()).toContain('6.6632')
  expect(text()).toContain('贬值')
  expect(text()).toContain('仅作提示, 不参与加权')
  // 概率算法说明必须是可解释的, 不能是黑盒
  expect(text()).toContain('正态分布')
  expect(text()).toContain('不是黑盒模型')
})

it('盘前简报: ok=false 降级 + 驱动源为 null 时不炸', async () => {
  vi.spyOn(api, 'riskPremarket').mockResolvedValue({
    ok: false, bias: '震荡', bias_key: 'flat', strength: '弱', strength_key: 'weak',
    probs: { up: 33.3, flat: 33.3, down: 33.3 }, gap_pct: { low: 0, high: 0 }, amplitude: 1.1, signal: 0,
    a50: { price: null, chg_pct: null }, us: { chg_pct: null, rows: [] }, asia: { chg_pct: null, rows: [] },
    cnh: { price: null, chg_pct: null, note: '' }, drivers: [],
    advice: '盘前简报组装失败, 无法给出方向判断。',
  })
  await render(<PremarketCard />)
  expect(text()).toContain('盘前简报暂无数据（A50 / 外围行情获取失败）')
  expect(text()).toContain('盘前简报组装失败')
  expect(text()).not.toContain('NaN')
})

it('盘前简报: 请求失败显示错误态与重试', async () => {
  vi.spyOn(api, 'riskPremarket').mockRejectedValue(new Error('网络连接中断'))
  await render(<PremarketCard />)
  expect(host.querySelector('[role="alert"]')).not.toBeNull()
  expect(text()).toContain('盘前简报加载失败')
  expect(text()).toContain('网络连接中断')
  const retry = [...host.querySelectorAll<HTMLButtonElement>('button')].find(b => b.textContent?.includes('重试'))
  expect(retry).toBeTruthy()
})

it('两张卡都用独立 query key, 互不复用缓存', async () => {
  await render(<><GlobalRiskCard /><PremarketCard /></>)
  expect(api.riskRadar).toHaveBeenCalledTimes(1)
  expect(api.riskPremarket).toHaveBeenCalledTimes(1)
  expect(card('风险雷达')).toBeTruthy()
  expect(card('盘前简报')).toBeTruthy()
  expect(client.getQueryData(['risk', 'radar', 'default'])).toBeTruthy()
  expect(client.getQueryData(['risk', 'premarket'])).toBeTruthy()
})
