import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, type PaperPlan, type PaperPlanCfg } from '@/lib/api'
import {
  cfgSummary,
  defaultEntryMode,
  entryModeOptions,
  ENTRY_MODE_FALLBACK_LABELS,
  pickPlanScale,
  pickPlanSummary,
  planApi,
  planConfigKey,
  planEntryMode,
  planListKey,
  PLAN_STATUS_LABEL,
} from './planClient'

// 只替换 api 的调用层: planClient 只做查询键 + 归一化 + 透传, 不需要真 fetch。
// @/lib/api 里的类型在编译期擦除, 故工厂里返回 { api } 足够。
vi.mock('@/lib/api', () => ({
  api: {
    paperPlans: vi.fn(),
    paperPlanCreate: vi.fn(),
    paperPlanDelete: vi.fn(),
    paperPlansFromStrategy: vi.fn(),
    paperPlansFromRows: vi.fn(),
    paperPlansPreview: vi.fn(),
    paperPlanConfig: vi.fn(),
    paperPlanConfigSave: vi.fn(),
  },
}))

const mocked = vi.mocked(api)

const cfg: PaperPlanCfg = {
  risk_budget_pct: 0.5,
  max_pos_pct: 15,
  min_pos_pct: 3,
  atr_stop_mult: 2,
  max_stop_pct: 8,
  entry_zone_pct: 2,
  target1_r: 1.5,
  target2_r: 3,
  max_hold_days: 10,
  regime_scale_enabled: true,
  regime_stop_tighten_enabled: true,
}

const labels = {
  state_labels: { strong: '强势' },
  exposure: { strong: 1.2 },
  stop_k: { strong: 0.8 },
}

const plan: PaperPlan = {
  id: 'p1',
  symbol: '600000',
  status: 'pending',
  entry_low: 10,
  entry_high: 10.4,
  stop: 9.6,
  created_at: '2026-11-02T09:30:00',
}

beforeEach(() => vi.clearAllMocks())

describe('planApi', () => {
  it('列表: 把 status 透传给后端并按契约返回 plans/total', async () => {
    mocked.paperPlans.mockResolvedValue({ plans: [plan], total: 1 })
    await expect(planApi.list('acc_1', 'pending,bought')).resolves.toEqual({ plans: [plan], total: 1 })
    expect(mocked.paperPlans).toHaveBeenCalledWith('acc_1', 'pending,bought')
  })

  it('列表: 缺 plans/total 的响应归一化成空表', async () => {
    // 后端旧版本/异常响应: 不能把 undefined 传给表格
    mocked.paperPlans.mockResolvedValue({} as never)
    await expect(planApi.list('acc_1')).resolves.toEqual({ plans: [], total: 0 })
  })

  it('fromStrategy: 创建数透传给调用方 (用于「已生成 N 条」提示)', async () => {
    mocked.paperPlansFromStrategy.mockResolvedValue({
      ok: true,
      created: 3,
      plans: [plan, { ...plan, id: 'p2' }, { ...plan, id: 'p3' }],
      state: { date: '2026-11-01', state: 'strong', label: '强势', score: 82 },
    })
    const res = await planApi.fromStrategy('acc_1', { strategy_id: 'demo', top_n: 3 })
    expect(res.created).toBe(3)
    expect(res.plans).toHaveLength(3)
    expect(res.state?.state).toBe('strong')
    expect(mocked.paperPlansFromStrategy).toHaveBeenCalledWith({ strategy_id: 'demo', top_n: 3 }, 'acc_1')
  })

  it('fromStrategy: created 缺失时回退到 plans.length', async () => {
    mocked.paperPlansFromStrategy.mockResolvedValue({ ok: true, plans: [plan], state: null } as never)
    await expect(planApi.fromStrategy('acc_1', { strategy_id: 'demo' })).resolves.toMatchObject({ created: 1, state: null })
  })

  it('planConfig: 读写都按账户隔离', async () => {
    mocked.paperPlanConfig.mockResolvedValue({ cfg, state: { state: 'strong', label: '强势' }, labels })
    const got = await planApi.config('acc_1')
    expect(got.cfg.risk_budget_pct).toBe(0.5)
    expect(got.labels.exposure.strong).toBe(1.2)

    mocked.paperPlanConfigSave.mockResolvedValue({ ok: true, cfg })
    await planApi.saveConfig('acc_1', cfg)
    expect(mocked.paperPlanConfigSave).toHaveBeenCalledWith(cfg, 'acc_1')
  })

  it('planConfig: labels 缺失时给空表而不是 undefined', async () => {
    mocked.paperPlanConfig.mockResolvedValue({ cfg, state: null } as never)
    const got = await planApi.config('acc_1')
    expect(got.labels).toEqual({ state_labels: {}, exposure: {}, stop_k: {} })
    expect(got.state).toBeNull()
  })

  it('错误响应: 后端 message 原样抛给 UI 捕获', async () => {
    mocked.paperPlanConfigSave.mockRejectedValue(new Error('单笔风险预算超出上限'))
    await expect(planApi.saveConfig('acc_1', cfg)).rejects.toThrow('单笔风险预算超出上限')

    mocked.paperPlansFromStrategy.mockRejectedValue(new Error('策略不存在'))
    await expect(planApi.fromStrategy('acc_1', { strategy_id: 'nope' })).rejects.toThrow('策略不存在')

    mocked.paperPlanDelete.mockRejectedValue(new Error('计划已触发, 不能取消'))
    await expect(planApi.remove('acc_1', 'p1')).rejects.toThrow('计划已触发, 不能取消')
  })

  it('create/remove 带上 account 作用域', async () => {
    mocked.paperPlanCreate.mockResolvedValue({ ok: true, plan })
    await planApi.create('acc_1', { symbol: '600000', entry_low: 10, entry_high: 10.4 })
    expect(mocked.paperPlanCreate).toHaveBeenCalledWith({ symbol: '600000', entry_low: 10, entry_high: 10.4 }, 'acc_1')

    mocked.paperPlanDelete.mockResolvedValue({ ok: true })
    await planApi.remove('acc_1', 'p1')
    expect(mocked.paperPlanDelete).toHaveBeenCalledWith('p1', 'acc_1')
  })
})

