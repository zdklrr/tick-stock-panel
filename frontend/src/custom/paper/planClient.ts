/**
 * 模拟盘「交易计划」客户端 — 查询键 + 薄封装。
 *
 * 数据请求本身走 lib/api 里与后端契约一一对应的 paperPlans* 函数 (复用断网/401/422
 * 统一错误处理), 这里只补两件事:
 *  1. 稳定的查询键 (账户隔离, 便于整块失效);
 *  2. 响应归一化 (旧后端/异常响应缺数组字段时不炸表格)。
 */
import {
  api,
  type PaperPlan,
  type PaperPlanCfg,
  type PaperPlanCreatePayload,
  type PaperPlanEntryMode,
  type PaperPlanFromRowsResponse,
  type PaperPlanFromStrategyResponse,
  type PaperPlanHoldings,
  type PaperPlanListResponse,
  type PaperPlanPreviewResponse,
  type PaperPlanTiming,
  type PaperRegimeState,
  type PaperPlanLabels,
} from '@/lib/api'

/** 计划列表键: status 进键 → 不同筛选各自缓存, 不互相覆盖 */
export const planListKey = (account: string, status = '') =>
  ['custom', 'paper-plan', 'plans', account, status] as const

export const planConfigKey = (account: string) =>
  ['custom', 'paper-plan', 'config', account] as const

/** 整块失效前缀 (新增/删除计划会同时影响列表与配置摘要) */
export const planAllKey = ['custom', 'paper-plan'] as const

export interface PlanConfigBundle {
  cfg: PaperPlanCfg
  state: PaperRegimeState | null
  labels: PaperPlanLabels
  /** 「优中选优」只数缩放 (后端算好): 上限 → 本期最多几只 */
  holdings?: PaperPlanHoldings
  /** 大盘择时口径 (后端算好, 只读本地) */
  timing?: PaperPlanTiming
}

/** labels 缺失时的兜底空表: 只影响「参数」弹窗里的映射表展示, 不影响撮合 */
const emptyLabels: PaperPlanLabels = { state_labels: {}, exposure: {}, stop_k: {} }

