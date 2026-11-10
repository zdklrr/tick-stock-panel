// @vitest-environment jsdom
/**
 * 看板决策辅助四卡 — 口径回归。
 *
 * 重点不是「能渲染」, 而是**单位口径**与**降级路径**:
 *   · chg_pct / gap_pct / pct_vs_ma* / suggested_exposure_pct 是百分数数值, 渲染时不能再乘 100;
 *   · exposure / parts[].value / suggested_exposure 是仓位系数, 显示成 ×0.35;
 *   · probs 是 0~100, 可直接当宽度; dn_ratio 是 0~1, 只有它需要 ×100;
 *   · 任一路取数失败 → 显示「—」/「数据缺失」, 绝不画 0, 也不白屏。
 */
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, type Premarket, type RiskAdvice, type RiskRadar, type RiskTiming, type RegimeRow } from '@/lib/api'
import { PremarketWidget, TimingWidget, RegimePhaseWidget, GlobalRiskWidget } from './RiskWidgets'

const premarketOk: Premarket = {
  ok: true,
  bias: '偏空',
  bias_key: 'bear',
  strength: '中',
  strength_key: 'mid',
  probs: { up: 17.5, flat: 24.3, down: 58.2 },
  gap_pct: { low: -0.86, high: 0.24 },
  amplitude: 1.1,
  signal: -0.629,
  as_of: '2026-11-04',
  a50: { price: 12807.1, chg_pct: -0.435, as_of: '2026-11-04' },
  us: { chg_pct: -0.606, rows: [{ code: 'IXIC', name: '纳斯达克', chg_pct: -1.07 }], as_of: '2026-11-03' },
  asia: { chg_pct: -1.5, rows: [], as_of: '2026-11-04' },
  cnh: { price: 6.6616, chg_pct: 0.233, note: '人民币贬值 (仅提示, 不参与加权)', as_of: '2026-11-04' },
  drivers: ['A50 期货 -0.43%'],
  advice: '预计 偏空 (中), 开盘缺口 -0.9% ~ +0.2%',
}

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

const adviceReal: RiskAdvice = {
  ok: true,
  suggested_exposure: 0.267,
  suggested_exposure_pct: 26.7,
  label: '轻仓试单',
  advice: '建议仓位 27% (轻仓试单)。主要压制来自大盘择时 ×0.35 (空头趋势)。',
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
    paper: { active_plans: 0, last_adj: null, last_adj_reason: null, timing_scale: null, note: '' },
    timing: timingRiskOff,
  },
}

const radarOk: RiskRadar = {
  ok: true,
  score: 68.3,
  level: 'elevated',
  level_text: '偏高',
  exposure: 0.762,
  global_exposure: 0.762,
  regions: {
    us: { score: 67.1, exposure: 0.78 },
    asia: { score: 85.0, exposure: 0.55 },
    hk: { score: 42.0, exposure: 1.08 },
  },
  indexes: [],
  breadth: { ok: true, score: 56.6, dn_ratio: 0.5665, up: 2239, down: 3154, total: 5568 },
  equity: { ok: false, factor: 1.0, drawdown_pct: null, window_days: 60 },
  missing: [],
  advice: '综合风险 68 分 (偏高)',
  fetched_at: '2026-11-03 13:20:00',
  as_of: '2026-11-03',
  stale_days: 1,
}

const regimeRow: RegimeRow = {
  date: '2026-11-02',
  state: 'lean_strong',
  score: 67,
  limit_up: 58,
  limit_down: 4,
  broken_limit: 12,
  max_consecutive: 5,
  seal_rate: 0.71,
  up_count: 2800,
  down_count: 2500,
  up_ratio: 0.53,
  index_pct: 0.42,
  above_ma20_pct: 0.51,
  total_amount: 1.2e12,
  avg_turnover: 3.1,
  phase: 'repair',
}

let root: Root | null = null
let host: HTMLDivElement | null = null

function mount(node: ReactNode) {
  host = document.createElement('div')
  document.body.appendChild(host)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  root = createRoot(host)
  return act(async () => {
    root!.render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>)
  })
}

/**
 * 轮询退避: 等骨架消失且目标文本出现。
 *
 * **不要用固定 sleep**: 全量 vitest 并行跑时, 机器负载高, 固定 400ms 往往不够
 * (表现为「整卡渲染成空」, 断言拿到 '')。改成「等到出现预期内容为止」,
 * 慢机器上自动多等, 快机器上立刻返回。
 */
