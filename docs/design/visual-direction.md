# Visual Direction: Three Candidates for the Market Screens

> **Status:** decided (Phase 3, 2026-11-05): **Pink Paper and Terminal, as one theme system**: one layout, two skins.
> Dalal Street Board is dropped. Built in `web/src/theme/` ([the decision](#the-decision-one-layout-two-skins)). The three
> candidates below are kept as the record of what was compared.
>
> **Visual direction:** the look and feel every screen shares: colours, type, how numbers and motion behave. One is
> picked, turned into **design tokens** (named CSS variables such as `--up` or `--font-num`) and then every screen is
> built from those tokens only.

The web app has two audiences. The **projector dashboard** (the presenter view, 1920×1080) must be readable from the
back of a classroom. The **phone trading page** (390×844 and up) must work one-handed on a small, possibly dim screen.
Each direction below is a static HTML mockup with the same realistic data (our five tickers, ₹ in Indian grouping, a
plausible order book, trade tape, 5-second candles and the join QR), so they can be compared side by side rather than
described in words.

| Direction | Dashboard | Phone | Tokens |
|---|---|---|---|
| 1. Pink Paper | [mockups/pink-paper/dashboard.html](mockups/pink-paper/dashboard.html) | [mobile.html](mockups/pink-paper/mobile.html) | [tokens.css](mockups/pink-paper/tokens.css) |
| 2. Dalal Street Board | [mockups/dalal-street/dashboard.html](mockups/dalal-street/dashboard.html) | [mobile.html](mockups/dalal-street/mobile.html) | [tokens.css](mockups/dalal-street/tokens.css) |
| 3. Terminal | [mockups/terminal/dashboard.html](mockups/terminal/dashboard.html) | [mobile.html](mockups/terminal/mobile.html) | [tokens.css](mockups/terminal/tokens.css) |

`docs/design/mockups/render.sh <dir>` screenshots all six with headless Chrome (dashboards at 1920×1080 plus a 25%
copy, phones at 390×844). The fake data lives in [mockups/shared/market.js](mockups/shared/market.js). The QR panel in
the mockups is a QR-like pattern; the real page encodes `PUBLIC_BASE_URL/join`.


## The decision: one layout, two skins

The owner picked **Pink Paper as the light theme and Terminal as the dark theme**, as one theme system. There is **one
layout and one component set**: Terminal's structure (watchlist, hero price, candlestick chart, depth, time and sales,
the QR join box, the status bar; on the phone: account, hero and chart, ticker selector, order ticket, open orders,
holdings). A theme changes only the **tokens** and a few **theme-only details**.

**Why the two pair well.** Paper is a light theme: dark ink on a light ground keeps its contrast in a bright classroom
and on a weak projector, where dark themes wash out to grey. Terminal is a dark theme: it suits a dim room, and phones
at night, where a salmon page would be glaring. Both are typographic (rules, monospaced numbers, no decoration), so
one layout reads as native in either.

| | Where it lives |
|---|---|
| Tokens: colours, fonts, rules, radii, density, flash, candle style | `web/src/theme/tokens.css`, under `:root[data-theme='paper']` and `:root[data-theme='terminal']` |
| The one layout | `web/src/theme/app.css` (tokens only) |
| Theme-only details | Elements with class `paper-only` or `terminal-only`, hidden by the other theme (not separate components) |
| Which theme shows | `web/src/theme/theme.ts`; the same rules run inline in `index.html` before the first paint |

**Theme-only details.** Paper: the serif masthead "CircuitBreaker *Markets*", the italic dateline with the real date,
italic chart captions, hollow up-candles, the "ink dries" flash. Terminal: the F-key bar, the `> TICKER EQUITY GP▌`
command line, filled candles, the cell-light flash, `<GO>` before the submit button, the F-key tabs at the bottom of the
phone.

**Switching.** The URL's `?theme=paper|terminal` wins over everything (choose the projector's theme on demo day). Then
the choice made with the toggle in the header, remembered per device in `localStorage`. Then the device's
`prefers-color-scheme` (light → paper, dark → terminal). An inline script in `index.html` sets `data-theme` before the
first paint, so the wrong theme never flashes. A test checks that it and `resolveTheme` agree in every case. Lightweight
Charts takes its colours in JavaScript, so the chart re-applies its layout and series options on every theme change
(also tested).

**Fonts** are the ones each mockup used: Newsreader + IBM Plex Mono + Libre Franklin (paper), JetBrains Mono + IBM Plex
Sans Condensed (terminal). They are Google Fonts' files, **self-hosted** through Fontsource, so phones on the demo
hotspot need no internet. Only the weights used are loaded, all with `font-display: swap`.

### Tokens, both themes

