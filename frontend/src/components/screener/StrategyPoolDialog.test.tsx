// @vitest-environment jsdom
// AI 标签「草稿」分区只列 AI 来源的 research_only 草稿: 发布闸 (POST /{id}/publish)
// 仅接受 source=ai, 内置研究模板 (factor_rank_research, research_only) 混入会渲染出
// 点了必然 400「仅 AI 策略可经发布端点上线」的「发布」按钮, 属误导性 UI。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyPoolDialog } from './StrategyPoolDialog'
import type { StrategyDetail } from '@/lib/api'

const mockStrategyList = vi.fn()
const mockStrategyPublish = vi.fn(async (id: string) => ({ ok: true, strategy_id: id }))
const mockStrategyDelete = vi.fn(async (..._args: unknown[]) => ({ ok: true }))
vi.mock('@/lib/api', () => ({
  api: {
    strategyList: (...args: unknown[]) => mockStrategyList(...args),
    strategyPublish: (id: string) => mockStrategyPublish(id),
    strategyDelete: (id: string) => mockStrategyDelete(id),
  },
}))

function mkStrategy(p: Partial<StrategyDetail> & Pick<StrategyDetail, 'id' | 'name' | 'source'>): StrategyDetail {
  return {
    description: '', tags: [], research_only: false,
    execution_backend: 'polars_expr', asset_types: ['stock'], timeframes: ['1d'],
    version: '1', basic_filter: {}, params: [], params_defaults: {},
    scoring: {}, scoring_directions: {}, entry_signals: [], exit_signals: [],
    minute_exit_trigger_supported_signals: [], stop_loss: null, take_profit: null,
    trailing_stop: null, trailing_take_profit_activate: null,
    trailing_take_profit_drawdown: null, max_hold_days: null,
    order_by: '', descending: true, limit: 50,
    ...p,
  }
}

const aiDraft = mkStrategy({ id: 'ai_breakout_v1', name: 'AI突破', source: 'ai', research_only: true })
const builtinResearch = mkStrategy({ id: 'factor_rank_research', name: '因子排名研究', source: 'custom', research_only: true })
const builtinPublic = mkStrategy({ id: 'trend_follow', name: '趋势跟随', source: 'custom' })
const compositePublic = mkStrategy({ id: 'composite_mix_v1', name: '混合叠加', source: 'composite' })

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)

async function render(props: Partial<React.ComponentProps<typeof StrategyPoolDialog>> = {}) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  root = createRoot(container)
  await act(async () => {
    root!.render(
      <StrategyPoolDialog pool={[]} onConfirm={() => {}} onClose={() => {}} {...props} />,
    )
  })
}

function clickTab(label: string) {
  const tab = Array.from(container.querySelectorAll('button'))
    .find(b => b.textContent?.startsWith(label))
  expect(tab).toBeTruthy()
  act(() => { tab!.click() })
}

const clickAiTab = () => clickTab('AI')

beforeEach(() => {
  mockStrategyList.mockReset()
  mockStrategyList.mockResolvedValue({ strategies: [aiDraft, builtinResearch, builtinPublic, compositePublic] })
  mockStrategyPublish.mockClear()
  mockStrategyDelete.mockClear()
})
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})

it('AI tab lists only ai-source drafts with a publish button', async () => {
  await render()
  clickAiTab()

  expect(container.textContent).toContain('AI突破')
  const publishBtn = Array.from(container.querySelectorAll('button'))
    .find(b => b.textContent?.includes('发布'))
  expect(publishBtn).toBeTruthy()

  await act(async () => { publishBtn!.click() })
  expect(mockStrategyPublish).toHaveBeenCalledWith('ai_breakout_v1')
})

it('builtin research template is not offered for publish anywhere in the pool', async () => {
  await render()

  // 默认「全部」分组: research_only 项不进待选, 模板应完全不出现
  expect(container.textContent).not.toContain('因子排名研究')
  expect(container.textContent).toContain('趋势跟随')

  clickAiTab()
  // 草稿分区同样不列非 AI 来源的 research 项
  expect(container.textContent).not.toContain('因子排名研究')
})

