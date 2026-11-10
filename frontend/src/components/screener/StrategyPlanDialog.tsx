/**
 * 「生成交易计划」弹窗 —— 把策略结果表里勾选出来的标的直接转成模拟盘交易计划。
 *
 * 与「模拟盘 → 交易计划 → 从策略生成」的区别: 那个入口会**重跑一遍策略**并取前 N 名;
 * 这里只把用户**亲眼看过并勾选**的那几行发给后端, 不重跑策略, 行情快照也保持一致 ——
 * 也就是需求里说的「人选买或者不买」。
 *
 * **预览由后端算** (`POST /api/paper/plans/preview`, 只算不落盘): 前端不再自己重写一套
 * 计划数学 (旧实现平行算了一遍, 与后端 build_plan 必然漂移, 且失败时只能把整行画成
 * `--`, 用户完全看不出「为什么买不了」)。现在由后端逐行回 ok=false + 中文原因,
 * 界面把原因原样讲清楚。
 *
 * 真正落盘仍走 `POST /api/paper/plans/from_rows`, 数值以后端重算结果为准。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ClipboardList, LoaderCircle, X } from 'lucide-react'
import { api, type PaperPlanEntryMode, type PaperPlanLabels, type PaperPlanPreview } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import {
  defaultEntryMode,
  entryModeOptions,
  pickPlanScale,
  pickPlanSummary,
  planAllKey,
  planApi,
  planConfigKey,
} from '@/custom/paper/planClient'

/** 后端一次最多接受 100 行 (api/paper.py 的 MAX_PLAN_ROWS) */
const MAX_ROWS = 100

const EMPTY_LABELS: PaperPlanLabels = { state_labels: {}, exposure: {}, stop_k: {} }

