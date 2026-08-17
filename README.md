# silver-digest

**Scheduled sales reporting for NCR Voyix Silver (formerly NCR Silver).**

The Back Office has no scheduled reports. If you want yesterday's numbers you
log in and pull them by hand, every day, for every location. There is no email
digest, no subscription, no API you can point at it.

This logs in for you, downloads the Daily Summary CSV, records it to a history
file you own, and sends you the numbers on Telegram, email, SMS or WhatsApp —
on a schedule you pick. A companion bot answers ad-hoc questions like
`/sales January 2025` in about twenty seconds.

It is running in production for a two-location retailer.

```
Sales Sat 03/14  $3,400

Main Street $1,850.00
Second Shop $1,550.00
TOTAL $3,400.00
```

---

## Why this is harder than it looks

If you're thinking of writing your own, these are the things that cost time.
All of them are handled here.

**The portal is a Sencha ExtJS app with no `<form>` element.** The Log In
control is a `<button type="button">`, so the conventional
`input[type=submit]` selector matches nothing. ExtJS also emits two kinds of id:
author-assigned ones like `#Login-btnEl` are stable, while auto-generated ones
(`ext-comp-1234`, `ext-gen-56`, `button-1060`) change between sessions. Build a
selector on one of those and it works today and fails tomorrow.

**The report is nested two iframes deep.** The ExtJS shell holds
`ReportForm.aspx`, which holds a Telerik report viewer. The outer page contains
none of the numbers. Worse, the shell keeps the requested date in *its own*
input fields — so a date check against the top document passes while the report
underneath shows something else entirely.

**There is a direct report URL**, which is the single biggest simplification
available: `ReportForm.aspx?reportType=ds&startDate=…&endDate=…` renders the
Daily Summary for any range. That skips the menu clicks and the date picker,
the two most fragile steps. It also means one day and one whole year cost the
same request — which is what makes historical queries practical.

**The Export button does not work.** Not a selector problem; the handler is a
stub:

```js
anchor.onclick         = ReportTextButton_Click
ReportTextButton_Click → this.Exec()
ToolbarButton_Exec     → if (Enabled && Toolbar) Toolbar.OnCommand(this)
ReportToolbar_OnCommand(b) { alert("OnCommand: " + b.CommandName); }   // stub
```

Nothing overrides `OnCommand`, so a click either does nothing or raises a dialog
that automation auto-dismisses. Either way, no file. The viewer object exposes
the real mechanism, and that is what this uses:

```js
ExportInternal = function (format) {
    if (this.exportUrl) window.open(this.exportUrl + "&ExportFormat=" + format, "_self");
}
```

`exportUrl` must be read off the live viewer at run time — it carries a
per-render `instanceID` that is stale the moment the report re-renders.

**There are two different CSVs.** Clicking Export in the UI gives a header row
with plain numbers. The export URL — the one that actually works — gives a
**headerless** file in the report's visual column order, with currency symbols
and accounting negatives like `($4.02)`. So the parser reads by position.

**The row width is not constant.** Observed at both 66 and 63 fields, because it
grows with how many discount names, price overrides and cleared-item lines the
period produced. Anything relying on a fixed index near the end of the row will
eventually read the wrong cell.

### Reading a headerless CSV by position, safely

Positional parsing is normally a bad idea. What makes it safe here is that every
run is checked against the report's own arithmetic:

```
gross − discounts − overrides − returns = net sales
net sales + tax                         = ticket total
```

Seven of the ten mapped columns appear in those identities. If the portal
reorders anything, the sums stop reconciling and the run **fails loudly** rather
than filing a confident wrong answer. That is stronger than trusting a header
string, which can be renamed with no signal at all.

The log says which identities actually ran, so "reconciles" means something
specific rather than "nothing objected":

```
totals check: 2/2 identities verified (gross-discounts-overrides-returns; net+tax)
```

A row where *no* identity could be evaluated is refused outright.

