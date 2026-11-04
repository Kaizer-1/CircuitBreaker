// Screenshots of the real screens, in both themes, against the running stack (make up && make bots):
// the dashboard at 1920×1080 (plus a 25% copy, the back-of-the-room test) and the phone at 390×844.
// A trader joins, buys some CHAIWALA and leaves a resting order, so the phone has something to show.
// Uses the installed Google Chrome (Playwright's "chrome" channel): nothing to download.
//   node scripts/screenshots.mjs <output dir> [base URL, default http://localhost:5173]
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

const out = process.argv[2]
const base = process.argv[3] ?? 'http://localhost:5173'
if (!out) throw new Error('usage: node scripts/screenshots.mjs <output dir> [base URL]')
mkdirSync(out, { recursive: true })

async function call(method, path, body, token) {
  const response = await fetch(base + path, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!response.ok)
    throw new Error(`${method} ${path}: HTTP ${response.status} ${await response.text()}`)
  return response.json()
}

const admin = await call('POST', '/v1/admin/sessions', {
  nickname: 'presenter',
  passcode: process.env.ADMIN_PASSCODE ?? 'cb-dev-admin-passcode',
})
const nickname = `Raju${Math.random().toString(36).slice(2, 7)}`
const trader = await call('POST', '/v1/sessions', { nickname })
const book = await call('GET', '/v1/market/CHAIWALA/order-book', null, trader.access_token)
await call(
  'POST',
  '/v1/orders',
  { ticker: 'CHAIWALA', side: 'BUY', order_type: 'MARKET', quantity: 20 },
  trader.access_token,
)
const below = Math.round(((book.best_bid_paise ?? 24500) * 0.99) / 5) * 5
await call(
  'POST',
  '/v1/orders',
  { ticker: 'CHAIWALA', side: 'BUY', order_type: 'LIMIT', quantity: 5, limit_price_paise: below },
  trader.access_token,
)
const session = {
  token: trader.access_token,
  traderId: trader.trader_id,
  nickname: trader.nickname,
  expiresAt: trader.expires_at,
}

const browser = await chromium.launch({ channel: 'chrome' })
for (const theme of ['paper', 'terminal']) {
  const desk = await browser.newContext({ viewport: { width: 1920, height: 1080 } })
  await desk.addInitScript(
    (t) => sessionStorage.setItem('circuitbreaker.admin_token', t),
    admin.access_token,
  )
  const dashboard = await desk.newPage()
  await dashboard.goto(`${base}/dashboard?theme=${theme}`)
  await dashboard.waitForTimeout(7000) // the streams' snapshots, a few trades and book updates
  await dashboard.screenshot({ path: `${out}/dashboard-${theme}-1920x1080.png` })
  await desk.close()

  const phone = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
  })
  await phone.addInitScript(
    (s) => localStorage.setItem('circuitbreaker.trader', s),
    JSON.stringify(session),
  )
  const trade = await phone.newPage()
  await trade.goto(`${base}/trade?theme=${theme}`)
  await trade.waitForTimeout(6000)
  // No horizontal scroll on the phone: the page must not be wider than the screen.
  const width = await trade.evaluate(() => document.documentElement.scrollWidth)
  const wide = await trade.evaluate(() =>
    [...document.querySelectorAll('body *')]
      .filter((e) => e.getBoundingClientRect().right > window.innerWidth + 0.5)
      .slice(0, 5)
      .map((e) => `${e.tagName.toLowerCase()}.${e.className}`),
  )
  if (width > 390) throw new Error(`${theme} phone page is ${width}px wide: ${wide.join(', ')}`)
  await trade.screenshot({ path: `${out}/mobile-${theme}-390x844.png` })
  await trade.screenshot({ path: `${out}/mobile-${theme}-390-full.png`, fullPage: true })
  await phone.close()
}
await browser.close()
console.log(`screenshots in ${out} (trader ${nickname})`)