function money(v: number | null | undefined, digits = 0): string {
  if (v == null || !Number.isFinite(Number(v))) return '--'
  return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** 数值格: 有值才显示, 没有就显式写「—」而不是空白 */
function cell(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  return Number(v).toFixed(digits)
}

interface Props {
  open: boolean
  onClose: () => void
  /** 结果表当前可见的行 (与表格同一份数据) */
  rows: any[]
  /** 结果表里已勾选的标的; 空集合时默认全选 */
  selected?: Set<string>
}

const thCls = 'px-2 py-2 font-medium whitespace-nowrap text-[11px]'
const tdCls = 'px-2 py-1.5 whitespace-nowrap text-[11px]'

export function StrategyPlanDialog({ open, onClose, rows, selected }: Props) {
  const qc = useQueryClient()
  const [account, setAccount] = useState('')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [skipMsg, setSkipMsg] = useState<string[]>([])
  // 持仓数上限: 空串 = 跟随账户 cfg.max_symbols (0 时文案显示「不限制」)
  const [limitRaw, setLimitRaw] = useState('')
  const [entryMode, setEntryMode] = useState<PaperPlanEntryMode | ''>('')

  // ── 账户列表: 打开时取一次, 默认选第一个可用账户 ───────────
  const accountsQ = useQuery({
    queryKey: QK.paperAccounts,
    queryFn: () => api.paperAccounts(),
    enabled: open,
  })
  const accounts = accountsQ.data?.accounts ?? []

  useEffect(() => {
    if (!open) return
    if (account && accounts.some(a => a.id === account)) return
    const first = accounts.find(a => a.status !== 'frozen') ?? accounts[0]
    if (first && first.id !== account) setAccount(first.id)
  }, [open, account, accounts])

  // ── 勾选状态: 每次打开按「结果表的选择」初始化 ─────────────
  useEffect(() => {
    if (!open) return
    const avail = new Set(rows.map((r: any) => String(r?.symbol ?? '')))
    const init = selected && selected.size > 0
      ? [...selected].filter(s => avail.has(s))
      : [...avail]
    setPicked(new Set(init))
    setSkipMsg([])
    setEntryMode('')
  }, [open, rows, selected])

  const cfgQ = useQuery({
    queryKey: planConfigKey(account || '-'),
    queryFn: () => planApi.config(account),
    enabled: open && !!account,
  })

  const cfg = cfgQ.data?.cfg
  const labels = cfgQ.data?.labels ?? EMPTY_LABELS
  const state = cfgQ.data?.state ?? null
  // **大盘择时** (不是情绪周期!): 后端 plan_config.timing 给的是 risk_on/weak/risk_off 口径
  const timing = cfgQ.data?.timing ?? null
  // 后端算好的「优中选优」只数缩放 (max_symbols → 本期最多几只)
  const serverHoldings = cfgQ.data?.holdings ?? null

  // 未填时默认采用账户配置的持仓数上限 (cfg.max_symbols, 0 = 不限制);
  // 账户切换或配置刷新时重新同步, 用户手填后不会被覆盖 (cfg 引用变化才会触发)。
  useEffect(() => {
    if (!open || !cfg) return
    const n = Number(cfg.max_symbols ?? 0)
    setLimitRaw(n > 0 ? String(Math.min(n, 20)) : '')
  }, [open, cfg])

  // ── 结果行 → 后端预览用的行 (只挑后端认识的字段) ────────────
  const scoreOf = useCallback((symbol: string): number => {
    const row: any = rows.find((r: any) => String(r?.symbol ?? '') === symbol)
    const v = row?.score ?? row?.total_score ?? row?.rank_score ?? 0
    const n = Number(v)
    return Number.isFinite(n) ? n : 0
  }, [rows])

  const previewRows = useMemo(
    () => rows.map((r: any) => ({
      symbol: r.symbol,
      name: r.name ?? null,
      close: r.close ?? r.price ?? r.raw_close ?? null,
      atr_14: r.atr_14 ?? r.atr ?? null,
      low: r.low ?? r.raw_low ?? null,
      score: scoreOf(String(r?.symbol ?? '')) || null,
    })),
    [rows, scoreOf],
  )

  // ── 优中选优: 只数上限 + 大盘择时 × 情绪周期 ───────────────
  // 上限留空 = 跟随账户 plan_config 的 max_symbols (后端 0 表示不限);
  // 用户填了就以其为准 (1~20), 提交时作为 top_n 发给后端, 由后端按 score 降序截断。
  const cfgLimit = Number(cfg?.max_symbols ?? 0)
  const typedLimit = limitRaw.trim() === '' ? null : Number(limitRaw)
  const scale = useMemo(() => {
    const base = pickPlanScale({
      cfg, labels, state,
      // 大盘择时系数用后端 trend 口径 (risk_on/weak/risk_off), 不是情绪周期倍率表
      timingExposure: timing?.exposure ?? null,
      phaseExposure: serverHoldings?.phase_exposure ?? null,
      limit: typedLimit,
      candidates: rows.length,
      rankKey: 'score',
    })
    // 后端给的只数上限 (基于 cfg.max_symbols, 已按择时×情绪缩过) 与用户填的上限
    // 取更小的那个 —— 只缩不放, 免得前端放 20 只、后端按 5 只上限拒单。
    const serverLimit = serverHoldings?.limit ?? null
    const holdingsLimit = serverLimit != null
      ? Math.min(base.holdingsLimit, Math.max(1, serverLimit))
      : base.holdingsLimit
    return {
      ...base,
      holdingsLimit,
      fromServer: base.fromServer || serverLimit != null || timing?.exposure != null,
    }
  }, [cfg, labels, state, timing?.exposure, serverHoldings?.limit, serverHoldings?.phase_exposure, typedLimit, rows.length])
  const unlimited = !(typedLimit ?? cfgLimit) || (typedLimit ?? cfgLimit) <= 0
  const limitInvalid = typedLimit != null && (!Number.isFinite(typedLimit) || typedLimit < 1 || typedLimit > 20)
  const mode = entryMode || defaultEntryMode(cfg, labels)
  const modeOptions = entryModeOptions(labels)

  // ── 后端预览 (只算不落盘) ──────────────────────────────────
  const previewQ = useQuery({
    queryKey: ['screener', 'plan-preview', account, mode, typedLimit, previewRows],
    queryFn: () => planApi.preview(account, {
      rows: previewRows,
      top_n: scale.holdingsLimit,
      rank_key: scale.rankKey,
      entry_mode: mode,
      source: 'screener',
    }),
    enabled: open && !!account && previewRows.length > 0,
    // 行内容变了才重算, 别因为引用变化反复打后端
    staleTime: 30_000,
  })

  const previews: PaperPlanPreview[] = previewQ.data?.previews ?? []
  const equity = Number(previewQ.data?.equity ?? 0)
  const ready = previews.filter(p => p.ok)
  const chosen = ready.filter(p => picked.has(p.symbol))
  const blocked = previews.filter(p => !p.ok)

  // 预览按上限截断: 与后端 top_n 后的结果保持一致 (按 score 降序取前 M 只)
  const rankedChosen = useMemo(() => {
    if (unlimited) return chosen
    const cap = scale.holdingsLimit
    if (chosen.length <= cap) return chosen
    const scored = [...chosen].sort((a, b) => scoreOf(b.symbol) - scoreOf(a.symbol))
    const keep = new Set(scored.slice(0, cap).map(p => p.symbol))
    return chosen.filter(p => keep.has(p.symbol))
  }, [chosen, scale.holdingsLimit, unlimited])

  // 预计占用: 只统计按上限截断后真正会建计划的标的
  const needCash = useMemo(
    () => rankedChosen.reduce((sum, p) => sum + (p.qty_planned ?? 0) * (p.entry_ref ?? 0), 0),
    [rankedChosen],
  )

  const toggle = (symbol: string) => {
    setPicked(prev => {
      const next = new Set(prev)
      if (next.has(symbol)) next.delete(symbol)
      else next.add(symbol)
      return next
    })
  }

  const allReadyPicked = ready.length > 0 && ready.every(p => picked.has(p.symbol))
  const toggleAll = () => {
    setPicked(allReadyPicked ? new Set() : new Set(ready.map(p => p.symbol)))
  }

  const createMut = useMutation({
    mutationFn: () => {
      const keep = new Set(rankedChosen.map(p => p.symbol))
      const rowsToSend = previewRows.filter(r => keep.has(String(r?.symbol ?? '')))
      return planApi.fromRows(account, {
        rows: rowsToSend,
        top_n: scale.holdingsLimit,
        rank_key: scale.rankKey,
        entry_mode: mode,
        source: 'screener',
      })
    },
    onSuccess: data => {
      const created = Number(data?.created ?? 0)
      const skipped = data?.skipped ?? []
      qc.invalidateQueries({ queryKey: planAllKey })
      if (created > 0) {
        toast(
          `已生成 ${created} 条交易计划${skipped.length ? `（${skipped.length} 只跳过）` : ''}，可在 模拟盘 → 交易计划 查看`,
          'success',
        )
        onClose()
        return
      }
      setSkipMsg(skipped.map(s => `${s.symbol}: ${s.reason}`))
      toast(skipped[0]?.reason || '没有生成任何计划', 'error')
    },
    onError: (e: any) => toast(e?.message || '生成交易计划失败', 'error'),
  })

  if (!open) return null

  const stateLabel = state ? (labels.state_labels[state.state] ?? state.label ?? state.state) : '无大盘情绪数据'
  // 择时口径: 大盘趋势 (risk_on/weak/risk_off) 优先, 拿不到才回落情绪周期倍率
  const timingLabel = timing?.label ?? (timing?.state ? timing.state : null)
  const timingExposure = Number(timing?.exposure ?? 0) || null
  const phaseExposure = scale.phaseExposure
  const loading = accountsQ.isLoading || cfgQ.isLoading || previewQ.isLoading
  const previewError = previewQ.isError ? (previewQ.error as any)?.message || '预览失败' : null

  return (
    <Modal
      onClose={onClose}
      labelledBy="strategy-plan-title"
      overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm"
      panelClassName="w-[1080px] max-w-[96vw] max-h-[90vh] bg-surface/95 backdrop-blur-xl border border-border/50 rounded-2xl shadow-2xl flex flex-col overflow-hidden"
    >
      {/* 标题 */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border/50">
        <div className="flex items-center gap-2">
          <ClipboardList className="h-4 w-4 text-accent" />
          <span id="strategy-plan-title" className="text-sm font-semibold text-foreground">生成交易计划</span>
          <span className="text-[11px] text-muted">
            勾选的标的进入模拟盘交易计划: 价格进区间自动买入, 触止损/目标自动卖出
          </span>
        </div>
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-elevated text-muted hover:text-foreground transition-colors cursor-pointer"
          title="关闭"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* 工具条: 账户 + 权益 + 大盘口径 */}
      <div className="flex flex-wrap items-center gap-3 px-4 py-2.5 border-b border-border/50 bg-elevated/30">
        <label className="flex items-center gap-1.5 text-[11px] text-muted">
          目标账户
          <select
            value={account}
            onChange={e => setAccount(e.target.value)}
            disabled={accountsQ.isLoading}
            className="h-7 rounded-btn border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent/50"
          >
            {accounts.length === 0 && <option value="">无可用账户</option>}
            {accounts.map(a => (
              <option key={a.id} value={a.id}>
                {a.name || a.id}{a.status === 'frozen' ? '（已停止）' : ''}
              </option>
            ))}
          </select>
        </label>
        <span className="text-[11px] text-muted">
          权益 <span className="font-mono text-secondary">{money(equity, 2)}</span>
          {equity > 0 && equity < 1000 && (
            <span className="ml-1 text-warning">（资金偏少，可能买不了一手）</span>
          )}
        </span>
        {/* 大盘择时 (trend) 与 情绪周期 (phase) 是两套口径, 分开显示, 不要混 */}
        <span className="inline-flex items-center gap-1 rounded-full bg-warning/10 px-2 py-0.5 text-[11px] text-warning"
          title="大盘择时: 沪深300 相对 MA20/MA60 的趋势状态, 只缩放买入">
          大盘择时 {timingLabel ?? '数据缺失'} · 仓位 ×{timingExposure ?? '—'}
        </span>
        <span className="inline-flex items-center gap-1 rounded-full bg-bull/10 px-2 py-0.5 text-[11px] text-bull"
          title="情绪周期: 市场情绪阶段对应的仓位倍率">
          情绪周期 {state ? stateLabel : '无数据'} · 仓位 ×{phaseExposure}
        </span>
        <span className="ml-auto text-[11px] text-muted">
          已选 <span className="font-mono text-accent">{chosen.length}</span> 只
          {!unlimited && chosen.length > rankedChosen.length && (
            <span className="text-warning"> → 按上限保留 {rankedChosen.length} 只</span>
          )}
          {' '}· 预计占用 <span className="font-mono text-secondary">{money(needCash)}</span>
        </span>
      </div>

      {/* 优中选优: 持仓数上限 + 执行方式 */}
      <div className="flex flex-wrap items-center gap-3 border-b border-border/50 bg-elevated/20 px-4 py-2">
        <label className="flex items-center gap-1.5 text-[11px] text-muted">
          持仓数上限
          <input
            type="number"
            min={1}
            max={20}
            step={1}
            inputMode="numeric"
            value={limitRaw}
            placeholder={unlimited ? '不限制' : String(cfgLimit)}
            onChange={e => setLimitRaw(e.target.value)}
            className="h-7 w-16 rounded-btn border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent/50"
          />
          <span className="text-[10px] text-muted">
            {unlimited ? '（当前不限制，可填 1~20）' : `（默认 ${cfgLimit} 只）`}
          </span>
        </label>
        <label className="flex items-center gap-1.5 text-[11px] text-muted">
          执行方式
          <select
            value={mode}
            onChange={e => setEntryMode(e.target.value as PaperPlanEntryMode)}
            className="h-7 rounded-btn border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent/50"
          >
            {modeOptions.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </label>
        {limitInvalid && <span className="text-[10px] text-warning">持仓数上限需在 1~20 之间</span>}
      </div>
      {/* 「优中选优」口径说明: A/B/M 取后端 plan_config 的 state, 缺失时前端推导 */}
      <div className="border-b border-border/50 bg-accent/5 px-4 py-1.5 text-[11px] leading-relaxed text-accent/90">
        {pickPlanSummary(scale, unlimited)}
        <span className="ml-1 text-muted">
          （{scale.fromServer ? '系数由后端给出' : '后端未返回缩放系数，按 plan_config.labels 与大盘状态推导'}）
        </span>
        {serverHoldings?.reason && (
          <span className="ml-1 text-muted">· {serverHoldings.reason}</span>
        )}
      </div>

      {/* 预览表 */}
      <div className="flex-1 overflow-auto">
        {previewError ? (
          <div className="flex flex-col items-center gap-2 py-16 text-xs text-warning">
            <AlertTriangle className="h-5 w-5" />
            <span>预览失败: {previewError}</span>
          </div>
        ) : loading ? (
          <div className="flex items-center justify-center gap-2 py-16 text-xs text-muted">
            <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
            正在计算交易计划…
          </div>
        ) : previews.length === 0 ? (
          <div className="py-16 text-center text-xs text-muted">结果表里没有可转成计划的标的</div>
        ) : (
          <table className="w-full text-left">
            <thead className="sticky top-0 z-10 bg-elevated text-secondary">
              <tr>
                <th className={cn(thCls, 'pl-4')}>
                  <input
                    type="checkbox"
                    aria-label="全选"
                    checked={allReadyPicked}
                    disabled={ready.length === 0}
                    onChange={toggleAll}
                    className="h-3.5 w-3.5 accent-accent cursor-pointer"
                  />
                </th>
                <th className={thCls}>标的</th>
                <th className={cn(thCls, 'text-right')}>现价</th>
                <th className={cn(thCls, 'text-right')}>买入区间</th>
                <th className={cn(thCls, 'text-right')}>止损</th>
                <th className={cn(thCls, 'text-right')}>目标1 (减半)</th>
                <th className={cn(thCls, 'text-right')}>目标2 (清仓)</th>
                <th className={cn(thCls, 'text-right')}>仓位</th>
                <th className={cn(thCls, 'text-right')}>计划股数</th>
                <th className={cn(thCls, 'text-right')}>止损亏损</th>
                <th className={cn(thCls, 'text-right')}>目标1 盈利</th>
                <th className={cn(thCls, 'text-right')}>目标2 盈利</th>
                <th className={cn(thCls, 'text-right')}>盈亏比</th>
                <th className={thCls}>备注 / 不可生成原因</th>
              </tr>
            </thead>
            <tbody>
              {previews.map(p => (
                <tr
                  key={p.symbol}
                  className={cn(
                    'border-t border-border/60 transition-colors',
                    p.ok ? 'hover:bg-elevated/50' : 'bg-warning/[0.04]',
                    picked.has(p.symbol) && p.ok ? 'bg-accent/5' : '',
                  )}
                >
                  <td className={cn(tdCls, 'pl-4')}>
                    <input
                      type="checkbox"
                      aria-label={`选择 ${p.symbol}`}
                      checked={picked.has(p.symbol)}
                      disabled={!p.ok}
                      title={p.ok ? '' : p.reason}
                      onChange={() => toggle(p.symbol)}
                      className="h-3.5 w-3.5 accent-accent cursor-pointer disabled:cursor-not-allowed"
                    />
                  </td>
                  <td className={tdCls}>
                    <span className="font-mono text-secondary">{p.symbol}</span>
                    {p.name && <span className="ml-1.5 text-muted">{p.name}</span>}
                  </td>
                  <td className={cn(tdCls, 'text-right font-mono')}>{cell(p.entry_ref)}</td>
                  <td className={cn(tdCls, 'text-right font-mono text-secondary')}>
                    {p.entry_low != null && p.entry_high != null
                      ? `${p.entry_low.toFixed(2)} ~ ${p.entry_high.toFixed(2)}`
                      : '—'}
                  </td>
                  <td className={cn(tdCls, 'text-right font-mono text-bear')}>
                    {p.stop != null
                      ? `${p.stop.toFixed(2)}${p.stop_pct != null ? ` (-${p.stop_pct.toFixed(2)}%)` : ''}`
                      : '—'}
                  </td>
                  <td className={cn(tdCls, 'text-right font-mono text-bull')}>{cell(p.target1)}</td>
                  <td className={cn(tdCls, 'text-right font-mono text-bull')}>{cell(p.target2)}</td>
                  <td className={cn(tdCls, 'text-right font-mono')}>{p.pos_pct != null ? `${p.pos_pct.toFixed(2)}%` : '—'}</td>
                  <td className={cn(tdCls, 'text-right font-mono')}>{p.qty_planned ?? '—'}</td>
                  <td className={cn(tdCls, 'text-right font-mono text-bear')}>
                    {p.risk_amount != null ? `-${money(p.risk_amount)}` : '—'}
                  </td>
                  <td className={cn(tdCls, 'text-right font-mono text-bull')}>
                    {p.gain_at_t1 != null ? `+${money(p.gain_at_t1)}` : '—'}
                  </td>
                  <td className={cn(tdCls, 'text-right font-mono text-bull')}>
                    {p.gain_at_t2 != null ? `+${money(p.gain_at_t2)}` : '—'}
                  </td>
                  <td className={cn(tdCls, 'text-right font-mono text-secondary')}>
                    {p.rr != null ? `${p.rr}R` : '—'}
                  </td>
                  <td className={cn(tdCls, 'text-muted', 'whitespace-normal', 'max-w-[280px]')}>
                    {p.ok
                      ? (p.note || '可生成计划')
                      : <span className="text-warning">{p.reason}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* 不可生成的原因汇总: 让用户一眼看到「为什么全是灰的」 */}
      {blocked.length > 0 && (
        <div className="flex items-start gap-1.5 border-t border-border/50 bg-warning/5 px-4 py-2 text-[11px] text-warning">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <div className="space-y-0.5">
            <div>
              {blocked.length} 只无法生成计划（共 {previews.length} 只）。常见原因:
              账户资金不足以买一手、缺价格/ATR 数据、止损距离无效。提高仓位上限、给账户加资金或换低价标的即可。
            </div>
            {blocked.slice(0, 6).map(p => (
              <div key={p.symbol} className="font-mono">{p.symbol} {p.name ? `(${p.name})` : ''}: {p.reason}</div>
            ))}
            {blocked.length > 6 && <div className="text-muted">…还有 {blocked.length - 6} 只</div>}
          </div>
        </div>
      )}

      {/* 跳过明细 */}
      {skipMsg.length > 0 && (
        <div className="flex items-start gap-1.5 border-t border-border/50 bg-warning/5 px-4 py-2 text-[11px] text-warning">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <div className="space-y-0.5">
            <div>以下标的没有建成计划（同标的有活跃计划时会跳过，避免重复建仓）：</div>
            {skipMsg.map(line => <div key={line} className="font-mono">{line}</div>)}
          </div>
        </div>
      )}

      {/* 底部 */}
      <div className="flex items-center justify-between gap-2 px-4 py-3 border-t border-border/50">
        <p className="text-[11px] text-muted leading-relaxed">
          计划生成后不会立即买入：价格进入买入区间才成交；止损/目标1(卖一半)/目标2(清仓)自动卖出，
          <span className="text-secondary">卖出不受大盘择时影响</span>。仓位与止损已按当前大盘状态缩放。
        </p>
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={onClose}
            className="px-3 py-1.5 rounded-btn text-xs text-secondary border border-border hover:text-foreground transition-colors cursor-pointer"
          >
            取消
          </button>
          <button
            onClick={() => createMut.mutate()}
            disabled={createMut.isPending || !account || rankedChosen.length === 0 || rankedChosen.length > MAX_ROWS || limitInvalid}
            className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-btn text-xs font-medium text-white bg-accent hover:bg-accent/90 transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {createMut.isPending ? <LoaderCircle className="h-3 w-3 animate-spin" /> : <ClipboardList className="h-3 w-3" />}
            {createMut.isPending ? '生成中…' : `生成 ${rankedChosen.length} 条计划`}
          </button>
        </div>
      </div>
    </Modal>
  )
}
