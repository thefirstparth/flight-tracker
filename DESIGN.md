# DESIGN.md: Flight price fare board

The dashboard (`dashboard_template.html`) is one page that answers one question
per trip: **is this a good price to book?** Every change to the page follows
this file. If a change needs to break a rule, update the rule here first.

## 1. Feel

- An **airport departure board**, made calm: one line per flight, fixed
  columns, nothing decorative. It should feel "boringly obvious".
- **Price first.** The biggest thing on every card is the price, and it never
  appears without context (a verdict, a change, a comparison).
- **Details on tap.** Price history and date comparisons are one tap away, never
  on the first screen.
- **Honest about freshness.** The top always says when prices were last checked,
  and turns red when checks stop or Google is not answering.

## 2. Colour (one theme, no light/dark switch)

Warm stone between light and dark. Colour carries meaning only; it is never
decoration.

| Token | Value | Used for |
|---|---|---|
| `--bg` | `#e7e5e0` | Page |
| `--panel` | `#f6f5f2` | Cards, menus |
| `--inset` | `#eeece7` | Board rows, neutral chips, fact boxes |
| `--line` | `#dcd8d0` | Hairlines and borders (1px) |
| `--ink` | `#161513` | Main text and prices |
| `--ink-2` | `#57544e` | Secondary text |
| `--ink-3` | `#807c75` | Labels, captions, small caps |
| `--navy` / `--navy-wash` | `#1f3a5f` / `#dfe5ec` | The one accent: Book button, "your dates", chart line, tags |
| `--down` / `--down-bg` | `#12744a` / `#d9ede1` | **Cheaper**: price fell, lowest seen, savings |
| `--up` / `--up-bg` | `#b3321f` / `#f5ded8` | **Dearer**: price rose, rising fast, warnings |
| `--amber` / `--amber-bg` | `#835800` / `#f1e6c9` | **Above the lowest** (not bad, not great) |

Rules:
- Green always means cheaper and red always means dearer. Nothing else uses them.
- Every colour signal also has words or an arrow (↓ ₹890 today, "Lowest price
  we've seen"), so colour is never the only signal.
- Airline squares use the airline's own colour, white code on top.

## 3. Type

- **Text:** the system sans (`system-ui`, San Francisco, Segoe UI, Roboto).
  No downloaded fonts: the page loads nothing from other sites.
- **Times and flight codes:** the system monospace (`--mono`: ui-monospace,
  SF Mono, Cascadia Mono, Roboto Mono, Menlo, Consolas), like a departure
  board: `10:40 → 15:50`, `6E 1217`.
- **Prices and other numbers:** the sans with tabular figures
  (`font-variant-numeric: tabular-nums`), so they line up without looking
  mechanical.
- **Labels:** 11px small caps, letter-spaced 0.09em, `--ink-3`
  ("ROUND TRIP · FROM BENGALURU", "OUT", "BACK").

| Role | Size / weight |
|---|---|
| Page title | 30px / 650 (26px on phones) |
| Trip name, countdown | 26px / 650 (23px / 22px on phones) |
| Card price | 40px / 650 (34px on phones) |
| Board times (mono) | 18px / 600 (16px on phones) |
| Body | 15px / 400 |
| Secondary | 13–14px |
| Labels, captions | 11–12px |

## 4. Spacing

- A 4px grid. Spacing between and around cards, sections and the page edge
  uses steps of 8: **8, 16, 24, 32, 64**.
- Inside components, 4 and 12 are allowed for tight gaps (icon to text,
  rows in a list).
- Card padding: 24px (16px on phones). Gap between cards: 16px. Gap between
  trips: 32px.
- Corner radius: 20px cards, 12–14px inner boxes, 999px pills.

## 5. Components

- **Trip board:** small-caps route (`BLR ⇄ HKT`), trip name, dates with
  weekdays, and a countdown ("56 days to go") on the right.
- **Fare card head:** who tag (You / Parents), trip type, the big price, one
  verdict pill, today's change on the right with a 30-day trend line (green
  if lower than 30 days ago, red if higher, grey if flat). An optional one-line
  note sits underneath, e.g. how much more the preferred flight costs.
- **Verdict pill** (pick one): "Lowest since tracking began" (green), "₹X above
  the lowest" (amber), "Rising · up ₹X in a day" (red), "Tracking started · no
  movement yet" or "Waiting for the first price check" (neutral), "Not found
  on the last check" (red).
- **Book or wait?** (under the price): verdict title in green (Good time to book),
  amber (Fair time to book) or red (Wait for a better price), the strongest
  reason, and a green–amber–red scale (40/20/40) with a knob at the score.
  "Why?" opens every reason with a BOOK / WAIT / — tag, Google's last 60 days
  for the dates with the typical range shaded green, and a one-line note on
  how it is worked out. Never shown as a percentage or "accuracy".
- **Board rows:** OUT / BACK label, weekday + date, times (mono), airline
  square + flight code (mono) + airline and route, duration, "Direct".
- **Options:** every acceptable flight combination, cheapest first. Show 3,
  then "Show all N options". The cheapest is green with "CHEAPEST", the rest
  show "+₹X". The preferred flight is marked "preferred".
- **Compare dates:** chips first ("51% above a typical Fri–Tue trip", "Return
  Mon: save ₹7,139"). On tap: bars for the same trip in other weeks (yours in
  navy, the cheapest in green) and a 3×3 grid for a day earlier or later
  (yours outlined in navy, significant savings in green). "Significant" means
  at least ₹1,000 or 5%, whichever is larger.
- **Price history:** step line in navy with a light navy fill (prices jump;
  never smooth them), a crosshair tooltip, and three fact boxes: lowest,
  highest, when tracking began.
- **Buttons:** Book is the only filled button (navy pill). Refresh and the menu
  (Setup, Log out) are round icon buttons.

## 6. App icon and install

- Icon: the airport-signage navy (`#1f3a5f`) square, full bleed, with the
  Material "flight" plane in `--panel` stone, turned 45° (take-off). Source:
  `static/icon.svg`; PNGs at 512, 192, 180 (Apple) and 32 (favicon).
- Installed app: name "Flight Prices", short name "Flights", standalone,
  background and theme colour `--bg` (`#e7e5e0`).

## 7. Writing

- Plain words, short. "Lowest price we've seen", not "Price index minimum".
- Every date has its weekday: "Fri 22 Jan".
- Rupees with Indian grouping: ₹1,23,456. Compact form only where space is
  tight: ₹43.7k.
- Say what a comparison is based on: "Cheapest direct round trip on any airline".

## 8. Do and don't

- ✅ Keep one theme. ✅ Keep every price next to its context.
- ✅ Say what advice is based on; call it a guide, not a prediction.
- ✅ Use step lines for price history. ✅ Hide details behind a tap.
- ✅ Insert all text as text (never as HTML), and load nothing from other sites.
- ❌ No dark mode, no neon, no gradients for decoration.
- ❌ No smoothed or area-only charts for prices, and no pie charts.
- ❌ No new colours for meaning. Use the five above.
- ❌ Nothing on the first screen that you don't need in order to decide whether to book.
