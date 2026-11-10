/**
 * 风险雷达卡 —— 外围 7 指数 + A 股宽度 + 账户回撤 → 合成仓位系数。
 *
 * 单位口径是硬约定 (与 lib/api.ts「风险雷达」段、backend/app/services/global_risk.py 同步),
 * 渲染时不做任何启发式换算:
 *   · chg_pct / ma20_pct / drawdown_pct 是**百分数数值** (-1.07 表示 -1.07%),
 *     直接 toFixed 拼 %, 不要再乘 100 —— 所以不能用 lib/format 的 fmtPct。
 *   · score 是 0~100 分 (50 = 中性, 越高越危险)。
 *   · exposure / equity.factor 是**仓位系数** (0.35~1.10 / 0.20~1.00), 显示成 ×0.74。
 *   · breadth.dn_ratio 是 0~1 小数 (0.1466 = 14.66% 在跌), 只有这里要 ×100。
 * 字段允许 null、regions 允许缺键 (该区域全取数失败), 一律降级成「缺失」而不是画 0。
 */
import { type CSSProperties } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { CloudOff, Radar, RefreshCw } from 'lucide-react'
import { api, type RiskIndex, type RiskLevel, type RiskRadar, type RiskRegionKey } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPrice, priceColorClass } from '@/lib/format'
import { Skeleton } from '@/components/data/Skeleton'

const REGION_ORDER: RiskRegionKey[] = ['us', 'asia', 'hk']
const REGION_LABEL: Record<RiskRegionKey, string> = { us: '美股', asia: '亚太', hk: '恒生' }

/** 风险等级色: 低=绿 / 正常=蓝 / 偏高=黄 / 高=红 / 未知=灰 */
const LEVEL_COLOR: Record<RiskLevel, string> = {
  low: '#12b76a', normal: '#3b82f6', elevated: '#f79009', high: '#f04438', unknown: '#94a3b8',
}

/** 与后端 score_level() 完全一致的分档 (阈值 75 / 55 / 30), 用于单指数与区域分着色。 */
function scoreLevel(score: number | null | undefined): RiskLevel {
  if (score == null || Number.isNaN(score)) return 'unknown'
  if (score >= 75) return 'high'
  if (score >= 55) return 'elevated'
  if (score >= 30) return 'normal'
  return 'low'
}

/** 等级色向正文色压一点, 保证浅色/深色主题下都够对比度。 */
function readable(color: string): string {
  return `color-mix(in srgb, ${color} 82%, hsl(var(--fg-primary)))`
}

