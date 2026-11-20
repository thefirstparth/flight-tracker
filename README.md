# Flight price tracker (direct flights only)

Checks the direct flights in `watches.toml` every 2 hours and messages you on
Telegram (or email) when a price drops, reaches your target, climbs fast, or a
watched flight disappears. Every price is saved to `data/history.csv`, a
dashboard page is rebuilt after each check, and a summary with the dashboard
attached arrives each morning at about 08:23 IST.

Prices come from Google Flights through the open-source
[`flights`](https://github.com/punitarani/fli) library, so they are the same
prices Google shows. They are not more accurate. The difference is that you
decide how often prices are checked and what counts as worth an alert.
Always confirm on the airline's own website before booking.

## How it differs from Google Flights tracking

| | Google Flights tracking | This tracker |
|---|---|---|
| Prices | Google's | Same (Google's) |
| How often checked | Not published | Every 2 hours |
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

## Set it up in its own repository (about 15 minutes)

1. **Create a new repository** on the GitHub account that should run it (yours
   or a friend's). Make it **private**, otherwise your travel dates are public.
2. **Copy everything in this folder to the top level of that repository**,
   including the hidden `.github` folder:
   ```bash
   git clone https://github.com/<account>/<new-repo>.git
   cp -r flight-tracker/. <new-repo>/
   cd <new-repo> && git add -A && git commit -m "Flight tracker" && git push
   ```
   (In this folder the schedule is switched off. It only runs when
   `.github/workflows/track.yml` sits at the top of a repository.)
3. **Create a Telegram bot** (free):
   - In Telegram, open **@BotFather**, send `/newbot`, pick a name. It gives you
     a **token** like `123456:ABC-DEF...`.
   - Open your new bot and send it any message (e.g. "hi").
   - In a browser open `https://api.telegram.org/bot<TOKEN>/getUpdates` and find
     `"chat":{"id":123456789` — that number is your **chat id**.
4. **Add the secrets**: in the repository, *Settings → Secrets and variables →
   Actions → New repository secret*:
   - `TELEGRAM_BOT_TOKEN` = the token
   - `TELEGRAM_CHAT_ID` = the chat id
   - Optional email instead of (or as well as) Telegram: `EMAIL_TO` (your
     address) and `EMAIL_PASSWORD` (a Gmail **app password**, from
     Google Account → Security → 2-Step Verification → App passwords).
     For a non-Gmail account also set `SMTP_HOST`/`SMTP_PORT` in the workflow.
5. **Test it**: *Actions* tab → enable workflows if asked → **Track flight
   prices** → *Run workflow* → mode `test-notify`. You should get a test
   message. Then run it once with mode `check`: you get a "Flight tracker
   started" message with every current price.

From then on it runs by itself every 2 hours. Each run commits the new prices
and the rebuilt dashboard to the repository.

## Changing what is watched

Edit `watches.toml` (the GitHub web editor is fine). Each watch is a route and
dates, and either exact flights (`flights = ["6E 1457", "6E 1458"]`) or the
cheapest direct option (leave `flights` out). Add `target = 26000` to be told
when a price reaches that number. Alert thresholds are in `[settings]`.
Watches stop by themselves after their departure date.

## Cost (free)

- A run normally takes 1–3 billed minutes, about 13 runs a day: roughly
  **400–1,200 of the 2,000 free minutes a month** on a private repository
  (20–60%). On days when Google answers badly a run can take longer; the
  tracker stops retrying after 8 minutes. Public repositories have unlimited
  minutes but expose your plans. To use fewer minutes, check every 3 hours
  (change `*/2` to `*/3` in the workflow).
- Checking every hour instead would double that (40–80%), close to the 80%
  line, so 2 hours is the default. Change it in `.github/workflows/track.yml`.
- Price history grows by about 1 MB a year; the dashboard page stays well
  under 1 MB. Telegram and Gmail are free.

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
- **Unofficial access.** The library imitates the Google Flights website. Google
  answers about one request in three with an error (and with "too many
  requests" when asked a lot), so each request is retried up to 5 times. A
  check where Google did not answer is not counted as a flight going missing. If Google changes its format the tracker stops getting prices.
  After 3 empty checks in a row it messages you and the run shows as failed;
  updating the version in `requirements.txt` usually fixes it. Automated access
  is a grey area under Google's terms; with the shipped watch list a check
  makes about 15 requests (round trips: one for the outbound list, then one per
  watched outbound flight and the 2 cheapest), plus retries.
- **Gaps between checks.** A fare that appears and disappears within 2 hours
  can be missed.
- **Round trips** are priced as Google prices them. The "cheapest direct round
  trip" can combine two airlines (e.g. IndiGo out, Air India Express back),
  which usually means two separate tickets.
- **GitHub schedules** can start a few minutes late at busy times.
