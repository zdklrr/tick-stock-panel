/**
 * 大盘择时卡 —— 沪深300 收盘 vs MA20/MA60 判四态 (zdk 口径) + 三路合成的建议仓位。
 *
 * 这里有两个**正交**维度, 不要混为一谈:
 *   · 大盘择时 (本卡上半部): 只回答「趋势在不在」, 4 态 + 系数 1.0/0.65/0.35/0.5。
 *   · 情绪周期 (页面其它卡): 回答「情绪在哪个阶段」, 5 档。
 *   · 风险雷达 (GlobalRiskCard): 回答「外围风险多高」。
 * 三者相乘才是可执行的仓位数字 (本卡下半部), 分列展示是为了让用户看懂**是谁在压仓位**。
 *
 * 单位口径是硬约定 (与 lib/api.ts「大盘择时」段、backend/app/services/market_timing.py 同步),
 * 渲染时不做任何启发式换算:
 *   · close / ma20 / ma60 是价格, 用 fmtPrice。
 *   · pct_vs_ma20 / pct_vs_ma60 是**百分数数值** (-4.69 表示 -4.69%), 直接 toFixed 拼 %,
 *     不要再乘 100 —— 所以不能用 lib/format 的 fmtPct。
 *   · exposure / parts[].value / suggested_exposure 是**仓位系数**, 显示成 ×0.35。
 *   · suggested_exposure_pct 是**百分数数值** (26.7 表示 26.7%), 直接拼 %。
 * 任何一路取数失败时后端按中性 1.0 参与并标 missing=true, 前端显示「数据缺失(按 1.0 计)」而不是画 0。
 */
import { type CSSProperties } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { CloudOff, Compass, RefreshCw } from 'lucide-react'
import {
  api,
  type RiskAdvice,
  type RiskAdvicePart,
  type RiskTiming,
  type TrendState,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPrice, priceColorClass } from '@/lib/format'
import { Skeleton } from '@/components/data/Skeleton'

/** 四态配色, 沿用项目 token 语义: 偏多=红(danger/bull) / 偏弱=黄(warning) / 空头=绿(bear) / 缺失=灰(muted)。 */
const TREND_COLOR: Record<TrendState, string> = {
  risk_on: '#f04438',
  weak: '#f79009',
  risk_off: '#12b76a',
  unknown: '#94a3b8',
}

/** 合成仓位分档着色: 越高越激进(偏红), 越低越防守(偏绿) —— 与上面的红涨绿跌语义一致。 */
function exposureColor(value: number): string {
  if (value >= 0.85) return '#f04438' // 可以满仓做多
  if (value >= 0.55) return '#f79009' // 正常仓位
  if (value >= 0.35) return '#3b82f6' // 半仓为宜
  return '#12b76a' // 轻仓试单 / 仅留底仓
}

/** 等级色向正文色压一点, 保证浅色/深色主题下都够对比度。 */
function readable(color: string): string {
  return `color-mix(in srgb, ${color} 82%, hsl(var(--fg-primary)))`
}

/** 百分数数值 → "+1.23%" / "-4.69%" / "—" (不乘 100)。 */
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

/**
 * 建议仓位的百分比文案 —— **以后端 suggested_exposure_pct 为准**（百分数数值, 26.5 表示 26.5%）。
 * 与看板 TimingWidget 同一口径: 保留一位小数, 不再自己取整到整数百分比,
 * 避免同一份数据在两处显示成 26.5% / 27% 两种数字。
 * 仅当后端字段缺失时才用系数 ×100 回退（此时没有小数来源, 取整更诚实）。
 */
function suggestedPctText(advice: RiskAdvice): string {
  const pct = advice.suggested_exposure_pct
  if (pct != null && !Number.isNaN(pct)) return `${numText(pct, 1)}%`
  const coeff = advice.suggested_exposure
  if (coeff == null || Number.isNaN(coeff)) return '—'
  return `${numText(coeff * 100, 0)}%`
}