/** 百分数数值 → "+1.07%" / "-5.25%" / "—" (不乘 100)。 */
function pctText(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`
}

function scoreText(v: number | null | undefined): string {
  return v == null || Number.isNaN(v) ? '—' : v.toFixed(1)
}

/** 与页内 .regime-panel 同一套外壳尺寸/色板 (不改 Regime.css, 用内联 class 复刻)。 */
const shell = 'min-w-0 rounded-[8px] border border-border bg-surface p-4'
const indexGrid: CSSProperties = { gridTemplateColumns: 'minmax(0, 1fr) 54px 52px 46px 26px' }

function CardHeading({ hint, fetchedAt, asOf, staleDays }: {
  hint: string
  fetchedAt?: string
  asOf?: string | null
  staleDays?: number | null
}) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1.5">
      <div className="flex shrink-0 items-center gap-[7px]">
        <Radar className="h-[15px] w-[15px] shrink-0 text-accent" aria-hidden="true" />
        <h2 className="m-0 text-[13px] font-semibold leading-normal text-foreground">风险雷达</h2>
        {asOf && (
          <span
            className="rounded-[3px] bg-elevated px-1.5 font-mono text-[10px] leading-[1.8] text-secondary"
            title="外围数据对应的交易日 (不是抓取时刻)"
          >
            数据 {asOf}
            {typeof staleDays === 'number' && staleDays > 0 ? ` · 滞后 ${staleDays} 天` : ''}
          </span>
        )}
      </div>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[11px] leading-relaxed text-secondary">
        <span className="max-w-[70ch]">{hint}</span>
        {fetchedAt && <time className="font-mono text-[10px] tabular-nums">抓取 {fetchedAt}</time>}
      </div>
    </div>
  )
}

function StateBlock({ title, detail, onRetry, busy }: {
  title: string, detail?: string, onRetry: () => void, busy?: boolean
}) {
  return (
    <div
      className="mt-3 flex flex-col items-start gap-2 rounded-[8px] border border-dashed border-border p-4 text-[12px] leading-relaxed text-secondary"
      role="alert"
    >
      <span className="flex items-center gap-1.5 text-[12px] font-semibold text-foreground">
        <CloudOff className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        {title}
      </span>
      {detail && <p className="m-0 max-w-[70ch] [overflow-wrap:anywhere]">{detail}</p>}
      <button
        type="button"
        onClick={onRetry}
        disabled={busy}
        className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-elevated px-2.5 py-1 text-[11px] text-foreground transition-colors hover:border-accent/60 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <RefreshCw className={cn('h-3 w-3', busy && 'animate-spin')} aria-hidden="true" />
        {busy ? '重试中…' : '重试'}
      </button>
    </div>
  )
}

function IndexRow({ ix }: { ix: RiskIndex }) {
  const level = scoreLevel(ix.score)
  return (
    <div
      className={cn('grid items-baseline gap-x-1.5 py-[3px] text-[11px] tabular-nums', ix.stale && 'opacity-50')}
      style={indexGrid}
      title={`${ix.name} (${ix.code}) · 数据源 ${ix.source}${ix.as_of ? ` · 截至 ${ix.as_of}` : ''}${ix.stale ? ' · 数据源已过期, 不参与加权' : ''}`}
    >
      <span className="flex min-w-0 items-baseline gap-1.5">
        <span className="truncate text-[11px] text-foreground">{ix.name}</span>
        {ix.stale && (
          <span className="shrink-0 rounded-[3px] bg-elevated px-1 text-[9px] leading-[1.7] text-secondary">数据过期</span>
        )}
      </span>
      <span className={cn('text-right font-mono', ix.price == null && 'text-secondary')}>
        {ix.price == null ? '缺失' : fmtPrice(ix.price)}
      </span>
      <span className={cn('text-right font-mono', priceColorClass(ix.chg_pct))}>{pctText(ix.chg_pct)}</span>
      <span className={cn('text-right font-mono', priceColorClass(ix.ma20_pct))}>{pctText(ix.ma20_pct)}</span>
      <span className="text-right font-mono font-semibold" style={{ color: readable(LEVEL_COLOR[level]) }}>
        {scoreText(ix.score)}
      </span>
    </div>
  )
}

function RegionCard({ regionKey, data }: { regionKey: RiskRegionKey, data: RiskRadar }) {
  const region = data.regions[regionKey]
  const rows = data.indexes.filter(ix => ix.region === regionKey)
  const level = scoreLevel(region?.score)
  return (
    <div className="min-w-0 rounded-[6px] border border-border bg-base/45 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-1">
        <h3 className="m-0 text-[12px] font-semibold leading-normal text-foreground">{REGION_LABEL[regionKey]}</h3>
        <span className="font-mono text-[13px] font-semibold tabular-nums" style={{ color: readable(LEVEL_COLOR[level]) }}>
          {region?.score == null ? '缺失' : scoreText(region.score)}
          {region?.score != null && <small className="ml-0.5 font-sans text-[9px] font-normal text-secondary">分</small>}
        </span>
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-secondary">
        仓位系数{' '}
        <b className="font-mono text-[12px] font-semibold text-foreground">
          {region?.exposure == null ? '—' : `×${region.exposure.toFixed(2)}`}
        </b>
      </p>
      {rows.length > 0 ? (
        <div className="mt-1.5 border-t border-border">
          <div className="grid items-baseline gap-x-1.5 pb-[3px] pt-1 text-[10px] leading-relaxed text-secondary" style={indexGrid}>
            <span>指数</span>
            <span className="text-right">现价</span>
            <span className="text-right">涨跌</span>
            <span className="text-right">MA20</span>
            <span className="text-right">分</span>
          </div>
          <div className="divide-y divide-border/60 border-t border-border/60">
            {rows.map(ix => <IndexRow key={ix.code} ix={ix} />)}
          </div>
        </div>
      ) : (
        <p className="mt-2 text-[11px] leading-relaxed text-secondary">本区域指数数据缺失</p>
      )}
    </div>
  )
}

function BreadthRow({ data }: { data: RiskRadar }) {
  const { breadth } = data
  const dnPct = breadth.dn_ratio == null ? null : Math.max(0, Math.min(1, breadth.dn_ratio)) * 100
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] leading-relaxed text-secondary">
      <span className="shrink-0 text-[11px] font-medium text-foreground">A股宽度</span>
      {breadth.ok ? (
        <>
          <div
            className="h-1.5 min-w-[80px] flex-1 overflow-hidden rounded bg-bear/20"
            role="img"
            aria-label={`A股宽度: 上涨 ${breadth.up} / 下跌 ${breadth.down} / 共 ${breadth.total}`}
          >
            <span className="block h-full rounded bg-bear transition-[width] duration-200" style={{ width: `${dnPct ?? 0}%` }} />
          </div>
          <span>
            上涨 <b className="font-mono text-[12px] font-semibold text-foreground">{breadth.up}</b>
            {' / '}下跌 <b className="font-mono text-[12px] font-semibold text-foreground">{breadth.down}</b>
            {' / '}共 <b className="font-mono text-[12px] font-semibold text-foreground">{breadth.total}</b>
          </span>
          <span className="text-[10px]">下跌占比 {dnPct == null ? '—' : `${dnPct.toFixed(2)}%`}</span>
        </>
      ) : (
        <span>样本不足</span>
      )}
    </div>
  )
}

function EquityRow({ data }: { data: RiskRadar }) {
  const { equity } = data
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] leading-relaxed text-secondary">
      <span className="shrink-0 text-[11px] font-medium text-foreground">账户回撤</span>
      {equity.ok ? (
        <span>
          当前回撤{' '}
          <b className="font-mono text-[12px] font-semibold text-foreground">
            {equity.drawdown_pct == null ? '—' : `${equity.drawdown_pct.toFixed(2)}%`}
          </b>
          {' · '}系数 <b className="font-mono text-[12px] font-semibold text-foreground">×{equity.factor.toFixed(2)}</b>
          <span className="ml-1 text-[10px]">（近 {equity.window_days} 个交易日净值）</span>
        </span>
      ) : (
        <span>无净值数据</span>
      )}
    </div>
  )
}

export function GlobalRiskCard() {
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: QK.riskRadar(),
    queryFn: () => api.riskRadar(),
    // 后端有 45s 缓存; 前端 60s 轮询、30s 内视为新鲜, 面板常开也不会把后端压垮。
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
  const retry = () => { void qc.invalidateQueries({ queryKey: QK.riskRadar() }) }
  const hint = '外围 7 指数 / A股宽度 / 账户回撤 → 仓位系数 · 60 秒自动刷新'

  if (query.isLoading) {
    return (
      <section className={shell} aria-label="风险雷达" aria-busy="true">
        <CardHeading hint={hint} />
        <div className="mt-3 grid gap-2.5" role="status" aria-label="正在加载风险雷达">
          <Skeleton w="w-44" h="h-8" />
          <Skeleton w="w-3/4" h="h-3" />
          <div className="mt-1 grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))' }}>
            {REGION_ORDER.map(key => (
              <div key={key} className="grid gap-2 rounded-[6px] border border-border p-3">
                <Skeleton w="w-16" h="h-3" />
                <Skeleton w="w-24" h="h-3" />
                <Skeleton h="h-3" />
                <Skeleton w="w-5/6" h="h-3" />
              </div>
            ))}
          </div>
        </div>
      </section>
    )
  }

  if (query.isError) {
    const message = query.error instanceof Error ? query.error.message : String(query.error)
    return (
      <section className={shell} aria-label="风险雷达">
        <CardHeading hint={hint} />
        <StateBlock
          title="风险雷达加载失败"
          detail={`${message}（外围行情接口偶发超时, 可稍后重试）`}
          onRetry={retry}
          busy={query.isFetching}
        />
      </section>
    )
  }

  const data = query.data
  if (!data) {
    // react-query 的 data 在首个成功响应前是 undefined (isLoading 之外还可能是
    // enabled=false / gc 回收后重挂载), 这里显式兜底而不是靠非空断言。
    return (
      <section className={shell} aria-label="风险雷达">
        <CardHeading hint={hint} />
        <StateBlock
          title="风险雷达暂无数据"
          detail="尚未取到风险雷达数据, 可稍后重试。"
          onRetry={retry}
          busy={query.isFetching}
        />
      </section>
    )
  }
  const level: RiskLevel = LEVEL_COLOR[data.level] ? data.level : scoreLevel(data.score)
  const accent = LEVEL_COLOR[level]
  const levelColor = readable(accent)

  if (!data.ok) {
    return (
      <section className={shell} aria-label="风险雷达">
        <CardHeading
          hint={hint}
          fetchedAt={data.fetched_at || undefined}
          asOf={data.as_of}
          staleDays={data.stale_days}
        />
        <StateBlock
          title="风险雷达暂无数据（外围行情获取失败）"
          detail={data.advice || undefined}
          onRetry={retry}
          busy={query.isFetching}
        />
      </section>
    )
  }

  return (
    <section className={shell} aria-label="风险雷达">
      <CardHeading
        hint={hint}
        fetchedAt={data.fetched_at || undefined}
        asOf={data.as_of}
        staleDays={data.stale_days}
      />

      {/* ── 主数值: 仓位系数 + 等级徽标 + 综合风险分 ── */}
      <div className="mt-3 flex flex-wrap items-end justify-between gap-x-5 gap-y-2">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="text-[11px] text-secondary">仓位系数</span>
          <span className="font-mono text-[28px] font-semibold leading-tight tracking-tight" style={{ color: levelColor }}>
            ×{data.exposure.toFixed(2)}
          </span>
          <span
            className="inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold leading-relaxed"
            style={{ color: levelColor, borderColor: levelColor, backgroundColor: `color-mix(in srgb, ${accent} 14%, transparent)` }}
            title="综合风险等级: 低 / 正常 / 偏高 / 高"
          >
            {data.level_text || '未知'}
          </span>
        </div>
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[11px] text-secondary">
          <span>
            综合风险分 <b className="font-mono text-[15px] font-semibold" style={{ color: levelColor }}>{scoreText(data.score)}</b>
            <span className="text-[10px]"> / 100</span>
          </span>
          {data.global_exposure != null && (
            <span>
              仅外围 <b className="font-mono text-[12px] font-semibold text-foreground">×{data.global_exposure.toFixed(2)}</b>
            </span>
          )}
          <span>
            回撤调整 <b className="font-mono text-[12px] font-semibold text-foreground">×{data.equity.factor.toFixed(2)}</b>
          </span>
        </div>
      </div>
      <div
        className="mt-2.5 h-1 overflow-hidden rounded bg-base"
        role="img"
        aria-label={`综合风险分 ${scoreText(data.score)}，满分 100，越高越危险`}
      >
        <span
          className="block h-full rounded transition-[width] duration-200"
          style={{ width: `${Math.max(0, Math.min(100, data.score ?? 0))}%`, backgroundColor: accent }}
        />
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-secondary">
        综合分 = 三区域风险分加权 (美股 45% / 亚太 35% / 恒生 20%); 仓位系数 = 外围系数 × 账户回撤系数, 夹在 ×0.35 ~ ×1.10。
      </p>

      {/* ── 三个区域小卡 + 各区域指数明细 ── */}
      <div className="mt-3.5 grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))' }}>
        {REGION_ORDER.map(key => <RegionCard key={key} regionKey={key} data={data} />)}
      </div>

      {/* ── A股宽度 / 账户回撤 ── */}
      <div className="mt-3.5 grid gap-2 border-t border-border pt-3">
        <BreadthRow data={data} />
        <EquityRow data={data} />
      </div>

      {data.advice && <p className="mt-3 text-[12px] leading-relaxed text-foreground [overflow-wrap:anywhere]">{data.advice}</p>}
      {data.missing.length > 0 && (
        <p className="mt-2 text-[11px] leading-relaxed text-warning [overflow-wrap:anywhere]">
          缺失: {data.missing.join(', ')} — 这些指数取数失败或数据源已过期, 未参与加权
        </p>
      )}
    </section>
  )
}
