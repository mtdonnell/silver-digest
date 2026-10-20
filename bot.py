#!/usr/bin/env python3
"""
bot.py  --  Ask the Telegram bot for sales figures over any date range.

    !sales today
    !sales last week
    !sales last 10 days
    !items last 7 days

Why this can work at all: the portal's report URL takes a startDate and an
endDate, so one day and one whole month cost the same single request. The
nightly job just passes the same date twice. Everything here reuses the
scraping and parsing already proven by that job.

SECURITY -- the important part
------------------------------
This bot answers with live business revenue. Anyone can find a Telegram bot's
username and message it, so every incoming message is checked against
TELEGRAM_CHAT_ID and silently ignored otherwise. Silently, not with a refusal:
a reply confirms the bot is live and worth attacking. Unauthorised attempts are
logged so you can see them.

Run as a long-lived service (systemd unit silver-bot.service).
"""

from __future__ import annotations

import calendar
import logging
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta

import notify
import silver_report as sr

log = logging.getLogger("silver.bot")

POLL_TIMEOUT = 50          # long-poll; Telegram holds the request open
STALE_AFTER = 600          # ignore commands older than 10 min (e.g. after downtime)
OFFSET_FILE = sr.DATA / "bot_offset.txt"

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

HELP = """<b>Sales</b>  (!sales or /sales)

<pre>!sales
!sales today
!sales yesterday
!sales last week
!sales last 7 days
!sales last 10 days
!sales this month
!sales last month
!sales ytd</pre>

Bare <code>!sales</code> is today. Give it 20-30 seconds.

<b>Items</b>  (!items or !bestsellers)

<pre>!items
!items last 7 days
!items last week
!items yesterday</pre>

Top 15 per shop by name, qty sold, and dollars, plus an Excel sheet of
every item, shops side by side with totals at the bottom. Same date phrases as sales. Items you rang as
open-price / unnamed will not show — only what is in the item list.

<b>/history</b> — local daily totals
<b>/help</b> — this message"""


# ===========================================================================
#  DATE RANGE PARSING
# ===========================================================================

def month_range(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def parse_range(text: str, today: date) -> tuple[date, date, str] | None:
    """
    Turn a human phrase into (start, end, label). None if unrecognised.

    Deliberately conservative: anything it cannot parse confidently gets a
    help message rather than a guess, because silently answering for the wrong
    period is worse than answering nothing.
    """
    t = " ".join(text.lower().split())

    if t in ("yesterday", "y"):
        d = today - timedelta(days=1)
        return d, d, d.strftime("%a %m/%d")
    if t == "today":
        return today, today, "today"

    if t in ("this month", "month to date", "mtd"):
        s, _ = month_range(today.year, today.month)
        return s, today, f"{today:%B %Y} so far"
    if t == "last month":
        first_this = today.replace(day=1)
        prev = first_this - timedelta(days=1)
        s, e = month_range(prev.year, prev.month)
        return s, e, f"{prev:%B %Y}"

    if t in ("this week", "week to date", "wtd"):
        s = today - timedelta(days=today.weekday())
        return s, today, "this week"
    if t == "last week":
        this_mon = today - timedelta(days=today.weekday())
        s = this_mon - timedelta(days=7)
        return s, s + timedelta(days=6), f"week of {s:%m/%d}"

    if t in ("this year", "ytd", "year to date"):
        return date(today.year, 1, 1), today, f"{today.year} YTD"
    if t == "last year":
        y = today.year - 1
        return date(y, 1, 1), date(y, 12, 31), str(y)

    # rolling window: "last 10 days", "past 7d", "last 10"
    m = re.fullmatch(r"(?:last|past|previous)\s+(\d+)\s*(?:d|day|days)?", t)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 366:
            s = today - timedelta(days=n - 1)
            return s, today, f"last {n} day{'s' if n != 1 else ''}"
        return None

    # explicit range: "2025-01-01 to 2025-01-31"  (also accepts .. and -)
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?:to|\.\.|through|-)\s*(\d{4}-\d{2}-\d{2})", t)
    if m:
        try:
            s, e = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        except ValueError:
            return None
        if s > e:
            s, e = e, s
        return s, e, f"{s.isoformat()} to {e.isoformat()}"

    # single ISO day
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        try:
            d = date.fromisoformat(t)
        except ValueError:
            return None
        return d, d, d.strftime("%a %m/%d")

    # "2025-01" or "2025/01"
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", t)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            s, e = month_range(y, mo)
            return s, e, f"{s:%B %Y}"
        return None

    # "january 2025" / "jan 2025" / bare "january" (most recent one)
    m = re.fullmatch(r"([a-z]+)\.?(?:\s+(\d{4}))?", t)
    if m and m.group(1) in MONTHS:
        mo = MONTHS[m.group(1)]
        if m.group(2):
            y = int(m.group(2))
        else:
            # No year given: the most recent occurrence of that month.
            y = today.year if mo <= today.month else today.year - 1
        s, e = month_range(y, mo)
        return s, e, f"{s:%B %Y}"

    # "january 1 2025 to january 31 2025" is not supported on purpose -- too
    # many ways to get it subtly wrong. ISO form above covers it unambiguously.
    return None