async function settle(expect?: string | RegExp) {
  const hit = () => {
    const t = host?.textContent ?? ''
    // 骨架屏还没撤 → 数据没到, 继续等
    if (host?.querySelector('.animate-pulse')) return false
    if (expect == null) return t.trim().length > 0
    return typeof expect === 'string' ? t.includes(expect) : expect.test(t)
  }
  for (let i = 0; i < 150; i += 1) {
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    if (hit()) return
  }
}

function text(): string {
  return host?.textContent ?? ''
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
})

afterEach(() => {
  act(() => { root?.unmount() })
  host?.remove()
  root = null
  host = null
  vi.restoreAllMocks()
})

describe('盘前简报 widget', () => {
  it('方向/强度/信号/缺口按 0~100 与百分数数值口径渲染, 不重复乘 100', async () => {
    vi.spyOn(api, 'riskPremarket').mockResolvedValue(premarketOk)
    await mount(<PremarketWidget />)
    await settle()
    const t = text()
    expect(t).toContain('偏空')
    expect(t).toContain('中')
    expect(t).toContain('S=-0.63')
    // 缺口是百分数数值: -0.86 → "-0.86%" (一位小数) 而不是 "-86.00%"
    expect(t).toContain('-0.9%')
    expect(t).toContain('+0.2%')
    expect(t).not.toContain('-86.00%')
    // probs 已是 0~100, 直接显示
    expect(t).toContain('17.5%')
    expect(t).toContain('58.2%')
    // 三个驱动源
    expect(t).toContain('A50 期货')
    expect(t).toContain('-0.43%')
    expect(t).toContain('权重 65%')
    expect(t).toContain('对模拟盘')
  })

  it('显示数据日期: 顶层预判日 + 各驱动源自己的日期', async () => {
    vi.spyOn(api, 'riskPremarket').mockResolvedValue(premarketOk)
    await mount(<PremarketWidget />)
    await settle()
    const t = text()
    // 顶层是「预判日」= 下一个交易日
    expect(t).toContain('预判日 2026-11-04')
    // 美股隔夜是上一个交易日收盘, 与预判日不同 —— 必须能看出来
    expect(t).toContain('2026-11-03')
  })

  it('后端没给 as_of 时显示「日期未知」而不是假装是今天', async () => {
    vi.spyOn(api, 'riskPremarket').mockResolvedValue({
      ...premarketOk,
      as_of: null,
      a50: { ...premarketOk.a50!, as_of: null },
      us: { ...premarketOk.us!, as_of: null },
      asia: { ...premarketOk.asia!, as_of: null },
      cnh: { ...premarketOk.cnh!, as_of: null },
    })
    await mount(<PremarketWidget />)
    await settle()
    const t = text()
    expect(t).toContain('预判日日期未知')
    expect(t).not.toContain('2026-11-04')
  })

  it('ok=false 时降级显示 advice, 不白屏', async () => {
    vi.spyOn(api, 'riskPremarket').mockResolvedValue({
      ...premarketOk, ok: false, advice: '外围行情获取失败',
    })
    await mount(<PremarketWidget />)
    await settle()
    expect(text()).toContain('盘前简报降级')
    expect(text()).toContain('外围行情获取失败')
  })

  it('请求失败显示错误态并给出重试', async () => {
    vi.spyOn(api, 'riskPremarket').mockRejectedValue(new Error('timeout'))
    await mount(<PremarketWidget />)
    await settle()
    expect(text()).toContain('盘前简报加载失败')
    expect(text()).toContain('timeout')
    expect(host!.querySelector('button')?.textContent).toContain('重试')
  })
})

