# Flight price tracker (direct flights only)

A small web app for two people sharing one password. About every 30 minutes
it checks the direct flights in `watches.toml` (exact flights, a choice such
as "IndiGo 6E 1217 or Akasa QP 623", or any direct flight one way) and shows
them on a fare-board dashboard.

**Telegram** only messages you when a tracked price changes (a drop or rise
of ₹500 or 3%, whichever is larger, since the last message), so you know when
to book. Nothing else is sent unless you turn it on in `[settings] alerts`.

**On the dashboard only**, twice a day, each trip is compared with:
- the same trip (same weekday, same length) in the other weeks within 30 days
  either side, e.g. "51% above a typical Fri–Tue trip";
- leaving or returning one day earlier or later (a 3×3 grid), with savings
  shown, e.g. "Return Mon: save ₹7,139".
These use the cheapest direct round trip on any airline for each date pair
(Google's date grid), which is the right yardstick for "what does this trip
cost", not your exact flights.

**Book or wait?** Each card also shows a verdict (Good time to book / Fair
time to book / Wait for a better price) on a green–amber–red scale, with the
reasons on tap. It is a transparent guide, not a forecast model, built from:
Google Flights' own typical price range for the trip (based on its last 12
months), Google's last 60 days of prices for your dates (both read from the
same Google answer as the flight prices, so no extra requests), whether your
dates are a peak compared with other weeks, and the days left before
departure (fares are lowest on average about 54–78 days out). Google's own
"cheaper dates" suggestion appears under Compare dates.