| Token | Paper | Terminal | Use |
|---|---|---|---|
| `--bg` | `#F7E1CF` | `#0A0B0C` | Page |
| `--surface` | `#F0D3BD` | `#111315` | Panels (paper: only the ticket and the join box) |
| `--head-bg` | none | `#1A1D20` | Panel title bars |
| `--text` / `--text-2` / `--text-3` | `#1B1611` / `#4A3F37` / `#62574D` | `#E8E3D6` / `#A39D90` / `#8C867A` | Text, labels, captions |
| `--rule` / `--rule-soft` | `#1B1611` / `#C4A790` | `#2A2E32` / `#1C1F22` | Rules, row separators |
| `--accent` | `#7A1C2C` (claret) | `#FFA028` (amber) | Masthead / commands, keys, selection |
| `--up` / `--down` | `#0A6A45` / `#A3201F` | `#45D07A` / `#FF5C5C` | Changes (always with ▲ ▼ and a sign) |
| `--font-head` / `--font-num` / `--font-ui` | Newsreader / IBM Plex Mono / Libre Franklin | Plex Sans Condensed / JetBrains Mono / JetBrains Mono | Type |
| `--frame` / `--frame-strong` | 1 px ink / 3 px double | 1 px grid / 1 px grid | Outlines |
| `--radius` | 0 | 0 | No rounded data surfaces |
| `--gap` / `--pad` / `--cell-y` | 22 / 14 / 5 px | 6 / 10 / 3 px | Density: print breathes, a terminal is dense |
| `--flash-*` | Fills with ink, paper-coloured digits, 900 ms | Lights the cell dark green or red, 600 ms | A price change (the hero changes its digits' colour instead) |
| `--candle-up-fill` | `#F7E1CF` (hollow) | `#45D07A` (filled) | Candle style |
| `--chart-tag` | `#1B1611` (ink) | not set: the last candle's colour | The chart's last-price tag and line |

**Measured, not guessed.** Every text colour is at least 4.5:1 on every surface it sits on, in both themes (70 pairs).
Paper's `--text-3` was darkened from `#6E6157` to `#62574D` because it measured 4.2:1 on the ticket's tint. Both
dashboards pass the 25% test (the 1920×1080 screen scaled to 480×270 still shows the hero price clearly). Component tests
run in both themes against the real `tokens.css`. `web/scripts/screenshots.mjs` renders the real screens in both themes
with Playwright. It fails if any element overflows its container or the screen, if the terminal skin's fixed tabs hide
the phone's last item, or if the hero price is not back in its normal colour after a flash.

**Self-review against the owner's lists (both themes).** No gradients, glass, blur, glow or shadows; no banned
typefaces; no rounded data surfaces (all radii are 0); no emoji or decorative icons (▲ ▼ ● are text characters); amber,
not cyan, on dark. Digits are tabular everywhere (set on `body`), prices right-aligned, every change has colour plus
▲ ▼ and a sign. Motion is only the price flash and a new trade entering the tape; `prefers-reduced-motion` turns both
off. Fixed during the review: the dashboard's main area collapsed in the paper theme when the F-key row was hidden; the
paper hero was wider than its column; the hero's filled flash hid the digits, so it now changes their colour; the
phone header wrapped, so the nickname moved into the account panel; the chart's attribution logo covered the small
chart, so it is a text link on each page instead (the Lightweight Charts licence allows it); the System Status page's
rounded card grid became ruled rows on the same tokens.

**Second review, by the owner (2026-11-05), both themes, both sizes.**
- **Chart:** with less data than the chart's width holds, the candles now spread to fit instead of leaving the left
  side empty. Once the data fills the width, the chart rolls as usual.
- **Price axis:** volume sits on its own unlabelled overlay scale in the bottom band, and an edge label is drawn only
  when it fits whole (Terminal's top "249.00" was clipped). A tick label is left blank where the last-price tag would
  half cover it, or where it would sit beside the volume bars. Paper's tag is ink, because a hollow candle's paper
  body made it invisible.
- **Phone holdings:** the P&L column was clipped at 390 px, so the average cost now sits under the stock name.
- **Phone tabs:** the page leaves room under its last item for the fixed tab bar plus `env(safe-area-inset-bottom)`.
  A tab scrolls its section to the top of the screen without leaving the page (no `#hash`), and the lit tab follows
  the scroll. The tail of the page (holdings) is at least a screen high, so every section can reach the top: on a
  short page every tab used to land on the same spot, with F1 always lit (fixed 2026-11-14).
- **iPhone status bar:** `viewport-fit=cover` lets the page run under the status bar, so a fixed strip
  `env(safe-area-inset-top)` high, in the skin's background, sits there and the phone pages start below it.
- **QR address:** the dashboard warns when the join address is localhost.
- **Nicknames:** shown as typed, with a quieter suffix only for a duplicate.
- **Hero flash:** checked in the browser to return to the normal colour after a flash. The green hero in the first
  Terminal screenshot was mid-flash.
- **Trades tape:** it shows only whole rows; Paper's rows are taller, and the bottom one was half cut.

## The three candidates (for the record)

### Rules every direction follows

| Rule | How |
|---|---|
| Contrast | Every text colour is ≥ 4.5:1 against the surface it sits on (WCAG AA), measured, not guessed |
| Readable from the back of the room | Hero price at 150–176 px on the dashboard. All three still show it clearly when the 1920×1080 shot is scaled to 25% (480×270) |
| Columns that don't jitter | Every number uses tabular figures (`font-variant-numeric: tabular-nums`) and prices are right-aligned, so decimals line up |
| Up/down without relying on colour | Green/red **plus** ▲/▼ and a +/− sign, everywhere a change is shown |
| Indian money format | ₹ with Indian grouping from integer paise: `Intl.NumberFormat('en-IN')` gives ₹1,00,073.00 |
| Motion that means something | Only two animations: a price flash when a number changes, and a new trade entering the tape. Nothing decorative. `prefers-reduced-motion` turns both off |
| One design system | Each direction is one `tokens.css` used by both its dashboard and its phone page |
| Phones | Tap targets ≥ 44 px (main buttons 48–58 px), body text ≥ 15 px, no horizontal scroll except the deliberate ticker strip in Dalal Street |
| Not generic | No gradients, glass, glow or shadow-heavy cards; no Inter/Poppins/Space Grotesk/Montserrat/Roboto; no grid of identical rounded KPI cards; radius ≤ 2 px; no emoji or decorative icons; and no "dark mode with a cyan accent" |

### 1. Pink Paper

**Mood:** the markets page of a financial newspaper (the salmon pages of the FT and the Economic Times) brought to
life. Calm, editorial, authoritative: the market as a story you read.

| Token | Hex | Use |
|---|---|---|
| `--paper` | `#F7E1CF` | Salmon newsprint background |
| `--paper-tint` | `#F0D3BD` | Boxed panels (the order ticket, the join box) |
| `--ink` | `#1B1611` | Text and hairline rules (14:1 on paper) |
| `--ink-2` / `--ink-3` | `#4A3F37` / `#6E6157` | Secondary text, captions (8.1:1 and 4.7:1) |
| `--up` | `#0A6A45` | Deep green ink (5.3:1 on paper) |
| `--down` | `#A3201F` | Claret-red ink (6.0:1) |
| `--accent` | `#7A1C2C` | The masthead's "Markets" |

**Type:** **Newsreader** (headings and the masthead, a serif made for news with display optical sizes) +
**IBM Plex Mono** (every number, in hairline-ruled tables) + **Libre Franklin** (small-caps labels and buttons, a
Franklin Gothic revival, the sans of newspaper headlines). *Why:* a serif says "newspaper" at once, while a monospaced
face keeps every digit the same width, so columns of prices read like a printed stock table.

**Numbers and the price flash:** numbers are set in Plex Mono, right-aligned, in black ink. A change "prints fresh
ink": the cell fills with green or red ink with paper-coloured digits, then dries back to paper over 900 ms (the
newest trade in the tape is shown mid-flash). Up candles are hollow green, down candles solid red, as on print charts.

**Why it fits a projector:** it is the only light direction. On a washed-out classroom projector, dark ink on a light
ground keeps its contrast where dark themes turn grey, and it looks unlike any trading app the audience has seen.
*Risk:* it is the least "live-feeling" of the three, and the large light area is bright on a phone at night.

### 2. Dalal Street Board (dropped)

**Mood:** the LED board on the Bombay Stock Exchange building at Dalal Street: black, quiet, with amber dot-matrix
numbers that you watch change. Theatrical but restrained: the LED is used **only** for the ticker strip and the hero
price; everything else is plain signage.

| Token | Hex | Use |
|---|---|---|
| `--board` | `#060606` | The board |
| `--panel` / `--line` | `#101010` / `#2A2A2A` | The order ticket, dividers |
| `--text` | `#ECE7DD` | Warm white "signage paint" (16:1) |
| `--text-2` / `--text-3` | `#A8A298` / `#7D786F` | Labels (≥ 4.6:1) |
| `--led` | `#FFB000` | Amber LED, lit |
| `--led-ghost` | `#171000` | The unlit dots behind strip digits (barely there; never on the hero, for legibility) |
| `--up` / `--down` | `#2FD67E` / `#FF5544` | Green and red LED |

**Type:** **Doto** (a dot-matrix face, for the LED numerals only) + **Barlow Condensed** (signage: tickers, headings,
buttons) + **Barlow** (table numbers with tabular figures, body). *Why:* the dot-matrix numerals are the board; a
condensed grotesque is the lettering painted on Indian street and station signs, and it fits long tickers like
CHAIWALA into narrow columns. Doto's decimal point is drawn as a small cross, so the mockup draws its own square LED
dot instead.

**Numbers and the price flash:** an LED change blanks the digits for 120 ms, then lights them green or red for about
a second before they return to amber, like a real board updating (DABBA in the strip is shown in its red state). Table
numbers are plain Barlow, right-aligned.

**Why it fits a projector:** amber-on-black is the highest-impact look in a dim room and instantly says "stock
exchange" to an Indian audience. *Risks:* dark themes lose contrast on weak projectors, and the dot-matrix hero is the
least crisp of the three at 25% scale (still readable).

### 3. Terminal

**Mood:** a professional trading terminal (the Bloomberg kind): dense, everything visible at once, amber commands on
near-black, function keys and a command line. Serious and fast: the market as an instrument.

| Token | Hex | Use |
|---|---|---|
| `--bg` / `--panel` / `--panel-head` | `#0A0B0C` / `#111315` / `#1A1D20` | Background, panels, panel title bars |
| `--grid` | `#2A2E32` | Panel borders and table rules |
| `--text` | `#E8E3D6` | Data (14.5:1 on a panel) |
| `--text-2` / `--text-3` | `#A39D90` / `#8C867A` | Labels (6.9:1 and 5.2:1) |
| `--amber` | `#FFA028` | The primary: commands, function keys, selection, the last-price line |
| `--up` / `--down` | `#45D07A` / `#FF5C5C` | Changes |
| `--flash-up` / `--flash-down` | `#0F3A22` / `#48161A` | A tick lights its cell |

**Type:** **JetBrains Mono** (all data, commands and the hero price, with tabular figures by design) + **IBM Plex Sans
Condensed** (panel titles, column headers, key labels). *Why:* a terminal is monospaced by nature, and a condensed
sans for the labels keeps headers short so the data gets the space.

**Numbers and the price flash:** a change lights its cell's background dark green or dark red for 600 ms with the number
at full brightness (MONSOON's last price and the newest trade are shown lit). The dashboard has a command line
(`> CHAIWALA EQUITY GP`), an F-key bar (F1 MARKET … F8 JOIN … F12 SYSTEM) and a status line. On the phone the F-keys
become the bottom tab bar.

**Why it fits a projector:** it shows the most information per screen (all five instruments with bid, ask, high, low
and volume, plus depth and the tape), which suits an audience that wants to see the whole system at work, and it pairs
naturally with the later chaos panel and incident feed. *Risks:* dense screens are harder to read from far away; the
hero price and the watchlist carry the room, the rest is for people near the front.

### Comparison

| | Pink Paper | Dalal Street Board | Terminal |
|---|---|---|---|
| Light / dark | Light | Dark | Dark |
| Hero price at 25% | Clear | Readable (dot matrix) | Clear |
| Information density | Medium | Low (big, few things) | High |
| Feels | Editorial, calm | Theatrical, Indian | Professional, technical |
| Weak projector | Best | Weakest | Middle |
| Fit with Phase 7's chaos panel and incident feed | Needs care (alerts on paper) | Good (amber alarm boards) | Best (it is already an ops screen) |

## Viva Notes

**Q: What are tabular figures and why do they matter here?**
A: Digits that all have the same width. Prices change many times a second; with proportional digits the columns would
shift on every change (a "1" is narrower than an "8") and the eye couldn't track them.

**Q: How is "up" shown to someone who cannot tell red from green?**
A: Never by colour alone: every change has ▲ or ▼ and a + or − sign, so the meaning survives a colour-blind viewer or a
projector that washes the colours out.

**Q: What does the price flash communicate, and why only two animations?**
A: That a number just changed, and in which direction. Motion is reserved for meaning (a flash, a new trade); anything
else on a live screen competes with the data, and reduced-motion users can switch both off.

**Q: Why two themes, and why not two separate UIs?**
A: Light paper for bright rooms and weak projectors, dark terminal for dim rooms and phones at night. One layout with
two skins means every feature is built once: a theme only swaps the tokens and a few theme-only details, so the two can
never drift apart.

**Q: How did you check the screen is readable from the back of the room?**
A: The 1920×1080 dashboard screenshot scaled to 25% (480×270) must still show the current price clearly; all three
directions pass, Dalal Street's dot-matrix hero most narrowly.
