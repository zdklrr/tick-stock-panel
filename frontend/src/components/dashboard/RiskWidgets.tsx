/**
 * 看板「决策辅助」四卡 — 盘前简报 / 大盘择时 / 情绪周期 / 风险雷达。
 *
 * 与 Regime 页里的同名大卡(MarketTimingCard / GlobalRiskCard / PremarketCard)是**不同尺寸的
 * 表达**: 那三张是整页宽度的详情卡, 这里是网格里的窄卡片, 只保留「一个数字 + 谁在压仓位」。
 * 因此不复用它们, 而是共用同一批 queryKey 与接口(缓存天然共享, 换页不重复请求)。
 *
 * 数据全部走**已有**接口, 不新增后端路由:
 *   · GET /api/risk/premarket  盘前简报 (api.riskPremarket)
 *   · GET /api/risk/timing     大盘择时四态 (api.riskTiming)
 *   · GET /api/risk/advice     三路合成建议仓位 (api.riskAdvice)
 *   · GET /api/risk/radar      风险雷达 (api.riskRadar)
 *   · GET /api/regime/latest   最新情绪周期阶段 (api.regimeLatest)
 *
 * 单位口径与 lib/api.ts 的硬约定一致, 渲染时不做任何启发式换算:
 *   · chg_pct / ma20_pct / gap_pct / amplitude / pct_vs_ma* / suggested_exposure_pct 是**百分数数值**,
 *     直接 toFixed 拼 %, 不要再乘 100 —— 所以不能用 lib/format 的 fmtPct。
 *   · exposure / factor / parts[].value / suggested_exposure 是**仓位系数**, 显示成 ×0.35。
 *   · probs 是 0~100 百分数(三段相加 100), 可直接当 CSS 宽度。
 *   · score 是 0~100 分(越高越危险); dn_ratio 是 0~1 小数。
 *
 * 配色 (用户要求「四卡一眼分清」):
 *   · **固定色标** = 左侧 3px 竖条 + 卡头图标底色 + 标题字色, 四卡各一色, 永远不随数据变:
 *       盘前简报 = accent(蓝) / 大盘择时 = warning(琥珀) / 情绪周期 = bull(红) / 风险雷达 = bear(绿)。
 *   · **状态色调** = 卡头底色带 + 边框 + 关键数字, 跟随卡内数值(择时四态 / 五档情绪 / 风险等级),
 *     让「卡片颜色」和「里面的状态」永远一致, 而不是四张卡各刷一种好看的色。
 *   · 只用 tailwind.config.ts 里定义的语义 token 与其 /10 /20 透明度变体,
 *     本文件**不出现任何十六进制颜色**(Regime 页那几张旧卡里有, 这里统一改用 token)。
 */