describe('查询键', () => {
  it('status 进键, 不同筛选各自缓存', () => {
    expect(planListKey('acc_1')).not.toEqual(planListKey('acc_1', 'pending,bought'))
    expect(planListKey('a')).not.toEqual(planListKey('b'))
    expect(planConfigKey('acc_1')).not.toEqual(planConfigKey('acc_2'))
    // 前缀失效能覆盖列表与配置
    expect(planListKey('acc_1').slice(0, 2)).toEqual(planConfigKey('acc_1').slice(0, 2))
  })
})

describe('展示辅助', () => {
  it('状态都有中文名', () => {
    for (const status of ['pending', 'bought', 'holding', 'closed', 'expired', 'cancelled'] as const) {
      expect(PLAN_STATUS_LABEL[status]).toBeTruthy()
    }
  })

  it('配置摘要用后端数值而不是硬编码', () => {
    expect(cfgSummary(cfg)).toContain('单笔风险 0.5%')
    expect(cfgSummary({ ...cfg, risk_budget_pct: 1.2 })).toContain('单笔风险 1.2%')
  })

  it('配置摘要带上只数上限/总仓上限, 0 与 100 显示为不限', () => {
    expect(cfgSummary({ ...cfg, max_symbols: 5, max_total_exposure_pct: 60 })).toContain('最多 5 只')
    expect(cfgSummary({ ...cfg, max_symbols: 5, max_total_exposure_pct: 60 })).toContain('总仓 ≤60%')
    expect(cfgSummary({ ...cfg, max_symbols: 0, max_total_exposure_pct: 100 })).toContain('只数不限')
    expect(cfgSummary({ ...cfg, max_symbols: 0, max_total_exposure_pct: 100 })).toContain('总仓不限')
  })
})

