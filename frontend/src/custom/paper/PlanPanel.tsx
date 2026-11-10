/**
 * 模拟盘「交易计划」面板。
 *
 * 机制: 策略/手工给出买入价格区间 + 止损 + 止盈 + 仓位占比, 后端定时撮合在价格进入
 * 区间时自动买入, 触及止损/目标1(减半)/目标2(清仓)时自动卖出。前端只负责展示与录入,
 * 所有金额 (risk_amount / gain_at_t1 / gain_at_t2 / loss_at_stop) 后端一律给正数,
 * 符号由这里按语义补 (止损加 '-', 目标加 '+')。
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Gauge, LoaderCircle, Plus, RefreshCw, ShoppingCart, SlidersHorizontal, Wand2, X } from 'lucide-react'
import {
  api,
  type PaperPlan,
  type PaperPlanCfg,
  type PaperPlanCreatePayload,
  type PaperPlanEntryMode,
  type PaperPlanLabels,
  type PaperRegimeState,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPct, priceColorClass } from '@/lib/format'
import { Modal } from '@/components/Modal'
import {
  cfgSummary,
  defaultEntryMode,
  ENTRY_MODE_SHORT,
  entryModeOptions,
  isOpenStatus,
  planAllKey,
  planApi,
  planConfigKey,
  planEntryMode,
  planListKey,
  PLAN_STATUS_CLASS,
  PLAN_STATUS_LABEL,
} from './planClient'

const inputCls = 'mt-1 w-full rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-sm outline-none focus:border-accent/50'
const labelCls = 'block text-[11px] text-muted'
const accentBtn = 'flex items-center gap-1 rounded-btn bg-accent/10 px-2.5 py-1 text-xs text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40'
const ghostBtn = 'flex items-center gap-1 rounded-btn border border-border px-2.5 py-1 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40'
const errCls = 'rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger'

/** 列表筛选 → 后端 status 参数 (逗号分隔多值) */
type StatusFilter = 'open' | 'all' | 'done'
const STATUS_QUERY: Record<StatusFilter, string | undefined> = {
  open: 'pending,bought,holding',
  all: undefined,
  done: 'closed,expired,cancelled',
}
const STATUS_FILTER_LABEL: Record<StatusFilter, string> = { open: '未了结', all: '全部', done: '已结束' }