import type { ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { CloudOff, Compass, Flame, RefreshCw, ShieldAlert, Sunrise, type LucideIcon } from 'lucide-react'
import {
  api,
  MARKET_PHASE_LABELS,
  REGIME_STATE_LABELS,
  type MarketPhase,
  type Premarket,
  type RiskAdvice,
  type RiskRadar,
  type RiskTiming,
  type TrendState,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPrice } from '@/lib/format'
import { Skeleton } from '@/components/data/Skeleton'
import { CalendarClock } from 'lucide-react'

// ── 语义配色层: token → class 的唯一翻译表 (禁止 hex) ─────────────────

type Tone = 'accent' | 'bull' | 'bear' | 'warning' | 'muted'

interface ToneStyle {
  /** 左侧固定色标竖条 */
  bar: string
  /** 卡片边框 / 状态色边界 */
  border: string
  /** 卡头底色带 */
  band: string
  /** 卡头图标底色块 */
  icon: string
  /** 卡头标题字色 */
  title: string
  /** 徽标(圆角胶囊) */
  chip: string
  /** 关键数字字色 */
  value: string
}

/**
 * tailwind.config.ts 里只有 accent / bull / bear / warning / danger / muted 这几个语义色
 * (danger 与 bull 是同一个色值, 所以实际可用的高区分度色相是 4 个: 蓝 / 红 / 绿 / 琥珀 + 灰)。
 * 四张卡正好各占一个: premarket=accent, timing=warning, regime=bull, risk=bear。
 */
const TONE: Record<Tone, ToneStyle> = {
  accent: {
    bar: 'bg-accent',
    border: 'border-accent/40',
    band: 'bg-accent/10',
    icon: 'bg-accent/20 text-accent',
    title: 'text-accent',
    chip: 'border-accent/40 bg-accent/10 text-accent',
    value: 'text-accent',
  },
  bull: {
    bar: 'bg-bull',
    border: 'border-bull/40',
    band: 'bg-bull/10',
    icon: 'bg-bull/20 text-bull',
    title: 'text-bull',
    chip: 'border-bull/40 bg-bull/10 text-bull',
    value: 'text-bull',
  },
  bear: {
    bar: 'bg-bear',
    border: 'border-bear/40',
    band: 'bg-bear/10',
    icon: 'bg-bear/20 text-bear',
    title: 'text-bear',
    chip: 'border-bear/40 bg-bear/10 text-bear',
    value: 'text-bear',
  },
  warning: {
    bar: 'bg-warning',
    border: 'border-warning/40',
    band: 'bg-warning/10',
    icon: 'bg-warning/20 text-warning',
    title: 'text-warning',
    chip: 'border-warning/40 bg-warning/10 text-warning',
    value: 'text-warning',
  },
  muted: {
    bar: 'bg-muted',
    border: 'border-border',
    band: 'bg-elevated/40',
    icon: 'bg-elevated text-muted',
    title: 'text-secondary',
    chip: 'border-border bg-elevated text-secondary',
    value: 'text-secondary',
  },
}

/** 四卡的固定身份色标 —— 不随数据变化, 用户靠它「一眼分清哪张是哪张」。 */
const ID_PREMARKET: Tone = 'accent'
const ID_TIMING: Tone = 'warning'
const ID_REGIME: Tone = 'bull'
const ID_RISK: Tone = 'bear'

/**
 * 大盘择时四态 → 状态色调。
 * A 股惯例: 偏多=红(bull) / 偏弱=黄(warning) / 空头=绿(bear) / 缺失=灰(muted)。
 * 与 Regime 页 MarketTimingCard 的语义完全一致(那边用的是同一组色值)。
 */
function trendTone(state: TrendState | null | undefined): Tone {
  switch (state) {
    case 'risk_on':
      return 'bull'
    case 'weak':
      return 'warning'
    case 'risk_off':
      return 'bear'
    default:
      return 'muted'
  }
}

/**
 * 风险等级 → 交通灯色调: 低=绿 / 正常=蓝 / 偏高=黄 / 高=红。
 * 与 Regime 页 GlobalRiskCard 的 LEVEL_COLOR 同源 —— 同一份数据在两张页面上不能红绿打架。
 */
function levelTone(level: string | null | undefined): Tone {
  switch (level) {
    case 'low':
      return 'bear'
    case 'normal':
      return 'accent'
    case 'elevated':
      return 'warning'
    case 'high':
      return 'bull'
    default:
      return 'muted'
  }
}

/** 五档情绪状态 → 色调: 强势/偏强=红 / 震荡=灰 / 偏弱=黄 / 弱势=绿。 */
const REGIME_STATE_TONE: Record<string, Tone> = {
  strong: 'bull',
  lean_strong: 'bull',
  range: 'muted',
  lean_weak: 'warning',
  weak: 'bear',
}

/** 盘前方向 → 色调: 偏多=红 / 震荡=灰 / 偏空=绿。 */
const BIAS_TONE: Record<Premarket['bias_key'], Tone> = { bull: 'bull', flat: 'muted', bear: 'bear' }

/** 三段概率 → 色调: 高开=红 / 平开=灰 / 低开=绿。 */
const PROB_TONE = { up: 'bull', flat: 'muted', down: 'bear' } as const

/** zdk 口径的择时仓位系数图例 (与 backend/app/services/market_timing.py 的 TREND_EXPOSURE 同表)。 */
const TREND_LEGEND = '偏多 ×1.00 · 偏弱 ×0.65 · 空头 ×0.35 · 缺失 ×0.50'

// ── 数值格式化 (口径见文件头, 全部不乘 100) ─────────────────────

/** 百分数数值 → "+1.23%" / "-4.69%" / "—"。 */
function pctText(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  const text = v.toFixed(digits)
  // -0.001 这类值 toFixed 后是 "-0.00", 去掉负零避免出现 "-0.00%"
  if (Number(text) === 0) return `${(0).toFixed(digits)}%`
  return `${v > 0 ? '+' : ''}${text}%`
}

function numText(v: number | null | undefined, digits = 2): string {
  return v == null || Number.isNaN(v) ? '—' : v.toFixed(digits)
}

/** 仓位系数 → "×0.35" / "—"。 */
function coeffText(v: number | null | undefined, digits = 2): string {
  return v == null || Number.isNaN(v) ? '—' : `×${v.toFixed(digits)}`
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/** 涨跌/偏离的语义字色: 正=红(bull) 负=绿(bear) 零/缺失=灰。 */
function pctToneClass(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v) || v === 0) return 'text-muted'
  return v > 0 ? 'text-bull' : 'text-bear'
}

// ── 卡片外壳 ────────────────────────────────────────────────

const shellBase =
  'relative flex h-full min-w-0 flex-col overflow-hidden rounded-card border bg-surface/80 '
  + 'shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm '
  + 'transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]'

/**
 * 统一卡壳 —— 两套颜色各管一摊, 互不串色:
 *   · **身份色 (identity)**: 左侧 3px 竖条 + 卡头底色带 + 卡头图标块 + 卡头标题字。
 *     四卡各一色且永不随数据变化 —— 用户靠它「一眼分清哪张是哪张」。
 *   · **状态色 (tone)**: 卡片外框 + 关键数字 + 徽标胶囊 + 分隔线。
 *     跟随卡内数值(择时四态 / 五档情绪 / 风险等级), 让卡片颜色和里面的状态一致。
 *
 * 为什么卡头不用状态色: 大盘择时的身份色是琥珀、risk_off 状态色是绿; 而风险雷达的身份色是绿、
 * 偏高(默认态)状态色是琥珀 —— 卡头一旦跟状态走, 这两张卡会互相「镜像成对方」, 正好废掉可辨识性。
 */