describe('买入执行方式 (entry_mode)', () => {
  it('计划缺 entry_mode 时按 zone 解释 (向后兼容)', () => {
    expect(planEntryMode({ entry_mode: 'market' })).toBe('market')
    expect(planEntryMode({} as never)).toBe('zone')
  })

  it('默认值优先级: cfg > labels > zone', () => {
    expect(defaultEntryMode({ default_entry_mode: 'next_open' } as never, { default_entry_mode: 'market' } as never)).toBe('next_open')
    expect(defaultEntryMode({} as never, { default_entry_mode: 'market' } as never)).toBe('market')
    expect(defaultEntryMode(null, null)).toBe('zone')
  })

  it('下拉项优先用后端 labels, 缺失时三项俱全且不空文案', () => {
    const fromServer = entryModeOptions({
      entry_modes: ['zone', 'market'],
      entry_mode_labels: { zone: '区间买', market: '市价买' },
    } as never)
    expect(fromServer).toEqual([
      { value: 'zone', label: '区间买' },
      { value: 'market', label: '市价买' },
    ])

    const fallback = entryModeOptions(null)
    expect(fallback.map(o => o.value)).toEqual(['zone', 'next_open', 'market'])
    for (const o of fallback) expect(o.label).toBe(ENTRY_MODE_FALLBACK_LABELS[o.value])
  })
})

describe('优中选优 (pickPlanScale)', () => {
  it('后端给出 A/B/M 时直接采用并标记 fromServer', () => {
    const scale = pickPlanScale({
      state: { timing_exposure: 0.8, phase_exposure: 1.1, holdings_limit: 3 } as never,
      limit: 10,
    })
    expect(scale).toMatchObject({ limit: 10, timingExposure: 0.8, phaseExposure: 1.1, holdingsLimit: 3, fromServer: true })
  })

  it('后端无系数时按 labels.exposure 推导 A, B 取中性 1, M 只缩不放', () => {
    const scale = pickPlanScale({
      labels: { exposure: { lean_weak: 0.5 } } as never,
      state: { state: 'lean_weak' } as never,
      limit: 10,
      rankKey: 'score',
    })
    expect(scale.timingExposure).toBe(0.5)
    expect(scale.phaseExposure).toBe(1)
    expect(scale.holdingsLimit).toBe(5) // ceil(10 * 0.5 * 1)
    expect(scale.fromServer).toBe(false)
  })

  it('大盘转强也不会超过用户填的上限', () => {
    const scale = pickPlanScale({ labels: { exposure: { strong: 1.2 } } as never, state: { state: 'strong' } as never, limit: 4 })
    expect(scale.holdingsLimit).toBe(4)
  })

  it('未填上限时回落到 cfg.max_symbols; 都为 0 时按候选数不限', () => {
    expect(pickPlanScale({ cfg: { max_symbols: 6 } as never }).limit).toBe(6)
    const unlimited = pickPlanScale({ cfg: { max_symbols: 0 } as never, candidates: 23 })
    expect(unlimited.limit).toBe(23)
    expect(unlimited.holdingsLimit).toBe(23)
  })

  it('说明文案按后端数值渲染, 不限时改口径', () => {
    const scale = pickPlanScale({ limit: 10, timingExposure: 0.65, phaseExposure: 1 })
    expect(pickPlanSummary(scale)).toBe('按评分降序取前 10 只 · 大盘择时 ×0.65 · 情绪周期 ×1 → 本期最多 7 只')
    expect(pickPlanSummary(scale, true)).toContain('按评分降序不限只数')
  })
})

describe('planApi.fromRows', () => {
  it('top_n / rank_key / entry_mode 透传给后端, skipped 明细原样返回', async () => {
    mocked.paperPlansFromRows.mockResolvedValue({
      ok: true,
      created: 2,
      plans: [plan, { ...plan, id: 'p2' }],
      skipped: [{ symbol: '000001', reason: '已存在未了结计划' }],
      state: null,
      entry_mode: 'zone',
      source: 'screener',
    })
    const res = await planApi.fromRows('acc_1', {
      rows: [{ symbol: '600000' }],
      top_n: 3,
      rank_key: 'score',
      entry_mode: 'zone',
      source: 'screener',
    })
    expect(res.created).toBe(2)
    expect(res.skipped).toEqual([{ symbol: '000001', reason: '已存在未了结计划' }])
    expect(mocked.paperPlansFromRows).toHaveBeenCalledWith(
      { rows: [{ symbol: '600000' }], top_n: 3, rank_key: 'score', entry_mode: 'zone', source: 'screener' },
      'acc_1',
    )
  })

  it('created/skipped 缺失时归一化, 不把 undefined 传给 UI', async () => {
    mocked.paperPlansFromRows.mockResolvedValue({ ok: true, plans: [plan] } as never)
    await expect(planApi.fromRows('acc_1', { rows: [] })).resolves.toMatchObject({ created: 1, skipped: [], state: null })
  })
})