# ===========================================================================
#  HANDLERS
# ===========================================================================

def authorised() -> set[str]:
    return {c.strip() for c in
            (notify._env_list("TELEGRAM_CHAT_ID") or []) if c.strip()}


def reply(chat_id: str, html: str) -> None:
    """Send one HTML message. Truncates rather than risking a 4096 rejection."""
    if len(html) > notify.TELEGRAM_LIMIT:
        html = html[:notify.TELEGRAM_LIMIT - 20] + "\n…(truncated)"
    notify.telegram_call("sendMessage", {
        "chat_id": chat_id, "text": html,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    })


def do_sales(chat_id: str, start: date, end: date, label: str) -> None:
    span_days = (end - start).days + 1
    reply(chat_id, f"Pulling <b>{label}</b> ({span_days} day"
                   f"{'s' if span_days != 1 else ''})… this takes 20-30s.")

    stores = sr.get_stores()
    results = []
    for store in stores:
        try:
            results.append(sr.run_store(store, start, end, headless=True))
        except Exception as e:
            log.error("bot: %s failed: %s", store.name, e)
            results.append(sr.StoreResult(
                store=store.name, business_day=f"{start}..{end}",
                ok=False, error=f"{type(e).__name__}: {e}"))

    subject, _short, long_body = sr.build_digest(results, start, label=label)
    if not all(r.ok for r in results):
        subject = "[CHECK] " + subject

    # Ad-hoc queries deliberately do NOT touch history.csv. That file holds one
    # row per store per DAY; writing a month-aggregate row into it would
    # corrupt every later total.
    #
    # Reuse the digest chunker so a multi-store answer that runs past
    # Telegram's 4096-character limit splits on line boundaries instead of
    # being cut mid-number.
    for chunk in notify._telegram_chunks(subject, long_body):
        notify.telegram_call("sendMessage", {
            "chat_id": chat_id, "text": chunk,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        })


_ITEMS_FILLER = re.compile(
    r"^(?:best\s*sellers?|bestsellers|top(?:\s+\d+)?)\s+", re.I)


def do_items(chat_id: str, start: date, end: date, label: str) -> None:
    span_days = (end - start).days + 1
    reply(chat_id, f"Pulling items for <b>{label}</b> ({span_days} day"
                   f"{'s' if span_days != 1 else ''})… 20-30s.")
    stores = sr.get_stores()
    results = []
    for store in stores:
        try:
            results.append(sr.run_store_items(store, start, end, headless=True))
        except Exception as e:
            log.error("bot items: %s failed: %s", store.name, e)
            results.append(sr.StoreResult(
                store=store.name, business_day=f"{start}..{end}",
                ok=False, error=f"{type(e).__name__}: {e}"))
    subject, body = sr.build_items_digest(results, label)
    for chunk in notify._telegram_chunks(subject, body):
        notify.telegram_call("sendMessage", {
            "chat_id": chat_id, "text": chunk,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        })

    # The digest only fits the top sellers; the attachment is every item.
    try:
        path = sr.write_items_file(results, start, end)
        if path:
            notify.telegram_send_document(
                chat_id, path, caption=f"Every item sold, {label}")
    except Exception as e:
        log.error("bot items: CSV attachment failed: %s", e)
        reply(chat_id, "Couldn't attach the full item list "
                       f"({notify._html_escape(str(e))[:200]}).")


