// Screenshots of the real screens, in both themes, against the running stack (make up && make bots):
// the dashboard at 1920×1080 (with the leaderboard, and with it hidden), the time-travel screen in
// its three faces (live with the proof, paused at a marked move, replaying at 10×), and the phone
// at 390×844.
// A trader joins through the real join page (as RajuTrader; the page adds a suffix only if that
// name is taken), buys some CALVERA and leaves a resting order, so the phone has something to show.
//
// The main shots show demo day: /config.js is answered with a LAN address (SHOT_PUBLIC_BASE_URL,
// default the hotspot example from docs/08-developer-setup.md), and dashboard-<theme>-qr-warning.png
// shows the warning the presenter sees when PUBLIC_BASE_URL is localhost.
//
// It fails, rather than taking a shot, if anything overflows its container (or the screen), if a
// hero label wraps onto two lines, if the last item on the phone hides behind the terminal skin's
// tabs, or if the hero price does not return to its normal colour after a flash.
// Uses the installed Google Chrome (Playwright's "chrome" channel): nothing to download.
//   node scripts/screenshots.mjs <output dir> [base URL, default http://localhost:5173]
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

const out = process.argv[2]
const base = process.argv[3] ?? 'http://localhost:5173'
const lan = process.env.SHOT_PUBLIC_BASE_URL ?? 'http://192.168.43.12:5173'
const nickname = process.env.SHOT_NICKNAME ?? 'RajuTrader'
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

/** Answer /config.js as the web container would with PUBLIC_BASE_URL=url. */
async function publicBaseUrl(context, url) {
  await context.route('**/config.js', (route) =>
    route.fulfill({
      contentType: 'application/javascript',
      body: `window.CB_CONFIG = { publicBaseUrl: ${JSON.stringify(url)} }\n`,
    }),
  )
}

/** Every element must stay inside its parent and the screen; text must not spill out of its box. */
async function assertNothingOverflows(page, what) {
  const found = await page.evaluate(() => {
    const tolerance = 1
    const name = (e) =>
      `${e.tagName.toLowerCase()}${e.className && typeof e.className === 'string' ? `.${e.className.trim().replace(/\s+/g, '.')}` : ''}` +
      ` "${(e.textContent ?? '').trim().slice(0, 24)}"`
    const bad = []
    for (const e of document.querySelectorAll('body *')) {
      if (e.closest('.chart') && !e.matches('.chart')) continue // the chart library's own canvases
      if (e.getAnimations().length > 0) continue // mid-motion (a new trade sliding in): not layout
      const style = getComputedStyle(e)
      if (style.display === 'none' || style.visibility === 'hidden') continue
      const r = e.getBoundingClientRect()
      if (r.width === 0 || r.height === 0) continue
      if (r.left < -tolerance || r.right > window.innerWidth + tolerance)
        bad.push(`${name(e)} is outside the screen (${Math.round(r.left)}..${Math.round(r.right)})`)
      const p = e.parentElement.getBoundingClientRect()
      if (style.position !== 'fixed') {
        if (r.left < p.left - tolerance || r.right > p.right + tolerance)
          bad.push(`${name(e)} sticks out of its container by ${Math.round(r.right - p.right)}px`)
        const block = !style.display.startsWith('inline')
        if (block && (r.top < p.top - tolerance || r.bottom > p.bottom + tolerance))
          bad.push(`${name(e)} sticks out of its container vertically`)
      }
      if (e.clientWidth > 0 && e.scrollWidth > e.clientWidth + tolerance)
        bad.push(`${name(e)}: its content is ${e.scrollWidth - e.clientWidth}px wider than it`)
    }
    if (document.documentElement.scrollWidth > window.innerWidth)
      bad.push(`the page scrolls sideways (${document.documentElement.scrollWidth}px)`)
    return bad
  })
  if (found.length) throw new Error(`${what}: overflow\n  ${found.slice(0, 12).join('\n  ')}`)
}

/** Labels that must stay on one line: the hero's change, its fact names and values (Phase 4
 * review: on paper "on the day" wrapped and "State hash" broke in two beside a long hash). */
async function assertHeroLabelsOnOneLine(page, what) {
  const wrapped = await page.evaluate(() =>
    [...document.querySelectorAll('.hero .change small, .hero .facts dt, .hero .facts dd')]
      .filter((e) => {
        const line = parseFloat(getComputedStyle(e).lineHeight) || e.getBoundingClientRect().height
        return e.getBoundingClientRect().height > line * 1.5
      })
      .map((e) => `"${(e.textContent ?? '').trim()}"`),
  )
  if (wrapped.length) throw new Error(`${what}: wraps onto two lines: ${wrapped.join(', ')}`)
}

