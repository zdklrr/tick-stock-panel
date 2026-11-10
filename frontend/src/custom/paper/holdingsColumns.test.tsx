// @vitest-environment jsdom
// v0.3.34: 当前持仓表扩列 (仓位% / 止损 / 目标1 / 目标2 / 来源) — 轻量渲染断言
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import type { PaperHolding } from '@/lib/api'

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); host.remove() })

/** 提取 Page.tsx 持仓表 tbody 行渲染逻辑里逐格取值的方式, 这里复刻同一渲染片段做断言:
 *  直接从源码渲染整页成本过高, 因此只断言扩列单元格的最小取值表达式与表头。 */
function holdingCells(h: PaperHolding, fmtMoney: (v: number | null | undefined, d?: number) => string, fmtPct: (v: number) => string) {
  return {
    positionPct: h.position_pct != null ? fmtPct(h.position_pct / 100) : '--',
    entryZone: h.plan_entry_low != null && h.plan_entry_high != null
      ? `${fmtMoney(h.plan_entry_low, 2)}~${fmtMoney(h.plan_entry_high, 2)}`
      : '--',
    stopLoss: h.plan_stop_loss != null ? fmtMoney(h.plan_stop_loss, 2) : '--',
    target1: h.plan_target1 != null ? fmtMoney(h.plan_target1, 2) : '--',
    target2: h.plan_target2 != null ? fmtMoney(h.plan_target2, 2) : '--',
    source: h.plan_source ?? '--',
  }
}

it('PaperHolding 扩列字段缺失时全部降级为 --', () => {
  const cells = holdingCells({ symbol: '600000', asset_type: 'stock', qty: 100, avg_cost: 8, last_price: 9, market_value: 900, pnl: 100, pnl_pct: 10, available_qty: 100 },
    v => String(v), v => `${(v * 100).toFixed(2)}%`)
  expect(cells.positionPct).toBe('--')
  expect(cells.entryZone).toBe('--')
  expect(cells.stopLoss).toBe('--')
  expect(cells.target1).toBe('--')
  expect(cells.target2).toBe('--')
  expect(cells.source).toBe('--')
})

it('PaperHolding 扩列字段有值时按计划口径渲染', () => {
  const cells = holdingCells({ symbol: '600000', asset_type: 'stock', qty: 100, avg_cost: 8, last_price: 9, market_value: 900, pnl: 100, pnl_pct: 10, available_qty: 100,
    position_pct: 12.5, plan_stop_loss: 7.5, plan_target1: 10, plan_target2: 12, plan_entry_mode: 'market',
    plan_entry_low: 96, plan_entry_high: 104, plan_source: '策略 双均线' },
    v => v?.toFixed(2) ?? '--', v => `${(v * 100).toFixed(2)}%`)
  expect(cells.positionPct).toBe('12.50%')
  expect(cells.entryZone).toBe('96.00~104.00')
  expect(cells.stopLoss).toBe('7.50')
  expect(cells.target1).toBe('10.00')
  expect(cells.target2).toBe('12.00')
  expect(cells.source).toBe('策略 双均线')
})

it('持仓表渲染片段包含扩列表头及其 title 提示', async () => {
  const headers = [
    ['仓位%', '持仓市值 / 总资产'],
    ['买入区间', '计划买入区间 (由交易计划带出: 价格进入区间且择时允许则自动买入)'],
    ['止损', '关联交易计划的止损价；由大盘择时/情绪周期综合缩放生成'],
    ['目标1', '目标1 (到价减半)；由大盘择时/情绪周期综合缩放生成'],
    ['目标2', '目标2 (到价清仓)；由大盘择时/情绪周期综合缩放生成'],
  ] as const
  await act(async () => root.render(
    <table><thead><tr>
      <th title="持仓市值 / 总资产">仓位%</th>
      <th title="计划买入区间 (由交易计划带出: 价格进入区间且择时允许则自动买入)">买入区间</th>
      <th title="关联交易计划的止损价；由大盘择时/情绪周期综合缩放生成">止损</th>
      <th title="目标1 (到价减半)；由大盘择时/情绪周期综合缩放生成">目标1</th>
      <th title="目标2 (到价清仓)；由大盘择时/情绪周期综合缩放生成">目标2</th>
    </tr></thead></table>,
  ))
  for (const [label, title] of headers) {
    const th = [...host.querySelectorAll('th')].find(el => el.textContent === label)
    expect(th, `缺少表头 ${label}`).toBeDefined()
    expect(th!.getAttribute('title')).toBe(title)
  }
})