describe('大盘择时 widget', () => {
  it('四态 + 择时系数 + 三路合成仓位分列展示', async () => {
    vi.spyOn(api, 'riskTiming').mockResolvedValue(timingRiskOff)
    vi.spyOn(api, 'riskAdvice').mockResolvedValue(adviceReal)
    await mount(<TimingWidget />)
    await settle()
    const t = text()
    expect(t).toContain('空头趋势')
    // 择时系数是仓位系数, 不乘 100
    expect(t).toContain('×0.35')
    // 数据日期: 日K 口径, 滞后天数用中文徽标而不是 T-N
    expect(t).toContain('2026-10-30')
    expect(t).toContain('滞后 4 天')
    expect(t).not.toContain('T-4')
    // 均线偏离是百分数数值
    expect(t).toContain('-4.69%')
    expect(t).toContain('-10.45%')
    expect(t).not.toContain('-469.00%')
    // 合成建议: 26.7% 直接拼 %
    expect(t).toContain('26.7%')
    expect(t).toContain('轻仓试单')
    expect(t).toContain('择时')
    expect(t).toContain('×1.00')
    expect(t).toContain('×0.76')
    expect(t).toContain('对模拟盘')
  })

  it('盘中那根K 未定型时挂「盘中·未定型」徽标, 收盘后不挂', async () => {
    vi.spyOn(api, 'riskTiming').mockResolvedValue({
      ...timingRiskOff,
      as_of: '2026-11-05',
      stale_days: 0,
      intraday: true,
    })
    vi.spyOn(api, 'riskAdvice').mockResolvedValue(adviceReal)
    await mount(<TimingWidget />)
    await settle()
    expect(text()).toContain('2026-11-05')
    expect(text()).toContain('盘中·未定型')
    // 数据是当天就不该再挂滞后徽标
    expect(text()).not.toContain('滞后')
  })

  it('收盘后定型的当天数据不挂「盘中」徽标', async () => {
    vi.spyOn(api, 'riskTiming').mockResolvedValue({
      ...timingRiskOff,
      as_of: '2026-11-05',
      stale_days: 0,
      intraday: false,
    })
    vi.spyOn(api, 'riskAdvice').mockResolvedValue(adviceReal)
    await mount(<TimingWidget />)
    await settle()
    expect(text()).toContain('2026-11-05')
    expect(text()).not.toContain('盘中·未定型')
    expect(text()).not.toContain('滞后')
  })

  it('某一路 missing 时显示 ×1.00* 而不是画 0', async () => {
    vi.spyOn(api, 'riskTiming').mockResolvedValue({
      ...timingRiskOff, ok: false, close: null, ma20: null, ma60: null,
      pct_vs_ma20: null, pct_vs_ma60: null, state: 'unknown', state_label: '数据缺失', exposure: 0.5,
    })
    vi.spyOn(api, 'riskAdvice').mockResolvedValue({
      ...adviceReal,
      parts: {
        ...adviceReal.parts,
        timing: { ...adviceReal.parts.timing, value: 1.0, missing: true, state: null, state_label: null, detail: '数据缺失 (按 1.0 计)' },
      },
    })
    await mount(<TimingWidget />)
    await settle()
    const t = text()
    expect(t).toContain('数据缺失')
    expect(t).toContain('×1.00*')
    expect(t).not.toContain('0.000')
  })

  it('只有 advice 失败时上半部仍渲染', async () => {
    vi.spyOn(api, 'riskTiming').mockResolvedValue(timingRiskOff)
    vi.spyOn(api, 'riskAdvice').mockRejectedValue(new Error('advice down'))
    await mount(<TimingWidget />)
    await settle()
    const t = text()
    expect(t).toContain('空头趋势')
    expect(t).toContain('综合建议仓位加载失败')
  })
})

describe('情绪周期 widget', () => {
  it('阶段/状态/情绪系数与三项指标正常渲染', async () => {
    vi.spyOn(api, 'regimeLatest').mockResolvedValue({ row: regimeRow })
    vi.spyOn(api, 'riskAdvice').mockResolvedValue(adviceReal)
    await mount(<RegimePhaseWidget />)
    await settle()
    const t = text()
    expect(t).toContain('修复')
    expect(t).toContain('偏强')
    expect(t).not.toContain('lean_strong')
    expect(t).toContain('×1.00')
    expect(t).toContain('67')
    expect(t).toContain('58')
    expect(t).toContain('5')
    expect(t).toContain('对模拟盘')
    expect(t).toContain('1.20')
    // 数据日期: 情绪周期是日级快照, 只在卡体里显示一次 (卡头不再重复)
    expect(t).toContain('数据 2026-11-02')
    expect((t.match(/2026-11-02/g) || []).length).toBe(1)
  })

  it('row 为 null 时显示暂无数据而不是崩溃', async () => {
    vi.spyOn(api, 'regimeLatest').mockResolvedValue({ row: null })
    vi.spyOn(api, 'riskAdvice').mockResolvedValue(adviceReal)
    await mount(<RegimePhaseWidget />)
    await settle()
    expect(text()).toContain('情绪周期暂无数据')
  })
})