Prices come from Google Flights through the open-source
[`flights`](https://github.com/punitarani/fli) library, so they are the same
prices Google shows. Always confirm on the airline's site before booking.

Everything runs on free plans with no card: **Vercel** (app and checks),
**Upstash Redis** (stored prices) and **Upstash QStash** (the 10-minute
schedule), both added from inside Vercel, plus **Telegram**.

## Install it like an app

The dashboard is an installable web app (it has an app manifest, icons and a
small service worker, which also keeps the last dashboard for when you are
offline and forgets it when you log out).

- **iPhone (Safari):** open the address, tap Share → **Add to Home Screen**.
- **Android (Chrome):** open the address, tap ⋮ → **Install app** (or "Add to
  Home screen").
- **Mac or Windows (Chrome or Edge):** open the address and click the install
  icon at the right of the address bar (or ⋮ → **Install Flight Prices**).
  On a Mac with Safari: File → **Add to Dock**.

Log in once inside the installed app; it stays logged in for 90 days.

## Put it live on Vercel (about 15 minutes, once)

1. **Vercel account** (Hobby, free): sign up at vercel.com.
2. **Import the code**: *Add New → Project → Import Git Repository*, pick the
   repository holding this folder. Set **Root Directory** to `flight-tracker`
   (skip if the folder's contents are at the top of their own repository) and
   Framework Preset **Other**. If the code is not on the repository's main
   branch, set *Settings → Git → Production Branch* to the branch that has it.
3. **Storage**: in the project, *Storage → Create Database → Upstash →
   Redis*, Free plan, connect it to the project. This adds the connection
   settings automatically.
4. **Schedule**: *Integrations → Marketplace → Upstash → QStash*, Free plan,
   connect it to the project. (Alternative without QStash: a free cron-job.org
   job calling `https://<your-app>/api/cron` every 10 minutes, method POST,
   with header `Authorization: Bearer <CRON_SECRET>`.)
5. **Settings → Environment Variables** (Production):
   - `DASHBOARD_PASSWORD`: the password you and your wife will use
   - `CRON_SECRET`: a long random text (only the scheduler uses it)
   - `TELEGRAM_BOT_TOKEN`: from @BotFather (`/newbot`)
   - `TELEGRAM_CHAT_ID`: message your bot once, then open
     `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": …}`
6. **Redeploy** (*Deployments → ⋯ → Redeploy*) so the settings take effect.
7. Open `https://<your-app>.vercel.app`, log in, open **Setup**:
   every line should be ✓. Press **Create / update the 10-minute schedule**
   and **Send a test Telegram message**. Then **Check now** on the dashboard.

`/health` shows whether storage is connected and how many watches loaded,
without any prices. Vercel's own daily cron (in `vercel.json`) sends the
morning summary.

**Adding or changing flights:** edit `watches.toml` (or ask Claude to) and
push; Vercel redeploys by itself. A web form for this can come later.

## How it differs from Google Flights tracking

| | Google Flights tracking | This tracker |
|---|---|---|
| Prices | Google's | Same (Google's) |
| How often checked | Not published | About every 30 minutes |
| When you're told | "Significant" changes, by email | Drop of ₹500 or 3% (whichever is larger) since the last alert, target reached, fast rise, flight gone |
| Where | Email | Telegram and/or email |
| Cheapest direct option across airlines | Per date, one way | Also for round trips |
| Price history | A graph per tracked flight | Charts per trip, every direct flight on the route, round trip vs two one-ways, alert log |
| Maintenance | None | May need a library update if Google changes something |

## The dashboard

`dashboard/index.html` is one self-contained page (no internet needed, nothing
loaded from other sites, so your plans stay private). It shows, per trip:

- the best direct price now, the lowest seen, and whether **two one-way tickets
  beat the round trip**;
- price charts for every watch (24 h / 7 days / 30 days / all), with a
  crosshair that reads out every price at a moment, your targets as dashed
  lines, and a table view;
- each watch's change over the chosen range, where today's price sits between
  the lowest and highest seen, and a status (lowest seen, rising, not found);
- **every direct flight on the route right now**, with times, durations and
  prices, like Google Flights' result list, marking the ones you watch;
- the alerts that were sent.

Light and dark themes, phone and desktop layouts. Preview the design with
made-up history: open `dashboard/demo.html` (download it from GitHub and open
it in a browser).

**How to see it:**
1. **Telegram/email (default):** the morning summary has the page attached.
   Tap it and open it in your browser (Chrome, Brave, Safari). If a file
   preview opens it without charts, it still lists every current price.
2. **From the repository:** download `dashboard/index.html` and open it.
3. **As a website (not set up):** GitHub Pages needs a paid plan for private
   repositories, and a public repository would publish your travel dates.
   Cloudflare Pages is free and can host it at an unlisted address, but needs a
   Cloudflare account and token, so it is left for later.

Rebuild it by hand with `python dashboard.py` (or `python dashboard.py --demo`).

## Other ways to run it

- **Your own computer:** see "Run it on your own computer" below; prices are
  saved in `data/` and the dashboard is written to `dashboard/index.html`.
- **GitHub Actions (second checker, on):** `.github/workflows/track.yml` runs
  the same check from GitHub's servers every 30 minutes, using the Vercel app's
  storage (four repository secrets: `KV_REST_API_URL`, `KV_REST_API_TOKEN`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Both share one lock and the same
  due rules, so nothing is searched or alerted twice; each pauses on its own
  when Google refuses it. Free for a public repository.

## Changing what is watched

Edit `watches.toml` (the GitHub web editor is fine). Each watch is a route and
dates, and either exact flights (`flights = ["6E 1457", "6E 1458"]`) or the
cheapest direct option (leave `flights` out). Add `target = 26000` to be told
when a price reaches that number. Alert thresholds are in `[settings]`.
Watches stop by themselves after their departure date.

## Cost (free) and free-plan usage

Estimates for the shipped watch list, checked every 10 minutes (each watch
refreshed about every 30). All stay well under 80% of the free limits:

| Free limit | Used | Share |
|---|---|---|
| QStash: 1,000 calls a day | 144 | 14% |
| Vercel Hobby: 1 million function calls a month | ~4,500 | under 1% |
| Vercel Hobby: 4 CPU-hours a month | ≤ 1 hour (0.8 s CPU per call measured, most of it start-up) | ≤ 25% |
| Upstash Redis: 500,000 commands a month | ~90,000 (10–20 per check, plus page views) | ~18% |
| Upstash Redis: 256 MB | well under 1 MB a year (only price changes are stored) | < 1% |

Each call stops starting new searches after 45 seconds, inside Vercel's 60 s
limit; whatever did not fit is checked on the next call. Telegram is free.

## Run it on your own computer instead

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python tracker.py --dry-run      # check prices, print only
python tracker.py                # check, save, alert (set the TELEGRAM_* variables first)
python dashboard.py --demo       # preview page with made-up history
python -m unittest -v            # offline tests
```
Needs Python 3.11 or newer. Schedule it with cron (macOS/Linux) or Task
Scheduler (Windows) if you want it to run automatically.

## Limits to know

- **Same data as Google.** Fares only on airline sites or with app/bank coupons
  are not seen.
- **Unofficial access.** The library imitates the Google Flights website.
  Google answers about half of all requests with a short error, at random, so
  each request is retried quickly (up to 6 tries). A check where Google did not
  answer, or that ran out of time, is not counted as a flight going missing.
  If Google changes its format the tracker stops getting prices; after 3 empty
  checks in a row it messages you, and updating the version in
  `requirements.txt` usually fixes it. Automated access is a grey area under
  Google's terms; a full check makes about 15 requests plus retries.
- **Flexible dates** use Google's date grid: the cheapest direct price per day,
  without the flight numbers.
- **Gaps between checks.** A fare that appears and disappears within 30 minutes
  can be missed.
- **Round trips** are priced as Google prices them. The "cheapest direct round
  trip" can combine two airlines (e.g. IndiGo out, Air India Express back),
  which usually means two separate tickets.
- **Schedules** (QStash, GitHub) can start a few minutes late at busy times.