export const planApi = {
  /** 列表 (status 为逗号分隔多值) */
  list: async (account: string, status?: string): Promise<PaperPlanListResponse> => {
    const data = await api.paperPlans(account, status)
    const plans = data?.plans ?? []
    return { plans, total: data?.total ?? plans.length }
  },

  create: (account: string, body: PaperPlanCreatePayload): Promise<{ ok: boolean; plan: PaperPlan }> =>
    api.paperPlanCreate(body, account),

  /** 取消 (pending) / 删除 (终态) 同一个入口, 由后端按状态判定 */
  remove: (account: string, id: string): Promise<{ ok: boolean }> =>
    api.paperPlanDelete(id, account),

  /** 跑策略 → 生成一批计划; created 透传给调用方做提示 */
  fromStrategy: async (
    account: string,
    body: { strategy_id: string; top_n?: number; params?: Record<string, unknown>; entry_mode?: PaperPlanEntryMode },
  ): Promise<PaperPlanFromStrategyResponse> => {
    const data = await api.paperPlansFromStrategy(body, account)
    return {
      ok: data?.ok ?? true,
      created: data?.created ?? data?.plans?.length ?? 0,
      plans: data?.plans ?? [],
      state: data?.state ?? null,
    }
  },

  config: async (account: string): Promise<PlanConfigBundle> => {
    const data = await api.paperPlanConfig(account)
    return {
      cfg: data.cfg,
      state: data.state ?? null,
      // **必须带出 holdings / timing**: 少了这两个, 前端只能拿 labels.exposure
      // (情绪周期倍率) 冒充「大盘择时系数」, 界面上就会显示「大盘 ×1」而真实是 ×0.35。
      holdings: data.holdings ?? undefined,
      timing: data.timing ?? undefined,
      labels: {
        state_labels: data.labels?.state_labels ?? emptyLabels.state_labels,
        exposure: data.labels?.exposure ?? emptyLabels.exposure,
        stop_k: data.labels?.stop_k ?? emptyLabels.stop_k,
        status_labels: data.labels?.status_labels,
        max_symbols: data.labels?.max_symbols,
        max_total_exposure_pct: data.labels?.max_total_exposure_pct,
        entry_modes: data.labels?.entry_modes,
        entry_mode_labels: data.labels?.entry_mode_labels,
        default_entry_mode: data.labels?.default_entry_mode,
      },
    }
  },

  saveConfig: (account: string, cfg: PaperPlanCfg): Promise<{ ok: boolean; cfg: PaperPlanCfg }> =>
    api.paperPlanConfigSave(cfg, account),

  /** 把「已经选出来的行」直接建计划 (不重跑策略); skipped 明细透传给调用方 */
  fromRows: async (
    account: string,
    body: {
      rows: Record<string, unknown>[]
      cfg?: PaperPlanCfg
      top_n?: number
      rank_key?: string
      entry_mode?: PaperPlanEntryMode
      source?: string
    },
  ): Promise<PaperPlanFromRowsResponse> => {
    const data = await api.paperPlansFromRows(body, account)
    return {
      ok: data?.ok ?? true,
      created: data?.created ?? data?.plans?.length ?? 0,
      plans: data?.plans ?? [],
      skipped: data?.skipped ?? [],
      state: data?.state ?? null,
      entry_mode: data?.entry_mode,
      source: data?.source,
    }
  },

  /**
   * 只算不落盘: 逐行预览「这只会怎么买 / 为什么买不了」。
   *
   * 前端**不再自己重算**计划数学 (旧实现平行算了一套, 与后端必然漂移, 且失败
   * 只能画 `--`)。这里把计算交回后端, 前端只负责把 ok=false 的原因讲清楚。
   */
  preview: async (
    account: string,
    body: {
      rows: Record<string, unknown>[]
      cfg?: PaperPlanCfg
      top_n?: number
      rank_key?: string
      entry_mode?: PaperPlanEntryMode
      source?: string
    },
  ): Promise<PaperPlanPreviewResponse> => {
    const data = await api.paperPlansPreview(body, account)
    return {
      ok: data?.ok ?? true,
      previews: data?.previews ?? [],
      account_id: data?.account_id ?? account,
      equity: Number(data?.equity ?? 0),
      cash: data?.cash ?? null,
      market_value: data?.market_value ?? null,
      initial_cash: data?.initial_cash ?? null,
      account_name: data?.account_name ?? null,
      state: data?.state ?? null,
      entry_mode: data?.entry_mode,
      timing: data?.timing ?? undefined,
      holdings: data?.holdings ?? undefined,
      summary: {
        total: Number(data?.summary?.total ?? 0),
        ok: Number(data?.summary?.ok ?? 0),
        blocked: Number(data?.summary?.blocked ?? 0),
        total_pos_pct: Number(data?.summary?.total_pos_pct ?? 0),
        need_cash: Number(data?.summary?.need_cash ?? 0),
      },
    }
  },
}

/** 状态 → 中文 / 徽章配色 (与后端 paper_plans.labels().status_labels 一一对应) */
export const PLAN_STATUS_LABEL: Record<PaperPlan['status'], string> = {
  pending: '待触发',
  bought: '已买入',
  holding: '持仓中',
  closed: '已了结',
  expired: '已过期',
  cancelled: '已取消',
}

export const PLAN_STATUS_CLASS: Record<PaperPlan['status'], string> = {
  pending: 'bg-elevated text-muted',
  bought: 'bg-accent/15 text-accent',
  holding: 'bg-accent/15 text-accent',
  closed: 'bg-elevated text-secondary',
  expired: 'bg-warning/15 text-warning',
  cancelled: 'bg-elevated text-muted',
}

/** 是否已建仓 (成本/盈亏列只对这两态有意义) */
export const isOpenStatus = (status: PaperPlan['status']) => status === 'bought' || status === 'holding'