describe('四卡配色区分 (固定身份色标 + 跟随数据的状态色)', () => {
  /** 卡壳: section 本体带状态边框; 首个 span 是左侧 3px 固定身份色条。 */
  function section(label: string): HTMLElement {
    const el = host?.querySelector(`section[aria-label="${label}"]`)
    if (!el) throw new Error(`section ${label} not found`)
    return el as HTMLElement
  }

  function identityBar(label: string): HTMLElement {
    const el = section(label).querySelector('span[aria-hidden="true"]')
    if (!el) throw new Error(`identity bar of ${label} not found`)
    return el as HTMLElement
  }

  it('四卡的固定身份色标互不相同', async () => {
    vi.spyOn(api, 'riskPremarket').mockResolvedValue(premarketOk)
    vi.spyOn(api, 'riskTiming').mockResolvedValue(timingRiskOff)
    vi.spyOn(api, 'riskAdvice').mockResolvedValue(adviceReal)
    vi.spyOn(api, 'regimeLatest').mockResolvedValue({ row: regimeRow })
    vi.spyOn(api, 'riskRadar').mockResolvedValue(radarOk)
    await mount(
      <>
        <PremarketWidget />
        <TimingWidget />
        <RegimePhaseWidget />
        <GlobalRiskWidget />
      </>,
    )
    await settle()
    const bars = ['盘前简报', '大盘择时', '情绪周期', '风险雷达'].map(identityBar)
    // 盘前=accent / 择时=warning / 情绪周期=bull / 风险雷达=bear
    expect(bars[0].className).toContain('bg-accent')
    expect(bars[1].className).toContain('bg-warning')
    expect(bars[2].className).toContain('bg-bull')
    expect(bars[3].className).toContain('bg-bear')
  })

  it('盘前简报: 外框固定走 accent 身份色, 方向语义只落在色字/徽标上', async () => {
    for (const biasKey of ['bull', 'flat', 'bear'] as const) {
      vi.spyOn(api, 'riskPremarket').mockResolvedValue({ ...premarketOk, bias_key: biasKey })
      await mount(<PremarketWidget />)
      await settle()
      // 外框永远是 accent: 「震荡」那天也不能退化成一张灰卡
      expect(section('盘前简报').className, biasKey).toContain('border-accent/40')
      expect(identityBar('盘前简报').className).toContain('bg-accent')
      act(() => { root?.unmount() })
      host?.remove()
      host = null
      root = null
      vi.restoreAllMocks()
    }
    // 但方向大字必须随 bias 变色 (偏多=红 / 震荡=灰 / 偏空=绿)
    vi.spyOn(api, 'riskPremarket').mockResolvedValue({ ...premarketOk, bias_key: 'flat', bias: '震荡' })
    await mount(<PremarketWidget />)
    await settle()
    const big = [...section('盘前简报').querySelectorAll('span')]
      .find(e => e.textContent === '震荡')
    expect(big).toBeTruthy()
    expect((big as HTMLElement).className).toContain('text-secondary')
  })

  it('大盘择时: 状态色调跟随四态 (risk_off 走 bear, risk_on 走 bull)', async () => {
    vi.spyOn(api, 'riskAdvice').mockResolvedValue(adviceReal)
    vi.spyOn(api, 'riskTiming').mockResolvedValue(timingRiskOff)
    await mount(<TimingWidget />)
    await settle()
    expect(section('大盘择时').className).toContain('border-bear/40')
    // 身份色标不随状态变
    expect(identityBar('大盘择时').className).toContain('bg-warning')
    act(() => { root?.unmount() })
    host?.remove()

    vi.spyOn(api, 'riskTiming').mockResolvedValue({
      ...timingRiskOff, state: 'risk_on', state_label: '趋势偏多', exposure: 1.0,
      pct_vs_ma20: 2.35, pct_vs_ma60: 6.1,
    })
    await mount(<TimingWidget />)
    await settle()
    expect(section('大盘择时').className).toContain('border-bull/40')
    expect(identityBar('大盘择时').className).toContain('bg-warning')
  })

  it('风险雷达: 等级色调 low=bear / elevated=warning / high=bull', async () => {
    const cases: Array<['low' | 'elevated' | 'high', string]> = [
      ['low', 'border-bear/40'],
      ['elevated', 'border-warning/40'],
      ['high', 'border-bull/40'],
    ]
    for (const [level, cls] of cases) {
      vi.spyOn(api, 'riskRadar').mockResolvedValue({
        ...radarOk, level, level_text: level, exposure: 0.9,
      })
      await mount(<GlobalRiskWidget />)
      await settle()
      expect(section('风险雷达').className, level).toContain(cls)
      act(() => { root?.unmount() })
      host?.remove()
      host = null
      root = null
      vi.restoreAllMocks()
    }
  })

  it('情绪周期: 五档状态决定状态色 (偏强=bull / 偏弱=warning / 弱势=bear)', async () => {
    const cases: Array<['lean_strong' | 'lean_weak' | 'weak', string]> = [
      ['lean_strong', 'border-bull/40'],
      ['lean_weak', 'border-warning/40'],
      ['weak', 'border-bear/40'],
    ]
    for (const [state, cls] of cases) {
      vi.spyOn(api, 'riskAdvice').mockResolvedValue(adviceReal)
      vi.spyOn(api, 'regimeLatest').mockResolvedValue({
        row: { ...regimeRow, state: state as typeof regimeRow.state },
      })
      await mount(<RegimePhaseWidget />)
      await settle()
      expect(section('情绪周期').className, state).toContain(cls)
      act(() => { root?.unmount() })
      host?.remove()
      host = null
      root = null
      vi.restoreAllMocks()
    }
  })

  it('渲染结果里没有任何硬编码十六进制颜色 (全部走语义 token)', async () => {
    vi.spyOn(api, 'riskPremarket').mockResolvedValue(premarketOk)
    vi.spyOn(api, 'riskTiming').mockResolvedValue(timingRiskOff)
    vi.spyOn(api, 'riskAdvice').mockResolvedValue(adviceReal)
    vi.spyOn(api, 'regimeLatest').mockResolvedValue({ row: regimeRow })
    vi.spyOn(api, 'riskRadar').mockResolvedValue(radarOk)
    await mount(
      <>
        <PremarketWidget />
        <TimingWidget />
        <RegimePhaseWidget />
        <GlobalRiskWidget />
      </>,
    )
    await settle()
    const html = host?.innerHTML ?? ''
    expect(html).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    // 也不能把颜色写进 inline style
    expect(html).not.toMatch(/(?:color|background-color)\s*:/i)
  })

  it('窄屏不溢出: 长数字/长文案都用截断或换行包裹, 不用定宽', async () => {
    vi.spyOn(api, 'riskPremarket').mockResolvedValue(premarketOk)
    await mount(<PremarketWidget />)
    await settle()
    // 缺口/权重这些「靠右贴边」的块必须是 shrink-0, 否则会挤压左侧文本导致溢出
    const html = section('盘前简报').innerHTML
    expect(html).toContain('shrink-0')
    // 底部脚注与降级文案允许任意位置换行, 避免长中文串撑破卡片
    expect(html).toContain('[overflow-wrap:anywhere]')
    expect(html).toContain('flex-wrap')
  })
})