function Card({ label, icon: Icon, title, hint, tone, identity, busy, children }: {
  label: string
  icon: LucideIcon
  title: string
  hint?: ReactNode
  tone: Tone
  identity: Tone
  busy?: boolean
  children: ReactNode
}) {
  const state = TONE[tone]
  const id = TONE[identity]
  return (
    <section
      className={cn(shellBase, state.border)}
      aria-label={label}
      aria-busy={busy || undefined}
    >
      <span
        className={cn('pointer-events-none absolute inset-y-0 left-0 w-[3px]', id.bar)}
        aria-hidden="true"
      />
      <header
        className={cn(
          'flex shrink-0 items-center justify-between gap-1.5 border-b px-1.5 py-1 pl-2.5',
          id.border,
          id.band,
        )}
      >
        <span className="flex min-w-0 items-center gap-1.5">
          <span className={cn('grid h-4 w-4 shrink-0 place-items-center rounded-[4px]', id.icon)}>
            <Icon className="h-3 w-3" aria-hidden="true" />
          </span>
          <span className={cn('truncate text-[11px] font-semibold leading-tight', id.title)}>{title}</span>
        </span>
        {hint != null && (
          <span className="max-w-[55%] shrink-0 truncate font-mono text-[10px] tabular-nums text-muted">{hint}</span>
        )}
      </header>
      <div className="flex min-w-0 flex-1 flex-col px-1.5 pb-1.5 pt-1.5">{children}</div>
    </section>
  )
}

/** 加载骨架: 保持与正式内容近似的高度, 避免看板抖动。 */
function WidgetLoading({ label }: { label: string }) {
  return (
    <div className="mt-1 space-y-1.5" role="status" aria-label={`正在加载${label}`}>
      <Skeleton w="w-24" h="h-5" />
      <Skeleton h="h-2.5" />
      <Skeleton w="w-3/4" h="h-2.5" />
    </div>
  )
}

/** 降级/错误块: 不白屏, 带重试。 */
function WidgetState({ title, detail, onRetry, busy }: {
  title: string
  detail: string
  onRetry: () => void
  busy: boolean
}) {
  return (
    <div
      role="alert"
      className="mt-1 flex flex-1 flex-col items-start justify-center gap-1.5 rounded-md border border-dashed border-border/60 px-2 py-2"
    >
      <span className="flex items-center gap-1 text-[11px] font-semibold text-secondary">
        <CloudOff className="h-3 w-3 shrink-0" aria-hidden="true" />
        {title}
      </span>
      <span className="text-[10px] leading-relaxed text-muted line-clamp-2 [overflow-wrap:anywhere]">{detail}</span>
      <button
        type="button"
        onClick={onRetry}
        disabled={busy}
        className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-1.5 py-0.5 text-[10px] text-foreground transition-colors hover:border-accent/60 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <RefreshCw className={cn('h-2.5 w-2.5', busy && 'animate-spin')} aria-hidden="true" />
        {busy ? '重试中…' : '重试'}
      </button>
    </div>
  )
}

/**
 * **数据日期徽标** —— 用户要能一眼看出「这是哪天的数据」。
 *
 * 三种状态:
 *   · `as_of` 有值 → 显示 `数据 2026-11-03`; 滞后超过 0 天再加一个 `滞后 N 天` 琥珀徽标。
 *   · `as_of` 缺失 → 显示 `日期未知`, 不假装是今天。
 *   · `as_of` 有值但 `staleDays` 缺失 → 只显示日期, 不臆造滞后天数。
 *
 * v0.3.41: `intraday` 为真时再加一个 `盘中·未定型` 蓝灰徽标 —— 开盘后当日那根 K
 * 还没收盘, MA20/MA60/四态会随行情跳动。用户要求「9:25 竞价结束就该是最新」,
 * 所以我们确实显示当天的盘中值, 但必须说清它不是收盘定型的数字。
 *
 * `label` 区分语义: 盘前简报顶层是「预判日」(当天), 大盘择时/风险雷达是「数据」(行情日)。
 * 统一放在卡体顶部第一行, 四张卡一致, 避免有的卡在标题旁、有的卡在底部。
 */
function DataDateRow({ as_of, staleDays, intraday, label = '数据' }: {
  as_of?: string | null
  staleDays?: number | null
  intraday?: boolean | null
  label?: string
}) {
  const has = !!as_of
  const lag = typeof staleDays === 'number' && staleDays > 0 ? staleDays : 0
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[10px] leading-none">
      <span
        className="inline-flex shrink-0 items-center gap-1 text-muted"
        title={has ? `数据日期 ${as_of}` : '后端未提供数据日期'}
      >
        <CalendarClock className="h-3 w-3 shrink-0 opacity-70" aria-hidden="true" />
        <span className="font-mono tabular-nums">
          {has ? `${label} ${as_of}` : `${label}日期未知`}
        </span>
      </span>
      {has && lag > 0 && (
        <span
          className="shrink-0 rounded-full bg-warning/15 px-1.5 py-0.5 font-medium text-warning"
          title={`数据日期 ${as_of}, 距今 ${lag} 天`}
        >
          滞后 {lag} 天
        </span>
      )}
      {has && intraday && (
        <span
          className="shrink-0 rounded-full bg-foreground/10 px-1.5 py-0.5 font-medium text-secondary"
          title="当日的日K还在盘中, 收盘前均线与四态会随行情变动"
        >
          盘中·未定型
        </span>
      )}
    </div>
  )
}

/** 「对模拟盘的影响」脚注 — 四卡统一, 贴底对齐; 窄屏允许换行, 不裁掉关键结论。 */
function ImpactNote({ children }: { children: ReactNode }) {
  return (
    <p className="mb-0 mt-auto border-t border-border/60 pt-1.5 text-[10px] leading-relaxed text-muted [overflow-wrap:anywhere]">
      {children}
    </p>
  )
}

