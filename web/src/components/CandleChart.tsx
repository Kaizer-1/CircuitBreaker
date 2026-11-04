// The candlestick chart (TradingView Lightweight Charts). Its colours live in JavaScript, not CSS, so
// on every theme change it re-reads the theme's tokens and re-applies the layout and series options
// (paper: hollow up-candles in green ink; terminal: filled candles). CandleChart.test.tsx checks it.
import {
  CandlestickSeries,
  ColorType,
  createChart,
  HistogramSeries,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useRef } from 'react'
import type { Candle } from '../lib/market'
import { cssToken, type Theme, useTheme } from '../theme/theme'

const IST_SECONDS = 19_800 // the chart draws UTC; shifting by +5:30 shows India time on the axis
const RUPEE_FORMAT = new Intl.NumberFormat('en-IN', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export function chartOptions() {
  return {
    layout: {
      background: { type: ColorType.Solid, color: cssToken('--chart-bg') },
      textColor: cssToken('--chart-text'),
      fontFamily: cssToken('--font-num'),
      fontSize: 15, // the price axis is read from the back of the room
      attributionLogo: false, // attribution is a text link on each page instead (the licence allows it)
    },
    grid: {
      vertLines: { visible: false },
      horzLines: { color: cssToken('--chart-grid') },
    },
  }
}

export function candleOptions() {
  return {
    upColor: cssToken('--candle-up-fill'),
    downColor: cssToken('--candle-down-fill'),
    borderUpColor: cssToken('--candle-up'),
    borderDownColor: cssToken('--candle-down'),
    wickUpColor: cssToken('--candle-up'),
    wickDownColor: cssToken('--candle-down'),
    borderVisible: true,
  }
}

const toTime = (c: Candle) => (Date.parse(c.bucket_start) / 1000 + IST_SECONDS) as UTCTimestamp

export function CandleChart({ candles, theme: forced }: { candles: Candle[]; theme?: Theme }) {
  const shown = useTheme()
  const theme = forced ?? shown
  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const series = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volume = useRef<ISeriesApi<'Histogram'> | null>(null)
  const drawn = useRef<{ first: string | null; count: number }>({ first: null, count: 0 })

  useEffect(() => {
    if (!box.current) return
    const c = createChart(box.current, {
      autoSize: true,
      ...chartOptions(),
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.08, bottom: 0.24 } },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: true, rightOffset: 2 },
      localization: { priceFormatter: (p: number) => RUPEE_FORMAT.format(p) },
      crosshair: { horzLine: { labelVisible: true }, vertLine: { labelVisible: false } },
      handleScale: false,
      handleScroll: false,
    })
    series.current = c.addSeries(CandlestickSeries, {
      ...candleOptions(),
      priceFormat: { type: 'price', precision: 2, minMove: 0.05 },
    })
    volume.current = c.addSeries(HistogramSeries, {
      priceScaleId: 'volume',
      priceFormat: { type: 'volume' },
      color: cssToken('--chart-volume'),
      lastValueVisible: false,
      priceLineVisible: false,
    })
    c.priceScale('volume').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
    chart.current = c
    return () => {
      c.remove()
      chart.current = series.current = volume.current = null
      drawn.current = { first: null, count: 0 }
    }
  }, [])

  // The theme changed: the CSS tokens did already; the chart's JavaScript colours follow.
  useEffect(() => {
    chart.current?.applyOptions(chartOptions())
    series.current?.applyOptions(candleOptions())
    volume.current?.applyOptions({ color: cssToken('--chart-volume') })
  }, [theme])

  useEffect(() => {
    const s = series.current
    const v = volume.current
    if (!s || !v || candles.length === 0) return
    const first = candles[0].bucket_start
    const bars = candles.map((c) => ({
      time: toTime(c),
      open: c.open_paise / 100,
      high: c.high_paise / 100,
      low: c.low_paise / 100,
      close: c.close_paise / 100,
    }))
    const vols = candles.map((c) => ({ time: toTime(c), value: c.volume }))
    const { first: was, count } = drawn.current
    if (was === first && (candles.length === count || candles.length === count + 1)) {
      s.update(bars[bars.length - 1]) // the forming candle moved, or a new one opened
      v.update(vols[vols.length - 1])
    } else {
      s.setData(bars)
      v.setData(vols)
    }
    drawn.current = { first, count: candles.length }
  }, [candles])

  return <div className="chart" ref={box} data-testid="candle-chart" />
}
