// @vitest-environment jsdom
/**
 * 监控中心噪音过滤 — 单元测试
 *
 * 覆盖用户需求:「自动跟单买入下单这类提示太多, 想能关上, 只关心策略与模拟盘的实时监控」。
 * 断言默认值(默认隐藏自动跟单, 保留成交)、过滤语义(只命中 source=paper 的指定 type)、
 * 持久化(跨刷新生效) 与订阅通知。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  MONITOR_NOISE_DEFAULT,
  filterNoiseAlerts,
  getMonitorNoiseSnapshot,
  isNoiseAlert,
  readMonitorNoise,
  storage,
  subscribeMonitorNoise,
  useMonitorNoise,
  writeMonitorNoise,
  type MonitorNoisePref,
} from '@/lib/storage'

const ev = (source: string, type: string) => ({ source, type })

beforeEach(() => {
  localStorage.clear()
  // 复位模块级缓存 (快照缓存 + 监听器), 保证用例之间互不影响
  writeMonitorNoise(MONITOR_NOISE_DEFAULT)
})

describe('噪音过滤默认值', () => {
  it('默认隐藏自动跟单, 默认保留成交', () => {
    expect(MONITOR_NOISE_DEFAULT).toEqual({ hideAutoOrder: true, hideFill: false })
    expect(readMonitorNoise()).toEqual({ hideAutoOrder: true, hideFill: false })
  })

  it('useMonitorNoise 是导出的 hook (供三处调用方共用同一份偏好)', () => {
    expect(typeof useMonitorNoise).toBe('function')
  })

  it('脏数据回退默认值', () => {
    storage.monitorNoise.set({ hideAutoOrder: 'yes', hideFill: null } as unknown as MonitorNoisePref)
    expect(readMonitorNoise()).toEqual({ hideAutoOrder: true, hideFill: false })
  })
})

describe('isNoiseAlert 判定', () => {
  const pref: MonitorNoisePref = { hideAutoOrder: true, hideFill: false }

  it('默认隐藏模拟盘自动跟单', () => {
    expect(isNoiseAlert(ev('paper', 'auto_order'), pref)).toBe(true)
  })

  it('默认保留模拟盘成交', () => {
    expect(isNoiseAlert(ev('paper', 'fill'), pref)).toBe(false)
    expect(isNoiseAlert(ev('paper', 'paper_fill'), pref)).toBe(false)
  })

  it('打开成交开关后隐藏 fill 与 paper_fill', () => {
    const p: MonitorNoisePref = { hideAutoOrder: false, hideFill: true }
    expect(isNoiseAlert(ev('paper', 'fill'), p)).toBe(true)
    expect(isNoiseAlert(ev('paper', 'paper_fill'), p)).toBe(true)
    expect(isNoiseAlert(ev('paper', 'auto_order'), p)).toBe(false)
    expect(isNoiseAlert(ev('paper', 'plan_buy'), p)).toBe(false)
  })

  it('策略/信号等非模拟盘来源永不隐藏', () => {
    const p: MonitorNoisePref = { hideAutoOrder: true, hideFill: true }
    expect(isNoiseAlert(ev('strategy', 'buy_signal'), p)).toBe(false)
    expect(isNoiseAlert(ev('strategy', 'fill'), p)).toBe(false)
    expect(isNoiseAlert(ev('signal', 'price'), p)).toBe(false)
    expect(isNoiseAlert(ev('sector', 'market'), p)).toBe(false)
    expect(isNoiseAlert(ev('paper', 'plan_sell'), p)).toBe(false)
  })

  it('统一按当前偏好过滤数组', () => {
    const events = [
      ev('paper', 'auto_order'),
      ev('paper', 'fill'),
      ev('strategy', 'buy_signal'),
      ev('signal', 'price'),
    ]
    expect(filterNoiseAlerts(events, MONITOR_NOISE_DEFAULT).map(e => e.type)).toEqual(['fill', 'buy_signal', 'price'])
    expect(filterNoiseAlerts(events, { hideAutoOrder: true, hideFill: true }).map(e => e.type)).toEqual(['buy_signal', 'price'])
  })
})

describe('偏好持久化', () => {
  it('写入后能读回 (跨刷新生效)', () => {
    writeMonitorNoise({ hideAutoOrder: false, hideFill: true })
    expect(localStorage.getItem('monitor-noise')).toBeTruthy()
    expect(readMonitorNoise()).toEqual({ hideAutoOrder: false, hideFill: true })
  })

  it('快照缓存与新值保持同步', () => {
    const before = getMonitorNoiseSnapshot()
    writeMonitorNoise({ hideAutoOrder: true, hideFill: true })
    const after = getMonitorNoiseSnapshot()
    expect(after).not.toBe(before)
    expect(after).toEqual({ hideAutoOrder: true, hideFill: true })
  })

  it('订阅者在偏好变化时被通知', () => {
    const fn = vi.fn()
    const off = subscribeMonitorNoise(fn)
    writeMonitorNoise({ hideAutoOrder: false, hideFill: true })
    expect(fn).toHaveBeenCalledTimes(1)
    off()
    writeMonitorNoise({ hideAutoOrder: true, hideFill: false })
    expect(fn).toHaveBeenCalledTimes(1)
  })
})
