// The candlestick chart (TradingView Lightweight Charts). Its colours live in JavaScript, not CSS, so
// on every theme change it re-reads the theme's tokens and re-applies the layout and series options
// (paper: hollow up-candles in green ink; terminal: filled candles). CandleChart.test.tsx checks it.
//
// The price axis keeps its labels apart: volume sits on its own overlay scale in the bottom band
// (no labels), candles keep clear of that band and of the top edge, an edge label is drawn only
// when it fits whole, and a tick label is left blank where the last-price tag would half cover it
// or where it would sit beside the volume bars.
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
const FONT_SIZE = 15 // the price axis is read from the back of the room
const BAR_SPACING = 6 // px per candle in the rolling window (the library's default)
const PRICE_MARGINS = { top: 0.1, bottom: 0.3 } // candles stay above the volume band
const VOLUME_MARGINS = { top: 0.78, bottom: 0 } // volume: the bottom 22% of the chart
const TAG_CLEARANCE = 20 // px from the last-price tag's centre in which a tick label is hidden
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
      fontSize: FONT_SIZE,
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
    // The last-price tag and line: ink in paper; unset in terminal, so the last candle's colour.
    priceLineColor: cssToken('--chart-tag'),
  }
}

/** True while every candle fits the chart at the normal spacing: then the candles are spread
 * across the whole width instead of bunching at the right with an empty left side. */
export function needsFit(candles: number, widthPx: number): boolean {
  return candles * BAR_SPACING < widthPx
}

/** The price axis's tick labels: blank below the lowest candle (beside the volume bars) and next
 * to the last-price tag (it would half cover them). */
export function priceTickLabels(
  prices: number[],
  {
    last,
    lowest,
    toY,
  }: { last: number | null; lowest: number | null; toY: (price: number) => number | null },
): string[] {
  const tagY = last === null ? null : toY(last)
  return prices.map((price) => {
    if (lowest !== null && price < lowest) return ''
    const y = toY(price)
    if (tagY !== null && y !== null && Math.abs(y - tagY) < TAG_CLEARANCE) return ''
    return RUPEE_FORMAT.format(price)
  })
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
  const fitted = useRef(false)
  const range = useRef<{ last: number | null; lowest: number | null }>({ last: null, lowest: null })

  useEffect(() => {
    if (!box.current) return
    const c = createChart(box.current, {
      autoSize: true,
      ...chartOptions(),
      rightPriceScale: { borderVisible: false, entireTextOnly: true, scaleMargins: PRICE_MARGINS },
      timeScale: {
        borderVisible: false,
        timeVisible: true,
        secondsVisible: true,
        rightOffset: 2,
        barSpacing: BAR_SPACING,
      },
      localization: {
        priceFormatter: (p: number) => RUPEE_FORMAT.format(p),
        tickmarksPriceFormatter: (prices: number[]) =>
          priceTickLabels(prices, {
            ...range.current,
            toY: (p) => series.current?.priceToCoordinate(p) ?? null,
          }),
      },
      crosshair: { horzLine: { labelVisible: true }, vertLine: { labelVisible: false } },
      handleScale: false,
      handleScroll: false,
    })
    series.current = c.addSeries(CandlestickSeries, {
      ...candleOptions(),
      priceFormat: { type: 'price', precision: 2, minMove: 0.05 },
    })
    volume.current = c.addSeries(HistogramSeries, {
      priceScaleId: 'volume', // an overlay scale: drawn without an axis or labels
      priceFormat: { type: 'volume' },
      color: cssToken('--chart-volume'),
      lastValueVisible: false,
      priceLineVisible: false,
    })
    c.priceScale('volume').applyOptions({ scaleMargins: VOLUME_MARGINS })
    chart.current = c
    return () => {
      c.remove()
      chart.current = series.current = volume.current = null
      drawn.current = { first: null, count: 0 }
      fitted.current = false
    }
  }, [])

  // The theme changed: the CSS tokens did already; the chart's JavaScript colours follow.
  useEffect(() => {
    chart.current?.applyOptions(chartOptions())
    series.current?.applyOptions(candleOptions())
    volume.current?.applyOptions({ color: cssToken('--chart-volume') })
  }, [theme])

  useEffect(() => {
    const c = chart.current
    const s = series.current
    const v = volume.current
    if (!c || !s || !v || candles.length === 0) return
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

    const time = c.timeScale()
    const fit = needsFit(candles.length, time.width() || box.current?.clientWidth || 0)
    if (fit) time.fitContent()
    else if (fitted.current) {
      time.applyOptions({ barSpacing: BAR_SPACING }) // enough candles now: the rolling window
      time.scrollToRealTime()
    }
    fitted.current = fit

    range.current = {
      last: bars[bars.length - 1].close,
      lowest: Math.min(...bars.map((b) => b.low)),
    }
    // Re-setting the margins makes the axis rebuild its tick labels around the new last price.
    c.priceScale('right').applyOptions({ scaleMargins: PRICE_MARGINS })
  }, [candles])

  return <div className="chart" ref={box} data-testid="candle-chart" />
}