/** 一路系数的紧凑展示 (大盘择时的三路来源)。 */
function CoeffChip({ label, part }: { label: string; part: RiskAdvice['parts']['timing'] }) {
  return (
    <span
      className="min-w-0 max-w-full truncate"
      title={part.missing ? `${label}: 数据缺失, 按 1.0 中性参与` : `${label}${part.state_label ? ` (${part.state_label})` : ''}: ${part.detail}`}
    >
      <span className="text-muted">{label}</span>
      <span className={cn('ml-0.5 font-mono tabular-nums', part.missing ? 'text-muted' : 'text-foreground')}>
        {part.missing ? '×1.00*' : coeffText(part.value)}
      </span>
    </span>
  )
}

// ── 1. 盘前简报 (固定色标: accent 蓝; 状态色: 方向 偏多/震荡/偏空) ────

/** 盘前简报 — 开盘方向/强度/信号 + 三段概率 + 缺口区间。 */
export function PremarketWidget() {
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: QK.riskPremarket,
    queryFn: () => api.riskPremarket(),
    // 与 Regime 页同名卡同节奏: 盘前数据 2 分钟内基本不变。
    refetchInterval: 120_000,
    staleTime: 60_000,
  })
  const retry = () => { void qc.invalidateQueries({ queryKey: QK.riskPremarket }) }

  if (query.isLoading) {
    return (
      <Card label="盘前简报" icon={Sunrise} title="盘前简报" tone={ID_PREMARKET} identity={ID_PREMARKET} busy>
        <WidgetLoading label="盘前简报" />
      </Card>
    )
  }
  if (query.isError) {
    return (
      <Card label="盘前简报" icon={Sunrise} title="盘前简报" tone="muted" identity={ID_PREMARKET}>
        <WidgetState
          title="盘前简报加载失败"
          detail={`${errorText(query.error)}（外围行情接口偶发超时, 可稍后重试）`}
          onRetry={retry}
          busy={query.isFetching}
        />
      </Card>
    )
  }

  const data: Premarket | undefined = query.data
  if (!data) {
    return (
      <Card label="盘前简报" icon={Sunrise} title="盘前简报" tone="muted" identity={ID_PREMARKET}>
        <WidgetState title="盘前简报暂无数据" detail="尚未取到盘前数据, 可稍后重试。" onRetry={retry} busy={query.isFetching} />
      </Card>
    )
  }
  if (!data.ok) {
    return (
      <Card label="盘前简报" icon={Sunrise} title="盘前简报" tone="muted" identity={ID_PREMARKET}>
        <WidgetState
          title="盘前简报降级"
          detail={data.advice || '外围行情获取失败, 开盘方向无法估算。'}
          onRetry={retry}
          busy={query.isFetching}
        />
      </Card>
    )
  }

  const biasTone = BIAS_TONE[data.bias_key] ?? 'muted'
  const biasStyle = TONE[biasTone]
  const probs: Array<{ key: keyof typeof PROB_TONE; label: string; value: number }> = [
    { key: 'up', label: '高开', value: data.probs?.up ?? 0 },
    { key: 'flat', label: '平开', value: data.probs?.flat ?? 0 },
    { key: 'down', label: '低开', value: data.probs?.down ?? 0 },
  ]
  const clampPct = (v: number) => Math.min(Math.max(Number.isFinite(v) ? v : 0, 0), 100)

  return (
    <Card
      label="盘前简报"
      icon={Sunrise}
      title="盘前简报"
      // 盘前简报是**资讯**卡: 外框/卡头固定走 accent 靛蓝(它的身份色), 不跟方向变灰。
      // 方向语义只落在下方大字与徽标上(偏多红 / 震荡灰 / 偏空绿), 这样「震荡」那天
      // 这张卡依然一眼可辨, 不会退化成一张灰卡。
      tone={ID_PREMARKET}
      identity={ID_PREMARKET}
      hint={`S=${data.signal > 0 ? '+' : ''}${numText(data.signal)}`}
    >
      {/* 数据日期: 盘前简报是「当天开盘预判」, 顶层 as_of 就是 A 股时区的今天;
          各驱动源另有自己的数据日期 (美股隔夜是上一交易日收盘) */}
      <DataDateRow as_of={data.as_of} label="预判日" />

      {/* 方向 + 强度 + 缺口 */}
      <div className="mt-1 flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
        <span className={cn('text-lg font-semibold leading-none', biasStyle.value)}>{data.bias}</span>
        <span className={cn('rounded-full border px-1.5 py-px text-[10px] font-medium', biasStyle.chip)}>{data.strength}</span>
        <span className="ml-auto shrink-0 font-mono text-[10px] tabular-nums text-muted">
          缺口 {pctText(data.gap_pct?.low, 1)} ~ {pctText(data.gap_pct?.high, 1)}
        </span>
      </div>

      {/* 三段概率: 高开 / 平开 / 低开 (probs 已是 0~100, 直接当宽度) */}
      <div
        className="mt-1.5 flex h-[14px] w-full overflow-hidden rounded-sm bg-elevated/60"
        role="img"
        aria-label={`开盘概率: 高开 ${probs[0].value.toFixed(1)}% / 平开 ${probs[1].value.toFixed(1)}% / 低开 ${probs[2].value.toFixed(1)}%`}
      >
        {probs.map(p => (
          <span
            key={p.key}
            className={cn('h-full', TONE[PROB_TONE[p.key]].bar)}
            style={{ width: `${clampPct(p.value)}%` }}
          />
        ))}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] tabular-nums text-muted">
        {probs.map(p => (
          <span key={p.key} className="inline-flex items-center gap-0.5">
            <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', TONE[PROB_TONE[p.key]].bar)} aria-hidden="true" />
            {p.label}<span className={cn('font-mono', TONE[PROB_TONE[p.key]].value)}>{p.value.toFixed(1)}%</span>
          </span>
        ))}
      </div>

      {/* 三个驱动源, 只给关键数字; 窄屏允许换行而不是溢出。每个驱动源标注自己的数据日期,
          因为美股隔夜是上一交易日收盘、亚太是当天, 混在一起看会误判新鲜度。
          用户 m18503: 亚太原片面, 应能看到日本/韩国各自表现 → 展开 asia.rows 单项。 */}
      <div className="mt-1.5 space-y-0.5 text-[10px] text-muted">
        <div className="flex flex-wrap items-center gap-x-1">
          <span className="shrink-0">A50 期货</span>
          <span className={cn('font-mono tabular-nums', pctToneClass(data.a50?.chg_pct))}>
            {pctText(data.a50?.chg_pct)}
          </span>
          <span className="ml-auto shrink-0 opacity-70">权重 65%</span>
          <span className="shrink-0 font-mono tabular-nums opacity-60">{data.a50?.as_of ?? ''}</span>
        </div>
        <div className="flex flex-wrap items-center gap-x-1">
          <span className="shrink-0">美股隔夜</span>
          <span className={cn('font-mono tabular-nums', pctToneClass(data.us?.chg_pct))}>
            {pctText(data.us?.chg_pct)}
          </span>
          {data.us?.rows?.length ? (
            <span className="flex flex-wrap items-center gap-x-1.5">
              {data.us.rows.map(r => (
                <span key={r.code} className="inline-flex items-center gap-0.5">
                  <span>{r.name}</span>
                  <span className={cn('font-mono tabular-nums', pctToneClass(r.chg_pct))}>{pctText(r.chg_pct)}</span>
                </span>
              ))}
            </span>
          ) : null}
          <span className="ml-auto shrink-0 opacity-70">权重 20%</span>
          <span className="shrink-0 font-mono tabular-nums opacity-60">{data.us?.as_of ?? ''}</span>
        </div>
        <div className="flex flex-wrap items-center gap-x-1">
          <span className="shrink-0">亚太同期</span>
          <span className={cn('font-mono tabular-nums', pctToneClass(data.asia?.chg_pct))}>
            {pctText(data.asia?.chg_pct)}
          </span>
          {data.asia?.rows?.length ? (
            <span className="flex flex-wrap items-center gap-x-1.5">
              {data.asia.rows.map(r => (
                <span key={r.code} className="inline-flex items-center gap-0.5">
                  <span>{r.name}</span>
                  <span className={cn('font-mono tabular-nums', pctToneClass(r.chg_pct))}>{pctText(r.chg_pct)}</span>
                </span>
              ))}
            </span>
          ) : null}
          <span className="ml-auto shrink-0 opacity-70">权重 15%</span>
          <span className="shrink-0 font-mono tabular-nums opacity-60">{data.asia?.as_of ?? ''}</span>
        </div>
      </div>

      <ImpactNote>
        对模拟盘: 偏空时开盘股数与建仓节奏会偏保守。本卡只提示<b className="text-secondary">开盘方向</b>,
        不直接改仓位系数 (系数见「大盘择时」「风险雷达」卡)。
      </ImpactNote>
    </Card>
  )
}

