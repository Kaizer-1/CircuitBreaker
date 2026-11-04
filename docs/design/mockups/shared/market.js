// Fake but realistic market data for the static design mockups (docs/design/visual-direction.md), and the
// helpers every direction uses to draw it. Seeded, so every render shows the same market.
;(function () {
  function prng(seed) {
    return function () {
      seed |= 0
      seed = (seed + 0x6d2b79f5) | 0
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296
    }
  }
  const rnd = prng(20261120)
  const tick = (p) => Math.round(p / 5) * 5

  // ₹ with Indian digit grouping (1,00,000.00), from integer paise.
  const inr = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  const qty = new Intl.NumberFormat('en-IN')
  function rupees(paise, { sign = false } = {}) {
    const s = inr.format(Math.abs(paise) / 100)
    if (!sign) return (paise < 0 ? '−' : '') + s
    return (paise > 0 ? '+' : paise < 0 ? '−' : '±') + s
  }
  function pct(x) {
    return (x > 0 ? '+' : x < 0 ? '−' : '±') + Math.abs(x).toFixed(2) + '%'
  }

  const tickers = [
    { ticker: 'CHAIWALA', name: 'Chaiwala Beverages Ltd', ref: 24550, last: 24785 },
    { ticker: 'JUGAAD', name: 'Jugaad Technologies Ltd', ref: 118000, last: 118640 },
    { ticker: 'DABBA', name: 'Dabba Logistics Ltd', ref: 41275, last: 41020 },
    { ticker: 'MONSOON', name: 'Monsoon Power Ltd', ref: 8820, last: 8855 },
    { ticker: 'FILMY', name: 'Filmy Studios Ltd', ref: 53005, last: 52815 },
  ].map((t) => ({
    ...t,
    change: t.last - t.ref,
    changePct: ((t.last - t.ref) / t.ref) * 100,
    high: tick(Math.max(t.ref, t.last) * 1.0042),
    low: tick(Math.min(t.ref, t.last) * 0.9961),
    volume: 18000 + Math.floor(rnd() * 40000),
  }))

  // 72 five-second candles ending at the hero's last price (CHAIWALA).
  function candles(last, count = 72) {
    const out = []
    let close = last
    for (let i = 0; i < count; i++) {
      const open = tick(close * (1 + (rnd() - 0.52) * 0.0024))
      const high = tick(Math.max(open, close) * (1 + rnd() * 0.0011))
      const low = tick(Math.min(open, close) * (1 - rnd() * 0.0011))
      out.unshift({ open, high, low, close, volume: Math.floor(40 + rnd() * 420) })
      close = open
    }
    return out
  }

  const hero = tickers[0]
  const book = {
    bids: Array.from({ length: 10 }, (_, i) => ({
      price: 24780 - i * 5 - (i > 3 ? 5 : 0),
      qty: 20 + Math.floor(rnd() * 180),
      orders: 1 + Math.floor(rnd() * 5),
    })),
    asks: Array.from({ length: 10 }, (_, i) => ({
      price: 24790 + i * 5 + (i > 4 ? 5 : 0),
      qty: 20 + Math.floor(rnd() * 180),
      orders: 1 + Math.floor(rnd() * 5),
    })),
  }

  const trades = []
  let t = new Date('2026-11-20T15:42:31+05:30').getTime()
  let p = hero.last
  for (let i = 0; i < 16; i++) {
    const side = rnd() < 0.55 ? 'BUY' : 'SELL'
    trades.push({ time: new Date(t), price: p, qty: 1 + Math.floor(rnd() * 60), side })
    t -= 400 + Math.floor(rnd() * 2600)
    p = tick(p * (1 + (rnd() - 0.5) * 0.0015))
  }
  const hhmmss = (d) => d.toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour12: false })

  // The phone's portfolio: joined with ₹1,00,000, bought 20 CHAIWALA at 245.50, 5 JUGAAD at 1,181.20.
  const holdings = [
    { ticker: 'CHAIWALA', qty: 20, avg: 24550, last: 24785 },
    { ticker: 'JUGAAD', qty: 5, avg: 118120, last: 118640 },
  ].map((h) => ({ ...h, value: h.qty * h.last, pnl: h.qty * (h.last - h.avg) }))
  const cash = 10000000 - 20 * 24550 - 5 * 118120
  const netWorth = cash + holdings.reduce((s, h) => s + h.value, 0)
  const portfolio = {
    nickname: 'RajuTrader',
    cash,
    reserved: 49200,
    holdings,
    netWorth,
    pnl: netWorth - 10000000,
    pnlPct: ((netWorth - 10000000) / 10000000) * 100,
  }
  const openOrders = [
    { side: 'BUY', ticker: 'CHAIWALA', type: 'LIMIT', qty: 2, price: 24600, status: 'OPEN' },
    { side: 'SELL', ticker: 'JUGAAD', type: 'LIMIT', qty: 3, price: 119500, status: 'PARTIALLY_FILLED', filled: 1 },
  ]

  // A candlestick chart as SVG, sized to its box. `c` names the colours (CSS values).
  function drawCandles(svg, list, c) {
    const w = svg.clientWidth || +svg.getAttribute('width')
    const h = svg.clientHeight || +svg.getAttribute('height')
    const volH = c.volume ? h * 0.18 : 0
    const plotH = h - volH - (c.axis ? 24 : 0)
    const right = c.axis ? 92 : 0
    const hi = Math.max(...list.map((k) => k.high))
    const lo = Math.min(...list.map((k) => k.low))
    const y = (v) => ((hi - v) / (hi - lo || 1)) * (plotH - 16) + 8
    const step = (w - right) / list.length
    const body = Math.max(2, step * 0.62)
    const maxV = Math.max(...list.map((k) => k.volume))
    let out = ''
    const lastY = y(list.at(-1).close)
    if (c.grid) {
      for (let i = 0; i <= 4; i++) {
        const v = tick(lo + ((hi - lo) * i) / 4) // axis labels sit on the 5-paise tick
        const yy = y(v).toFixed(1)
        out += `<line x1="0" x2="${w - right}" y1="${yy}" y2="${yy}" stroke="${c.grid}" stroke-width="1" ${c.gridDash ? `stroke-dasharray="${c.gridDash}"` : ''}/>`
        if (c.axis && Math.abs(+yy - lastY) > 26)
          out += `<text x="${w - right + 10}" y="${+yy + 5}" fill="${c.axis}" font-size="${c.axisSize || 16}" font-family="${c.axisFont}" style="font-variant-numeric:tabular-nums">${inr.format(v / 100)}</text>`
      }
    }
    list.forEach((k, i) => {
      const x = i * step + step / 2
      const up = k.close >= k.open
      const col = up ? c.up : c.down
      const top = y(Math.max(k.open, k.close))
      const bh = Math.max(1.5, Math.abs(y(k.open) - y(k.close)))
      out += `<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${y(k.high).toFixed(1)}" y2="${y(k.low).toFixed(1)}" stroke="${col}" stroke-width="${c.wick || 1.5}"/>`
      const fill = up && c.hollowUp ? c.bg : col
      out += `<rect x="${(x - body / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${body.toFixed(1)}" height="${bh.toFixed(1)}" fill="${fill}" stroke="${col}" stroke-width="1.5"/>`
      if (volH) {
        const vh = (k.volume / maxV) * (volH - 6)
        out += `<rect x="${(x - body / 2).toFixed(1)}" y="${(h - (c.axis ? 24 : 0) - vh).toFixed(1)}" width="${body.toFixed(1)}" height="${vh.toFixed(1)}" fill="${c.volume}"/>`
      }
    })
    if (c.lastLine) {
      const ly = y(list.at(-1).close).toFixed(1)
      out += `<line x1="0" x2="${w - right}" y1="${ly}" y2="${ly}" stroke="${c.lastLine}" stroke-width="1" stroke-dasharray="4 4"/>`
      if (c.axis)
        out += `<rect x="${w - right + 4}" y="${+ly - 14}" width="${right - 6}" height="28" fill="${c.lastLine}"/><text x="${w - right + 10}" y="${+ly + 6}" fill="${c.lastText || '#000'}" font-size="${c.axisSize || 16}" font-weight="700" font-family="${c.axisFont}" style="font-variant-numeric:tabular-nums">${inr.format(list.at(-1).close / 100)}</text>`
    }
    svg.innerHTML = out
  }

  // A QR-code-like grid for the join panel (a mockup: the real page encodes PUBLIC_BASE_URL/join).
  function drawQR(el, dark, light, size) {
    const n = 29
    const r = prng(7)
    const cell = size / n
    let out = `<rect width="${size}" height="${size}" fill="${light}"/>`
    const finder = (fx, fy) => {
      out += `<rect x="${fx * cell}" y="${fy * cell}" width="${7 * cell}" height="${7 * cell}" fill="${dark}"/>`
      out += `<rect x="${(fx + 1) * cell}" y="${(fy + 1) * cell}" width="${5 * cell}" height="${5 * cell}" fill="${light}"/>`
      out += `<rect x="${(fx + 2) * cell}" y="${(fy + 2) * cell}" width="${3 * cell}" height="${3 * cell}" fill="${dark}"/>`
    }
    const reserved = (x, y) => (x < 8 && y < 8) || (x > n - 9 && y < 8) || (x < 8 && y > n - 9)
    for (let y = 0; y < n; y++)
      for (let x = 0; x < n; x++)
        if (!reserved(x, y) && r() < 0.47)
          out += `<rect x="${x * cell}" y="${y * cell}" width="${cell + 0.3}" height="${cell + 0.3}" fill="${dark}"/>`
    finder(0, 0)
    finder(n - 7, 0)
    finder(0, n - 7)
    el.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">${out}</svg>`
  }

  window.MOCK = {
    tickers,
    hero,
    candles: candles(hero.last),
    miniCandles: candles(hero.last, 40),
    book,
    trades,
    hhmmss,
    portfolio,
    openOrders,
    rupees,
    pct,
    qty: (n) => qty.format(n),
    drawCandles,
    drawQR,
    joinUrl: 'http://192.168.43.12:5173/join',
  }
})()
