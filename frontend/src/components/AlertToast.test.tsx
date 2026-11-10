// @vitest-environment jsdom
// v0.3.34: 模拟盘买入提醒 toast 显示所属账户徽章 (非 paper 来源不显示)
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { AlertToastContainer, clearAlertToasts, pushAlertToasts } from './AlertToast'
import type { AlertEvent } from '@/lib/api'

vi.mock('framer-motion', async importOriginal => ({
  ...await importOriginal<typeof import('framer-motion')>(),
  // 简化动画: 直接渲染子元素, 避免 AnimatePresence 在 jsdom 下的时序问题
  motion: new Proxy({}, { get: (_t, prop) => (props: Record<string, unknown>) => {
    const { initial: _i, animate: _a, exit: _e, transition: _tr, layout: _l, ...rest } = props
    return (prop as string === 'div') ? <div {...rest} /> : null
  } }),
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))
vi.mock('@/lib/voiceBroadcast', () => ({ speakAlerts: vi.fn() }))
vi.mock('@/lib/notificationSound', () => ({ playNotificationSound: vi.fn() }))
vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: null }) }))
vi.mock('@/lib/useCustomSignalNames', () => ({ useCustomSignalNames: () => [] }))
vi.mock('@/lib/storage', () => ({
  filterNoiseAlerts: (alerts: AlertEvent[]) => alerts,
  readMonitorNoise: () => ({}),
}))

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  // AlertToast 的 _queue/_listeners 是模块级共享状态, 清空避免用例间串扰
  await act(async () => { clearAlertToasts() })
  await act(async () => root.unmount())
  host.remove()
  vi.clearAllMocks()
})

async function flush() { await act(async () => { await new Promise(r => setTimeout(r, 20)) }) }

function toastEvent(overrides: Partial<AlertEvent>): AlertEvent {
  return { ts: Date.now(), source: 'paper', type: 'fill', symbol: '600000', name: '浦发银行', message: '[主账户] 买入成交', ...overrides }
}

async function renderToast() {
  // 先挂载容器 (注册 _listeners), 再推入事件, 最后等一拍让 setItems 生效
  await act(async () => root.render(<MemoryRouter><AlertToastContainer /></MemoryRouter>))
  await flush()
}

function expectAccountBadge(expected: string | null) {
  const badge = [...host.querySelectorAll('span')]
    .find(el => (el.textContent ?? '').replace(/\s+/g, '').startsWith('账户：'))
  if (expected === null) {
    expect(badge).toBeUndefined()
  } else {
    expect(badge, '应渲染账户徽章').toBeDefined()
    expect(badge!.textContent!.replace(/\s+/g, '')).toBe(expected)
    expect(badge!.className).toContain('rounded-full')
    expect(badge!.className).toContain('bg-elevated')
  }
}

it('paper 成交事件带 account_name 时显示账户徽章', async () => {
  await renderToast()
  await act(async () => { pushAlertToasts([toastEvent({ account_name: '主账户' })]) })
  await flush()
  expectAccountBadge('账户：主账户')
})

it('paper 成交事件无 account_name 时不显示徽章', async () => {
  await renderToast()
  await act(async () => { pushAlertToasts([toastEvent({})]) })
  await flush()
  expectAccountBadge(null)
})

it('非 paper 来源即使带 account_name 也不显示徽章', async () => {
  await renderToast()
  await act(async () => { pushAlertToasts([toastEvent({ source: 'price', type: 'price_hit', account_name: '主账户' })]) })
  await flush()
  expectAccountBadge(null)
})
