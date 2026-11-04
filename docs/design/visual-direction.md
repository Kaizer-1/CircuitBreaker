# Visual Direction: Three Candidates for the Market Screens

> **Status:** proposal (Phase 3), waiting for the owner's pick. Nothing in `web/` is built from it yet.
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

## Rules every direction follows

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

## 1. Pink Paper

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

## 2. Dalal Street Board

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

## 3. Terminal

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

## Comparison

| | Pink Paper | Dalal Street Board | Terminal |
|---|---|---|---|
| Light / dark | Light | Dark | Dark |
| Hero price at 25% | Clear | Readable (dot matrix) | Clear |
| Information density | Medium | Low (big, few things) | High |
| Feels | Editorial, calm | Theatrical, Indian | Professional, technical |
| Weak projector | Best | Weakest | Middle |
| Fit with Phase 7's chaos panel and incident feed | Needs care (alerts on paper) | Good (amber alarm boards) | Best (it is already an ops screen) |

## Viva Notes

**Q: Why choose a visual direction before building any screen?**
A: The dashboard is what the audience actually sees. Fixing colours, type and motion as tokens first means both screens
(and every later one) share one design system instead of drifting apart, and the owner picks between real mockups,
not adjectives.

**Q: What are tabular figures and why do they matter here?**
A: Digits that all have the same width. Prices change many times a second; with proportional digits the columns would
shift on every change (a "1" is narrower than an "8") and the eye couldn't track them.

**Q: How is "up" shown to someone who cannot tell red from green?**
A: Never by colour alone: every change has ▲ or ▼ and a + or − sign, so the meaning survives a colour-blind viewer or a
projector that washes the colours out.

**Q: What does the price flash communicate, and why only two animations?**
A: That a number just changed, and in which direction. Motion is reserved for meaning (a flash, a new trade); anything
else on a live screen competes with the data, and reduced-motion users can switch both off.

**Q: How did you check the screen is readable from the back of the room?**
A: The 1920×1080 dashboard screenshot scaled to 25% (480×270) must still show the current price clearly; all three
directions pass, Dalal Street's dot-matrix hero most narrowly.