it('composite strategies have their own tab and are excluded from custom tab', async () => {
  await render()

  // 「叠加」分组只列叠加策略
  clickTab('叠加')
  expect(container.textContent).toContain('混合叠加')
  expect(container.textContent).not.toContain('趋势跟随')

  // 「自定义」分组不再并入叠加策略
  clickTab('自定义')
  expect(container.textContent).toContain('趋势跟随')
  expect(container.textContent).not.toContain('混合叠加')

  // 「全部」分组仍同时可见两类
  clickTab('全部')
  expect(container.textContent).toContain('趋势跟随')
  expect(container.textContent).toContain('混合叠加')
})

const button = (label: string) => [...container.querySelectorAll('button')].find(el => el.textContent?.trim() === label)!
const choice = (name: string) => container.querySelector<HTMLInputElement>(`[aria-label="选择策略：${name}"]`)!
const selected = () => container.querySelector('[aria-label="已选策略"]')!

it('keeps library rows stable while toggling selection and saves only on explicit confirmation', async () => {
  const confirm = vi.fn()
  await render({ onConfirm: confirm })
  act(() => choice('趋势跟随').click())
  expect(choice('趋势跟随').checked).toBe(true)
  expect(selected().textContent).toContain('趋势跟随')
  expect(confirm).not.toHaveBeenCalled()
  act(() => choice('趋势跟随').click())
  expect(choice('趋势跟随').checked).toBe(false)
  expect(selected().textContent).not.toContain('趋势跟随')
})

