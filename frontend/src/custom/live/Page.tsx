import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Ban,
  Link2,
  Link2Off,
  Play,
  RefreshCw,
  ShieldAlert,
  Wallet,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { Modal } from '@/components/Modal'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { fmtPrice } from '@/lib/format'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { liveApi, liveKeys, type LiveConfig, type LiveOrderLog } from './liveClient'

/** 万元分级金额: 大额用「万」避免一长串数字 */
function money(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  const n = Number(v)
  if (Math.abs(n) >= 10000) return `${(n / 10000).toFixed(2)} 万`
  return n.toFixed(2)
}

function signedClass(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return 'text-muted'
  if (v > 0) return 'text-danger'
  if (v < 0) return 'text-success'
  return 'text-muted'
}

function StatCard({
  label,
  value,
  sub,
  className,
}: {
  label: string
  value: string
  sub?: string
  className?: string
}) {
  return (
    <div className={cn('rounded-lg border border-border bg-elevated/40 px-3 py-2.5', className)}>
      <div className="text-[10px] text-muted">{label}</div>
      <div className="mt-1 font-mono text-[17px] leading-6 text-foreground">{value}</div>
      {sub && <div className="mt-0.5 text-[10px] text-muted">{sub}</div>}
    </div>
  )
}

function ToggleRow({
  label,
  hint,
  checked,
  disabled,
  danger,
  onChange,
}: {
  label: string
  hint?: string
  checked: boolean
  disabled?: boolean
  danger?: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-border/60 py-2.5 last:border-0">
      <div className="min-w-0">
        <div className={cn('text-sm', danger && checked ? 'text-danger' : 'text-foreground')}>{label}</div>
        {hint && <div className="mt-0.5 text-[10px] leading-relaxed text-muted">{hint}</div>}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cn(
          'relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition-colors disabled:opacity-50',
          checked ? (danger ? 'bg-danger' : 'bg-accent') : 'bg-border',
        )}
      >
        <span
          className={cn(
            'absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all',
            checked ? 'left-[1.125rem]' : 'left-0.5',
          )}
        />
      </button>
    </div>
  )
}