describe('风险雷达 widget', () => {
  it('仓位系数/等级/区域分数与下跌占比口径正确', async () => {
    vi.spyOn(api, 'riskRadar').mockResolvedValue(radarOk)
    await mount(<GlobalRiskWidget />)
    await settle()
    const t = text()
    expect(t).toContain('×0.76')
    expect(t).toContain('偏高')
    expect(t).toContain('68.3')
    expect(t).toContain('美股')
    expect(t).toContain('67.1')
    // dn_ratio 0.5665 是 0~1 小数, 只有这里需要 ×100 → 56.649… → "56.6%"
    expect(t).toContain('56.6%')
    expect(t).not.toContain('0.6%')
    // equity.ok=false → 明确说明未叠加回撤系数
    expect(t).toContain('未叠加回撤系数')
  })

  it('显示外围数据日期与滞后天数 (不是抓取时刻)', async () => {
    vi.spyOn(api, 'riskRadar').mockResolvedValue(radarOk)
    await mount(<GlobalRiskWidget />)
    await settle()
    const t = text()
    expect(t).toContain('2026-11-03')
    expect(t).toContain('滞后 1 天')
  })

  it('雷达没有 as_of 时显示「日期未知」', async () => {
    vi.spyOn(api, 'riskRadar').mockResolvedValue({ ...radarOk, as_of: null, stale_days: null })
    await mount(<GlobalRiskWidget />)
    await settle()
    const t = text()
    expect(t).toContain('日期未知')
    expect(t).not.toContain('滞后')
  })

  it('区域数据缺失时显示「缺失」而不是 0', async () => {
    vi.spyOn(api, 'riskRadar').mockResolvedValue({
      ...radarOk,
      regions: { us: { score: null, exposure: null } },
    })
    await mount(<GlobalRiskWidget />)
    await settle()
    const t = text()
    expect(t).toContain('缺失')
    expect(t).not.toContain('0.0×')
  })

  it('ok=false 时整卡降级', async () => {
    vi.spyOn(api, 'riskRadar').mockResolvedValue({
      ...radarOk, ok: false, score: null, level: 'unknown', level_text: '未知', advice: '风险雷达组装失败',
    })
    await mount(<GlobalRiskWidget />)
    await settle()
    const t = text()
    expect(t).toContain('风险雷达暂无数据')
    expect(t).toContain('风险雷达组装失败')
  })
})
