/**
 * 盘前简报卡 —— A50 期货 / 美股隔夜 / 亚太同期 → 开盘方向、缺口区间、三段概率。
 *
 * 单位口径是硬约定 (与 lib/api.ts「盘前简报」段、backend/app/services/premarket.py 同步),
 * 渲染时不做任何启发式换算:
 *   · chg_pct / gap_pct / amplitude 是**百分数数值** (-0.86 表示 -0.86%),
 *     直接 toFixed 拼 %, 不要再乘 100 —— 所以不能用 lib/format 的 fmtPct。
 *   · probs.{up,flat,down} 是 0~100 的百分数且三者相加 = 100, 可直接当宽度用。
 *   · signal 是合成信号原值 (量纲同百分数), 渲染成 S=-0.55, 不当百分比。
 *   · 离岸人民币只做提示, 后端明确不参与加权。
 * a50 / us / asia / cnh 都可能为 null, rows 在降级响应里缺失, 一律容错。
 */
import { type CSSProperties } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { CloudOff, RefreshCw, Sunrise } from 'lucide-react'
import { api, type PremarketBias, type PremarketRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'
import { Skeleton } from '@/components/data/Skeleton'

/** 偏多=红(涨) / 偏空=绿(跌) / 震荡=灰, 与全站 A 股语义色一致 */
const BIAS_COLOR: Record<PremarketBias, string> = { bull: '#f04438', flat: '#94a3b8', bear: '#12b76a' }

/** 概率堆叠条三段色: 高开红 / 平开灰 / 低开绿 */
const PROB_COLOR = { up: '#f04438', flat: '#94a3b8', down: '#12b76a' } as const
const PROB_LABEL = { up: '高开', flat: '平开', down: '低开' } as const
const PROB_KEYS = ['up', 'flat', 'down'] as const

const shell = 'min-w-0 rounded-[8px] border border-border bg-surface p-4'
const driverGrid: CSSProperties = { gridTemplateColumns: '76px minmax(0, 1fr)' }

/** 百分数数值 → "+0.24%" / "-0.86%" / "—" (不乘 100)。
 *  四舍五入后为 0 时去掉负号: 后端 gap_pct.high 常见 -0.025 → 一位小数会渲染成 "-0.0%" 这种噪音。 */
function pctText(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  const text = v.toFixed(digits)
  if (Number(text) === 0) return `${(0).toFixed(digits)}%`
  return `${v > 0 ? '+' : ''}${text}%`
}

function numText(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

function clampPct(v: number | null | undefined): number {
  if (v == null || Number.isNaN(v)) return 0
  return Math.max(0, Math.min(100, v))
}

function CardHeading({ hint, asOf }: { hint: string; asOf?: string | null }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1.5">
      <div className="flex shrink-0 items-center gap-[7px]">
        <Sunrise className="h-[15px] w-[15px] shrink-0 text-accent" aria-hidden="true" />
        <h2 className="m-0 text-[13px] font-semibold leading-normal text-foreground">盘前简报</h2>
        {asOf && (
          <span
            className="rounded-[3px] bg-elevated px-1.5 font-mono text-[10px] leading-[1.8] text-secondary"
            title="盘前简报对应的交易日"
          >
            预判日 {asOf}
          </span>
        )}
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

/** 一段驱动源的一行: 名称 + 合并涨跌幅 + 逐指数明细 + 该源自己的数据日期。 */
function DriverLine({ name, chg, rows, asOf }: {
  name: string, chg: number | null | undefined, rows?: PremarketRow[], asOf?: string | null
}) {
  return (
    <div className="grid items-baseline gap-x-2.5 gap-y-0.5 text-[11px] leading-relaxed" style={driverGrid}>
      <span className="text-secondary">{name}</span>
      <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <b className={cn('font-mono text-[12px] font-semibold', priceColorClass(chg))}>{pctText(chg)}</b>
        {asOf && (
          <span className="rounded-[3px] bg-elevated px-1 font-mono text-[10px] leading-[1.7] text-secondary" title={`${name} 数据日期`}>
            {asOf}
          </span>
        )}
        {rows && rows.length > 0 && (
          <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[10px] text-secondary">
            {rows.map(row => (
              <span key={row.code} className="whitespace-nowrap">
                {row.name}{' '}
                <em className={cn('font-mono not-italic', priceColorClass(row.chg_pct))}>{pctText(row.chg_pct)}</em>
              </span>
            ))}
          </span>
        )}
      </span>
    </div>
  )
}

export function PremarketCard() {
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: QK.riskPremarket,
    queryFn: () => api.riskPremarket(),
    // 后端有 120s 缓存, 前端轮询周期与之一致。
    refetchInterval: 120_000,
    staleTime: 60_000,
  })
  const retry = () => { void qc.invalidateQueries({ queryKey: QK.riskPremarket }) }
  const hint = 'A50 期货 65% / 美股隔夜 20% / 亚太同期 15% → 开盘方向与缺口 · 120 秒自动刷新'

  if (query.isLoading) {
    return (
      <section className={shell} aria-label="盘前简报" aria-busy="true">
        <CardHeading hint={hint} />
        <div className="mt-3 grid gap-2.5" role="status" aria-label="正在加载盘前简报">
          <Skeleton w="w-36" h="h-8" />
          <Skeleton h="h-[22px]" rounded="rounded" />
          <Skeleton w="w-2/3" h="h-3" />
          <Skeleton w="w-5/6" h="h-3" />
        </div>
      </section>
    )
  }

  if (query.isError) {
    const message = query.error instanceof Error ? query.error.message : String(query.error)
    return (
      <section className={shell} aria-label="盘前简报">
        <CardHeading hint={hint} />
        <StateBlock
          title="盘前简报加载失败"
          detail={`${message}（A50 / 外围行情接口偶发超时, 可稍后重试）`}
          onRetry={retry}
          busy={query.isFetching}
        />
      </section>
    )
  }

  const data = query.data
  if (!data) {
    // react-query 的 data 在首个成功响应前是 undefined; 显式兜底而不是非空断言。
    return (
      <section className={shell} aria-label="盘前简报">
        <CardHeading hint={hint} />
        <StateBlock
          title="盘前简报暂无数据"
          detail="尚未取到盘前简报数据, 可稍后重试。"
          onRetry={retry}
          busy={query.isFetching}
        />
      </section>
    )
  }
  if (!data.ok) {
    return (
      <section className={shell} aria-label="盘前简报">
        <CardHeading hint={hint} />
        <StateBlock
          title="盘前简报暂无数据（A50 / 外围行情获取失败）"
          detail={data.advice || undefined}
          onRetry={retry}
          busy={query.isFetching}
        />
      </section>
    )
  }

  const biasKey: PremarketBias = BIAS_COLOR[data.bias_key] ? data.bias_key : 'flat'
  const biasColor = BIAS_COLOR[biasKey]
  const cnh = data.cnh
  const cnhDir = cnh?.chg_pct == null ? null
    : cnh.chg_pct > 0.05 ? { text: '贬值', color: '#f79009' }
      : cnh.chg_pct < -0.05 ? { text: '升值', color: '#3b82f6' }
        : { text: '基本稳定', color: undefined }

  return (
    <section className={shell} aria-label="盘前简报">
      <CardHeading hint={hint} asOf={data?.as_of} />

      {/* ── 方向 + 强度 + 合成信号 ── */}
      <div className="mt-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1.5">
        <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
          <strong className="text-[28px] font-semibold leading-tight" style={{ color: biasColor }}>{data.bias || '—'}</strong>
          <span
            className="inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold leading-relaxed"
            style={{ color: biasColor, borderColor: biasColor, backgroundColor: `color-mix(in srgb, ${biasColor} 14%, transparent)` }}
            title="方向强度: 弱 / 中 / 强"
          >
            {data.strength || '—'}
          </span>
          <span className="text-[11px] text-secondary" title="合成信号原值 = A50 / 美股 / 亚太加权涨跌幅">
            S=<b className="font-mono text-[13px] font-semibold text-foreground">{data.signal > 0 ? '+' : ''}{numText(data.signal)}</b>
          </span>
        </div>
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[11px] text-secondary">
          <span>
            预计开盘缺口{' '}
            <b className="font-mono text-[13px] font-semibold text-foreground">
              {pctText(data.gap_pct?.low, 1)} ~ {pctText(data.gap_pct?.high, 1)}
            </b>
          </span>
          <span>
            预计振幅 <b className="font-mono text-[12px] font-semibold text-foreground">{numText(data.amplitude, 1)}%</b>
          </span>
        </div>
      </div>

      {/* ── 三段概率堆叠条 (0~100 的百分数, 直接当宽度) ── */}
      <div
        className="mt-3 flex h-[22px] overflow-hidden rounded bg-elevated"
        role="img"
        aria-label={`开盘概率: 高开 ${clampPct(data.probs.up).toFixed(1)}% / 平开 ${clampPct(data.probs.flat).toFixed(1)}% / 低开 ${clampPct(data.probs.down).toFixed(1)}%`}
      >
        {PROB_KEYS.map(key => {
          const value = clampPct(data.probs[key])
          if (value <= 0) return null
          return (
            <div
              key={key}
              className="flex min-w-0 shrink-0 grow-0 items-center justify-center overflow-hidden text-[10px] font-semibold leading-none text-white transition-[width] duration-200"
              style={{ width: `${value}%`, backgroundColor: PROB_COLOR[key] }}
              title={`${PROB_LABEL[key]} ${value.toFixed(1)}%`}
            >
              {value >= 9 ? `${value.toFixed(1)}%` : null}
            </div>
          )
        })}
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3.5 gap-y-1 text-[11px] text-secondary">
        {PROB_KEYS.map(key => (
          <span key={key} className="inline-flex items-center gap-1.5">
            <i className="inline-block h-2 w-2 rounded-sm" style={{ backgroundColor: PROB_COLOR[key] }} aria-hidden="true" />
            {PROB_LABEL[key]} <b className="font-mono text-[11px] font-semibold text-foreground">{clampPct(data.probs[key]).toFixed(1)}%</b>
          </span>
        ))}
      </div>

      {/* ── 驱动源明细 ── */}
      <div className="mt-3.5 grid gap-1.5 border-t border-border pt-3">
        <DriverLine name="A50 期货" chg={data.a50?.chg_pct} asOf={data.a50?.as_of} />
        <DriverLine name="美股隔夜" chg={data.us?.chg_pct} rows={data.us?.rows} asOf={data.us?.as_of} />
        <DriverLine name="亚太同期" chg={data.asia?.chg_pct} rows={data.asia?.rows} asOf={data.asia?.as_of} />
        {/* 离岸人民币: 后端明确「仅作提示, 不参与加权」, 单独一行 */}
        <div
          className="mt-0.5 grid items-baseline gap-x-2.5 gap-y-0.5 border-t border-dashed border-border/70 pt-1.5 text-[11px] leading-relaxed"
          style={driverGrid}
        >
          <span className="flex flex-wrap items-baseline gap-1 text-secondary">
            离岸人民币
          </span>
          <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <b className="font-mono text-[12px] font-semibold text-foreground">{numText(cnh?.price, 4)}</b>
            <span className={cn('font-mono text-[11px]', priceColorClass(cnh?.chg_pct))}>{pctText(cnh?.chg_pct)}</span>
            {cnhDir && <span className="text-[11px]" style={{ color: cnhDir.color }}>{cnhDir.text}</span>}
            <span className="rounded-[3px] bg-elevated px-1 text-[10px] leading-[1.7] text-secondary">仅作提示, 不参与加权</span>
            {cnh?.note && <span className="text-[10px] text-secondary [overflow-wrap:anywhere]">{cnh.note}</span>}
          </span>
        </div>
      </div>

      {/* ── 驱动理由 ── */}
      {data.drivers.length > 0 && (
        <ul className="mb-0 mt-3 list-disc space-y-0.5 pl-4">
          {data.drivers.map(driver => (
            <li key={driver} className="text-[11px] leading-relaxed text-secondary [overflow-wrap:anywhere]">{driver}</li>
          ))}
        </ul>
      )}

      {data.advice && <p className="mt-3 text-[12px] leading-relaxed text-foreground [overflow-wrap:anywhere]">{data.advice}</p>}

      <p className="mt-2.5 rounded-[6px] border border-border bg-base/45 px-2.5 py-2 text-[11px] leading-relaxed text-secondary">
        概率是把缺口按正态分布切三段算出来的, 不是黑盒模型: 缺口 ~ N(S × 0.5, σ = 0.55), |缺口| ≤ 0.20% 记为平开,
        其余按正负分成高开 / 低开。A50 缺失时权重退化为美股 55% + 亚太 45%。
      </p>
    </section>
  )
}
