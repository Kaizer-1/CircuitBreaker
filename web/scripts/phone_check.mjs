// The phone, checked the way phones really reach the laptop: plain http on an address that is not
// localhost. Browsers treat http://localhost as a secure context and http://<LAN IP> as not, and
// some APIs (crypto.randomUUID) exist only in a secure context: phone ordering never worked over
// the LAN until November 2026, because every earlier test ran on localhost.
//
// By default Chrome maps the name cb.test to the stack's host, so the page runs on http://cb.test
// (not a secure context) without a network; give a LAN address instead to use the real one.
// At 390×844, in both skins, as a load-test account (load-13, kind LOAD: never on the leaderboard):
//   1. the page is not a secure context and has no crypto.randomUUID (else the check proves nothing);
//   2. a MARKET buy of 1 CHAIWALA through the order ticket is sent with a v4 Idempotency-Key, fills,
//      and appears in Holdings; the same key sent again returns the same order (no second buy);
//   3. terminal skin: each F-key tab scrolls its section to the top and lights up, with no reload
//      and no #hash; paper skin: no tab bar;
//   4. the status-bar strip (iPhones) has the skin's background;
//   5. no error in the console and no uncaught exception.
// Needs the bots trading (a MARKET buy needs a seller). Uses the installed Google Chrome.
//   node scripts/phone_check.mjs [stack URL, default http://localhost:5173]
import { chromium } from 'playwright'

const base = new URL(process.argv[2] ?? 'http://localhost:5173')
const botSecret = process.env.BOT_SECRET ?? 'cb-dev-bot-secret'
const ACCOUNT = 'load-13'
const TICKER = 'CHAIWALA'
const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const local = ['localhost', '127.0.0.1', '[::1]'].includes(base.hostname)
const origin = local ? `http://cb.test${base.port ? `:${base.port}` : ''}` : base.origin

function check(ok, what) {
  if (!ok) throw new Error(`FAILED: ${what}`)
  console.log(`  ok  ${what}`)
}

async function call(method, path, body, headers = {}) {
  const response = await fetch(base.origin + path, {
    method,
    headers: { 'Content-Type': 'application/json', ...headers },
    body: body ? JSON.stringify(body) : undefined,
  })
  const text = await response.text()
  if (!response.ok) throw new Error(`${method} ${path}: HTTP ${response.status} ${text}`)
  return JSON.parse(text)
}

const session = await call(
  'POST',
  '/v1/bot-sessions',
  { account_name: ACCOUNT },
  {
    'X-Bot-Secret': botSecret,
  },
)
const auth = { Authorization: `Bearer ${session.access_token}` }
const held = async () =>
  (await call('GET', '/v1/portfolio', null, auth)).holdings.find((h) => h.ticker === TICKER)
    ?.quantity ?? 0

// A MARKET buy needs a seller: wait for the bots' asks (up to a minute after make bots).
for (let waited = 0; ; waited += 2) {
  const book = await call('GET', `/v1/market/${TICKER}/order-book`, null, auth)
  if (book.asks.length) break
  if (waited >= 60)
    throw new Error(`No sellers in ${TICKER}: start the bots first (make bots / make k8s-bots)`)
  await new Promise((resolve) => setTimeout(resolve, 2000))
}

