/**
 * 实盘扩展客户端 — 与 `backend/app/custom/live_trader` 的端点一一对应。
 *
 * 与模拟盘的关系: 完全独立。实盘只读模拟盘的委托簿做镜像, 真实资金/持仓全部现读
 * 同花顺客户端; 这里不做任何模拟盘请求 (复用 lib/api 的 paper* 反而会混淆两套账务)。
 *
 * 错误口径与 executionClient 一致: 后端 409 的 detail 是可读中文, 直接透出。
 */
import { ApiError } from '@/lib/api'

export interface LiveConfig {
  enabled: boolean
  dry_run: boolean
  exe_path: string
  accounts: string[]
  sources: string[]
  allow_sell: boolean
  max_orders_per_day: number
  max_order_amount: number
  poll_seconds: number
}

export interface LiveBrokerStatus {
  connected: boolean
  exe_path: string
  window_title: string | null
  connected_at: string | null
  last_error: string | null
  thread_alive: boolean
}

export interface LiveStatus {
  config: LiveConfig
  broker: LiveBrokerStatus
  state: { day: string | null; day_count: number; mirrored_count: number }
  limits: { max_orders_per_day: number; max_order_amount: number; remaining_orders: number }
}

export interface LiveBalance {
  cash_available: number
  cash_balance: number
  cash_withdrawable: number
  market_value: number
  total_asset: number
  raw: Record<string, unknown>
}

export interface LivePosition {
  symbol: string
  code: string
  name: string
  qty: number
  available: number
  frozen: number
  avg_cost: number
  price: number
  market_value: number
  pnl: number
  pnl_pct: number
  day_pnl: number
  position_pct: number
  market: string
}

export interface LiveAccount {
  balance: LiveBalance
  positions: LivePosition[]
}

export interface LiveOrderLog {
  at: string
  event: string
  account?: string
  order_id?: string
  symbol?: string
  name?: string
  side?: string
  price?: number
  qty?: number
  amount?: number
  source?: string
  decision?: 'send' | 'dry_run' | 'skip'
  reason?: string
  ok?: boolean
  entrust_no?: string | null
  error?: string | null
}

const BASE = '/api/custom/live-trader'

async function request<T>(endpoint: string, method = 'GET', body?: unknown, timeoutMs = 30_000): Promise<T> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(`${BASE}/${endpoint}`, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok) {
      const detail = (payload as { detail?: unknown } | null)?.detail
      const message =
        typeof detail === 'string'
          ? detail
          : Array.isArray(detail)
            ? detail.map((v: { msg?: string }) => v?.msg ?? String(v)).join('；')
            : `实盘请求失败 (${response.status})`
      throw new ApiError(response.status === 401 ? `未登录 (401)：${message}` : message, response.status)
    }
    return payload as T
  } catch (error) {
    if (controller.signal.aborted) throw new Error('实盘请求超时, 客户端 GUI 操作可能较慢, 请稍后刷新核对')
    throw error
  } finally {
    window.clearTimeout(timer)
  }
}

/** GUI 操作类端点 (连接/读资金/下单) 放宽到 60s: easytrader 单笔 3~5 秒, 读全量持仓更久。 */
const GUI_TIMEOUT = 60_000

export const liveApi = {
  status: () => request<LiveStatus>('status'),
  config: (patch: Partial<LiveConfig>) => request<{ config: LiveConfig }>('config', 'POST', patch),
  connect: () => request<{ broker: LiveBrokerStatus }>('connect', 'POST', undefined, GUI_TIMEOUT),
  disconnect: () => request<{ broker: LiveBrokerStatus }>('disconnect', 'POST'),
  account: () => request<LiveAccount>('account', 'GET', undefined, GUI_TIMEOUT),
  orders: (limit = 100) => request<{ orders: LiveOrderLog[] }>(`orders?limit=${limit}`),
  entrusts: () => request<{ entrusts: Record<string, unknown>[] }>('entrusts', 'GET', undefined, GUI_TIMEOUT),
  trades: () => request<{ trades: Record<string, unknown>[] }>('trades', 'GET', undefined, GUI_TIMEOUT),
  mirror: (account?: string) =>
    request<{ results: LiveOrderLog[] }>(
      account ? `mirror?account=${encodeURIComponent(account)}` : 'mirror',
      'POST',
      undefined,
      GUI_TIMEOUT,
    ),
  cancelAll: () => request<{ ok: boolean }>('cancel-all', 'POST', undefined, GUI_TIMEOUT),
}

export const liveKeys = {
  status: ['custom', 'live-trader', 'status'] as const,
  account: ['custom', 'live-trader', 'account'] as const,
  orders: (limit: number) => ['custom', 'live-trader', 'orders', limit] as const,
  entrusts: ['custom', 'live-trader', 'entrusts'] as const,
}