it('searches by ID and combines source and timeframe filtering', async () => {
  mockStrategyList.mockResolvedValue({ strategies: [builtinPublic, compositePublic,
    mkStrategy({ id: 'custom_minute', name: '分钟突破', source: 'custom', timeframes: ['1m'] })] })
  await render()
  clickTab('自定义')
  const input = container.querySelector<HTMLInputElement>('[aria-label="搜索策略"]')!
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => { setter.call(input, 'custom_minute'); input.dispatchEvent(new Event('input', { bubbles: true })) })
  expect(choice('分钟突破')).toBeTruthy()
  expect(choice('趋势跟随')).toBeNull()
  const timeframe = container.querySelector<HTMLSelectElement>('[aria-label="策略周期"]')!
  act(() => { timeframe.value = '1d'; timeframe.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(choice('分钟突破')).toBeNull()
  expect(container.textContent).toContain('没有符合条件的策略')
})

it('reorders by buttons and confirms the visible order', async () => {
  const confirm = vi.fn()
  await render({ pool: ['trend_follow', 'composite_mix_v1'], onConfirm: confirm })
  const move = container.querySelector<HTMLButtonElement>('[aria-label="下移：趋势跟随"]')!
  act(() => move.click())
  expect([...selected().querySelectorAll('[data-strategy-id]')].map(el => el.getAttribute('data-strategy-id')))
    .toEqual(['composite_mix_v1', 'trend_follow'])
  act(() => button('保存策略池').click())
  expect(confirm).toHaveBeenCalledWith(['composite_mix_v1', 'trend_follow'])
})

it('can restore a cleared selection and cancel without committing draft edits', async () => {
  const confirm = vi.fn(), close = vi.fn()
  await render({ pool: ['trend_follow'], onConfirm: confirm, onClose: close })
  act(() => button('清空所选').click())
  expect(selected().textContent).not.toContain('趋势跟随')
  act(() => button('还原').click())
  expect(choice('趋势跟随').checked).toBe(true)
  act(() => choice('混合叠加').click())
  act(() => button('取消').click())
  expect(confirm).not.toHaveBeenCalled()
  expect(close).toHaveBeenCalledTimes(1)
})

it('load failure blocks saving and retry preserves the original pool', async () => {
  const confirm = vi.fn()
  mockStrategyList.mockRejectedValueOnce(new Error('连接中断'))
  await render({ pool: ['trend_follow'], onConfirm: confirm })
  expect(container.textContent).toContain('策略列表加载失败')
  expect(button('保存策略池').disabled).toBe(true)
  expect(container.textContent).not.toContain('失效策略')
  await act(async () => button('重试').click())
  expect(choice('趋势跟随').checked).toBe(true)
  expect(confirm).not.toHaveBeenCalled()
})

it('invalid selections require an explicit removal before saving', async () => {
  const confirm = vi.fn()
  await render({ pool: ['deleted_strategy'], onConfirm: confirm })
  expect(container.textContent).toContain('失效策略')
  expect(button('保存策略池').disabled).toBe(true)
  act(() => button('移除失效项').click())
  expect(button('保存策略池').disabled).toBe(false)
  act(() => button('保存策略池').click())
  expect(confirm).toHaveBeenCalledWith([])
})

it('undoes clearing even when the selected strategies have not yet been saved', async () => {
  await render()
  act(() => choice('趋势跟随').click())
  act(() => choice('混合叠加').click())
  act(() => button('清空所选').click())
  act(() => button('撤销清空').click())
  expect(choice('趋势跟随').checked).toBe(true)
  expect(choice('混合叠加').checked).toBe(true)
})

// ── 删除策略文件 (与「移出策略池」区分) ───────────────────────────
const deleteBtn = (name: string) => container.querySelector<HTMLButtonElement>(`[aria-label="删除策略：${name}"]`)!
const confirmDialog = () => document.querySelector('[aria-label="删除策略确认"]')

it('library rows expose a delete button that requires explicit confirmation', async () => {
  const confirm = vi.fn()
  await render({ pool: [], onConfirm: confirm })

  act(() => deleteBtn('趋势跟随').click())
  const dialog = confirmDialog()
  expect(dialog).toBeTruthy()
  expect(dialog!.textContent).toContain('趋势跟随')
  // 仅点开确认框不得删除
  expect(mockStrategyDelete).not.toHaveBeenCalled()

  const confirmBtn = [...dialog!.querySelectorAll('button')].find(b => b.textContent?.includes('确认删除'))!
  await act(async () => { confirmBtn.click() })
  expect(mockStrategyDelete).toHaveBeenCalledWith('trend_follow')
  // 删除的是策略文件, 与「保存策略池」无关
  expect(confirm).not.toHaveBeenCalled()
  expect(confirmDialog()).toBeNull()
})

it('cancelling the delete dialog never calls the delete API', async () => {
  await render({ pool: [], onConfirm: () => {} })
  act(() => deleteBtn('趋势跟随').click())
  const cancelBtn = [...confirmDialog()!.querySelectorAll('button')].find(b => b.textContent?.trim() === '取消')!
  act(() => { cancelBtn.click() })
  expect(mockStrategyDelete).not.toHaveBeenCalled()
  expect(confirmDialog()).toBeNull()
  // 取消删除不影响策略勾选状态
  expect(choice('趋势跟随').checked).toBe(false)
})

it('research templates cannot be deleted from the pool at all', async () => {
  await render({ pool: [], onConfirm: () => {} })
  // 研究模板不进待选列表 (既有行为), 因此也拿不到它的删除按钮
  expect(container.textContent).not.toContain('因子排名研究')
  expect(container.querySelector('[aria-label="删除策略：因子排名研究"]')).toBeNull()
})

it('removing a deleted strategy from the pool keeps it out of the saved pool', async () => {
  const confirm = vi.fn()
  await render({ pool: ['trend_follow'], onConfirm: confirm })
  act(() => deleteBtn('趋势跟随').click())
  const confirmBtn = [...confirmDialog()!.querySelectorAll('button')].find(b => b.textContent?.includes('确认删除'))!
  await act(async () => { confirmBtn.click() })
  // 删除后该 id 已从草稿池摘除
  expect(selected().textContent).not.toContain('趋势跟随')
  act(() => button('保存策略池').click())
  expect(confirm).toHaveBeenCalledWith([])
})

it('surfaces the backend reason when deletion is refused', async () => {
  mockStrategyDelete.mockRejectedValueOnce(new Error('该策略被叠加策略 [mix] 引用，请先解除引用后再删除'))
  await render({ pool: [], onConfirm: () => {} })
  act(() => deleteBtn('趋势跟随').click())
  const confirmBtn = [...confirmDialog()!.querySelectorAll('button')].find(b => b.textContent?.includes('确认删除'))!
  await act(async () => { confirmBtn.click() })
  // 失败时确认框保持打开, 并显示后端原因
  expect(confirmDialog()).toBeTruthy()
  expect(confirmDialog()!.textContent).toContain('被叠加策略')
})