The second identity is exact even over a full year. The first drifts slightly
(~0.01%) when the portal aggregates a long range, so the tolerance is relative
rather than a flat few cents — a fixed tolerance rejects every multi-month
query, while a column shift misreads by orders of magnitude and is still caught.

**That guard also found a column.** One field was unmappable because it was zero
in every sample. Then a month-long query failed to reconcile by exactly the
amount of that month's only refund; searching the row for that value gave a
single candidate. A full-year pull confirmed it — Total ÷ Quantity matched
Average exactly. If you adapt this to another report, that's the technique: make
the guard fail, then look for the discrepancy as a literal value in the row.

---

## Install

Python 3.10+.

```bash
git clone https://github.com/mtdonnell/silver-digest
cd silver-digest
python3 -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
cp env.example .env
```

Create a **report-only Back Office user for each location** and use those
credentials, not your owner login. Rotating your own password then never breaks
the automation, and a leaked bot credential can't change prices or refund
transactions.

### 1. Prove delivery first

Delivery and scraping fail for unrelated reasons; debugging both at once is
miserable. Telegram takes about two minutes and needs no SMTP or carrier:

1. Message **@BotFather**, send `/newbot`, answer two prompts
2. Paste the token into `.env` as `TELEGRAM_BOT_TOKEN`
3. **Message your new bot.** Not optional — a bot may not message you first
4. `python silver_report.py --telegram-setup` → prints your `TELEGRAM_CHAT_ID`
5. `python silver_report.py --test-notify`

That sends a realistic digest from canned numbers with no browser involved, and
prints what a *failed* capture looks like so you can see the alert format before
you need it.

### 2. Then the real thing

```bash
python silver_report.py --date 2026-08-15 --show    # visible browser
python silver_report.py                             # yesterday
python silver_report.py --today
```

Compare the first run against the portal by eye. The script refuses to continue
unless the requested date actually appears in the report frame, which catches
the dangerous failure — a broken date selector exporting the portal's *default*
range, which would parse perfectly while being the wrong day.

---

## Delivery channels

`DIGEST_CHANNELS=telegram,smtp` — any combination, and each is attempted
independently so one misconfigured channel never suppresses another.

| Channel | Cost | Setup | Gets |
|---|---|---|---|
| `telegram` | free | ~2 min | Full breakdown |
| `smtp` | free | ~10 min (app password) | Full breakdown |
| `sms_gateway` | free | ~5 min | Short form, via your carrier's email-to-SMS bridge |
| `twilio_sms` | ~$0.008/msg | days (A2P 10DLC) | Short form, with delivery receipts |
| `twilio_whatsapp` | ~$0.005/msg | ~a week (Meta verification) | Approved template |

**Use at least two.** With one channel, a broken token means no digest *and* no
warning that there is no digest.

**On WhatsApp:** it only allows free-form business messages inside a 24-hour
window that *the customer* opens by messaging you first. A scheduled digest is
always outside that window, so it can only be sent as a pre-approved template,
by Content SID rather than message body. Twilio's sandbox hides this — it opens
a window when you send the `join` phrase, so free-form sends test green and then
break the next day. This never sends free-form to WhatsApp, so sandbox and
production behave identically. `env.example` has the template setup, including
why the variable count must never vary between sends.

---

## Ask for any date range

Run `bot.py` as a service and message it:

```
/sales January 2025          /sales 2025-01
/sales last month            /sales this month
/sales last week             /sales yesterday
/sales 2025-01-15            /sales 2025-01-01 to 2025-01-31
/sales last year             /sales ytd
/history                     /help
```

A bare month name means the most recent one. Twenty to thirty seconds per
location.

**Only your chat ID gets answers.** Anyone can find a bot's username, and this
one reports live revenue, so every message is checked against
`TELEGRAM_CHAT_ID`. Unauthorised messages are logged and ignored *silently* — a
refusal reply would confirm the bot is live and worth attacking. The bot refuses
to start at all if `TELEGRAM_CHAT_ID` is unset.