describe('planApi.preview (只算不落盘)', () => {
  it('透传 rows/top_n/entry_mode, 并把 previews 与 summary 原样带回', async () => {
    mocked.paperPlansPreview.mockResolvedValue({
      ok: true,
      previews: [
        { symbol: '600000', ok: true, reason: '', note: '', entry_low: 9.8, entry_high: 10.2, stop: 9.6, target1: 10.6, target2: 11.2, pos_pct: 20, qty_planned: 4000, rr: 1.5 },
        { symbol: '600519', ok: false, reason: '计划股数不足一手 (按仓位 20% 只能买 26 股), 一手 150000 元', note: '', entry_low: 1470, entry_high: 1530, stop: 1440 },
      ],
      account_id: 'acc_1',
      equity: 200000,
      summary: { total: 2, ok: 1, blocked: 1, total_pos_pct: 20, need_cash: 40000 },
      state: null,
    } as never)
    const res = await planApi.preview('acc_1', {
      rows: [{ symbol: '600000' }, { symbol: '600519' }],
      top_n: 2,
      rank_key: 'score',
      entry_mode: 'zone',
      source: 'screener',
    })
    expect(res.previews).toHaveLength(2)
    // 不可生成的那行也要带回原因 —— 界面靠它解释「为什么买不了」
    expect(res.previews[1].ok).toBe(false)
    expect(res.previews[1].reason).toContain('不足一手')
    // 失败行仍带回能算出的区间/止损, 界面才不用画一堆 `--`
    expect(res.previews[1].entry_low).toBe(1470)
    expect(res.summary).toEqual({ total: 2, ok: 1, blocked: 1, total_pos_pct: 20, need_cash: 40000 })
    expect(mocked.paperPlansPreview).toHaveBeenCalledWith(
      { rows: [{ symbol: '600000' }, { symbol: '600519' }], top_n: 2, rank_key: 'score', entry_mode: 'zone', source: 'screener' },
      'acc_1',
    )
  })

  it('响应缺字段时归一化, 不把 undefined 传给 UI', async () => {
    mocked.paperPlansPreview.mockResolvedValue({ ok: true } as never)
    const res = await planApi.preview('acc_1', { rows: [] })
    expect(res.previews).toEqual([])
    expect(res.equity).toBe(0)
    expect(res.summary).toEqual({ total: 0, ok: 0, blocked: 0, total_pos_pct: 0, need_cash: 0 })
  })
})

describe('planApi.config 必须带出 holdings / timing', () => {
  it('后端返回的择时系数与只数上限原样透出 (不能丢, 否则界面会把情绪倍率当大盘择时)', async () => {
    mocked.paperPlanConfig.mockResolvedValue({
      cfg,
      state: { state: 'lean_strong' },
      labels: { state_labels: {}, exposure: {}, stop_k: {} },
      holdings: { limit: 2, unlimited: false, timing_exposure: 0.35, phase_exposure: 1, reason: '只数上限 5 → 本期最多 2 只' },
      timing: { state: 'risk_off', label: '空头趋势', exposure: 0.35, ok: true },
    } as never)
    const res = await planApi.config('acc_1')
    expect(res.holdings?.limit).toBe(2)
    expect(res.holdings?.timing_exposure).toBe(0.35)
    expect(res.timing?.exposure).toBe(0.35)
    expect(res.timing?.state).toBe('risk_off')
  })

  it('后端没给 holdings/timing 时为 undefined (不是 null), 调用方用 ?? 兜底', async () => {
    mocked.paperPlanConfig.mockResolvedValue({
      cfg,
      state: null,
      labels: { state_labels: {}, exposure: {}, stop_k: {} },
    } as never)
    const res = await planApi.config('acc_1')
    expect(res.holdings).toBeUndefined()
    expect(res.timing).toBeUndefined()
  })
})