const browser = await chromium.launch({
  channel: 'chrome',
  args: local ? [`--host-resolver-rules=MAP cb.test ${base.hostname}`] : [],
})
console.log(`Phone check on ${origin} (stack ${base.origin}), as ${ACCOUNT}`)
try {
  for (const theme of ['paper', 'terminal']) {
    console.log(`${theme}:`)
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      isMobile: true,
      hasTouch: true,
    })
    await context.addInitScript(
      ([trader, skin]) => {
        localStorage.setItem('circuitbreaker.trader', JSON.stringify(trader))
        localStorage.setItem('circuitbreaker.theme', skin)
      },
      [
        {
          token: session.access_token,
          traderId: session.trader_id,
          nickname: session.nickname,
          expiresAt: session.expires_at,
        },
        theme,
      ],
    )
    const page = await context.newPage()
    const errors = []
    page.on('pageerror', (e) => errors.push(`uncaught: ${e.message}`))
    page.on('console', (m) => m.type() === 'error' && errors.push(`console: ${m.text()}`))
    let loads = 0
    page.on('load', () => loads++)

    await page.goto(`${origin}/trade`)
    const env = await page.evaluate(() => ({
      secure: window.isSecureContext,
      randomUUID: typeof crypto.randomUUID,
    }))
    check(
      !env.secure && env.randomUUID === 'undefined',
      'not a secure context, crypto.randomUUID undefined (as on a phone over the LAN)',
    )
    await page.locator('.worth').filter({ hasNotText: '—' }).waitFor()

    // 2. A MARKET buy through the ticket.
    const before = await held()
    await page
      .getByRole('group', { name: 'Order type' })
      .getByRole('button', { name: 'MARKET' })
      .tap()
    await page.getByLabel('Quantity').fill('1')
    const answer = page
      .waitForResponse((r) => r.url().endsWith('/v1/orders') && r.request().method() === 'POST', {
        timeout: 10_000,
      })
      .catch(() => null)
    await page.getByRole('button', { name: new RegExp(`BUY 1 ${TICKER} at market`, 'i') }).tap()
    const placed = await answer
    if (!placed) {
      const said = await page
        .getByRole('status')
        .textContent({ timeout: 1000 })
        .catch(() => '')
      check(false, `the order request was sent (none was; the page said: "${said || 'nothing'}")`)
    }
    const key = placed.request().headers()['idempotency-key']
    check(placed.ok(), `order accepted by the gateway (HTTP ${placed.status()})`)
    check(V4.test(key ?? ''), `Idempotency-Key is an RFC 4122 v4 UUID (${key})`)
    const order = await placed.json()
    // "Order sent…" until the fill arrives, then "Bought 1 CHAIWALA at …" (it can come first).
    await page
      .getByRole('status')
      .filter({ hasText: new RegExp(`Order sent|Bought 1 ${TICKER}`) })
      .waitFor()

    const deadline = Date.now() + 15_000
    let after = before
    while (after <= before && Date.now() < deadline) {
      await page.waitForTimeout(300)
      after = await held()
    }
    check(after === before + 1, `bought: ${TICKER} ${before} → ${after}`)
    await page
      .locator('#holdings td.stock', { hasText: TICKER })
      .locator('small', { hasText: new RegExp(`^${after.toLocaleString('en-IN')} @`) })
      .waitFor({ timeout: 10_000 })
    check(true, `Holdings shows ${after} ${TICKER}`)

    const again = await call('POST', '/v1/orders', placed.request().postDataJSON(), {
      ...auth,
      'Idempotency-Key': key,
    })
    await page.waitForTimeout(1000)
    check(
      again.order_id === order.order_id && (await held()) === after,
      'the same key again returns the same order and buys nothing more',
    )

    // 3. The F-key tabs.
    const tabs = page.getByRole('navigation', { name: 'Sections' })
    if (theme === 'terminal') {
      await page.evaluate(() => (window.__sameDocument = true))
      for (const [label, id] of [
        ['F2TICKET', 'ticket'],
        ['F3ORDERS', 'orders'],
        ['F4HOLDINGS', 'holdings'],
        ['F1MARKET', 'market'],
      ]) {
        await tabs.locator('a', { hasText: label.slice(2) }).tap()
        let y = -1
        for (let still = 0; still < 3;) {
          await page.waitForTimeout(100)
          const now = await page.evaluate(() => window.scrollY)
          still = now === y ? still + 1 : 0
          y = now
        }
        const state = await page.evaluate(
          (section) => ({
            same: window.__sameDocument === true,
            url: location.pathname + location.hash,
            lit: [...document.querySelectorAll('.tabs a.on')].map((a) => a.textContent),
            top: document.getElementById(section).getBoundingClientRect().top,
          }),
          id,
        )
        check(
          state.same &&
            loads === 1 &&
            state.url === '/trade' &&
            state.lit.join() === label &&
            state.top >= 0 &&
            state.top <= 40,
          `${label}: section at the top, tab lit, no reload, URL /trade (${JSON.stringify({ ...state, loads })})`,
        )
      }
      // Scrolling by hand (a finger, not a tab) moves the light too.
      const litAfter = async (y) => {
        await page.evaluate((to) => window.scrollTo(0, to), y)
        await page.waitForTimeout(200)
        return page.evaluate(() => document.querySelector('.tabs a.on')?.textContent)
      }
      const bottom = await page.evaluate(() => document.documentElement.scrollHeight)
      check(
        (await litAfter(bottom)) === 'F4HOLDINGS' && (await litAfter(0)) === 'F1MARKET',
        'scrolling by hand lights F4 at the bottom and F1 at the top',
      )
    } else check(!(await tabs.isVisible()), 'no tab bar in the paper skin')

    // 4. The iPhone status-bar strip is painted in the skin's background.
    const strip = await page.evaluate(() => ({
      strip: getComputedStyle(document.body, '::before').backgroundColor,
      body: getComputedStyle(document.body).backgroundColor,
      fixed: getComputedStyle(document.body, '::before').position,
    }))
    check(strip.fixed === 'fixed' && strip.strip === strip.body, `status-bar strip ${strip.strip}`)

    check(errors.length === 0, `no console errors${errors.length ? `: ${errors.join(' | ')}` : ''}`)
    await context.close()
  }
} finally {
  await browser.close()
}
console.log('Phone check passed.')
