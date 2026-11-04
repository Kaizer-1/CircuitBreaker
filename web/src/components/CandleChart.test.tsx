// Lightweight Charts takes its colours in JavaScript, so a theme change must re-apply them. It also
// decides when the candles are spread to fit and which price-axis labels are drawn.
import { act, render } from '@testing-library/react'
import { chooseTheme } from '../theme/theme'
import { BOTH, showTheme } from '../../test/themes'
import { CandleChart, needsFit, priceTickLabels } from './CandleChart'

const calls = vi.hoisted(() => ({
  chart: [] as unknown[],
  series: [] as unknown[],
  data: [] as unknown[],
  scales: [] as [string, unknown][],
  time: [] as string[],
  width: 600,
}))
vi.mock('lightweight-charts', () => {
  const series = () => ({
    applyOptions: (o: unknown) => calls.series.push(o),
    setData: (d: unknown) => calls.data.push(d),
    update: vi.fn(),
    priceToCoordinate: (p: number) => p,
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
        priceScale: (id: string) => ({ applyOptions: (o: unknown) => calls.scales.push([id, o]) }),
        timeScale: () => ({
          width: () => calls.width,
          fitContent: () => calls.time.push('fit'),
          scrollToRealTime: () => calls.time.push('real-time'),
          applyOptions: (o: { barSpacing: number }) => calls.time.push(`spacing ${o.barSpacing}`),
        }),
        remove: vi.fn(),
      }
    },
  }
})

const candle = (n: number) => ({
  bucket_start: new Date(Date.parse('2026-11-20T10:42:00Z') + n * 5000).toISOString(),
  open_paise: 24500,
  high_paise: 24600,
  low_paise: 24450,
  close_paise: 24550,
  volume: 10,
  trade_count: 2,
})
const candles = [candle(0)]
const many = (n: number) => Array.from({ length: n }, (_, i) => candle(i))
type Opts = {
  upColor?: string
  borderUpColor?: string
  priceScaleId?: string
  lastValueVisible?: boolean
  layout?: { background: { color: string } }
  rightPriceScale?: { entireTextOnly: boolean; scaleMargins: { top: number; bottom: number } }
}
const lastSeries = () => [...calls.series].reverse().find((o) => (o as Opts).upColor) as Opts
const lastChart = () => [...calls.chart].reverse().find((o) => (o as Opts).layout) as Opts

describe('the candlestick chart', () => {
  beforeEach(() => {
    calls.chart.length = calls.series.length = calls.data.length = 0
    calls.scales.length = calls.time.length = 0
    calls.width = 600
  })

  it.each(BOTH)('draws in the %s colours', (theme) => {
    showTheme(theme)
    render(<CandleChart candles={candles} />)
    const s = lastSeries()
    if (theme === 'paper') {
      expect(s.upColor).toBe('#f7e1cf') // hollow: the body is paper
      expect(s.borderUpColor).toBe('#0a6a45')
      expect(lastChart().layout?.background.color).toBe('#f7e1cf')
      expect((s as { priceLineColor?: string }).priceLineColor).toBe('#1b1611') // an ink tag
    } else {
      expect(s.upColor).toBe('#45d07a') // filled
      expect((s as { priceLineColor?: string }).priceLineColor).toBe('') // the candle's colour
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

  it('puts volume on its own unlabelled overlay at the bottom, clear of the candles', () => {
    showTheme('terminal')
    render(<CandleChart candles={candles} />)
    const vol = calls.series.find((o) => (o as Opts).priceScaleId === 'volume') as Opts
    expect(vol.lastValueVisible).toBe(false)
    const [, volMargins] = calls.scales.find(([id]) => id === 'volume')!
    const price = (calls.chart[0] as Opts).rightPriceScale!
    expect(price.entireTextOnly).toBe(true) // no half-clipped label at the top or bottom edge
    const top = (volMargins as { scaleMargins: { top: number } }).scaleMargins.top
    expect(1 - price.scaleMargins.bottom).toBeLessThanOrEqual(top) // candles end above the volume
  })

  it('spreads a few candles across the width, then rolls once they fill it', () => {
    showTheme('paper')
    const { rerender } = render(<CandleChart candles={many(10)} />)
    expect(calls.time).toEqual(['fit']) // 10 × 6 px < 600 px: no empty left side
    calls.time.length = 0
    rerender(<CandleChart candles={many(150)} />) // 150 × 6 px > 600 px
    expect(calls.time).toEqual(['spacing 6', 'real-time'])
    calls.time.length = 0
    rerender(<CandleChart candles={many(151)} />)
    expect(calls.time).toEqual([]) // already rolling: the library scrolls on its own
  })
})

describe('fitting and price-axis labels', () => {
  it('fits only while the candles take less than the width', () => {
    expect(needsFit(10, 600)).toBe(true)
    expect(needsFit(100, 600)).toBe(false)
    expect(needsFit(0, 0)).toBe(false)
  })

  it('leaves blank the labels the last-price tag would cover and those beside the volume', () => {
    const toY = (p: number) => (250 - p) * 40 // 1 rupee = 40 px, higher price = higher up
    const labels = priceTickLabels([249, 248, 247, 246, 245, 244, 243], {
      last: 247.3, // the tag's centre is 108 px down: 247 is 12 px away, 248 is 28 px
      lowest: 244.5, // candles end here; below are the volume bars
      toY,
    })
    expect(labels).toEqual(['249.00', '248.00', '', '246.00', '245.00', '', ''])
  })

  it('labels every tick before there is any data', () => {
    expect(priceTickLabels([1, 2], { last: null, lowest: null, toY: () => null })).toEqual([
      '1.00',
      '2.00',
    ])
  })
})
