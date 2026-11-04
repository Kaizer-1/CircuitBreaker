// Lightweight Charts takes its colours in JavaScript, so a theme change must re-apply them.
import { act, render } from '@testing-library/react'
import { chooseTheme } from '../theme/theme'
import { BOTH, showTheme } from '../../test/themes'
import { CandleChart } from './CandleChart'

const calls = vi.hoisted(() => ({
  chart: [] as unknown[],
  series: [] as unknown[],
  data: [] as unknown[],
}))
vi.mock('lightweight-charts', () => {
  const series = () => ({
    applyOptions: (o: unknown) => calls.series.push(o),
    setData: (d: unknown) => calls.data.push(d),
    update: vi.fn(),
    priceScale: () => ({ applyOptions: vi.fn() }),
  })
  return {
    ColorType: { Solid: 'solid' },
    CandlestickSeries: 'candles',
    HistogramSeries: 'volume',
    createChart: (_el: unknown, options: unknown) => {
      calls.chart.push(options)
      return {
        addSeries: (_kind: string, options: unknown) => {
          calls.series.push(options)
          return series()
        },
        applyOptions: (o: unknown) => calls.chart.push(o),
        priceScale: () => ({ applyOptions: vi.fn() }),
        remove: vi.fn(),
      }
    },
  }
})

const candles = [
  {
    bucket_start: '2026-11-20T10:42:00Z',
    open_paise: 24500,
    high_paise: 24600,
    low_paise: 24450,
    close_paise: 24550,
    volume: 10,
    trade_count: 2,
  },
]
type Opts = { upColor?: string; borderUpColor?: string; layout?: { background: { color: string } } }
const lastSeries = () => [...calls.series].reverse().find((o) => (o as Opts).upColor) as Opts
const lastChart = () => [...calls.chart].reverse().find((o) => (o as Opts).layout) as Opts

describe('the candlestick chart', () => {
  beforeEach(() => {
    calls.chart.length = calls.series.length = calls.data.length = 0
  })

  it.each(BOTH)('draws in the %s colours', (theme) => {
    showTheme(theme)
    render(<CandleChart candles={candles} />)
    const s = lastSeries()
    if (theme === 'paper') {
      expect(s.upColor).toBe('#f7e1cf') // hollow: the body is paper
      expect(s.borderUpColor).toBe('#0a6a45')
      expect(lastChart().layout?.background.color).toBe('#f7e1cf')
    } else {
      expect(s.upColor).toBe('#45d07a') // filled
      expect(lastChart().layout?.background.color).toBe('#111315')
    }
    expect(calls.data[0]).toEqual([
      {
        time: Date.parse('2026-11-20T10:42:00Z') / 1000 + 19_800,
        open: 245,
        high: 246,
        low: 244.5,
        close: 245.5,
      },
    ])
  })

  it('re-applies the layout and series colours when the theme changes', () => {
    showTheme('paper')
    render(<CandleChart candles={candles} />)
    expect(lastSeries().upColor).toBe('#f7e1cf')
    act(() => chooseTheme('terminal'))
    expect(lastSeries().upColor).toBe('#45d07a')
    expect(lastChart().layout?.background.color).toBe('#111315')
    act(() => chooseTheme('paper'))
    expect(lastSeries().borderUpColor).toBe('#0a6a45')
    expect(lastChart().layout?.background.color).toBe('#f7e1cf')
  })
})