/** The terminal skin's tabs are fixed at the bottom: the page must leave room under its last item. */
async function assertTabsHideNothing(page, what) {
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
  await page.waitForTimeout(300)
  const { last, tabs } = await page.evaluate(() => {
    const items = [...document.querySelectorAll('.phone > *')].filter(
      (e) => !e.matches('.tabs, .toast') && e.getBoundingClientRect().height > 0,
    )
    return {
      last: items[items.length - 1].getBoundingClientRect().bottom,
      tabs: document.querySelector('.tabs').getBoundingClientRect().top,
    }
  })
  if (last > tabs)
    throw new Error(`${what}: the last item ends at ${last}px, under the tabs at ${tabs}px`)
  await page.evaluate(() => window.scrollTo(0, 0))
}

/** After a flash the hero price must be back in the normal text colour (the price block's own). */
async function assertHeroSettles(page, what) {
  for (let n = 0; n < 40; n++) {
    const state = await page.evaluate(async () => {
      const box = document.querySelector('[data-testid="hero-price"]')
      const price = box.lastElementChild
      await Promise.all(price.getAnimations().map((a) => a.finished.catch(() => null)))
      const now = box.lastElementChild // a new flash re-creates the element
      return {
        flashing: /flash-/.test(now.className) || now.getAnimations().length > 0,
        colour: getComputedStyle(now).color,
        neutral: getComputedStyle(box).color,
      }
    })
    if (!state.flashing) {
      if (state.colour !== state.neutral)
        throw new Error(
          `${what}: hero price is ${state.colour} after its flash, not ${state.neutral}`,
        )
      return
    }
    await page.waitForTimeout(50)
  }
  throw new Error(`${what}: the hero price never stopped flashing`)
}

const admin = await call('POST', '/v1/admin/sessions', {
  nickname: 'presenter',
  passcode: process.env.ADMIN_PASSCODE ?? 'cb-dev-admin-passcode',
})

const browser = await chromium.launch({ channel: 'chrome' })
const phoneSize = {
  viewport: { width: 390, height: 844 },
  deviceScaleFactor: 2,
  isMobile: true,
  hasTouch: true,
}

/** Join the way a phone does: the join page, the nickname as typed. Returns the stored session. */
async function joinAs(name) {
  const joining = await browser.newContext(phoneSize)
  const page = await joining.newPage()
  await page.goto(`${base}/join`)
  await page.getByLabel('Nickname').fill(name)
  await page.getByRole('button', { name: 'Start trading' }).click()
  await page.waitForURL('**/trade')
  const stored = await page.evaluate(() => localStorage.getItem('circuitbreaker.trader'))
  await joining.close()
  return JSON.parse(stored)
}
const session = await joinAs(nickname)
const twin = await joinAs(nickname) // the same name again: the page adds a short suffix
if (!twin.suffix) throw new Error(`a second ${nickname} joined without a suffix`)

const book = await call('GET', '/v1/market/CALVERA/order-book', null, session.token)
await call(
  'POST',
  '/v1/orders',
  { ticker: 'CALVERA', side: 'BUY', order_type: 'MARKET', quantity: 20 },
  session.token,
)
const below = Math.round(((book.best_bid_paise ?? 24500) * 0.99) / 5) * 5
await call(
  'POST',
  '/v1/orders',
  { ticker: 'CALVERA', side: 'BUY', order_type: 'LIMIT', quantity: 5, limit_price_paise: below },
  session.token,
)

async function phonePage(theme, trader) {
  const phone = await browser.newContext(phoneSize)
  await publicBaseUrl(phone, lan)
  await phone.addInitScript(
    (s) => localStorage.setItem('circuitbreaker.trader', s),
    JSON.stringify(trader),
  )
  const page = await phone.newPage()
  await page.goto(`${base}/trade?theme=${theme}`)
  await page.waitForTimeout(6000)
  return page
}

async function presenterContext(url) {
  const desk = await browser.newContext({ viewport: { width: 1920, height: 1080 } })
  await publicBaseUrl(desk, url)
  await desk.addInitScript(
    (t) => sessionStorage.setItem('circuitbreaker.admin_token', t),
    admin.access_token,
  )
  return desk
}