// ── 2. 大盘择时 (固定色标: warning 琥珀; 状态色: 四态) ──────────────

/** 大盘择时 — 沪深300 收盘 vs MA20/MA60 四态 + 系数 + 三路合成的建议仓位。 */
export function TimingWidget() {
  const qc = useQueryClient()
  const timingQuery = useQuery({
    queryKey: QK.riskTiming(),
    queryFn: () => api.riskTiming(),
    // 竞价结束到收盘之间当日K 还在定型, 后端每 3 分钟补算一次 →
    // 前端 2 分钟轮询, 用户打开页面就能看到当天数字 (原来 5 分钟会慢一拍)。
    refetchInterval: 120_000,
    staleTime: 60_000,
  })
  const adviceQuery = useQuery({
    queryKey: QK.riskAdvice(),
    queryFn: () => api.riskAdvice(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
  const retry = () => {
    void qc.invalidateQueries({ queryKey: QK.riskTiming() })
    void qc.invalidateQueries({ queryKey: QK.riskAdvice() })
  }
  const busy = timingQuery.isFetching || adviceQuery.isFetching

  if (timingQuery.isLoading) {
    return (
      <Card label="大盘择时" icon={Compass} title="大盘择时" tone={ID_TIMING} identity={ID_TIMING} busy>
        <WidgetLoading label="大盘择时" />
      </Card>
    )
  }
  if (timingQuery.isError) {
    return (
      <Card label="大盘择时" icon={Compass} title="大盘择时" tone="muted" identity={ID_TIMING}>
        <WidgetState
          title="大盘择时加载失败"
          detail={`${errorText(timingQuery.error)}（本地日K 读取失败, 可稍后重试）`}
          onRetry={retry}
          busy={busy}
        />
      </Card>
    )
  }

  const timing: RiskTiming | undefined = timingQuery.data
  if (!timing) {
    return (
      <Card label="大盘择时" icon={Compass} title="大盘择时" tone="muted" identity={ID_TIMING}>
        <WidgetState title="大盘择时暂无数据" detail="尚未取到择时数据, 可稍后重试。" onRetry={retry} busy={busy} />
      </Card>
    )
  }

  // risk_off → 整卡走 bear 色调, risk_on → bull, weak → warning, 缺失 → muted。
  const stateTone = trendTone(timing.state)
  const stateStyle = TONE[stateTone]
  const advice: RiskAdvice | undefined = adviceQuery.data
  const parts = advice?.parts

  return (
    <Card
      label="大盘择时"
      icon={Compass}
      title="大盘择时"
      tone={stateTone}
      identity={ID_TIMING}
      hint={timing.symbol || undefined}
    >
      {/* 数据日期: 大盘择时是日K 口径, as_of 就是这根K线的交易日; 盘中那根未定型 */}
      <DataDateRow as_of={timing.as_of} staleDays={timing.stale_days} intraday={timing.intraday} />

      {/* 四态 + 择时系数 */}
      <div className="mt-1 flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
        <span className={cn('text-lg font-semibold leading-none', stateStyle.value)}>{timing.state_label}</span>
        <span
          className={cn('rounded-full border px-1.5 py-px font-mono text-[10px] tabular-nums', stateStyle.chip)}
          title={TREND_LEGEND}
        >
          择时系数 {coeffText(timing.exposure)}
        </span>
      </div>

      {/* 收盘 / MA20 / MA60 */}
      <div className="mt-1 grid grid-cols-3 gap-1 text-[10px]">
        <div className="min-w-0 rounded-sm bg-elevated/45 px-1.5 py-1">
          <div className="truncate text-muted">收盘</div>
          <div className="truncate font-mono text-[11px] tabular-nums text-foreground">{fmtPrice(timing.close, 2)}</div>
        </div>
        <div className="min-w-0 rounded-sm bg-elevated/45 px-1.5 py-1">
          <div className="truncate text-muted">MA20</div>
          <div className="truncate font-mono text-[11px] tabular-nums text-foreground">{fmtPrice(timing.ma20, 2)}</div>
          <div className={cn('truncate font-mono tabular-nums', pctToneClass(timing.pct_vs_ma20))}>
            {pctText(timing.pct_vs_ma20)}
          </div>
        </div>
        <div className="min-w-0 rounded-sm bg-elevated/45 px-1.5 py-1">
          <div className="truncate text-muted">MA60</div>
          <div className="truncate font-mono text-[11px] tabular-nums text-foreground">{fmtPrice(timing.ma60, 2)}</div>
          <div className={cn('truncate font-mono tabular-nums', pctToneClass(timing.pct_vs_ma60))}>
            {pctText(timing.pct_vs_ma60)}
          </div>
        </div>
      </div>

      {/* 三路合成建议仓位: 分列展示「是谁在压仓位」 */}
      {advice ? (
        <div className={cn('mt-1.5 border-t pt-1.5', stateStyle.border)}>
          <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
            <span className="shrink-0 text-[10px] text-muted">建议仓位</span>
            <span className={cn('font-mono text-base font-semibold leading-none tabular-nums', stateStyle.value)}>
              {numText(advice.suggested_exposure_pct, 1)}%
            </span>
            <span className={cn('rounded-full border px-1.5 py-px text-[10px]', stateStyle.chip)}>{advice.label}</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-[10px]">
            {parts && <CoeffChip label="择时" part={parts.timing} />}
            {parts && <CoeffChip label="情绪" part={parts.phase} />}
            {parts && <CoeffChip label="外围" part={parts.risk} />}
          </div>
        </div>
      ) : (
        <div className="mt-1.5 border-t border-border/60 pt-1.5 text-[10px] text-muted">
          {adviceQuery.isError ? '综合建议仓位加载失败' : '综合建议仓位加载中…'}
          <button
            type="button"
            onClick={retry}
            className="ml-1 underline decoration-dotted underline-offset-2 hover:text-accent"
          >
            重试
          </button>
        </div>
      )}

      <ImpactNote>
        对模拟盘: <b className={cn('font-mono tabular-nums', stateStyle.value)}>{coeffText(timing.exposure)}</b> 在建仓时乘到买入股数上,
        系数 &lt;1 就是按比例缩小持仓数与仓位比例; 卖出(止损/止盈/超时)永不受影响。
      </ImpactNote>
    </Card>
  )
}

// ── 3. 情绪周期 (固定色标: bull 红; 状态色: 五档情绪) ──────────────

/** 情绪周期 — 最新阶段 + 五档状态分 + 情绪系数 (来自合成建议的 phase 路)。 */
export function RegimePhaseWidget() {
  const qc = useQueryClient()
  const latestQuery = useQuery({
    queryKey: QK.regimeLatest,
    queryFn: () => api.regimeLatest(),
    // 情绪周期是日级离线计算, 但盘中后端每 3 分钟补算一次 →
    // 2 分钟轮询与后端节奏对齐 (原来 5 分钟会慢一拍)。
    refetchInterval: 120_000,
    staleTime: 60_000,
  })
  const adviceQuery = useQuery({
    queryKey: QK.riskAdvice(),
    queryFn: () => api.riskAdvice(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
  const retry = () => {
    void qc.invalidateQueries({ queryKey: QK.regimeLatest })
    void qc.invalidateQueries({ queryKey: QK.riskAdvice() })
  }
  const busy = latestQuery.isFetching || adviceQuery.isFetching

  if (latestQuery.isLoading) {
    return (
      <Card label="情绪周期" icon={Flame} title="情绪周期" tone={ID_REGIME} identity={ID_REGIME} busy>
        <WidgetLoading label="情绪周期" />
      </Card>
    )
  }
  if (latestQuery.isError) {
    return (
      <Card label="情绪周期" icon={Flame} title="情绪周期" tone="muted" identity={ID_REGIME}>
        <WidgetState
          title="情绪周期加载失败"
          detail={`${errorText(latestQuery.error)}（环境快照读取失败, 可稍后重试）`}
          onRetry={retry}
          busy={busy}
        />
      </Card>
    )
  }

  const row = latestQuery.data?.row
  if (!row) {
    return (
      <Card label="情绪周期" icon={Flame} title="情绪周期" tone="muted" identity={ID_REGIME}>
        <WidgetState
          title="情绪周期暂无数据"
          detail="最新交易日还没有环境快照, 可在「市场环境」页点「重算」补全。"
          onRetry={retry}
          busy={busy}
        />
      </Card>
    )
  }

  // 阶段可能缺 (旧数据/未重算), 缺失时只显示五档状态。
  const phase: MarketPhase | null = row.phase ?? null
  const phaseLabel = phase ? MARKET_PHASE_LABELS[phase] : '阶段缺失'
  const stateTone = REGIME_STATE_TONE[row.state] ?? 'muted'
  const stateStyle = TONE[stateTone]
  const stateLabel = row.state ? (REGIME_STATE_LABELS[row.state] ?? row.state) : '—'
  const phasePart = adviceQuery.data?.parts?.phase

  return (
    <Card
      label="情绪周期"
      icon={Flame}
      title="情绪周期"
      tone={stateTone}
      identity={ID_REGIME}
    >
      {/* 数据日期: 情绪周期是日级快照, row.date 就是该交易日的日期 */}
      <DataDateRow as_of={row.date} />

      <div className="mt-1 flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
        <span className={cn('text-lg font-semibold leading-none', stateStyle.value)}>{phaseLabel}</span>
        <span className={cn('rounded-full border px-1.5 py-px text-[10px]', stateStyle.chip)}>{stateLabel}</span>
        {phasePart && (
          <span
            className="ml-auto shrink-0 font-mono text-[10px] tabular-nums text-muted"
            title={phasePart.missing ? '情绪系数数据缺失, 按 1.0 中性参与' : phasePart.detail}
          >
            情绪系数 {phasePart.missing ? '×1.00*' : coeffText(phasePart.value)}
          </span>
        )}
      </div>

      <div className="mt-1 grid grid-cols-3 gap-1 text-[10px]">
        <div className="min-w-0 rounded-sm bg-elevated/45 px-1.5 py-1">
          <div className="truncate text-muted">综合分</div>
          <div className={cn('truncate font-mono text-[11px] tabular-nums', stateStyle.value)}>{numText(row.score, 0)}</div>
        </div>
        <div className="min-w-0 rounded-sm bg-elevated/45 px-1.5 py-1">
          <div className="truncate text-muted">涨停/跌停</div>
          <div className="truncate font-mono text-[11px] tabular-nums">
            <span className="text-bull">{row.limit_up}</span>
            <span className="text-muted">/</span>
            <span className="text-bear">{row.limit_down}</span>
          </div>
        </div>
        <div className="min-w-0 rounded-sm bg-elevated/45 px-1.5 py-1">
          <div className="truncate text-muted">最高板</div>
          <div className="truncate font-mono text-[11px] tabular-nums text-foreground">
            {row.max_consecutive || 0}<span className="text-muted">板</span>
          </div>
        </div>
      </div>

      <ImpactNote>
        对模拟盘: 五档状态的仓位系数 (强势 1.20 / 偏强 1.00 / 震荡 0.75 / 偏弱 0.50 / 弱势 0.30)
        与择时、外围系数相乘后决定建仓股数; 只会缩小或放大买入量, 不会拦住卖出。
      </ImpactNote>
    </Card>
  )
}

// ── 4. 风险雷达 (固定色标: bear 绿; 状态色: 风险等级) ──────────────

/** 风险雷达 — 外围风险分/等级 + 仓位系数 + 三区域概况。 */
export function GlobalRiskWidget() {
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: QK.riskRadar(),
    queryFn: () => api.riskRadar(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
  const retry = () => { void qc.invalidateQueries({ queryKey: QK.riskRadar() }) }

  if (query.isLoading) {
    return (
      <Card label="风险雷达" icon={ShieldAlert} title="风险雷达" tone={ID_RISK} identity={ID_RISK} busy>
        <WidgetLoading label="风险雷达" />
      </Card>
    )
  }
  if (query.isError) {
    return (
      <Card label="风险雷达" icon={ShieldAlert} title="风险雷达" tone="muted" identity={ID_RISK}>
        <WidgetState
          title="风险雷达加载失败"
          detail={`${errorText(query.error)}（外围行情接口偶发超时, 可稍后重试）`}
          onRetry={retry}
          busy={query.isFetching}
        />
      </Card>
    )
  }

  const data: RiskRadar | undefined = query.data
  if (!data) {
    return (
      <Card label="风险雷达" icon={ShieldAlert} title="风险雷达" tone="muted" identity={ID_RISK}>
        <WidgetState title="风险雷达暂无数据" detail="尚未取到风险雷达数据, 可稍后重试。" onRetry={retry} busy={query.isFetching} />
      </Card>
    )
  }
  if (!data.ok) {
    return (
      <Card label="风险雷达" icon={ShieldAlert} title="风险雷达" tone="muted" identity={ID_RISK}>
        <WidgetState
          title="风险雷达暂无数据"
          detail={data.advice || '外围行情获取失败, 仓位按无风险信号处理。'}
          onRetry={retry}
          busy={query.isFetching}
        />
      </Card>
    )
  }

  // 低=绿 / 正常=蓝 / 偏高=黄 / 高=红 —— 与 Regime 页大卡同一套, 不红绿打架。
  const levelToneValue = levelTone(data.level)
  const levelStyle = TONE[levelToneValue]
  const regionOrder: Array<{ key: 'us' | 'asia' | 'hk'; label: string }> = [
    { key: 'us', label: '美股' },
    { key: 'asia', label: '亚太' },
    { key: 'hk', label: '恒生' },
  ]

  return (
    <Card
      label="风险雷达"
      icon={ShieldAlert}
      title="风险雷达"
      tone={levelToneValue}
      identity={ID_RISK}
      hint={data.level_text || undefined}
    >
      {/* 数据日期: 风险雷达是外围指数快照, as_of = 这批外围数据对应的交易日 */}
      <DataDateRow as_of={data.as_of} staleDays={data.stale_days} />

      <div className="mt-1 flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
        <span className="shrink-0 text-[10px] text-muted">仓位系数</span>
        <span className={cn('font-mono text-lg font-semibold leading-none tabular-nums', levelStyle.value)}>
          {coeffText(data.exposure)}
        </span>
        <span className={cn('rounded-full border px-1.5 py-px text-[10px] font-medium', levelStyle.chip)}>
          {data.level_text || '未知'}
        </span>
        <span className="ml-auto shrink-0 font-mono text-[10px] tabular-nums text-muted">
          风险分 {data.score != null ? data.score.toFixed(1) : '—'}
        </span>
      </div>

      {/* 三区域 + 单指数明细: 分数 + 该区域系数 + 各指数涨跌幅。
          用户 m18503: 亚太原片面, 应能看到日本/韩国/恒生各自表现 ——
          后端 /api/risk/radar 已返回 indexes(每只含 chg_pct), 这里把聚合区域展开。 */}
      <div className="mt-1.5 space-y-1 text-[10px]">
        {regionOrder.map(({ key, label }) => {
          const region = data.regions?.[key]
          const idxs = data.indexes?.filter(i => i.region === key) ?? []
          return (
            <div key={key} className="space-y-0.5">
              <div className="flex items-center gap-1">
                <span className="w-8 shrink-0 text-muted">{label}</span>
                <span className="truncate font-mono tabular-nums text-foreground">
                  {region?.score != null ? region.score.toFixed(1) : '缺失'}
                </span>
                <span className="ml-auto shrink-0 font-mono tabular-nums text-secondary">
                  {region?.exposure != null ? coeffText(region.exposure) : '—'}
                </span>
              </div>
              {idxs.length > 0 && (
                <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 pl-2">
                  {idxs.map(ix => (
                    <span
                      key={ix.code}
                      className={cn('inline-flex items-center gap-0.5', ix.stale && 'opacity-50')}
                      title={`${ix.name}${ix.as_of ? ` · ${ix.as_of}` : ''}${ix.stale ? ' · 数据过期' : ''}`}
                    >
                      <span className="text-muted">{ix.name}</span>
                      <span className={cn('font-mono tabular-nums', pctToneClass(ix.chg_pct))}>
                        {pctText(ix.chg_pct)}
                      </span>
                    </span>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* A股宽度: dn_ratio 是 0~1 小数, 这里才乘 100 */}
      {data.breadth?.ok && data.breadth.dn_ratio != null && (
        <div className="mt-1 flex items-center gap-1 text-[10px] text-muted">
          <span className="shrink-0">下跌占比</span>
          <span className="h-1 min-w-0 flex-1 overflow-hidden rounded-sm bg-bear/20" aria-hidden="true">
            <span
              className="block h-full rounded-sm bg-bear"
              style={{ width: `${Math.min(Math.max(data.breadth.dn_ratio, 0), 1) * 100}%` }}
            />
          </span>
          <span className="shrink-0 font-mono tabular-nums text-foreground">{(data.breadth.dn_ratio * 100).toFixed(1)}%</span>
        </div>
      )}

      <ImpactNote>
        对模拟盘: 外围系数 <b className={cn('font-mono tabular-nums', levelStyle.value)}>{coeffText(data.exposure)}</b>
        {data.equity?.ok ? `（含账户回撤 ×${numText(data.equity.factor)}）` : '（账户无净值数据, 未叠加回撤系数）'}
        在建仓时按比例缩小持仓数与仓位; 风险越高压得越狠, 但不会压到 0 —— 卖出永不受影响。
      </ImpactNote>
    </Card>
  )
}