Ad-hoc queries never write to `history.csv`, which holds one row per store per
*day*; a month-aggregate row would corrupt every later total.

---

## Scheduling

**systemd** (recommended on Linux). Two timers is a good pattern: one just after
close for the digest you actually read, one overnight as a quiet retry that
updates the record and only messages you if something broke.

```ini
# /etc/systemd/system/silver-close.timer
[Timer]
OnCalendar=Sun,Mon,Tue,Wed,Thu 20:15
OnCalendar=Fri,Sat 21:15
Persistent=false
```

The overnight unit runs with `--quiet-unless-failed` and `Persistent=true`, so a
run missed while the machine was off happens on next boot. Set the machine's
timezone with an **IANA name**, not a UTC offset — an offset is an hour wrong for
half the year, and it silently shifts both the schedule and what "yesterday"
means.

**Docker** — a `Dockerfile` and `docker-compose.yml` are included. Note
`shm_size: 1gb`; Chromium crashes on Docker's default 64MB `/dev/shm`, which is
the most common "worked locally, dies in the container" cause. The included
`scheduler.py` sleeps rather than using cron, because cron in a container runs
with a stripped environment and fails silently.

**Windows** — Task Scheduler. Tick *Run whether user is logged on or not* and
*Wake the computer to run this task*, or a 3am job on a sleeping desktop simply
never fires.

---

## Housekeeping

Both scheduled paths prune when they finish. To preview without deleting:

```bash
python silver_report.py --cleanup            # previews
python silver_report.py --cleanup --force    # actually deletes
```

| What | Kept | Aged by |
|---|---|---|
| Daily exports | 450 days | date in the **filename** |
| Range exports | 14 days | **mtime** |
| Failure screenshots / HTML | 30 days | timestamp in filename |
| `history.csv` backups | newest 5 | — |
| Logs | 5MB × 3 rotations | — |

The two different aging keys matter. Daily exports age by filename date, so
re-pulling an old day doesn't reset its clock. Range exports age by mtime,
because their filename holds the period *queried* — a January 2025 query run
today would look 20 months old and be deleted instantly.

**`history.csv` is never pruned.** It's the only irreplaceable file here.

---

## How it fails, and how you'll know

- A failed capture prefixes the subject `[CHECK]` and names which store broke
  and why. **Silence never means success** — a message goes out either way.
- Channels fail independently. If *every* one fails, the log records it CRITICAL.
- A run that delivers nothing exits non-zero. It never reports success.
- Screenshot and HTML dump of the failure moment land in `failures/`.
- If the export path fails and it falls back to reading the rendered page, the
  digest says `(page-text)` next to the store name. An early warning nobody
  reads isn't one.
- Exit codes: `0` fine · `1` capture failed · `2` crash · `3` captured but
  delivery failed.

If the portal changes, the fix is almost always one string in the `SELECTORS`
block at the top of `silver_report.py`.

---

## Adapting it

`recon.py` is a guided helper for exactly this: it opens a visible browser,
walks you through logging in and reaching the report, inventories every element
**including inside iframes**, and captures the export — printing the first 40
lines of the CSV so you can see the real column layout. That output is what the
selectors and the parser get written against.

If your report differs, `parse_export()` is the one function to change.
Everything downstream is shape-independent.

---

## Files

| File | What it is |
|---|---|
| `silver_report.py` | The job. Log in, export, parse, verify, record, send. |
| `notify.py` | Delivery: Telegram, email, carrier SMS, Twilio SMS, WhatsApp. |
| `bot.py` | Telegram bot for ad-hoc date-range queries. |
| `recon.py` | Discovery helper for adapting to a different portal or report. |
| `scheduler.py` | Clock for the Docker path. |
| `env.example` | Config template, heavily commented. |

---

## Notes

Not affiliated with, endorsed by, or supported by NCR Voyix. It automates
retrieval of *your own* data from a portal you already have credentials for,
using the same requests a browser makes. If NCR changes the Back Office, this
will break, and fixing it is on you.

MIT licensed.
