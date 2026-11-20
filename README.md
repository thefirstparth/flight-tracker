# Flight price tracker (direct flights only)

Checks the direct flights in `watches.toml` every 2 hours and messages you on
Telegram (or email) when a price drops, reaches your target, climbs fast, or a
watched flight disappears. Every price is saved to `data/history.csv`, and a
summary arrives each morning at about 08:23 IST.

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
| Maintenance | None | May need a library update if Google changes something |

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

From then on it runs by itself every 2 hours.

## Changing what is watched

Edit `watches.toml` (the GitHub web editor is fine). Each watch is a route and
dates, and either exact flights (`flights = ["6E 1457", "6E 1458"]`) or the
cheapest direct option (leave `flights` out). Add `target = 26000` to be told
when a price reaches that number. Alert thresholds are in `[settings]`.
Watches stop by themselves after their departure date.

## Cost (free)

- Each run takes about 1–2 billed minutes, about 13 runs a day: roughly
  **400–800 of the 2,000 free minutes a month** on a private repository
  (20–40%). Public repositories have unlimited minutes but expose your plans.
- Checking every hour instead would double that (40–80%), close to the 80%
  line, so 2 hours is the default. Change it in `.github/workflows/track.yml`.
- Price history grows by about 1 MB a year. Telegram and Gmail are free.

## Run it on your own computer instead

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python tracker.py --dry-run      # check prices, print only
python tracker.py                # check, save, alert (set the TELEGRAM_* variables first)
python -m unittest -v            # offline tests
```
Needs Python 3.11 or newer. Schedule it with cron (macOS/Linux) or Task
Scheduler (Windows) if you want it to run automatically.

## Limits to know

- **Same data as Google.** Fares only on airline sites or with app/bank coupons
  are not seen.
- **Unofficial access.** The library imitates the Google Flights website. Google
  answers about one request in three with an error, so each search is retried
  up to 5 times. If Google changes its format the tracker stops getting prices.
  After 3 empty checks in a row it messages you and the run shows as failed;
  updating the version in `requirements.txt` usually fixes it. Automated access
  is a grey area under Google's terms; with the shipped watch list this makes
  roughly 15–60 requests every 2 hours (a round trip costs one request per
  outbound flight, plus retries).
- **Gaps between checks.** A fare that appears and disappears within 2 hours
  can be missed.
- **Round trips** are priced as Google prices them. The "cheapest direct round
  trip" can combine two airlines (e.g. IndiGo out, Air India Express back),
  which usually means two separate tickets.
- **GitHub schedules** can start a few minutes late at busy times.