/** Time travel: paused at the most recent marked move, replaying from there at 10×, then back to
 * live with the proof ("Verified: hashes match"). Every face must show its mode. */
async function timeTravel(theme) {
  const desk = await presenterContext(lan)
  const page = await desk.newPage()
  await page.goto(`${base}/time-travel?theme=${theme}`)
  await page.waitForTimeout(4000)
  const markers = page.getByRole('button', { name: /^Jump to / })
  if ((await markers.count()) === 0) throw new Error(`${theme} time travel: no marked moves yet`)
  await markers.last().click()
  await page.getByText(/Paused · sequence/).waitFor()
  await page.waitForTimeout(800)
  await assertNothingOverflows(page, `${theme} time travel, paused`)
  await assertHeroLabelsOnOneLine(page, `${theme} time travel, paused`)
  await page.screenshot({ path: `${out}/time-travel-${theme}-historical.png` })

  await page.getByRole('button', { name: '10×' }).click()
  await page.getByRole('button', { name: /Play/ }).click()
  await page.getByText(/Replaying at 10×/).waitFor()
  await page.waitForTimeout(4000)
  await assertNothingOverflows(page, `${theme} time travel, replaying`)
  await assertHeroLabelsOnOneLine(page, `${theme} time travel, replaying`)
  await page.screenshot({ path: `${out}/time-travel-${theme}-playing.png` })

  await page.getByRole('button', { name: /Back to live/ }).click()
  await page
    .getByText(/Verified at sequence [\d,]+: hashes match/)
    .first()
    .waitFor({ timeout: 10_000 })
  await page.waitForTimeout(800)
  await assertNothingOverflows(page, `${theme} time travel, live`)
  await assertHeroLabelsOnOneLine(page, `${theme} time travel, live`)
  await page.screenshot({ path: `${out}/time-travel-${theme}-live.png` })
  await desk.close()
}

async function dashboard(theme, url, file) {
  const desk = await presenterContext(url)
  const page = await desk.newPage()
  await page.goto(`${base}/dashboard?theme=${theme}`)
  await page.waitForTimeout(7000) // the streams' snapshots, a few trades and book updates
  await assertNothingOverflows(page, `${theme} dashboard`)
  await assertHeroLabelsOnOneLine(page, `${theme} dashboard`)
  await page.screenshot({ path: `${out}/${file}` })
  await assertHeroSettles(page, `${theme} dashboard`)
  if (url === lan) {
    // Projector safety: the presenter takes the leaderboard off the screen.
    await page.getByRole('button', { name: 'Hide leaderboard' }).click()
    await page.waitForTimeout(500)
    await assertNothingOverflows(page, `${theme} dashboard, leaderboard hidden`)
    await page.screenshot({ path: `${out}/dashboard-${theme}-leaderboard-off.png` })
  }
  await desk.close()
}

for (const theme of ['paper', 'terminal']) {
  await dashboard(theme, lan, `dashboard-${theme}-1920x1080.png`)
  await dashboard(theme, 'http://localhost:5173', `dashboard-${theme}-qr-warning.png`)
  await timeTravel(theme)

  const trade = await phonePage(theme, session)
  await assertNothingOverflows(trade, `${theme} phone`)
  if (theme === 'terminal') await assertTabsHideNothing(trade, `${theme} phone`)
  await trade.screenshot({ path: `${out}/mobile-${theme}-390x844.png` })
  // Scrolled to the end: the last item must clear the terminal skin's fixed tabs.
  await trade.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
  await trade.waitForTimeout(300)
  await trade.screenshot({ path: `${out}/mobile-${theme}-390x844-end.png` })
  // The whole page: a viewport as tall as the page, so the fixed tabs sit at its bottom (a
  // fullPage capture would paint them where the 844 px viewport ended, mid-page).
  const tall = await trade.evaluate(() => document.documentElement.scrollHeight)
  await trade.setViewportSize({ width: 390, height: tall })
  await trade.waitForTimeout(500)
  await trade.screenshot({ path: `${out}/mobile-${theme}-390-full.png` })
  await trade.context().close()

  const second = await phonePage(theme, twin)
  await second
    .locator('#account')
    .screenshot({ path: `${out}/mobile-${theme}-duplicate-nickname.png` })
  await second.context().close()
}
await browser.close()
console.log(
  `screenshots in ${out} (traders ${session.nickname} and ${twin.nickname}; checks passed)`,
)