/** 计划配置摘要的一行文案: 数字全部来自后端 cfg, 不硬编码默认值 */
export function cfgSummary(cfg: PaperPlanCfg): string {
  const r = (v: number) => (Number.isFinite(v) ? v : 0)
  const maxSymbols = r(cfg.max_symbols ?? 0)
  const maxExposure = r(cfg.max_total_exposure_pct ?? 100)
  return [
    `单笔风险 ${r(cfg.risk_budget_pct)}%`,
    `仓位 ${r(cfg.min_pos_pct)}~${r(cfg.max_pos_pct)}%`,
    `止损 ${r(cfg.atr_stop_mult)}×ATR (≤${r(cfg.max_stop_pct)}%)`,
    `买入区间 ±${r(cfg.entry_zone_pct)}%`,
    `目标 ${r(cfg.target1_r)}R / ${r(cfg.target2_r)}R`,
    `最长 ${r(cfg.max_hold_days)} 天`,
    maxSymbols > 0 ? `最多 ${maxSymbols} 只` : '只数不限',
    maxExposure < 100 ? `总仓 ≤${maxExposure}%` : '总仓不限',
  ].join(' · ')
}

// ── 买入执行方式 (entry_mode) ────────────────────────────────
/** 落库缺省值: 老计划没有该字段时按 zone (进区间才买) 解释 */
export const DEFAULT_ENTRY_MODE: PaperPlanEntryMode = 'zone'

/** 与后端 trade_plan.ENTRY_MODE_LABELS 同文案; labels 没返回时兜底, 保证下拉不空 */
export const ENTRY_MODE_FALLBACK_LABELS: Record<PaperPlanEntryMode, string> = {
  zone: '按买入区间 (价格进区间才买)',
  next_open: '次日开盘 (不等区间, 开盘价成交)',
  market: '立即市价 (不等区间, 当前价成交)',
}

/** 下拉项: 短名给表格/徽章, 长名给弹窗 dropdown */
export const ENTRY_MODE_SHORT: Record<PaperPlanEntryMode, string> = {
  zone: '区间',
  next_open: '次开',
  market: '市价',
}

export const ENTRY_MODE_ORDER: PaperPlanEntryMode[] = ['zone', 'next_open', 'market']

/** 下拉用的 (值, 中文名) 列表: 优先用后端 labels.entry_mode_labels, 缺失回落内置文案 */
export function entryModeOptions(labels?: PaperPlanLabels | null): Array<{ value: PaperPlanEntryMode; label: string }> {
  const modes = labels?.entry_modes?.length ? labels.entry_modes : ENTRY_MODE_ORDER
  return modes.map(m => ({
    value: m,
    label: labels?.entry_mode_labels?.[m] ?? ENTRY_MODE_FALLBACK_LABELS[m] ?? m,
  }))
}

/** 计划的执行方式: 字段缺失按 zone (向后兼容 T10 之前建的计划) */
export const planEntryMode = (plan: Pick<PaperPlan, 'entry_mode'>): PaperPlanEntryMode =>
  plan.entry_mode ?? DEFAULT_ENTRY_MODE

/** 账户级默认执行方式: cfg 优先, 其次后端 labels.default_entry_mode, 最后 zone */
export function defaultEntryMode(cfg?: PaperPlanCfg | null, labels?: PaperPlanLabels | null): PaperPlanEntryMode {
  const fromCfg = cfg?.default_entry_mode
  if (fromCfg) return fromCfg
  const fromLabels = labels?.default_entry_mode
  if (fromLabels) return fromLabels
  return DEFAULT_ENTRY_MODE
}

// ── 「优中选优」: 按评分取前 N 只, 再按大盘择时 × 情绪周期缩放 ──────────
/**
 * 大盘趋势状态 → 仓位系数。用于后端尚未返回 `state.timing_exposure` 时前端推导。
 *
 * 注意与后端 `trade_plan.EXPOSURE_BY_STATE` (按**情绪周期** strong/lean_strong/…) 是
 * **两套不同的口径**: 这里按 `/api/risk/timing` 的 `TrendState` (risk_on/weak/risk_off/unknown)。
 * 数值取 zdk 的择时仓位系数, 与后端 `market_timing.TREND_EXPOSURE` 同源。
 */
export const TREND_EXPOSURE: Record<string, number> = {
  risk_on: 1.0,
  weak: 0.65,
  risk_off: 0.35,
  unknown: 0.5,
}