export function Live() {
  const qc = useQueryClient()
  const [confirmEnable, setConfirmEnable] = useState(false)
  const [confirmCancel, setConfirmCancel] = useState(false)

  const statusQ = useQuery({ queryKey: liveKeys.status, queryFn: liveApi.status, refetchInterval: 15_000 })
  const accountQ = useQuery({
    queryKey: liveKeys.account,
    queryFn: liveApi.account,
    enabled: statusQ.data?.broker.connected === true,
    refetchInterval: 30_000,
    retry: false,
  })
  const ordersQ = useQuery({ queryKey: liveKeys.orders(100), queryFn: () => liveApi.orders(100), refetchInterval: 15_000 })
  const accountsQ = useQuery({ queryKey: QK.paperAccounts, queryFn: api.paperAccounts, retry: false })

  const cfg: LiveConfig | undefined = statusQ.data?.config
  const broker = statusQ.data?.broker
  const limits = statusQ.data?.limits
  const state = statusQ.data?.state

  const accountOptions = useMemo(() => {
    const rows = accountsQ.data?.accounts ?? []
    return rows.map(a => ({ id: a.id, label: a.name ? `${a.name} (${a.id})` : a.id }))
  }, [accountsQ.data])

  const patch = useMutation({
    mutationFn: (p: Partial<LiveConfig>) => liveApi.config(p),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: liveKeys.status })
      toast('实盘配置已保存', 'success')
    },
    onError: (e: Error) => toast(e.message),
  })

  const connect = useMutation({
    mutationFn: liveApi.connect,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: liveKeys.status })
      qc.invalidateQueries({ queryKey: liveKeys.account })
      toast('已连接同花顺客户端', 'success')
    },
    onError: (e: Error) => toast(e.message),
  })

  const disconnect = useMutation({
    mutationFn: liveApi.disconnect,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: liveKeys.status })
      qc.removeQueries({ queryKey: liveKeys.account })
    },
    onError: (e: Error) => toast(e.message),
  })

  const mirror = useMutation({
    mutationFn: () => liveApi.mirror(),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: liveKeys.orders(100) })
      qc.invalidateQueries({ queryKey: liveKeys.status })
      toast(`本次镜像 ${r.results.length} 笔`, 'success')
    },
    onError: (e: Error) => toast(e.message),
  })

  const cancelAll = useMutation({
    mutationFn: liveApi.cancelAll,
    onSuccess: () => {
      setConfirmCancel(false)
      qc.invalidateQueries({ queryKey: liveKeys.status })
      toast('已请求全部撤单', 'success')
    },
    onError: (e: Error) => toast(e.message),
  })

  const handleEnable = (next: boolean) => {
    if (next) {
      setConfirmEnable(true)
      return
    }
    patch.mutate({ enabled: false })
  }

  const orders: LiveOrderLog[] = ordersQ.data?.orders ?? []
  const account = accountQ.data
  const positions = account?.positions ?? []
  const balance = account?.balance

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="实盘交易"
        subtitle="通过 easytrader 驱动本机同花顺下单客户端 · 只发单不追价, 请务必盯盘"
        right={
          <div className="flex items-center gap-2">
            {broker?.connected ? (
              <button
                type="button"
                onClick={() => disconnect.mutate()}
                disabled={disconnect.isPending}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm text-secondary hover:bg-elevated disabled:opacity-50"
              >
                <Link2Off className="h-3.5 w-3.5" /> 断开
              </button>
            ) : (
              <button
                type="button"
                onClick={() => connect.mutate()}
                disabled={connect.isPending}
                className="inline-flex items-center gap-1.5 rounded-md border border-accent/60 bg-accent/10 px-3 py-1.5 text-sm text-accent hover:bg-accent/20 disabled:opacity-50"
              >
                <Link2 className="h-3.5 w-3.5" /> {connect.isPending ? '连接中…' : '连接同花顺'}
              </button>
            )}
            <button
              type="button"
              onClick={() => statusQ.refetch()}
              disabled={statusQ.isFetching}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm text-secondary hover:bg-elevated disabled:opacity-50"
            >
              <RefreshCw className={cn('h-3.5 w-3.5', statusQ.isFetching && 'animate-spin')} /> 刷新
            </button>
          </div>
        }
      />

      <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
        {statusQ.isError ? (
          <ErrorState
            title="实盘扩展未就绪"
            hint={(statusQ.error as Error)?.message}
            retrying={statusQ.isFetching}
            onRetry={() => statusQ.refetch()}
          />
        ) : (
          <div className="mx-auto max-w-6xl space-y-4">
            {/* 安全横幅 */}
            {cfg?.enabled && !cfg.dry_run && (
              <div className="flex items-start gap-2 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2.5">
                <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
                <div className="text-[11px] leading-relaxed text-danger">
                  <b>实盘自动下单已开启（非演练）。</b>
                  模拟盘产生的计划/自动跟单委托会被自动镜像到同花顺客户端并真实成交。
                  程序只发单，不查成交、不撤单、不追价；客户端窗口必须保持可见。
                </div>
              </div>
            )}
            {cfg?.enabled && cfg.dry_run && (
              <div className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2.5">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                <div className="text-[11px] leading-relaxed text-warning">
                  <b>演练模式。</b>镜像逻辑会完整走一遍（写入委托日志），但不会真正发单。确认无误后再关闭「演练模式」。
                </div>
              </div>
            )}

            {/* 连接状态 */}
            <section className="rounded-lg border border-border bg-elevated/20 p-3">
              <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-[11px]">
                <span className="flex items-center gap-1.5">
                  <span
                    className={cn(
                      'inline-block h-2 w-2 rounded-full',
                      broker?.connected ? 'bg-success' : 'bg-danger',
                    )}
                  />
                  <span className="text-secondary">{broker?.connected ? '客户端已连接' : '客户端未连接'}</span>
                  {broker?.window_title && <span className="text-muted">· {broker.window_title}</span>}
                </span>
                <span className="text-muted">
                  路径 <span className="font-mono text-secondary">{broker?.exe_path ?? cfg?.exe_path ?? '—'}</span>
                </span>
                {broker?.connected_at && <span className="text-muted">连接于 {broker.connected_at}</span>}
                {broker?.last_error && <span className="text-danger">上次错误：{broker.last_error}</span>}
              </div>
            </section>

            {/* 真实账户 */}
            <section>
              <div className="mb-2 flex items-center gap-2">
                <Wallet className="h-4 w-4 text-accent" />
                <h2 className="text-sm font-medium text-foreground">真实账户</h2>
                <span className="text-[10px] text-muted">现读同花顺客户端，与模拟盘账务完全独立</span>
              </div>
              {!broker?.connected ? (
                <div className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-[11px] text-muted">
                  先点右上角「连接同花顺」读取真实资金与持仓
                </div>
              ) : accountQ.isError ? (
                <ErrorState
                  title="读取真实账户失败"
                  hint={(accountQ.error as Error)?.message}
                  retrying={accountQ.isFetching}
                  onRetry={() => accountQ.refetch()}
                />
              ) : (
                <div className="space-y-2">
                  <div className="grid grid-cols-2 gap-2 lg:grid-cols-5">
                    <StatCard label="总资产" value={money(balance?.total_asset)} />
                    <StatCard label="可用资金" value={money(balance?.cash_available)} />
                    <StatCard label="股票市值" value={money(balance?.market_value)} />
                    <StatCard label="可取金额" value={money(balance?.cash_withdrawable)} />
                    <StatCard label="持仓只数" value={String(positions.filter(p => p.qty > 0).length)} />
                  </div>
                  <div className="overflow-hidden rounded-lg border border-border">
                    <table className="w-full text-[11px]">
                      <thead className="bg-elevated/60 text-muted">
                        <tr>
                          <th className="px-2 py-1.5 text-left font-normal">代码 / 名称</th>
                          <th className="px-2 py-1.5 text-right font-normal">持仓</th>
                          <th className="px-2 py-1.5 text-right font-normal">可卖</th>
                          <th className="px-2 py-1.5 text-right font-normal">成本</th>
                          <th className="px-2 py-1.5 text-right font-normal">现价</th>
                          <th className="px-2 py-1.5 text-right font-normal">市值</th>
                          <th className="px-2 py-1.5 text-right font-normal">浮动盈亏</th>
                          <th className="px-2 py-1.5 text-right font-normal">当日盈亏</th>
                          <th className="px-2 py-1.5 text-right font-normal">仓位%</th>
                        </tr>
                      </thead>
                      <tbody>
                        {positions.length === 0 && (
                          <tr>
                            <td colSpan={9} className="px-2 py-6 text-center text-muted">
                              客户端当前无持仓
                            </td>
                          </tr>
                        )}
                        {positions.map(p => (
                          <tr key={p.symbol} className="border-t border-border/60">
                            <td className="px-2 py-1.5">
                              <span className="font-mono text-foreground">{p.symbol}</span>
                              {p.name && <span className="ml-1.5 text-secondary">{p.name}</span>}
                            </td>
                            <td className="px-2 py-1.5 text-right font-mono">{p.qty}</td>
                            <td className="px-2 py-1.5 text-right font-mono">
                              <span className={p.available < p.qty ? 'text-warning' : ''}>{p.available}</span>
                            </td>
                            <td className="px-2 py-1.5 text-right font-mono text-muted">{fmtPrice(p.avg_cost, 3)}</td>
                            <td className="px-2 py-1.5 text-right font-mono text-foreground">{fmtPrice(p.price, 3)}</td>
                            <td className="px-2 py-1.5 text-right font-mono">{money(p.market_value)}</td>
                            <td className={cn('px-2 py-1.5 text-right font-mono', signedClass(p.pnl))}>
                              {money(p.pnl)}
                              <span className="ml-1 text-[10px]">({p.pnl_pct.toFixed(2)}%)</span>
                            </td>
                            <td className={cn('px-2 py-1.5 text-right font-mono', signedClass(p.day_pnl))}>
                              {money(p.day_pnl)}
                            </td>
                            <td className="px-2 py-1.5 text-right font-mono text-secondary">{p.position_pct.toFixed(2)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </section>

            {/* 配置 */}
            <section className="rounded-lg border border-border bg-elevated/20 p-3">
              <h2 className="mb-1 text-sm font-medium text-foreground">镜像配置</h2>
              <p className="mb-1 text-[10px] leading-relaxed text-muted">
                只镜像<b>计划驱动</b>与<b>自动跟单</b>两类委托；手动下单不会被镜像。数量原样透传，
                <b>不做任何择时/风险缩放</b>（缩放已在模拟盘建计划时算过）。
              </p>
              <ToggleRow
                label="启用实盘镜像"
                hint="关闭时完全不扫描委托，也不会碰客户端"
                checked={cfg?.enabled ?? false}
                disabled={patch.isPending}
                danger
                onChange={handleEnable}
              />
              <ToggleRow
                label="演练模式（dry_run）"
                hint="走完整决策链并写日志，但不真正发单。强烈建议先跑一遍演练"
                checked={cfg?.dry_run ?? true}
                disabled={patch.isPending}
                onChange={v => patch.mutate({ dry_run: v })}
              />
              <ToggleRow
                label="允许实盘卖出"
                hint="铁律: 卖出永远不受择时/风险系数影响；这里只做总开关"
                checked={cfg?.allow_sell ?? true}
                disabled={patch.isPending}
                onChange={v => patch.mutate({ allow_sell: v })}
              />

              <div className="grid gap-3 pt-3 sm:grid-cols-2 lg:grid-cols-4">
                <label className="block">
                  <span className="text-[10px] text-muted">当日发单上限（笔）</span>
                  <input
                    type="number"
                    min={0}
                    max={200}
                    value={cfg?.max_orders_per_day ?? 10}
                    onChange={e => patch.mutate({ max_orders_per_day: Number(e.target.value) })}
                    className="mt-1 w-full rounded-md border border-border bg-bg px-2 py-1.5 font-mono text-sm text-foreground"
                  />
                  <span className="mt-0.5 block text-[10px] text-muted">
                    今日已发 {state?.day_count ?? 0} / 剩余 {limits?.remaining_orders ?? '—'}
                  </span>
                </label>
                <label className="block">
                  <span className="text-[10px] text-muted">单笔金额上限（元）</span>
                  <input
                    type="number"
                    min={1}
                    step={1000}
                    value={cfg?.max_order_amount ?? 20000}
                    onChange={e => patch.mutate({ max_order_amount: Number(e.target.value) })}
                    className="mt-1 w-full rounded-md border border-border bg-bg px-2 py-1.5 font-mono text-sm text-foreground"
                  />
                </label>
                <label className="block">
                  <span className="text-[10px] text-muted">扫描间隔（秒）</span>
                  <input
                    type="number"
                    min={2}
                    max={300}
                    value={cfg?.poll_seconds ?? 5}
                    onChange={e => patch.mutate({ poll_seconds: Number(e.target.value) })}
                    className="mt-1 w-full rounded-md border border-border bg-bg px-2 py-1.5 font-mono text-sm text-foreground"
                  />
                </label>
                <label className="block">
                  <span className="text-[10px] text-muted">客户端路径</span>
                  <input
                    type="text"
                    defaultValue={cfg?.exe_path ?? ''}
                    onBlur={e => {
                      const v = e.target.value.trim()
                      if (v && v !== cfg?.exe_path) patch.mutate({ exe_path: v })
                    }}
                    className="mt-1 w-full rounded-md border border-border bg-bg px-2 py-1.5 font-mono text-[11px] text-foreground"
                  />
                </label>
              </div>

              <div className="mt-3 border-t border-border/60 pt-3">
                <div className="text-[10px] text-muted">镜像账户（不选则不镜像任何账户）</div>
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {accountOptions.length === 0 && (
                    <span className="text-[11px] text-muted">尚未创建模拟盘账户</span>
                  )}
                  {accountOptions.map(opt => {
                    const on = (cfg?.accounts ?? []).includes(opt.id)
                    return (
                      <button
                        key={opt.id}
                        type="button"
                        disabled={patch.isPending}
                        onClick={() => {
                          const cur = new Set(cfg?.accounts ?? [])
                          if (on) cur.delete(opt.id)
                          else cur.add(opt.id)
                          patch.mutate({ accounts: Array.from(cur) })
                        }}
                        className={cn(
                          'rounded-full border px-2.5 py-1 text-[11px] transition-colors disabled:opacity-50',
                          on
                            ? 'border-accent bg-accent/15 text-accent'
                            : 'border-border text-secondary hover:bg-elevated',
                        )}
                      >
                        {opt.label}
                      </button>
                    )
                  })}
                </div>
              </div>
            </section>

            {/* 委托日志 */}
            <section>
              <div className="mb-2 flex items-center justify-between gap-2">
                <h2 className="text-sm font-medium text-foreground">镜像委托记录</h2>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => mirror.mutate()}
                    disabled={mirror.isPending || !cfg?.enabled}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-[11px] text-secondary hover:bg-elevated disabled:opacity-50"
                  >
                    <Play className="h-3 w-3" /> 立即扫描一次
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmCancel(true)}
                    disabled={!broker?.connected}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-[11px] text-secondary hover:bg-elevated disabled:opacity-50"
                  >
                    <Ban className="h-3 w-3" /> 全部撤单
                  </button>
                </div>
              </div>
              {orders.length === 0 ? (
                <EmptyState
                  icon={Wallet}
                  title="暂无镜像记录"
                  hint="开启镜像后，每一笔被处理（发出/演练/被闸门拒绝）的委托都会记在这里"
                />
              ) : (
                <div className="overflow-hidden rounded-lg border border-border">
                  <table className="w-full text-[11px]">
                    <thead className="bg-elevated/60 text-muted">
                      <tr>
                        <th className="px-2 py-1.5 text-left font-normal">时间</th>
                        <th className="px-2 py-1.5 text-left font-normal">账户</th>
                        <th className="px-2 py-1.5 text-left font-normal">代码 / 名称</th>
                        <th className="px-2 py-1.5 text-left font-normal">方向</th>
                        <th className="px-2 py-1.5 text-right font-normal">价格</th>
                        <th className="px-2 py-1.5 text-right font-normal">数量</th>
                        <th className="px-2 py-1.5 text-right font-normal">金额</th>
                        <th className="px-2 py-1.5 text-left font-normal">结果</th>
                        <th className="px-2 py-1.5 text-left font-normal">说明</th>
                      </tr>
                    </thead>
                    <tbody>
                      {orders.map((o, i) => (
                        <tr key={`${o.at}-${i}`} className="border-t border-border/60">
                          <td className="whitespace-nowrap px-2 py-1.5 font-mono text-muted">
                            {o.at?.slice(5, 19).replace('T', ' ')}
                          </td>
                          <td className="px-2 py-1.5 text-muted">{o.account ?? '—'}</td>
                          <td className="px-2 py-1.5">
                            <span className="font-mono text-foreground">{o.symbol ?? '—'}</span>
                            {o.name && <span className="ml-1.5 text-secondary">{o.name}</span>}
                          </td>
                          <td className="px-2 py-1.5">
                            <span className={o.side === 'buy' ? 'text-danger' : 'text-success'}>
                              {o.side === 'buy' ? '买入' : o.side === 'sell' ? '卖出' : (o.side ?? '—')}
                            </span>
                          </td>
                          <td className="px-2 py-1.5 text-right font-mono">{o.price != null ? fmtPrice(o.price, 3) : '—'}</td>
                          <td className="px-2 py-1.5 text-right font-mono">{o.qty ?? '—'}</td>
                          <td className="px-2 py-1.5 text-right font-mono">{o.amount != null ? money(o.amount) : '—'}</td>
                          <td className="px-2 py-1.5">
                            {o.ok ? (
                              <span className="text-success">
                                {o.decision === 'dry_run' ? '演练' : o.entrust_no ? `已受理 ${o.entrust_no}` : '成功'}
                              </span>
                            ) : (
                              <span className="text-warning">{o.error ? '失败' : '跳过'}</span>
                            )}
                          </td>
                          <td className="max-w-[16rem] px-2 py-1.5 text-muted" title={o.error ?? o.reason ?? ''}>
                            <span className="line-clamp-2">{o.error ?? o.reason ?? '—'}</span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>
        )}
      </div>

      {confirmEnable && (
        <Modal onClose={() => setConfirmEnable(false)} ariaLabel="确认开启实盘镜像" panelClassName="w-[34rem]">
          <div className="p-4">
            <div className="flex items-start gap-2">
              <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-danger" />
              <div>
                <h3 className="text-sm font-medium text-foreground">确认开启实盘镜像？</h3>
                <ul className="mt-2 space-y-1 text-[11px] leading-relaxed text-secondary">
                  <li>· 开启后，被选中的模拟盘账户产生的<b>计划/自动跟单委托</b>会被自动发送到同花顺客户端。</li>
                  <li>· 当前演练模式：<b>{cfg?.dry_run ? '已开启（不会真发单）' : '已关闭（会真实成交）'}</b>。</li>
                  <li>· 程序只发单，<b>不查成交、不撤单、不追价</b>；单笔约 3~5 秒，客户端窗口必须可见。</li>
                  <li>· 闸门仍然生效：当日笔数上限 {cfg?.max_orders_per_day ?? 10}、单笔金额上限 {money(cfg?.max_order_amount ?? 20000)}。</li>
                </ul>
              </div>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmEnable(false)}
                className="rounded-md border border-border px-3 py-1.5 text-sm text-secondary hover:bg-elevated"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => {
                  setConfirmEnable(false)
                  patch.mutate({ enabled: true })
                }}
                className="rounded-md bg-danger px-3 py-1.5 text-sm text-white hover:bg-danger/90"
              >
                确认开启
              </button>
            </div>
          </div>
        </Modal>
      )}

      {confirmCancel && (
        <Modal onClose={() => setConfirmCancel(false)} ariaLabel="确认全部撤单" panelClassName="w-[30rem]">
          <div className="p-4">
            <h3 className="text-sm font-medium text-foreground">确认撤销客户端全部未成交委托？</h3>
            <p className="mt-2 text-[11px] leading-relaxed text-secondary">
              这会直接操作同花顺客户端的「全撤」按钮，撤销当前所有未成交委托（包括手动挂的单）。
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmCancel(false)}
                className="rounded-md border border-border px-3 py-1.5 text-sm text-secondary hover:bg-elevated"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => cancelAll.mutate()}
                disabled={cancelAll.isPending}
                className="rounded-md bg-danger px-3 py-1.5 text-sm text-white hover:bg-danger/90 disabled:opacity-50"
              >
                {cancelAll.isPending ? '撤单中…' : '全部撤单'}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