function money(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** 空输入 → null; 非法数字 → NaN (由调用方判错) */
function numOrNull(raw: string): number | null {
  const t = raw.trim()
  if (!t) return null
  return Number(t)
}

/** 持仓价 (来自账户持仓快照, 没有持仓为 null) + 浮动盈亏 */
function planPnl(plan: PaperPlan, price: number | null) {
  const qty = plan.qty_filled ?? 0
  if (plan.entry_price == null || price == null || qty <= 0) return null
  return {
    amount: (price - plan.entry_price) * qty,
    pct: plan.entry_price > 0 ? price / plan.entry_price - 1 : null,
  }
}

// ================================================================
// 大盘状态徽章: 当前 regime state + 仓位倍率 + 止损收紧系数
// ================================================================
function RegimeBadge({ state, labels, cfg }: { state: PaperRegimeState | null; labels: PaperPlanLabels; cfg: PaperPlanCfg }) {
  if (!state) {
    return (
      <span
        className="rounded-full bg-elevated px-2 py-0.5 text-[10px] text-muted"
        title="无可用的市场环境数据 — 仓位与止损按 1.0 倍执行。可在 设置→数据管道 打开 regime 计算后重试"
      >
        无大盘数据，按 1.0 倍执行
      </span>
    )
  }
  const label = labels.state_labels[state.state] ?? state.label ?? state.state
  const exposure = labels.exposure[state.state]
  const stopK = labels.stop_k[state.state]
  const parts = [
    exposure != null ? `仓位 ×${exposure}` : null,
    stopK != null ? `止损 ×${stopK}` : null,
  ].filter((v): v is string => v != null)
  return (
    <span
      className="flex items-center gap-1 rounded-full bg-accent/10 px-2 py-0.5 text-[10px] text-accent"
      title={[
        `大盘状态: ${label}${state.score != null ? ` (${state.score} 分)` : ''}${state.date ? ` · ${state.date}` : ''}`,
        cfg.regime_scale_enabled ? '仓位随大盘情绪缩放: 已开启' : '仓位随大盘情绪缩放: 已关闭',
        cfg.regime_stop_tighten_enabled ? '止损随大盘收紧: 已开启' : '止损随大盘收紧: 已关闭',
      ].join('\n')}
    >
      <Gauge className="h-3 w-3" />
      自动跟随大盘 · {label}
      {parts.length > 0 && <span className="font-mono text-[10px] opacity-80">{parts.join(' · ')}</span>}
    </span>
  )
}

// ================================================================
// 今日决策: 把「大盘择时 / 情绪周期 / 风险雷达 / 计划仓位」压成一张卡
// 全部复用已有接口 (/api/risk/timing|radar|advice + 计划列表 + 账户概览),
// 后端没给的字段在前端按明确公式推导, 公式写在各自注释里。
// ================================================================
function TodayDecision({
  acc,
  cfg,
  labels,
  state,
  openPlans,
  positionPct,
}: {
  acc: string
  cfg: PaperPlanCfg
  labels: PaperPlanLabels
  state: PaperRegimeState | null
  /** 未了结计划 (pending + bought + holding) */
  openPlans: PaperPlan[]
  /** 当前仓位 % (市值 / 总资产); 无概览时为 null */
  positionPct: number | null
}) {
  const timingQ = useQuery({ queryKey: QK.riskTiming(), queryFn: () => api.riskTiming(), staleTime: 60_000 })
  const radarQ = useQuery({ queryKey: QK.riskRadar(acc), queryFn: () => api.riskRadar(acc), staleTime: 60_000 })
  const adviceQ = useQuery({ queryKey: QK.riskAdvice(acc), queryFn: () => api.riskAdvice(acc), staleTime: 60_000 })

  const timing = timingQ.data
  const radar = radarQ.data
  const advice = adviceQ.data

  const holding = openPlans.filter(p => p.status === 'bought' || p.status === 'holding').length
  const pending = openPlans.filter(p => p.status === 'pending').length
  // 计划仓位合计: pos_pct 是后端已乘过大盘/外围系数的最终仓位, 这里只做求和
  const planPosPct = openPlans.reduce((s, p) => s + (p.pos_pct ?? 0), 0)
  const noFill = openPlans.length > 0 && holding === 0

  // 今日上限 Y%: 优先用后端综合建议 (择时 × 情绪周期 × 外围风险, 已夹在 15%~120%);
  // 缺该接口时前端推导 = timing.exposure × labels.exposure[state] × radar.exposure × 100,
  // 再用 cfg.max_total_exposure_pct (人为闸门) 夹一次。
  const regimeK = state ? (labels.exposure[state.state] ?? 1) : 1
  const gate = cfg.max_total_exposure_pct ?? 100
  const derivedCap = (timing?.exposure ?? 1) * regimeK * (radar?.exposure ?? 1) * 100
  const capPct = Math.min(advice?.suggested_exposure_pct ?? derivedCap, gate)

  // 缩量系数 K: 本轮实际乘上的择时系数 (后端回显优先, 其次 /api/risk/timing 的 exposure)
  const shrinkK = advice?.detail?.paper?.timing_scale ?? timing?.exposure ?? null
  // 单笔风险系数: 账户回撤系数 (0.20~1.00, 无净值历史为 1.0)
  const equityFactor = radar?.equity?.factor ?? null
  const indexes = (radar?.indexes ?? []).filter(i => i.chg_pct != null).slice(0, 3)
  const updatedAt = radar?.fetched_at ? radar.fetched_at.slice(11, 19) : null

  const num = (v: number | null | undefined, digits = 2) => (v == null ? '—' : v.toFixed(digits))
  const sgn = (v: number, digits = 2) => `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`

  return (
    <div className="rounded-card border border-border bg-base/40 p-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        <span className="flex items-center gap-1 text-[11px] font-medium text-secondary">
          <Gauge className="h-3 w-3 text-accent" /> 今日决策 · 按当天大盘定仓位
        </span>
        {updatedAt && <span className="text-[10px] text-muted">更新于 {updatedAt}</span>}
        {(timingQ.isError || radarQ.isError || adviceQ.isError) && (
          <span className="text-[10px] text-warning">部分数据取数失败, 已按中性格数计算</span>
        )}
        {timing?.stale && <span className="text-[10px] text-warning">择时数据滞后 {timing.stale_days ?? '?'} 天</span>}
      </div>

      <div className="mt-1.5 space-y-0.5 text-[11px] leading-relaxed text-muted">
        {/* 行2: 大盘择时 (沪深300 收盘 vs MA20) */}
        <div>
          <span className="text-secondary">大盘 {timing?.state_label ?? labels.state_labels[state?.state ?? ''] ?? '未知'}</span>
          {timing && (
            <span className="ml-1 font-mono">
              {timing.symbol} {num(timing.close)} / MA20 {num(timing.ma20)}
              <span className={cn('ml-1', (timing.pct_vs_ma20 ?? 0) >= 0 ? 'text-bull' : 'text-bear')}>
                {timing.pct_vs_ma20 == null ? '—' : sgn(timing.pct_vs_ma20)}
              </span>
            </span>
          )}
        </div>
        {/* 行3: 当前仓位 / 今日上限 (满仓=100%) */}
        <div>
          当前仓位 <span className="font-mono text-foreground">{positionPct == null ? '—' : `${num(positionPct, 1)}%`}</span>
          <span className="mx-1 text-border">/</span>
          今日上限 <span className="font-mono text-accent">{num(capPct, 1)}%</span>
          <span className="ml-1 text-[10px]">（满仓=100%）</span>
          {advice?.label && <span className="ml-1 text-[10px]">· {advice.label}</span>}
        </div>
        {/* 行4: 计划只数与计划仓位 */}
        <div>
          计划 <span className="font-mono text-foreground">{openPlans.length}</span> 只（持有 {holding} / 待买 {pending}）·
          计划仓位 <span className="font-mono text-foreground">{num(planPosPct, 1)}%</span>
        </div>
        {/* 行5: 本轮成交情况 + 缩量系数 */}
        <div>
          本轮{' '}
          <span className={noFill ? '' : 'text-secondary'}>
            {openPlans.length === 0 ? '无计划' : noFill ? '无成交（等待价格进入入场区间）' : `已买入 ${holding} 只`}
          </span>
          {shrinkK != null && <span className="ml-2 text-[10px]">按今日盘面缩量 ×{num(shrinkK)}</span>}
        </div>
        {/* 行6: 风险雷达 → 系数 → 各指数涨跌 → 自身回撤 → 单笔风险系数 */}
        <div className="flex flex-wrap items-center gap-x-1">
          <span className="text-secondary">
            风险雷达 {radar?.level_text ?? '—'}
            {radar?.score != null && `(${radar.score}分)`}
          </span>
          <span>→ 系数</span>
          <span className="font-mono text-foreground">{num(radar?.exposure)}</span>
          {indexes.length > 0 && (
            <span className="font-mono text-[10px]">
              {indexes.map(i => (
                <span key={i.code} className={cn('ml-1', (i.chg_pct ?? 0) >= 0 ? 'text-bull' : 'text-bear')}>
                  {i.name} {sgn(i.chg_pct ?? 0)}
                </span>
              ))}
            </span>
          )}
          <span className="ml-1">自身回撤</span>
          <span className="font-mono">{radar?.equity?.drawdown_pct == null ? '—' : `${num(radar.equity.drawdown_pct)}%`}</span>
          <span>→ 单笔风险 ×</span>
          <span className="font-mono text-foreground">{num(equityFactor)}</span>
        </div>
      </div>
      <div className="mt-1 text-[10px] leading-relaxed text-muted/80">
        口径: 今日上限取自 /api/risk/advice 的综合建议仓位(择时 × 情绪周期 × 外围风险, 已夹 15%~120%), 再受「计划参数 → 总仓位上限」约束;
        缩量系数取本轮下单实际应用的择时系数; 单笔风险系数取账户净值回撤系数。
      </div>
    </div>
  )
}

// ================================================================
// 计划参数弹窗
// ================================================================
// NumericKey 取 PaperPlanCfg 里值为 number 的键。两处 NonNullable 都必要:
// ① 映射值时 `PaperPlanCfg[K] extends number` 对可选字段是 `number | undefined`,
//    不加 NonNullable 会漏掉 max_symbols / max_total_exposure_pct;
// ② 索引结果会带上可选键的 undefined, 不加则 Record<NumericKey, string> 报 TS2344。
type NumericKey = NonNullable<{
  [K in keyof PaperPlanCfg]: NonNullable<PaperPlanCfg[K]> extends number ? K : never
}[keyof PaperPlanCfg]>

const NUM_FIELDS: Array<{ key: NumericKey; label: string; hint: string; step: string }> = [
  { key: 'risk_budget_pct', label: '单笔风险预算 %', hint: '单笔计划触发止损时的最大亏损占账户权益', step: '0.1' },
  { key: 'max_pos_pct', label: '单笔最大仓位 %', hint: '单只标的最多投入的权益占比', step: '1' },
  { key: 'min_pos_pct', label: '单笔最小仓位 %', hint: '仓位低于该比例则不再开仓', step: '1' },
  { key: 'atr_stop_mult', label: 'ATR 止损倍数', hint: '止损 = 买入价 − N × ATR', step: '0.1' },
  { key: 'max_stop_pct', label: '止损上限 %', hint: '止损距离超过该比例时按该比例截断', step: '0.5' },
  { key: 'entry_zone_pct', label: '买入区间幅度 %', hint: '区间 = 参考价 ±该比例', step: '0.5' },
  { key: 'target1_r', label: '目标1 (R 倍数)', hint: '目标1 = 买入价 + N × 风险距离, 到价减半', step: '0.1' },
  { key: 'target2_r', label: '目标2 (R 倍数)', hint: '目标2 = 买入价 + N × 风险距离, 到价清仓', step: '0.1' },
  { key: 'max_hold_days', label: '最长持有天数', hint: '到期未触发目标则按计划失效处理', step: '1' },
  { key: 'max_symbols', label: '最多持仓只数 (0=不限)', hint: '人为闸门, 默认 0 即不限 —— 仓位由策略与风险自然决定', step: '1' },
  { key: 'max_total_exposure_pct', label: '总仓位上限 % (100=不限)', hint: '人为闸门, 默认 100 即不限 —— 仓位由策略与风险自然决定', step: '5' },
]

// 后端缺省值兜底 (老版本 / 刚升级还没回写过 plan_config.json 时)
const DEFAULTS: Record<NumericKey, number> = {
  risk_budget_pct: 1.0,
  max_pos_pct: 20.0,
  min_pos_pct: 2.0,
  atr_stop_mult: 2.0,
  max_stop_pct: 12.0,
  entry_zone_pct: 2.0,
  target1_r: 1.5,
  target2_r: 3.0,
  max_hold_days: 20,
  max_symbols: 0,
  max_total_exposure_pct: 100.0,
}

function PlanCfgModal({
  acc,
  cfg,
  labels,
  onClose,
  onSaved,
}: {
  acc: string
  cfg: PaperPlanCfg
  labels: PaperPlanLabels
  onClose: () => void
  onSaved: () => void
}) {
  const qc = useQueryClient()
  const [err, setErr] = useState<string | null>(null)
  const [values, setValues] = useState<Record<NumericKey, string>>(() => {
    const init = {} as Record<NumericKey, string>
    for (const f of NUM_FIELDS) {
      // 老后端可能没有新增字段 (可选); 不能用 String(undefined) 显示成 "undefined"
      const raw = cfg[f.key]
      init[f.key] = raw === undefined || raw === null ? String(DEFAULTS[f.key]) : String(raw)
    }
    return init
  })
  const [scaleOn, setScaleOn] = useState(cfg.regime_scale_enabled)
  const [tightenOn, setTightenOn] = useState(cfg.regime_stop_tighten_enabled)

  const save = useMutation({
    mutationFn: () => {
      const next: PaperPlanCfg = { ...cfg, regime_scale_enabled: scaleOn, regime_stop_tighten_enabled: tightenOn }
      const writable = next as unknown as Record<string, number | boolean>
      for (const f of NUM_FIELDS) {
        const v = Number(values[f.key])
        if (!Number.isFinite(v)) throw new Error(`「${f.label}」必须是数字`)
        writable[f.key] = v
      }
      if (next.min_pos_pct > next.max_pos_pct) throw new Error('单笔最小仓位不能大于最大仓位')
      return planApi.saveConfig(acc, next)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: planConfigKey(acc) })
      void qc.invalidateQueries({ queryKey: planAllKey })
      onSaved()
    },
    onError: e => setErr((e as Error).message),
  })

  const rows = Object.entries(labels.exposure).sort((a, b) => b[1] - a[1])

  return (
    <Modal onClose={onClose} labelledBy="plan-cfg-title" panelClassName="w-[92vw] max-w-lg rounded-card border border-border bg-surface shadow-xl">
      <div className="p-5">
        <h3 id="plan-cfg-title" className="text-sm font-semibold text-foreground">计划参数</h3>
        <p className="mt-1 text-[11px] leading-relaxed text-muted">
          影响之后新生成或重新计算的计划; 已生成计划的止损/目标已冻结在计划上, 不会被改动。参数按账户独立保存。
        </p>
        <div className="mt-4 grid grid-cols-2 gap-3">
          {NUM_FIELDS.map(f => (
            <label key={f.key} className={cn(labelCls, f.key === 'max_hold_days' && 'col-span-2 sm:col-span-1')} title={f.hint}>
              {f.label}
              <input
                className={inputCls}
                type="number"
                step={f.step}
                inputMode="decimal"
                value={values[f.key]}
                disabled={save.isPending}
                onChange={e => setValues(v => ({ ...v, [f.key]: e.target.value }))}
              />
            </label>
          ))}
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted">
          最后两项是「人为闸门」，默认 <span className="text-secondary">0 只 / 100%</span> 即都不限：
          模拟盘默认由策略选出的仓位、再乘大盘系数与外围风险系数自动决定买多少，不需要你手设上限。
          只有想强制压住仓位时才改。
        </p>

        <label className="mt-4 flex cursor-pointer items-center justify-between rounded-btn border border-border bg-base px-3 py-2">
          <span className="text-xs text-secondary">
            仓位随大盘情绪缩放
            <span className="ml-1 text-[10px] text-muted">关闭后仓位不再随大盘情绪缩放</span>
          </span>
          <input type="checkbox" className="h-3.5 w-3.5" checked={scaleOn} disabled={save.isPending} onChange={e => setScaleOn(e.target.checked)} />
        </label>
        <label className="mt-2 flex cursor-pointer items-center justify-between rounded-btn border border-border bg-base px-3 py-2">
          <span className="text-xs text-secondary">
            止损随大盘收紧
            <span className="ml-1 text-[10px] text-muted">关闭后止损不再随大盘收紧</span>
          </span>
          <input type="checkbox" className="h-3.5 w-3.5" checked={tightenOn} disabled={save.isPending} onChange={e => setTightenOn(e.target.checked)} />
        </label>

        {rows.length > 0 && (
          <div className="mt-4 rounded-card border border-border bg-base/40 p-3">
            <div className="text-[11px] text-muted">大盘状态 → 应用系数 (后端口径)</div>
            <table className="mt-1.5 w-full text-[11px]">
              <thead>
                <tr className="text-left text-[10px] text-muted">
                  <th className="py-1 font-normal">状态</th>
                  <th className="py-1 text-right font-normal">仓位倍率</th>
                  <th className="py-1 text-right font-normal">止损系数</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {rows.map(([state, exposure]) => (
                  <tr key={state} className="border-t border-border/50">
                    <td className="py-1 font-sans text-secondary">{labels.state_labels[state] ?? state}</td>
                    <td className="py-1 text-right">×{exposure}</td>
                    <td className="py-1 text-right">{labels.stop_k[state] != null ? `×${labels.stop_k[state]}` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {(!scaleOn || !tightenOn) && (
              <div className="mt-2 text-[10px] text-warning">有开关已关闭 — 上表对应列不会生效。</div>
            )}
          </div>
        )}

        {err && <div className={cn('mt-2', errCls)}>{err}</div>}
        <div className="mt-4 flex gap-2">
          <button
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white disabled:opacity-50"
            disabled={save.isPending}
            onClick={() => { setErr(null); save.mutate() }}
          >
            {save.isPending ? '保存中…' : '保存参数'}
          </button>
          <button className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  )
}

// ================================================================
// 手工新建计划
// ================================================================
function NewPlanModal({ acc, onClose, onCreated }: { acc: string; onClose: () => void; onCreated: (plan: PaperPlan) => void }) {
  const [symbol, setSymbol] = useState('')
  const [low, setLow] = useState('')
  const [high, setHigh] = useState('')
  const [stop, setStop] = useState('')
  const [t1, setT1] = useState('')
  const [t2, setT2] = useState('')
  const [note, setNote] = useState('')
  const [err, setErr] = useState<string | null>(null)

  const create = useMutation({
    mutationFn: () => {
      const sym = symbol.trim().toUpperCase()
      if (!sym) throw new Error('请填写标的代码')
      const entryLow = numOrNull(low)
      const entryHigh = numOrNull(high)
      if (entryLow == null || !Number.isFinite(entryLow) || entryLow <= 0) throw new Error('买入区间下限必须是正数')
      if (entryHigh == null || !Number.isFinite(entryHigh) || entryHigh <= 0) throw new Error('买入区间上限必须是正数')
      if (entryHigh < entryLow) throw new Error('买入区间上限不能小于下限')
      const stopVal = numOrNull(stop)
      const t1Val = numOrNull(t1)
      const t2Val = numOrNull(t2)
      for (const [v, name] of [[stopVal, '止损'], [t1Val, '目标1'], [t2Val, '目标2']] as const) {
        if (v != null && (!Number.isFinite(v) || v <= 0)) throw new Error(`${name}必须是正数 (留空表示按参数自动计算)`)
      }
      const body: PaperPlanCreatePayload = {
        symbol: sym,
        entry_low: entryLow,
        entry_high: entryHigh,
        source: 'manual',
        ...(stopVal != null ? { stop: stopVal } : {}),
        ...(t1Val != null ? { target1: t1Val } : {}),
        ...(t2Val != null ? { target2: t2Val } : {}),
        ...(note.trim() ? { note: note.trim() } : {}),
      }
      return planApi.create(acc, body)
    },
    onSuccess: data => onCreated(data.plan),
    onError: e => setErr((e as Error).message),
  })

  return (
    <Modal onClose={() => { if (!create.isPending) onClose() }} closeOnBackdrop={!create.isPending} labelledBy="plan-new-title" panelClassName="w-[92vw] max-w-lg rounded-card border border-border bg-surface shadow-xl">
      <div className="p-5">
        <h3 id="plan-new-title" className="text-sm font-semibold text-foreground">新建交易计划</h3>
        <p className="mt-1 text-[11px] leading-relaxed text-muted">
          价格进入买入区间时自动买入; 止损/目标1(减半)/目标2(清仓)由后端撮合触发。留空的止损与止盈按「计划参数」自动计算。
        </p>
        <div className="mt-4 grid grid-cols-2 gap-3">
          <label className={`${labelCls} col-span-2`}>
            标的代码
            <input className={inputCls} value={symbol} placeholder="如 600000" disabled={create.isPending} onChange={e => setSymbol(e.target.value)} />
          </label>
          <label className={labelCls}>
            买入区间下限
            <input className={inputCls} type="number" step="0.01" inputMode="decimal" value={low} disabled={create.isPending} onChange={e => setLow(e.target.value)} />
          </label>
          <label className={labelCls}>
            买入区间上限
            <input className={inputCls} type="number" step="0.01" inputMode="decimal" value={high} disabled={create.isPending} onChange={e => setHigh(e.target.value)} />
          </label>
          <label className={labelCls}>
            止损价 (可选)
            <input className={inputCls} type="number" step="0.01" inputMode="decimal" value={stop} disabled={create.isPending} onChange={e => setStop(e.target.value)} />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className={labelCls}>
              目标1
              <input className={inputCls} type="number" step="0.01" inputMode="decimal" value={t1} disabled={create.isPending} onChange={e => setT1(e.target.value)} />
            </label>
            <label className={labelCls}>
              目标2
              <input className={inputCls} type="number" step="0.01" inputMode="decimal" value={t2} disabled={create.isPending} onChange={e => setT2(e.target.value)} />
            </label>
          </div>
          <label className={`${labelCls} col-span-2`}>
            备注 (可选)
            <input className={inputCls} value={note} disabled={create.isPending} onChange={e => setNote(e.target.value)} />
          </label>
        </div>
        {err && <div className={cn('mt-2', errCls)}>{err}</div>}
        <div className="mt-4 flex gap-2">
          <button
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white disabled:opacity-50"
            disabled={create.isPending}
            onClick={() => { setErr(null); create.mutate() }}
          >
            {create.isPending ? '创建中…' : '创建计划'}
          </button>
          <button className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground" onClick={onClose} disabled={create.isPending}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  )
}

// ================================================================
// 从策略生成计划
// ================================================================
function GenerateModal({ acc, onClose, onCreated }: { acc: string; onClose: () => void; onCreated: (created: number, state: PaperRegimeState | null) => void }) {
  const [strategyId, setStrategyId] = useState('')
  const [topN, setTopN] = useState('10')
  const [err, setErr] = useState<string | null>(null)
  const strategies = useQuery({
    queryKey: [...QK.screenerStrategies('all', 'all'), 'paper-plan'],
    queryFn: () => api.screenerStrategies(undefined, 'all'),
    staleTime: 60_000,
  })
  // 执行方式下拉的选项与默认值都取后端 labels; 后端没返时 planClient 内置兜底
  const cfgQ = useQuery({ queryKey: planConfigKey(acc), queryFn: () => planApi.config(acc) })
  const [entryMode, setEntryMode] = useState<PaperPlanEntryMode | ''>('')
  const modeOptions = entryModeOptions(cfgQ.data?.labels)
  const effectiveMode: PaperPlanEntryMode = entryMode || defaultEntryMode(cfgQ.data?.cfg, cfgQ.data?.labels)

  const generate = useMutation({
    mutationFn: () => {
      if (!strategyId) throw new Error('请选择策略')
      const n = Number(topN)
      if (!Number.isFinite(n) || n < 1) throw new Error('取前 N 只必须是正整数')
      return planApi.fromStrategy(acc, {
        strategy_id: strategyId,
        top_n: Math.floor(n),
        entry_mode: effectiveMode,
      })
    },
    onSuccess: data => onCreated(data.created, data.state),
    onError: e => setErr((e as Error).message),
  })

  const presets = strategies.data?.presets ?? []

  return (
    <Modal onClose={() => { if (!generate.isPending) onClose() }} closeOnBackdrop={!generate.isPending} labelledBy="plan-gen-title" panelClassName="w-[92vw] max-w-lg rounded-card border border-border bg-surface shadow-xl">
      <div className="p-5">
        <h3 id="plan-gen-title" className="text-sm font-semibold text-foreground">从策略生成计划</h3>
        <p className="mt-1 text-[11px] leading-relaxed text-muted">
          跑一次策略, 取前 N 只按最新收盘价生成买入区间/止损/止盈/仓位; 之后由后端定时撮合自动买卖, 不需要页面保持打开。
        </p>
        <label className={cn('mt-4', labelCls)}>
          策略
          <select className={inputCls} value={strategyId} disabled={generate.isPending || strategies.isPending} onChange={e => setStrategyId(e.target.value)}>
            <option value="">{strategies.isPending ? '加载中…' : '请选择策略'}</option>
            {presets.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
        {strategies.isError && <div className={cn('mt-2', errCls)}>{(strategies.error as Error).message}</div>}
        {!strategies.isPending && !strategies.isError && presets.length === 0 && (
          <div className="mt-2 text-[11px] text-muted">暂无可用策略 — 请先在「策略」页保存一个策略。</div>
        )}
        <div className="mt-3 grid grid-cols-2 gap-3">
          <label className={labelCls}>
            取前 N 只
            <input className={inputCls} type="number" min={1} step="1" inputMode="numeric" value={topN} disabled={generate.isPending} onChange={e => setTopN(e.target.value)} />
          </label>
          <label className={labelCls} title="决定计划被触发时以什么价格成交">
            买入执行方式
            <select className={inputCls} value={effectiveMode} disabled={generate.isPending} onChange={e => setEntryMode(e.target.value as PaperPlanEntryMode)}>
              {modeOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </label>
        </div>
        <div className="mt-1 text-[10px] leading-relaxed text-muted">
          区间 = 价格进入买入区间才成交（默认, 最保守）; 次开 = 次日开盘按开盘价成交, 不等区间; 市价 = 立即按当前价成交。
        </div>
        {err && <div className={cn('mt-2', errCls)}>{err}</div>}
        <div className="mt-4 flex gap-2">
          <button
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white disabled:opacity-50"
            disabled={generate.isPending || !strategyId}
            onClick={() => { setErr(null); generate.mutate() }}
          >
            {generate.isPending ? '生成中…' : '生成计划'}
          </button>
          <button className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground" onClick={onClose} disabled={generate.isPending}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  )
}

// ================================================================
// 主面板
// ================================================================
export function PlanPanel({ acc }: { acc: string }) {
  const qc = useQueryClient()
  const [filter, setFilter] = useState<StatusFilter>('open')
  const [newOpen, setNewOpen] = useState(false)
  const [genOpen, setGenOpen] = useState(false)
  const [cfgOpen, setCfgOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const statusParam = STATUS_QUERY[filter]
  const listQ = useQuery({
    queryKey: planListKey(acc, statusParam ?? ''),
    queryFn: () => planApi.list(acc, statusParam),
  })
  const cfgQ = useQuery({ queryKey: planConfigKey(acc), queryFn: () => planApi.config(acc) })
  // 现价取自账户持仓快照 (计划本身不落实时行情, 避免表格里出现过期价格)
  const overviewQ = useQuery({ queryKey: QK.paperOverview(acc), queryFn: () => api.paperOverview(acc), staleTime: 15_000 })
  // 今日决策卡要「未了结计划」全集 (不受当前筛选影响) + 账户历史胜率
  const openQ = useQuery({
    queryKey: planListKey(acc, STATUS_QUERY.open ?? ''),
    queryFn: () => planApi.list(acc, STATUS_QUERY.open),
  })
  const statsQ = useQuery({ queryKey: QK.paperStats(acc), queryFn: () => api.paperStats(acc), staleTime: 60_000 })

  const plans = listQ.data?.plans ?? []
  const openPlans = openQ.data?.plans ?? []
  const cfg = cfgQ.data?.cfg
  const labels = cfgQ.data?.labels
  const overview = overviewQ.data
  const prices = useMemo(() => {
    const map: Record<string, number> = {}
    for (const h of overviewQ.data?.holdings ?? []) map[h.symbol] = h.last_price
    return map
  }, [overviewQ.data])

  // 当前仓位 %: 持仓市值 / 总资产 (含现金), 分母异常时返回 null 而不是 NaN
  const positionPct = useMemo(() => {
    const total = overview?.total
    const mv = overview?.market_value
    if (total == null || mv == null || total <= 0) return null
    return (mv / total) * 100
  }, [overview])

  // 历史胜率: 账户已了结回合的胜率 (后端 paper.stats 口径, 不是策略历史胜率)
  const winRate = statsQ.data && statsQ.data.rounds > 0 ? statsQ.data.win_rate : null

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: planAllKey })
    void qc.invalidateQueries({ queryKey: planConfigKey(acc) })
    void qc.invalidateQueries({ queryKey: QK.paperStats(acc) })
  }

  const removeM = useMutation({
    mutationFn: (id: string) => planApi.remove(acc, id),
    onSuccess: () => { setNotice('计划已移出'); invalidate() },
    onError: e => setNotice((e as Error).message),
  })

  // 行内「买入」: 按计划自己的股数/单价直接下市价单, 成功后计划由后端撮合逻辑接管
  const buyM = useMutation({
    mutationFn: (plan: PaperPlan) => {
      const qty = plan.qty_filled && plan.qty_filled > 0 ? plan.qty_filled : plan.qty_planned
      if (!qty || qty <= 0) throw new Error(`${plan.symbol} 没有可用股数, 请先在计划参数里设置仓位`)
      return api.paperOrderCreate({ symbol: plan.symbol, side: 'buy', qty, order_type: 'market' }, acc)
    },
    onSuccess: (_d, plan) => { setNotice(`已提交 ${plan.symbol} 买入委托`); invalidate() },
    onError: e => setNotice((e as Error).message),
  })

  return (
    <section className="space-y-3 rounded-card border border-border bg-surface p-4" aria-label="交易计划">
      {/* 头部: 标题 + 大盘状态 + 配置摘要 */}
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-medium">
          <SlidersHorizontal className="h-4 w-4 text-accent" />
          交易计划
          <span className="text-[10px] font-normal text-muted">{listQ.data?.total ?? plans.length} 条</span>
        </h3>
        {cfg && labels && <RegimeBadge state={cfgQ.data?.state ?? null} labels={labels} cfg={cfg} />}
        <button className={`${ghostBtn} ml-auto`} onClick={() => { void listQ.refetch(); void cfgQ.refetch() }} disabled={listQ.isFetching}>
          <RefreshCw className={cn('h-3 w-3', listQ.isFetching && 'animate-spin')} /> 刷新
        </button>
      </div>
      {cfg && <div className="text-[11px] leading-relaxed text-muted">{cfgSummary(cfg)}</div>}
      {cfg && labels && (
        <TodayDecision
          acc={acc}
          cfg={cfg}
          labels={labels}
          state={cfgQ.data?.state ?? null}
          openPlans={openPlans}
          positionPct={positionPct}
        />
      )}
      <div className="text-[11px] leading-relaxed text-muted">
        计划会在价格进入买入区间时自动买入; 触及止损 / 目标1(卖一半) / 目标2(清仓)时自动卖出。卖出不受大盘择时影响。
      </div>

      {/* 工具栏 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-0.5 rounded-btn bg-base p-0.5">
          {(['open', 'all', 'done'] as const).map(k => (
            <button
              key={k}
              onClick={() => setFilter(k)}
              className={cn('rounded-btn px-2 py-0.5 text-[11px] transition-colors', filter === k ? 'bg-elevated text-foreground' : 'text-muted hover:text-secondary')}
            >
              {STATUS_FILTER_LABEL[k]}
            </button>
          ))}
        </div>
        <button className={accentBtn} onClick={() => setGenOpen(true)}>
          <Wand2 className="h-3 w-3" /> 从策略生成计划
        </button>
        <button className={ghostBtn} onClick={() => setNewOpen(true)}>
          <Plus className="h-3 w-3" /> 新建计划
        </button>
        <button className={ghostBtn} onClick={() => setCfgOpen(true)} disabled={!cfg}>
          <SlidersHorizontal className="h-3 w-3" /> 计划参数
        </button>
      </div>

      {notice && (
        <div className="flex items-center justify-between gap-2 rounded-btn bg-elevated/60 px-2.5 py-1.5 text-[11px] text-secondary">
          <span>{notice}</span>
          <button onClick={() => setNotice(null)} className="rounded p-0.5 text-muted hover:text-foreground" aria-label="关闭提示"><X className="h-3 w-3" /></button>
        </div>
      )}

      {listQ.isError && (
        <div className={errCls}>
          {(listQ.error as Error).message}
          <button className="ml-2 underline underline-offset-2" onClick={() => listQ.refetch()}>重试</button>
        </div>
      )}
      {cfgQ.isError && <div className={errCls}>计划参数读取失败: {(cfgQ.error as Error).message}</div>}

      {/* 计划表 */}
      {listQ.isPending ? (
        <div className="flex items-center justify-center gap-2 py-8 text-xs text-muted">
          <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> 加载中…
        </div>
      ) : plans.length === 0 ? (
        <div className="py-8 text-center text-xs leading-relaxed text-muted">
          {filter === 'open' ? '暂无未了结的计划' : filter === 'done' ? '暂无已结束的计划' : '暂无计划'}
          <br />
          点「从策略生成计划」批量建仓线, 或「新建计划」手工录入一只标的。
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1240px] text-xs">
            <thead>
              <tr className="text-left text-[10px] text-muted">
                <th className="whitespace-nowrap py-1.5 pr-2 font-normal">代码 / 名称</th>
                <th className="whitespace-nowrap px-2 py-1.5 font-normal">状态</th>
                <th className="whitespace-nowrap px-2 py-1.5 font-normal">执行</th>
                <th className="whitespace-nowrap px-2 py-1.5 text-right font-normal">买入区间</th>
                <th className="whitespace-nowrap px-2 py-1.5 text-right font-normal">现价</th>
                <th className="whitespace-nowrap px-2 py-1.5 text-right font-normal" title="止损价 / 止损幅度 / 触发止损的预期亏损金额">止损 / 预期亏</th>
                <th className="whitespace-nowrap px-2 py-1.5 text-right font-normal" title="目标1 价 / 到价减半时的预期盈利">目标1 / 预期赚</th>
                <th className="whitespace-nowrap px-2 py-1.5 text-right font-normal" title="目标2 价 / 到价清仓时的预期盈利">目标2 / 预期赚</th>
                <th className="whitespace-nowrap px-2 py-1.5 text-right font-normal">仓位</th>
                <th className="whitespace-nowrap px-2 py-1.5 text-right font-normal">计划/已成交</th>
                <th className="whitespace-nowrap px-2 py-1.5 text-right font-normal">成本</th>
                <th className="whitespace-nowrap px-2 py-1.5 text-right font-normal">盈亏</th>
                <th className="whitespace-nowrap px-2 py-1.5 text-right font-normal" title="目标1 盈利 / 止损亏损">盈亏比</th>
                <th className="whitespace-nowrap px-2 py-1.5 text-right font-normal" title="账户已了结回合的历史胜率 (本次计划尚未成交时不代表该计划胜率)">历史胜率</th>
                <th className="whitespace-nowrap px-2 py-1.5 font-normal">来源</th>
                <th className="whitespace-nowrap px-2 py-1.5" />
              </tr>
            </thead>
            <tbody className="font-mono">
              {plans.map(plan => {
                const price = prices[plan.symbol] ?? null
                // 浮动盈亏只在已建仓且仍有持仓时成立; 终态计划不拿持仓价反推
                const pnl = isOpenStatus(plan.status) ? planPnl(plan, price) : null
                const removed = plan.status === 'closed' || plan.status === 'cancelled' || plan.status === 'expired'
                const mode = planEntryMode(plan)
                const canBuy = plan.status === 'pending'
                return (
                  <tr key={plan.id} className="border-t border-border/50 align-top transition-colors hover:bg-elevated/40">
                    <td className="whitespace-nowrap py-1.5 pr-2 font-sans">
                      <div className="flex items-center gap-1.5">
                        <span>{plan.symbol}</span>
                        {plan.name && <span className="text-[10px] text-muted">{plan.name}</span>}
                      </div>
                      {plan.last_error && (
                        <div
                          className="max-w-[13rem] truncate font-sans text-[10px] text-warning"
                          title={`${plan.last_error}（这是撮合过程中的状态说明, 不是系统异常）`}
                        >
                          {plan.last_error}
                        </div>
                      )}
                      {plan.note && <div className="max-w-[10rem] truncate font-sans text-[10px] text-muted" title={plan.note}>{plan.note}</div>}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5">
                      <span className={cn('rounded-full px-1.5 py-px text-[10px] leading-4', PLAN_STATUS_CLASS[plan.status])}>
                        {PLAN_STATUS_LABEL[plan.status]}
                      </span>
                      {plan.t1_done && <span className="ml-1 rounded-full bg-accent/10 px-1.5 py-px text-[10px] leading-4 text-accent" title="目标1 已到价减半, 剩余仓位止损抬到保本">T1 已减半</span>}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5" title={entryModeOptions(labels).find(o => o.value === mode)?.label ?? mode}>
                      <span className={cn('rounded-full px-1.5 py-px font-sans text-[10px] leading-4', mode === 'zone' ? 'bg-elevated text-secondary' : 'bg-accent/10 text-accent')}>
                        {ENTRY_MODE_SHORT[mode]}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-right">
                      {money(plan.entry_low)} ~ {money(plan.entry_high)}
                      {plan.entry_ref != null && <div className="text-[10px] text-muted">参考 {money(plan.entry_ref)}</div>}
                    </td>
                    <td className={cn('whitespace-nowrap px-2 py-1.5 text-right', price == null && 'text-muted')}>
                      {price == null ? '—' : money(price, 3)}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-right">
                      {plan.stop != null ? <span className="text-bear">{money(plan.stop)}</span> : <span className="text-muted">自动</span>}
                      {plan.stop_pct != null && <div className="text-[10px] text-muted">-{money(Math.abs(plan.stop_pct), 2)}%</div>}
                      {plan.loss_at_stop != null && (
                        <div className="text-[10px] text-bear" title="按计划仓位与止损价计算的预期亏损金额">
                          -{money(Math.abs(plan.loss_at_stop), 0)}
                        </div>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-right">
                      {plan.target1 != null ? money(plan.target1) : <span className="text-muted">自动</span>}
                      {plan.gain_at_t1 != null && (
                        <div className="text-[10px] text-bull" title="目标1 到价减半时的预期盈利金额">
                          +{money(Math.abs(plan.gain_at_t1), 0)}
                        </div>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-right">
                      {plan.target2 != null ? money(plan.target2) : <span className="text-muted">自动</span>}
                      {plan.gain_at_t2 != null && (
                        <div className="text-[10px] text-bull" title="目标2 到价清仓时的预期盈利金额">
                          +{money(Math.abs(plan.gain_at_t2), 0)}
                        </div>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-right">
                      {plan.pos_pct != null ? `${money(plan.pos_pct, 1)}%` : '—'}
                      {plan.exposure != null && <div className="text-[10px] text-muted" title="已按大盘状态缩放后的仓位倍率">×{money(plan.exposure, 2)}</div>}
                      {plan.capped && <div className="text-[10px] text-warning" title="仓位被单笔最大仓位上限截断">已截断</div>}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-right">
                      {plan.qty_planned != null ? plan.qty_planned : '—'}
                      <span className="text-muted"> / {plan.qty_filled ?? 0}</span>
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-right">
                      {plan.entry_price != null ? money(plan.entry_price, 3) : '—'}
                      {plan.entry_date && <div className="text-[10px] text-muted">{plan.entry_date}</div>}
                    </td>
                    <td className={cn('whitespace-nowrap px-2 py-1.5 text-right', pnl ? priceColorClass(pnl.amount) : 'text-muted')}>
                      {pnl ? (
                        <>
                          {(pnl.amount >= 0 ? '+' : '-') + money(Math.abs(pnl.amount), 0)}
                          <div className="text-[10px]">{fmtPct(pnl.pct)}</div>
                        </>
                      ) : '—'}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-right" title={plan.rr != null ? `目标1 盈利 / 止损亏损 = ${money(plan.rr, 2)}` : '缺少止损或目标价, 无法计算盈亏比'}>
                      {plan.rr != null ? `${money(plan.rr, 2)} : 1` : '—'}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-right text-muted" title="账户已了结回合的胜率 (后端 /api/paper/stats 口径); 计划尚未成交时与该计划无关">
                      {winRate == null ? '—' : `${money(winRate, 1)}%`}
                    </td>
                    <td className="max-w-[8rem] truncate px-2 py-1.5 text-[10px] text-muted" title={plan.source ?? undefined}>{plan.source ?? '—'}</td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-right">
                      <div className="flex items-center justify-end gap-1">
                        {canBuy && (
                          <button
                            onClick={() => buyM.mutate(plan)}
                            disabled={buyM.isPending}
                            className="rounded p-0.5 text-muted transition-colors hover:text-accent disabled:opacity-40"
                            title="按计划的股数以市价买入 (不等区间)"
                            aria-label="买入"
                          >
                            <ShoppingCart className="h-3.5 w-3.5" />
                          </button>
                        )}
                        {!removed && (
                          <button
                            onClick={() => removeM.mutate(plan.id)}
                            disabled={removeM.isPending}
                            className="rounded p-0.5 text-muted transition-colors hover:text-danger disabled:opacity-40"
                            title={plan.status === 'pending' ? '移出计划 (取消未触发的计划)' : '移出计划 (删除计划, 不影响已成交持仓)'}
                            aria-label="移出计划"
                          >
                            <X className="h-3.5 w-3.5" />
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {newOpen && (
        <NewPlanModal
          acc={acc}
          onClose={() => setNewOpen(false)}
          onCreated={plan => { setNewOpen(false); setNotice(`已创建 ${plan.symbol} 的交易计划`); invalidate() }}
        />
      )}
      {genOpen && (
        <GenerateModal
          acc={acc}
          onClose={() => setGenOpen(false)}
          onCreated={(created, state) => {
            setGenOpen(false)
            setNotice(`已生成 ${created} 条计划${state?.label ? ` (大盘: ${state.label})` : ''}`)
            invalidate()
          }}
        />
      )}
      {cfgOpen && cfg && labels && (
        <PlanCfgModal
          acc={acc}
          cfg={cfg}
          labels={labels}
          onClose={() => setCfgOpen(false)}
          onSaved={() => { setCfgOpen(false); setNotice('计划参数已保存') }}
        />
      )}
    </section>
  )
}