export interface PickPlanScale {
  /** 只数上限 (UI 上用户填的「持仓数上限」, 与 cfg.max_symbols 同义) */
  limit: number
  /** 排序键 */
  rankKey: string
  /** 大盘择时系数 A */
  timingExposure: number
  /** 情绪周期系数 B */
  phaseExposure: number
  /** 本期最多只数 M */
  holdingsLimit: number
  /** A/B/M 是否后端直接给出 (否则为前端推导) */
  fromServer: boolean
}

/**
 * 推导「优中选优」的缩放结果。
 *
 * 取值优先级:
 *  1. 后端 `state.timing_exposure` / `state.phase_exposure` / `state.holdings_limit` (T14 上线后);
 *  2. 前端推导:
 *     - A = labels.exposure[state.state] (情绪周期倍率表, 与建计划时同一张表);
 *     - B = 1 (后端没有独立的两路口径, 不再凭空造第二路倍数);
 *     - M = ceil(limit × A × B), 且夹在 [1, limit] —— 只缩不放:
 *       用户填的是「我最多拿几只」, 大盘转强也不该超过它。
 *
 * A 优先用后端 risk/timing 的择时系数 (调用方从 `/api/risk/timing` 取 exposure 传入),
 * 没有趋势数据时回落 labels.exposure, 再没有就是 1 (中性, 不加不减)。
 */
export function pickPlanScale(args: {
  cfg?: PaperPlanCfg | null
  labels?: PaperPlanLabels | null
  state?: PaperRegimeState | null
  /** /api/risk/timing 的择时系数 (0~1); 缺省时用 labels.exposure */
  trendExposure?: number | null
  /** 显式指定大盘择时系数 A (优先于 trendExposure / labels.exposure) */
  timingExposure?: number | null
  /** 显式指定情绪周期系数 B (优先于后端 state.phase_exposure) */
  phaseExposure?: number | null
  /** 用户本次填的只数上限; 缺省取 cfg.max_symbols, 0 视为不限 (= 全部候选) */
  limit?: number | null
  /** 候选行数, 用于「不限」时的 M */
  candidates?: number
  rankKey?: string
}): PickPlanScale {
  const { cfg, labels, state, trendExposure, candidates } = args
  const cfgLimit = Number(cfg?.max_symbols ?? 0)
  const rawLimit = args.limit != null && args.limit > 0 ? args.limit : cfgLimit
  const unlimited = !(rawLimit > 0)
  const limit = unlimited ? Math.max(Number(candidates ?? 0), 1) : Math.floor(rawLimit)

  const stateKey = state?.state ?? ''
  const fromLabels = stateKey ? (labels?.exposure?.[stateKey] ?? null) : null
  const serverTiming = state?.timing_exposure ?? null
  const serverPhase = state?.phase_exposure ?? null
  const timing = clampPos(args.timingExposure ?? serverTiming ?? trendExposure ?? fromLabels ?? 1, 1.5)
  const phase = clampPos(args.phaseExposure ?? serverPhase ?? 1, 1.5)

  const scaled = Math.ceil(limit * timing * phase)
  const holdingsLimit = clampInt(state?.holdings_limit ?? (unlimited ? limit : scaled), 1, limit)

  return {
    limit,
    rankKey: args.rankKey ?? 'score',
    timingExposure: timing,
    phaseExposure: phase,
    holdingsLimit,
    fromServer:
      serverTiming != null && serverPhase != null && state?.holdings_limit != null,
  }
}

const clampPos = (v: number, max: number) => {
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return 1
  return Math.min(Math.max(n, 0.05), max)
}

const clampInt = (v: number, lo: number, hi: number) => {
  const n = Math.floor(Number(v))
  if (!Number.isFinite(n)) return hi
  return Math.min(Math.max(n, lo), hi)
}

/** 「优中选优」的一行说明文案 (弹窗里展示给用户看的口径解释) */
export function pickPlanSummary(scale: PickPlanScale, unlimited = false): string {
  const fmt = (v: number) => (Math.round(v * 100) / 100).toFixed(2).replace(/\.?0+$/, '')
  const head = unlimited
    ? '按评分降序不限只数'
    : `按评分降序取前 ${scale.limit} 只`
  return `${head} · 大盘择时 ×${fmt(scale.timingExposure)} · 情绪周期 ×${fmt(scale.phaseExposure)} → 本期最多 ${scale.holdingsLimit} 只`
}