def do_history(chat_id: str) -> None:
    import csv
    if not sr.HISTORY.exists():
        reply(chat_id, "No history file yet — it fills up as the nightly job runs.")
        return
    with sr.HISTORY.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        reply(chat_id, "History file is empty.")
        return
    days = sorted({r["business_day"] for r in rows})
    lines = [f"{len(rows)} rows across {len(days)} day(s)",
             f"from {days[0]} to {days[-1]}", ""]
    for r in rows[-10:]:
        lines.append(f"{r['business_day']}  {r['store']:<9} "
                     f"{r.get('gross_sales','?'):>10}")
    reply(chat_id, "<b>Local history</b>\n<pre>"
          + notify._html_escape("\n".join(lines)) + "</pre>")


def handle(msg: dict) -> None:
    chat = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat or not text:
        return

    allowed = authorised()
    if chat not in allowed:
        # Silence, not a refusal: replying confirms the bot is live.
        log.warning("ignoring message from unauthorised chat %s: %r",
                    chat, text[:60])
        return

    sent = msg.get("date", 0)
    age = time.time() - sent
    if age > STALE_AFTER:
        log.info("skipping stale command (%.0fs old): %r", age, text[:60])
        return

    cmd, _, rest = text.partition(" ")
    cmd = cmd.lower().lstrip("/!").split("@")[0]    # /sales or !sales
    rest = rest.strip()
    log.info("command %r args %r from %s", cmd, rest, chat)

    if cmd in ("help", "start"):
        reply(chat, HELP)
    elif cmd == "history":
        do_history(chat)
    elif cmd in ("sales", "sale"):
        _run_dated(chat, rest, do_sales, default="today")
    elif cmd in ("items", "item", "bestsellers", "bestseller", "best"):
        rest = _ITEMS_FILLER.sub("", rest).strip()
        _run_dated(chat, rest, do_items, default="today")
    else:
        reply(chat, f"Unknown command {notify._html_escape(cmd)!r}.\n\n{HELP}")


def _run_dated(chat: str, rest: str, fn, default: str) -> None:
    phrase = rest or default
    parsed = parse_range(phrase, date.today())
    if not parsed:
        reply(chat, f"Couldn't read {notify._html_escape(phrase)!r} as a date "
                    f"range.\n\n{HELP}")
        return
    start, end, label = parsed
    if start > date.today():
        reply(chat, "That range is in the future.")
        return
    fn(chat, start, end, label)


# ===========================================================================
#  MAIN LOOP
# ===========================================================================

def read_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def write_offset(v: int) -> None:
    try:
        OFFSET_FILE.write_text(str(v))
    except Exception as e:
        log.warning("could not persist offset: %s", e)


def main() -> int:
    # Its own file, and via silver_report's helper -- a bare basicConfig here
    # would be a no-op, because importing silver_report used to configure the
    # root logger first and every bot line silently went to the wrong file.
    sr.setup_logging(sr.DATA / "bot.log")

    me = notify.telegram_call("getMe", {})["result"]
    allowed = authorised()
    if not allowed:
        log.critical("TELEGRAM_CHAT_ID is not set -- refusing to start. Without "
                     "it the bot would answer anyone who messages it.")
        return 1
    log.info("bot @%s up; answering only chat id(s): %s",
             me.get("username"), ", ".join(sorted(allowed)))

    offset = read_offset()
    while True:
        try:
            # Socket timeout must exceed the long-poll window, or every poll
            # aborts on schedule and the bot spends its life retrying.
            res = notify.telegram_call("getUpdates", {
                "offset": str(offset), "timeout": str(POLL_TIMEOUT),
                "allowed_updates": '["message"]',
            }, timeout=POLL_TIMEOUT + 15)["result"]
        except Exception as e:
            log.warning("getUpdates failed (%s) -- retrying in 15s", e)
            time.sleep(15)
            continue

        for upd in res:
            offset = max(offset, upd["update_id"] + 1)
            write_offset(offset)      # persist BEFORE handling, so a crash
                                      # mid-command cannot loop on it forever
            try:
                if "message" in upd:
                    handle(upd["message"])
            except Exception:
                log.error("handler blew up:\n%s", traceback.format_exc())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