/** 与页内 .regime-panel 同一套外壳尺寸/色板 (不改 Regime.css, 用内联 class 复刻)。 */
const shell = 'min-w-0 rounded-[8px] border border-border bg-surface p-4'
const priceGrid: CSSProperties = { gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))' }
const partGrid: CSSProperties = { gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))' }

const PART_ORDER: Array<RiskAdvicePart['key']> = ['timing', 'phase', 'risk']

function CardHeading({ hint }: { hint: string }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1.5">
      <div className="flex shrink-0 items-center gap-[7px]">
        <Compass className="h-[15px] w-[15px] shrink-0 text-accent" aria-hidden="true" />
        <h2 className="m-0 text-[13px] font-semibold leading-normal text-foreground">大盘择时</h2>
      </div>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[11px] leading-relaxed text-secondary">
        <span className="max-w-[70ch]">{hint}</span>
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

/** 单路系数小卡: 分列展示, 让用户看出「是谁在压仓位」。 */
function PartCell({ part }: { part: RiskAdvicePart }) {
  return (
    <div className="min-w-0 rounded-[6px] border border-border bg-base/45 p-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-[11px] text-secondary">{part.label}</span>
        <span className="shrink-0 font-mono text-[13px] font-semibold tabular-nums text-foreground">
          ×{numText(part.value)}
        </span>
      </div>
      <div className="mt-1 truncate text-[10px] leading-relaxed text-secondary">
        {part.missing
          ? <span className="text-warning">数据缺失(按 1.0 计)</span>
          : (part.state_label || part.detail || '—')}
      </div>
    </div>
  )
}

/** 上半部: 四态 + 均线明细。数据缺失时也不整卡消失, 只把数字换成「—」。 */
function TimingBody({ timing }: { timing: RiskTiming }) {
  const color = readable(TREND_COLOR[timing.state] || TREND_COLOR.unknown)
  return (
    <>
      <div className="mt-3 flex flex-wrap items-end justify-between gap-x-5 gap-y-2">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="text-[11px] text-secondary">趋势状态</span>
          <span className="text-[24px] font-semibold leading-tight" style={{ color }}>
            {timing.state_label || '数据缺失'}
          </span>
          <span
            className="inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold leading-relaxed"
            style={{
              color,
              borderColor: color,
              backgroundColor: `color-mix(in srgb, ${TREND_COLOR[timing.state] || TREND_COLOR.unknown} 14%, transparent)`,
            }}
            title="择时仓位系数: 趋势偏多 ×1.00 / 趋势偏弱 ×0.65 / 空头趋势 ×0.35 / 数据缺失 ×0.5"
          >
            择时系数 ×{numText(timing.exposure)}
          </span>
        </div>
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[11px] text-secondary">
          <span className="font-mono tabular-nums">
            {timing.symbol} · {timing.bars} 根日K
          </span>
          {timing.as_of && (
            <time className="font-mono text-[10px] tabular-nums" dateTime={timing.as_of}>
              截至 {timing.as_of}
            </time>
          )}
          {timing.stale && (
            <span
              className="inline-flex items-center rounded-[3px] bg-elevated px-1 text-[9px] leading-[1.7] text-warning"
              title={timing.stale_days != null ? `日K 滞后 ${timing.stale_days} 天, 建议先更新行情再参考` : '日K 滞后'}
            >
              数据滞后{timing.stale_days != null ? ` ${timing.stale_days} 天` : ''}
            </span>
          )}
        </div>
      </div>

      <div className="mt-3 grid gap-2" style={priceGrid}>
        <div className="min-w-0 rounded-[6px] border border-border bg-base/45 px-2.5 py-2">
          <div className="text-[10px] leading-relaxed text-secondary">收盘</div>
          <div className="font-mono text-[14px] font-semibold tabular-nums text-foreground">
            {fmtPrice(timing.close, 3)}
          </div>
        </div>
        <div className="min-w-0 rounded-[6px] border border-border bg-base/45 px-2.5 py-2">
          <div className="text-[10px] leading-relaxed text-secondary">MA20</div>
          <div className="flex items-baseline justify-between gap-2">
            <span className="font-mono text-[13px] tabular-nums text-foreground">{fmtPrice(timing.ma20, 3)}</span>
            <span className={cn('font-mono text-[11px] tabular-nums', priceColorClass(timing.pct_vs_ma20))}>
              {pctText(timing.pct_vs_ma20)}
            </span>
          </div>
        </div>
        <div className="min-w-0 rounded-[6px] border border-border bg-base/45 px-2.5 py-2">
          <div className="text-[10px] leading-relaxed text-secondary">MA60</div>
          <div className="flex items-baseline justify-between gap-2">
            <span className="font-mono text-[13px] tabular-nums text-foreground">{fmtPrice(timing.ma60, 3)}</span>
            <span className={cn('font-mono text-[11px] tabular-nums', priceColorClass(timing.pct_vs_ma60))}>
              {pctText(timing.pct_vs_ma60)}
            </span>
          </div>
        </div>
      </div>

      {timing.advice && (
        <p className="mb-0 mt-2.5 text-[11px] leading-relaxed text-secondary">{timing.advice}</p>
      )}
    </>
  )
}

/** 下半部: 三路合成后的可执行数字。 */
function AdviceBody({ advice }: { advice: RiskAdvice }) {
  const color = readable(exposureColor(advice.suggested_exposure))
  const paper = advice.detail?.paper
  return (
    <div className="mt-3.5 border-t border-dashed border-border pt-3">
      <div className="flex flex-wrap items-end justify-between gap-x-5 gap-y-2">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="text-[11px] text-secondary">综合建议仓位</span>
          <span className="font-mono text-[28px] font-semibold leading-tight tracking-tight" style={{ color }}>
            {suggestedPctText(advice)}
          </span>
          <span
            className="inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold leading-relaxed"
            style={{
              color,
              borderColor: color,
              backgroundColor: `color-mix(in srgb, ${exposureColor(advice.suggested_exposure)} 14%, transparent)`,
            }}
            title="大盘择时 × 情绪周期 × 外围风险, 三路相乘后夹在 0.15~1.20"
          >
            {advice.label || '—'}
          </span>
        </div>
        <div className="text-[11px] text-secondary">
          三路合成 <span className="font-mono tabular-nums text-foreground">×{numText(advice.suggested_exposure, 3)}</span>
          {advice.detail?.clamped && (
            <span className="ml-1.5 text-[10px]" title={`原始乘积超界, 已夹到 [${advice.detail.floor}, ${advice.detail.cap}]`}>
              (已夹到 {numText(advice.detail.floor)}~{numText(advice.detail.cap)})
            </span>
          )}
        </div>
      </div>

      {/* 三路来源分列 —— 谁在压仓位一眼可见 */}
      <div className="mt-2.5 grid gap-2" style={partGrid} role="list" aria-label="建议仓位的三路来源">
        {PART_ORDER.map(key => {
          const part = advice.parts?.[key]
          return part ? <div key={key} role="listitem"><PartCell part={part} /></div> : null
        })}
      </div>

      {advice.advice && (
        <p className="mb-0 mt-2.5 text-[11px] leading-relaxed text-secondary">{advice.advice}</p>
      )}

      {/* 链路脚注: 说清这个数字在哪生效、不影响什么 */}
      <p className="mb-0 mt-2 border-t border-border/60 pt-2 text-[10px] leading-relaxed text-secondary">
        {paper && paper.active_plans > 0
          ? (
            <>
              当前账户有 <span className="text-foreground">{paper.active_plans}</span> 个进行中的交易计划
              {paper.last_adj != null && (
                <>，最近一次生效系数 <span className="font-mono tabular-nums text-foreground">×{numText(paper.last_adj)}</span>
                  {paper.last_adj_reason ? `（${paper.last_adj_reason}）` : ''}</>
              )}
              {paper.timing_scale != null && (
                <>，建仓时冻结的择时系数 <span className="font-mono tabular-nums text-foreground">×{numText(paper.timing_scale)}</span></>
              )}
              。该系数在建仓时乘到买入股数上；卖出（止损/止盈/超时）永不受它影响。
            </>
          )
          : (
            <>
              这个系数在模拟盘交易计划建仓时自动乘到买入股数上；卖出（止损/止盈/超时）永不受它影响。
              {paper?.note ? ` ${paper.note}` : ' 当前账户没有进行中的交易计划，该建议将在你新建交易计划时生效。'}
            </>
          )}
      </p>
    </div>
  )
}

export function MarketTimingCard() {
  const qc = useQueryClient()
  const timingQuery = useQuery({
    queryKey: QK.riskTiming(),
    queryFn: () => api.riskTiming(),
    // 日K 一天只更新一次; 5 分钟轮询足够, 1 分钟内视为新鲜。
    refetchInterval: 300_000,
    staleTime: 60_000,
  })
  const adviceQuery = useQuery({
    queryKey: QK.riskAdvice(),
    queryFn: () => api.riskAdvice(),
    // 合成建议里含外围风险, 跟着风险雷达的 60s 节奏走。
    refetchInterval: 60_000,
    staleTime: 30_000,
  })

  const retry = () => {
    void qc.invalidateQueries({ queryKey: QK.riskTiming() })
    void qc.invalidateQueries({ queryKey: QK.riskAdvice() })
  }
  const busy = timingQuery.isFetching || adviceQuery.isFetching
  const hint = '沪深300 收盘 vs MA20/MA60 → 趋势四态 · 与情绪周期、风险雷达三个正交维度'

  if (timingQuery.isLoading && adviceQuery.isLoading) {
    return (
      <section className={shell} aria-label="大盘择时" aria-busy="true">
        <CardHeading hint={hint} />
        <div className="mt-3 grid gap-2.5" role="status" aria-label="正在加载大盘择时">
          <Skeleton w="w-40" h="h-7" />
          <Skeleton h="h-3" />
          <div className="grid gap-2" style={priceGrid}>
            {[0, 1, 2].map(i => <Skeleton key={i} h="h-10" />)}
          </div>
          <Skeleton w="w-3/4" h="h-3" />
        </div>
      </section>
    )
  }

  const timingError = timingQuery.isError
    ? (timingQuery.error instanceof Error ? timingQuery.error.message : String(timingQuery.error))
    : null

  if (timingError) {
    return (
      <section className={shell} aria-label="大盘择时">
        <CardHeading hint={hint} />
        <StateBlock
          title="大盘择时加载失败"
          detail={`${timingError}（本地日K 读取失败, 可稍后重试）`}
          onRetry={retry}
          busy={busy}
        />
      </section>
    )
  }

  const timing = timingQuery.data
  if (!timing) {
    // react-query 的 data 在首个成功响应前是 undefined (gc 回收后重挂载也会),
    // 这里显式兜底而不是靠非空断言。
    return (
      <section className={shell} aria-label="大盘择时">
        <CardHeading hint={hint} />
        <StateBlock
          title="大盘择时暂无数据"
          detail="尚未取到大盘择时数据, 可稍后重试。"
          onRetry={retry}
          busy={busy}
        />
      </section>
    )
  }

  const advice = adviceQuery.data
  const adviceError = adviceQuery.isError
    ? (adviceQuery.error instanceof Error ? adviceQuery.error.message : String(adviceQuery.error))
    : null

  return (
    <section className={shell} aria-label="大盘择时">
      <CardHeading hint={hint} />
      <TimingBody timing={timing} />

      {advice
        ? <AdviceBody advice={advice} />
        : (
          <div className="mt-3.5 border-t border-dashed border-border pt-3">
            <StateBlock
              title={adviceError ? '综合建议仓位加载失败' : '综合建议仓位暂无数据'}
              detail={adviceError
                ? `${adviceError}（上半部的大盘择时不受影响, 可稍后重试）`
                : '尚未取到合成建议, 可稍后重试。上半部的大盘择时不受影响。'}
              onRetry={retry}
              busy={busy}
            />
          </div>
        )}
    </section>
  )
}
