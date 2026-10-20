#!/usr/bin/env python3
"""
silver_report.py  --  PHASE 2: The nightly job.

Logs into NCR Silver Back Office for each store, opens Results > Store Summary
for a given business day, captures the numbers, appends them to a local history
file, and emails/texts you a digest.

Designed to be run unattended by cron / Task Scheduler.

    python silver_report.py                # yesterday (normal nightly run)
    python silver_report.py --date 2026-08-15
    python silver_report.py --today
    python silver_report.py --show         # visible browser, for debugging
    python silver_report.py --test-notify  # canned digest, no browser --
                                           # use this to prove delivery works
                                           # before trusting the scraper

Configuration lives in a .env file next to this script. See env.example.
Delivery (email / SMS / WhatsApp) lives in notify.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import logging.handlers
import os
import pathlib
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from playwright.sync_api import Frame, Page, sync_playwright

import notify

HERE = pathlib.Path(__file__).resolve().parent


def load_env() -> None:
    """Minimal .env loader so you don't need an extra dependency."""
    envfile = HERE / ".env"
    if not envfile.exists():
        return
    for line in envfile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # setdefault, not assignment: a real environment variable (what Docker
        # passes in) must win over the file.
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()   # before anything reads DATA_DIR or opens the log file

# Everything the program PRODUCES goes here, and nothing it produces goes
# anywhere else. In a container this is a bind-mounted volume, so history.csv
# and the raw exports survive an image rebuild -- which matters, because that
# file is the part of this project you cannot regenerate.
DATA = pathlib.Path(os.environ.get("DATA_DIR", str(HERE))).resolve()
HISTORY = DATA / "history.csv"
FAILURES = DATA / "failures"
EXPORTS = DATA / "exports"
for d in (DATA, FAILURES, EXPORTS):
    d.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("silver")


def setup_logging(logfile: pathlib.Path) -> None:
    """
    Configure logging to a size-capped file plus stdout.

    Called from an entry point, NOT at import, for two reasons:

    1. bot.py imports this module. If logging were configured here at import,
       the bot's own basicConfig would be a silent no-op (basicConfig does
       nothing once the root logger has handlers) and every bot line would land
       in silver_report.log instead of bot.log.
    2. Two processes each holding a RotatingFileHandler on the SAME path will
       corrupt each other's rotation. Each entry point names its own file.

    Rotation matters because this runs unattended forever: an uncapped log is a
    slow disk leak that nobody notices until something else fails.
    """
    root = logging.getLogger()
    if root.handlers:                      # already configured; don't stack
        return
    fh = logging.handlers.RotatingFileHandler(
        logfile, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s")
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(logging.INFO)


# ===========================================================================
#  SELECTORS  --  THIS IS THE ONLY BLOCK YOU SHOULD NEED TO EDIT
# ===========================================================================
#  Everything under "Login", "report_url" and "Export" below is CONFIRMED
#  against the live portal and has been exercised in a successful end-to-end
#  run. The menu-navigation entries are an unexercised fallback, only reached
#  if the direct report URL stops rendering.
#
#  If the portal changes, this is still the first place to look. Playwright
#  selector syntax:
#
#     "#userName"                   -> element with id="userName"
#     "input[name='password']"      -> input whose name attribute is password
#     "text=Store Summary"          -> any element containing that visible text
#     "a:has-text('Results')"       -> a link containing that text
#
#  ONE RULE FOR THIS PORTAL: it is an ExtJS app, and ExtJS emits two kinds of
#  id. Author-assigned ones like #Login-btnEl are stable. Auto-generated ones
#  like ext-comp-1234, ext-gen-56 or button-1060 change between sessions --
#  never put one in a selector here.
# ===========================================================================

SELECTORS: dict[str, str] = {
    # --- Login: CONFIRMED against the live page, not guesses ---------------
    # The portal is an ExtJS app (Sencha). Note there is no <form> element at
    # all and the Log In control is <button type="button">, so the usual
    # input[type=submit] selector would never have matched. These ids come
    # from ExtJS component ids, which are author-assigned and stable -- unlike
    # the auto-generated ext-comp-#### ids elsewhere in the app, which are NOT
    # stable and must never be used in a selector.
    "login_url":       "https://mystore.ncrvoyix.com/app/Account/LogOn",
    "username_field":  "#login-user-name-inputEl",    # name="username"
    "password_field":  "#password-inputEl",           # name="password"
    "login_button":    "#Login-btnEl",                # <button>, text "Log In"
    "login_success":   "text=Results",                # proves we're logged in

    # --- Navigation --------------------------------------------------------
    # PREFERRED PATH: go straight to the report URL with the dates in the
    # query string. Confirmed to exist from recon -- this is the iframe the
    # portal itself loads. It skips the ExtJS menus AND the date picker, which
    # were the two most fragile things in this file. {day} is filled in with
    # an ISO date. reportType=ds is the Daily Summary.
    # reportType=ds is Daily Summary / Store Summary. reportType=is is Item
    # Sales -- confirmed from the live left-nav id reportsHomeis / showPage('is').
    "report_url":      ("https://mystore.ncrvoyix.com/app/Reports/ReportForm.aspx"
                        "?reportType={report_type}&containerName=reportDisplay"
                        "&startDate={start}&endDate={end}"
                        "&range=true&gridded=no&details=no"
                        "&showModifiers=false&showStoreDetails=false"
                        "&punchDetails=yes&showFinancialShift=true"
                        "&showCategories=false&showSubcategories=false"
                        "&showComponents=false&commissionsExcludeRefunds=false"),

    # FALLBACK PATH: only used if the direct URL does not render on its own.
    "results_menu":    "text=Results",
    "reports_menu":    "text=Reports",
    "store_summary":   "text=Store Summary",

    "date_field":      "input[id*='ate'], input[name*='ate']",
    "refresh_button":  "text=Refresh",                # may not exist; optional

    "report_ready":    "text=Gross Sales",            # proves the report rendered

    # Plain text (not a selector) used to identify WHICH iframe holds the
    # rendered report. The page nests the report two frames deep, so this is
    # how we tell the report frame apart from the nav shell.
    "report_marker":   "Gross Sales",
    # Item Sales (reportType=is) does not contain "Gross Sales". Any one of
    # these in the report frame means the items report actually rendered.
    # The live report labels its columns "Sold Qty." and "Amt." (recon
    # 2026-10-20), so "Sold Qty" is the one that actually matches.
    "items_report_markers": "Sold Qty|Sold Quantity|Net Sales Amount|Avg. Price|Avg Price",

    # --- Export: CONFIRMED from recon --------------------------------------
    # Not a menu at all. It is a classic ASP.NET ReportViewer toolbar: a
    # <select> of formats plus an <a>Export</a> link. So the flow is
    # select_option then click, not click-then-click.
    #
    # The toolbar lives in the ReportForm.aspx frame, which is a DIFFERENT
    # frame from the one holding the numbers (that one is the nested Telerik
    # viewer). Hence two frame resolvers below.
    #
    # Option values are: PDF, CSV, XLS, RTF, IMAGE, MHTML.
    "export_format_select": "#ReportViewer1_ReportToolbar_ExportGr_FormatList_DropDownList",
    "export_format_value":  "CSV",
    "export_button":        "#ReportViewer1_ReportToolbar_ExportGr_Export",
}

# CSV EXPORT COLUMN NAMES -- these are CONFIRMED, copied from a real
# DailySummary.csv header row. The report is much wider than expected: every
# measure comes as a Quantity / Total / Average triple, so "Gross Sales" alone
# is not a column and never was. Left side must match the header EXACTLY.
EXPORT_COLUMNS: dict[str, str] = {
    "Gross Sales Total":     "gross_sales",
    "Net Sales Total":       "net_sales",
    "Discounts Total":       "discounts",
    "Gross Refunds Total":   "returns",
    "Taxes Total":           "tax",
    "Ticket Total Quantity": "transactions",
    "Ticket Total Total":    "total_collected",
    "Ticket Total Average":  "avg_ticket",
    "Tips Total":            "tips",
    "No Sales":              "no_sales",
}

# How the figures relate, verified against a real export (Fri 08/15):
#   Gross Sales        1,000.00
#   - Discounts           10.00
#   - Overrides           -5.00
#   - Returns              0.00
#   = Net Sales          995.00
#   + Taxes              100.00
#   = Ticket Total     1,095.00   <- and this equals CASH + CREDIT exactly,
#                                    which is the cross-check that confirmed
#                                    the column mapping (illustrative figures)
# So "total_collected" is the money that actually hit the drawer, while
# "net_sales" is the revenue figure. They differ by tax, and which one you
# want as the headline is a business call, not a technical one.

# Labels as they appear in the RENDERED PAGE, used only by the page-text
# fallback. Deliberately separate from EXPORT_COLUMNS: the on-screen report
# groups things under short headings while the CSV uses the long triple names.
# Both sides map onto the same metric keys so history.csv stays consistent
# regardless of which path captured the row.
WANTED_FIELDS: dict[str, str] = {
    "Gross Sales":      "gross_sales",
    "Net Sales":        "net_sales",
    "Discounts":        "discounts",
    "Gross Refunds":    "returns",
    "Taxes":            "tax",
    "Ticket Total":     "transactions",
    "Tips":             "tips",
    "No Sales":         "no_sales",
}

# POSITIONAL MAP for the headerless export.
# ---------------------------------------------------------------------------
# The portal serves TWO different CSVs from the same report. Clicking Export in
# the UI yields a file with a header row and unformatted numbers. Fetching the
# viewer's own export URL yields a HEADERLESS file, laid out in the report's
# visual order, with currency symbols and accounting negatives. We use the URL
# (the click is a dead end -- see download_export), so we need positions.
#
# These indices were derived by cross-referencing the two exports for the SAME
# day, keeping only fields that could be identified unambiguously, and then
# checked against the two arithmetic identities below. Every index here either
# matched a unique value or sits in a confirmed Quantity/Total/Average triple.
#
# DELIBERATELY ABSENT: returns and tips. Both were 0.00 in the sample, which
# left 18 candidate positions each and no structural anchor to choose between
# them. Guessing would produce a plausible-looking wrong number -- the one
# failure this whole program is built to avoid. To pin them down, export one
# day that HAD a refund and a tip, and find the index carrying each value.
EXPORT_POSITIONS: dict[int, str] = {
    0:  "gross_sales",
    1:  "net_sales",
    3:  "discounts",
    4:  "overrides",
    10: "returns",           # Gross Refunds Total -- see note below
    12: "tax",
    14: "transactions",
    15: "total_collected",
    16: "avg_ticket",
}

# HOW returns WAS PINNED DOWN, since guessing was refused:
# every sampled single day had zero refunds, so the column was unidentifiable --
# a zero looks like every other zero. Pulling a longer range with refunds in it
# settled it, and the arithmetic guard below found the column rather than a
# human eyeballing the file:
#
#   1. A single month failed to reconcile by exactly the amount of that month's
#      only refund. Searching the row for that value gave one candidate: idx 10.
#   2. A full-year pull confirmed the triple. idx 10 (Total) divided by idx 21
#      (Quantity) equalled idx 11 (Average) exactly.
#
# Three mutually consistent facts, not a guess. If you are adapting this to a
# different report, that is the technique: make the guard fail, then look for
# the discrepancy as a literal value in the row.
#
# STILL UNMAPPED: tips. Zero in every sample including a full year, so there is
# nothing to locate it by -- and a wrong guess for a field that is always zero
# would never show up, which is exactly why it stays out.

# no_sales is NOT at a fixed index. The row's width varies with how many
# discount names / price overrides / cleared-item lines the day produced --
# observed at both 66 and 63 fields across four real exports. So it is located
# structurally instead: it is the final field, sitting three places after the
# literal "Cleared Items" label. Both of those held in all four samples, and
# we require them to AGREE before trusting the value, because no_sales appears
# in neither arithmetic identity and so has no other safety net.
NO_SALES_ANCHOR = "Cleared Items"
NO_SALES_OFFSET = 3


def extract_no_sales(row: list[str]) -> str | None:
    """Locate no_sales by its anchor, requiring the anchor and the row end to agree."""
    try:
        anchored = row.index(NO_SALES_ANCHOR) + NO_SALES_OFFSET
    except ValueError:
        return None
    if anchored != len(row) - 1 or anchored >= len(row):
        log.warning("no_sales anchor at %d but row ends at %d -- skipping the "
                    "field rather than guessing", anchored, len(row) - 1)
        return None
    v = clean_number(row[anchored])
    try:
        float(v)
    except (TypeError, ValueError):
        return None
    return v

# Canonical column order for history.csv -- the union of every path.
METRIC_KEYS: list[str] = list(dict.fromkeys(
    list(EXPORT_COLUMNS.values())
    + list(EXPORT_POSITIONS.values())
    + list(WANTED_FIELDS.values())))


def clean_number(value: str) -> str:
    """
    Normalise a currency cell to a bare number string.

    The headerless export writes "$1,234.56" and accounting negatives as
    "($4.02)". history.csv should hold something arithmetic-friendly, and the
    digest adds its own symbol back.
    """
    v = (value or "").strip().replace("$", "").replace(",", "")
    if v.startswith("(") and v.endswith(")"):
        v = "-" + v[1:-1]
    return v


def to_float(value: str) -> float | None:
    try:
        return float(clean_number(value))
    except (TypeError, ValueError):
        return None


def verify_totals(metrics: dict[str, str]) -> list[str]:
    """
    Check the figures against the report's own internal arithmetic.

    This is what makes reading a headerless file by position safe. Two
    identities hold in every one of these reports:

        gross - discounts - overrides - returns = net sales
        net sales + tax                         = ticket total

    The second is exact even over a full year. The first accumulates a little
    rounding drift when the portal aggregates a long range -- a 2025 full-year
    pull came out roughly 0.01% apart. So the tolerance is
    RELATIVE rather than a flat couple of cents: a flat tolerance would reject
    every multi-month query, and a column shift misreads by orders of magnitude
    (net_sales reading 38 instead of 946), which 0.1% still catches trivially.

    Six of the nine mapped positions appear in those equations, so if the
    portal ever reorders its columns the sums stop reconciling and we refuse
    the data instead of filing a confident wrong answer. A renamed header
    would give no such signal -- this check is strictly stronger.

    Returns a list of problems; empty means the numbers agree.
    """
    problems: list[str] = []
    ran: list[str] = []
    skipped: list[str] = []

    def chk(label: str, parts: list[str], expected: str, fn) -> None:
        vals = [to_float(metrics.get(p, "")) for p in parts]
        exp = to_float(metrics.get(expected, ""))
        if exp is None or any(v is None for v in vals):
            # A missing field must not quietly disable the check and leave the
            # row looking verified. Record it so the log distinguishes "the
            # numbers agree" from "nothing objected".
            absent = [p for p, v in zip(parts, vals) if v is None]
            if exp is None:
                absent.append(expected)
            skipped.append(f"{label} (no {', '.join(absent)})")
            return
        got = fn(vals)
        ran.append(label)
        tolerance = max(0.05, abs(exp) * 0.001)     # 0.1%, floored at 5c
        if abs(got - exp) > tolerance:
            problems.append(
                f"{label}: expected {expected}={exp:.2f} but the parts give "
                f"{got:.2f} (off by {abs(got - exp):.2f}, tolerance "
                f"{tolerance:.2f}) -- the export's columns may have moved")

    chk("gross-discounts-overrides-returns",
        ["gross_sales", "discounts", "overrides", "returns"],
        "net_sales", lambda v: v[0] - v[1] - v[2] - v[3])
    chk("net+tax", ["net_sales", "tax"],
        "total_collected", lambda v: v[0] + v[1])

    if ran:
        log.info("totals check: %d/2 identities verified (%s)%s",
                 len(ran), "; ".join(ran),
                 f"; NOT checked: {'; '.join(skipped)}" if skipped else "")
    else:
        # Zero identities ran means the arithmetic guard provided no protection
        # at all for this row, which is worth shouting about -- it is the only
        # thing licensing a positional read of a headerless file.
        log.error("totals check: NO identities could be verified (%s) -- these "
                  "numbers are UNVALIDATED", "; ".join(skipped))
        problems.append("no arithmetic identity could be checked, so the "
                        "positional column mapping is unverified for this run")
    return problems


# ===========================================================================
#  CONFIG
# ===========================================================================

@dataclass
class Store:
    name: str
    username: str
    password: str
    # "STORE2" etc. -- the .env prefix, so a rotated password can be written
    # back to the right line.
    key: str = ""


@dataclass
class ItemSale:
    name: str
    qty: float
    net_sales: float | None = None
    category: str = ""
    vendor: str = ""
    avg_price: float | None = None


@dataclass
class StoreResult:
    store: str
    business_day: str
    ok: bool = False
    metrics: dict[str, str] = field(default_factory=dict)
    items: list[ItemSale] = field(default_factory=list)
    raw_text: str = ""
    error: str = ""
    # Which path produced the numbers -- "export" (trustworthy) or "page-text"
    # (a fallback). Recorded in history.csv so a run you later find suspicious
    # can be traced to how it was captured.
    source: str = ""
    export_file: str = ""


def get_stores() -> list[Store]:
    stores: list[Store] = []
    for i in (1, 2, 3, 4, 5):
        user = os.environ.get(f"STORE{i}_USER")
        if not user:
            continue
        stores.append(Store(
            name=os.environ.get(f"STORE{i}_NAME", f"Store {i}"),
            username=user,
            password=os.environ[f"STORE{i}_PASS"],
            key=f"STORE{i}",
        ))
    if not stores:
        raise SystemExit("No stores configured. Copy .env.example to .env and fill it in.")
    return stores


# ===========================================================================
#  SCRAPING
# ===========================================================================

MONEY = re.compile(r"-?\$?\s*-?[\d,]+\.?\d{0,2}")


def login(page: Page, store: Store) -> None:
    """
    Log in, rotating the password first if Silver says it has expired.

    An expired password does NOT fail the login. Silver accepts it and then
    opens a modal "Change Password" form, and the report menu never appears.
    Before this was handled, that showed up only as a 45s timeout on
    "text=Results" (De Soto, 2026-10-14), so the cause had to be read off the
    failure screenshot.
    """
    _refresh_password(store)
    state = _submit_login(page, store, store.password)

    if state == "timeout" and store.key:
        # A rotation that died after Save but before the new password was
        # promoted leaves it in STORE<n>_PASS_PENDING, and the old one is now
        # rejected. Try it once before giving up.
        pending = _read_env_file().get(f"{store.key}_PASS_PENDING")
        if pending and pending != store.password:
            log.warning("[%s] login failed -- trying the pending password "
                        "from an unfinished rotation", store.name)
            state = _submit_login(page, store, pending)
            if state != "timeout":
                _set_env_values({f"{store.key}_PASS": pending,
                                 f"{store.key}_PASS_PENDING": None})
                store.password = pending
                log.info("[%s] pending password works -- promoted", store.name)

    if state == "change_password":
        rotate_password(page, store)
        state = _submit_login(page, store, store.password)
        if state != "ok":
            raise RuntimeError(f"password rotated, but logging in with the new "
                               f"one ended in {state!r}")
    elif state != "ok":
        raise RuntimeError("login did not reach the Results menu within 45s "
                           "(wrong password, or the portal changed)")
    log.info("[%s] logged in", store.name)


# The forced-change modal. ids are author-assigned ExtJS ids (stable), read
# off the 2026-10-14 failure snapshot.
CHANGE_PW = {
    "username": "#changepassword_membershipusername-inputEl",
    "old":      "#changepassword_OldPassword-inputEl",
    "new":      "#changepassword_Password-inputEl",
    "confirm":  "#changepassword_ConfirmPassword-inputEl",
    "save":     "#changepassword_changePasswordSaveButton-btnEl",
}
ENVFILE = HERE / ".env"
ROTATE_DRY_RUN = False


def _submit_login(page: Page, store: Store, password: str) -> str:
    """Returns "ok", "change_password" or "timeout"."""
    log.info("[%s] opening login page", store.name)
    page.goto(SELECTORS["login_url"], wait_until="load", timeout=60_000)
    page.fill(SELECTORS["username_field"], store.username)
    page.fill(SELECTORS["password_field"], password)
    page.click(SELECTORS["login_button"])
    results = page.locator(f"{SELECTORS['login_success']} >> visible=true")
    modal = page.locator(f"{CHANGE_PW['old']} >> visible=true")
    deadline = datetime.now() + timedelta(seconds=45)
    while datetime.now() < deadline:
        if modal.count():
            log.warning("[%s] Silver is forcing a password change", store.name)
            return "change_password"
        if results.count():
            return "ok"
        page.wait_for_timeout(500)
    return "timeout"


def _read_env_file() -> dict[str, str]:
    """The .env as it is on disk NOW. The Telegram bot is long-running, so its
    os.environ can hold a password the timer job has since rotated."""
    out: dict[str, str] = {}
    if not ENVFILE.exists():
        return out
    for line in ENVFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _refresh_password(store: Store) -> None:
    if store.key:
        current = _read_env_file().get(f"{store.key}_PASS")
        if current:
            store.password = current


def _set_env_values(updates: dict[str, str | None]) -> None:
    """
    Replace, add or (value None) remove keys in .env, atomically.

    A timestamped copy of the previous file is kept next to it first (newest
    10), because a half-written .env after a rotation would lock the job out
    of an account whose password no human knows.
    """
    import shutil
    text = ENVFILE.read_text(encoding="utf-8")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = ENVFILE.with_name(f".env.bak-{stamp}")
    shutil.copy2(ENVFILE, backup)
    os.chmod(backup, 0o600)
    for old in sorted(HERE.glob(".env.bak-*"))[:-10]:
        old.unlink()

    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                seen.add(k)
                if updates[k] is not None:
                    out.append(f"{k}={updates[k]}")
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen and v is not None:
            out.append(f"{k}={v}")

    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(HERE), prefix=".env.tmp-")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(out) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, ENVFILE)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise
    for k, v in updates.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def generate_password(length: int = 20) -> str:
    """Random, with every character class, and nothing a .env line or a shell
    could mangle (no quotes, spaces, $, # or backslash). The form caps at 32."""
    import secrets
    import string
    symbols = "!@%^*-_+."
    alphabet = string.ascii_letters + string.digits + symbols
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in symbols for c in pw)):
            return pw


def _modal_errors(page: Page) -> str:
    """The form's own validation text (ExtJS keeps it in data-errorqtip) plus
    any message box, for the log. Never includes field values."""
    bits: list[str] = []
    for key in ("old", "new", "confirm"):
        tip = page.locator(CHANGE_PW[key]).get_attribute("data-errorqtip") or ""
        tip = re.sub(r"<[^>]+>", " ", tip).strip()
        if tip:
            bits.append(f"{key}: {tip}")
    box = page.locator(".x-message-box >> visible=true")
    if box.count():
        bits.append("message: " + " ".join(box.first.inner_text().split()))
    return "; ".join(bits) or "no message shown"


def rotate_password(page: Page, store: Store) -> None:
    """
    Complete Silver's forced password change with a generated password.

    Order matters more than anything else here. The new password goes into
    .env as STORE<n>_PASS_PENDING BEFORE Save is clicked, so a crash at any
    point leaves it recoverable (login() tries PENDING when the main one
    fails). It only becomes STORE<n>_PASS after a fresh login with it works.
    The password itself is never logged or sent anywhere.
    """
    if not store.key:
        raise RuntimeError("password expired, and this store has no .env key "
                           "to write a new one to")
    import fcntl
    with open(DATA / ".rotate.lock", "w") as lockfh:
        # Both timers and the bot can log in; two rotations at once would
        # each invalidate the other's password.
        fcntl.flock(lockfh, fcntl.LOCK_EX)
        on_disk = _read_env_file().get(f"{store.key}_PASS")
        if on_disk and on_disk != store.password:
            raise RuntimeError("another run rotated this password while we "
                               "waited -- the next run will use the new one")

        new = generate_password()
        _set_env_values({f"{store.key}_PASS_PENDING": new})
        log.info("[%s] new password saved as pending; filling the form",
                 store.name)

        user = page.locator(CHANGE_PW["username"])
        if not (user.input_value() or "").strip():
            user.fill(store.username)
            user.press("Tab")
        for key, value in (("old", store.password), ("new", new),
                           ("confirm", new)):
            field_ = page.locator(CHANGE_PW[key])
            field_.fill(value)
            field_.press("Tab")   # ExtJS re-validates on blur, not on fill()

        save = page.locator(CHANGE_PW["save"])
        deadline = datetime.now() + timedelta(seconds=5)
        while save.get_attribute("disabled") is not None:
            if datetime.now() > deadline:
                _set_env_values({f"{store.key}_PASS_PENDING": None})
                raise RuntimeError("Save stayed disabled on the change-password "
                                   "form: " + _modal_errors(page))
            page.wait_for_timeout(250)

        if ROTATE_DRY_RUN:
            _set_env_values({f"{store.key}_PASS_PENDING": None})
            raise RuntimeError("dry run: form accepted, Save enabled, not "
                               "clicked")

        save.click()
        modal = page.locator(f"{CHANGE_PW['old']} >> visible=true")
        box = page.locator(".x-message-box >> visible=true")
        messages: list[str] = []
        deadline = datetime.now() + timedelta(seconds=30)
        while datetime.now() < deadline:
            if box.count():
                messages.append(" ".join(box.first.inner_text().split()))
                ok_btn = box.first.locator("text=OK")
                if ok_btn.count():
                    ok_btn.first.click()
                page.wait_for_timeout(500)
                continue
            if not modal.count():
                break
            page.wait_for_timeout(500)
        else:
            # Unknown whether Silver applied it, so PENDING stays: login()
            # will try it if the old password stops working.
            raise RuntimeError("change-password form did not close after "
                               "Save: " + ("; ".join(messages)
                                           or _modal_errors(page)))
        if messages:
            log.info("[%s] Silver said: %s", store.name, "; ".join(messages))

        # Prove it before promoting it: the caller logs in again with it.
        check = page.context.browser.new_context()
        try:
            state = _submit_login(check.new_page(), store, new)
        finally:
            check.close()
        if state != "ok":
            raise RuntimeError(f"saved a new password but a fresh login with "
                               f"it ended in {state!r}; it is kept as "
                               f"{store.key}_PASS_PENDING")
        _set_env_values({f"{store.key}_PASS": new,
                         f"{store.key}_PASS_PENDING": None})
        store.password = new
        log.warning("[%s] password expired and was rotated (verified)",
                    store.name)

    try:
        notify.send_all(
            f"Silver: {store.name} password rotated",
            f"{store.name}: Silver forced a password change. A new one was "
            f"set and verified automatically.",
            f"{store.name}: Silver forced a password change. A new random "
            f"password was set and verified automatically. It is in "
            f"/opt/silver/.env ({store.key}_PASS) on the silver-report box.",
            True)
    except Exception as e:   # the rotation worked; a notice failing is minor
        log.error("could not send the rotation notice: %s", e)


FRAME_URL_HINTS = ("Telerik.ReportViewer.axd", "ReportForm.aspx")


def toolbar_frame(page: Page) -> Frame:
    """
    Return the frame holding the export toolbar.

    This is NOT the same frame as the report content, and assuming it was
    would have made every export fail. The portal renders ReportForm.aspx --
    which carries the ASP.NET ReportViewer toolbar -- and then nests the
    actual rendered report inside it in a separate Telerik viewer frame. The
    numbers are in one, the Export control is in the other.

    Located by looking for the format <select> rather than by URL, so it keeps
    working if the toolbar moves.
    """
    sel = SELECTORS["export_format_select"]
    for f in page.frames:
        try:
            if f.query_selector(sel):
                return f
        except Exception:
            continue
    log.warning("export toolbar not found in any frame -- trying main frame")
    return page.main_frame


def _frame_with(page: Page, markers: list[str]) -> Frame | None:
    """The first report-ish frame whose body contains any of `markers`."""
    for f in page.frames:
        if not any(h in (f.url or "") for h in FRAME_URL_HINTS):
            continue
        try:
            body = f.inner_text("body")
        except Exception:
            continue
        if any(m in body for m in markers):
            return f
    return None


def report_frame(page: Page, markers: list[str] | None = None) -> Frame:
    """
    Return the frame that actually contains the rendered report.

    THIS IS NOT OPTIONAL PLUMBING. The portal nests the report two frames
    down -- the outer page is an ExtJS shell, inside it is ReportForm.aspx,
    and inside that is a Telerik report viewer. Reading `page.inner_text`
    returns navigation chrome and the shell's own date fields, not the
    numbers. Worse, the SHELL carries the requested date in its own inputs,
    so a date check run against the top document can pass while the report
    frame below shows something else entirely.

    Falls back to the main frame so a portal that stops using iframes keeps
    working rather than hard-failing.
    """
    markers = markers or [SELECTORS["report_marker"]]
    candidates = [f for f in page.frames
                  if any(h in (f.url or "") for h in FRAME_URL_HINTS)]

    # Prefer a frame that demonstrably holds report content.
    found = _frame_with(page, markers)
    if found:
        log.debug("report frame: %s", (found.url or "")[:80])
        return found

    if candidates:
        log.warning("no frame contained %s -- using first report-ish frame", markers)
        return candidates[0]

    log.warning("no report iframe found -- falling back to the main frame")
    return page.main_frame


def _report_markers(report_type: str) -> list[str]:
    if report_type == "is":
        return [m.strip() for m in SELECTORS["items_report_markers"].split("|")
                if m.strip()]
    return [SELECTORS["report_marker"]]


def open_store_summary(page: Page, start: date, end: date) -> None:
    open_report(page, start, end, report_type="ds")


def open_report(page: Page, start: date, end: date,
                report_type: str = "ds") -> None:
    """
    Get the report for the range start..end on screen.

    The portal's own URL takes a startDate and an endDate, so one day and one
    month cost exactly the same request -- which is what makes ad-hoc
    historical queries possible at all. A nightly run just passes the same date
    twice.

    Tries the direct URL first, since it removes the ExtJS menu clicks and the
    date picker -- historically the two most breakable steps. Falls back to
    clicking through the UI if the direct URL does not render, because direct
    navigation may depend on session state the shell sets up first.

    report_type: ds = Daily Summary, is = Item Sales (left-nav showPage('is')).
    """
    markers = _report_markers(report_type)
    if SELECTORS.get("report_url"):
        url = SELECTORS["report_url"].format(
            report_type=report_type,
            start=start.isoformat(),
            end=end.isoformat())
        try:
            log.info("trying direct report URL type=%s for %s..%s",
                     report_type, start.isoformat(), end.isoformat())
            page.goto(url, wait_until="load", timeout=60_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
            # The Telerik viewer fills its nested report frame AFTER
            # networkidle -- a long range takes a while -- so poll for it
            # rather than reading once and declaring the report empty.
            for _ in range(30):
                if _frame_with(page, markers):
                    log.info("direct report URL worked -- skipped the menus entirely")
                    verify_day_on_page(page, start, markers)
                    return
                page.wait_for_timeout(2_000)
            log.warning("direct URL loaded but no report content "
                        "(looked for %s) -- falling back to clicking through "
                        "the UI", markers)
        except Exception as e:
            log.warning("direct report URL failed (%s: %s) -- falling back to "
                        "clicking through the UI", type(e).__name__, e)

    if report_type != "ds":
        # navigate_by_menu only knows the way to Store Summary; following it
        # for any other report just times out waiting for "Gross Sales".
        raise RuntimeError(f"report type {report_type!r} did not render from "
                           f"the direct URL, and has no menu fallback")
    navigate_by_menu(page, start)


def navigate_by_menu(page: Page, day: date) -> None:
    """Fallback: click Results > Reports > Store Summary and set the date."""
    for key in ("results_menu", "reports_menu", "store_summary"):
        sel = SELECTORS[key]
        try:
            page.click(sel, timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            # Some menus are hover-only or the item is already visible.
            log.warning("could not click %s (%s) -- continuing", key, sel)

    # Set the date. Old ASP.NET date pickers usually accept typed input.
    formatted = day.strftime("%m/%d/%Y")
    try:
        fields = page.query_selector_all(SELECTORS["date_field"])
        for f in fields:          # often there is a From and a To field
            f.fill("")
            f.type(formatted, delay=40)
        page.keyboard.press("Enter")
        log.info("date set to %s (%d field(s))", formatted, len(fields))
    except Exception as e:
        log.warning("could not set date (%s) -- report may be showing its default", e)

    if page.query_selector(SELECTORS["refresh_button"]):
        page.click(SELECTORS["refresh_button"])

    page.wait_for_load_state("networkidle", timeout=30_000)
    page.wait_for_selector(SELECTORS["report_ready"], timeout=30_000)
    verify_day_on_page(page, day)


def verify_day_on_page(page: Page, day: date,
                       markers: list[str] | None = None) -> None:
    """
    Refuse to continue unless the requested date is actually on screen.

    This is the most important check in the file. Every navigation step above
    swallows its own failure and continues, and the only hard gate is that the
    report rendered -- which the portal's DEFAULT date range satisfies just as
    well as the one we asked for. Without this check, a broken date selector
    produces a clean download of the WRONG business day, parses perfectly, and
    gets filed as source=export with no [CHECK] on it. Wrong numbers that look
    trustworthy are worse than an obvious failure.

    It reads the REPORT FRAME, not the top document, and that distinction is
    the whole point: the ExtJS shell keeps the requested date in its own input
    fields, so checking the outer page would find the date we asked for even
    when the report underneath is showing something completely different --
    which is precisely the failure this is meant to catch.
    """
    # The rendered report writes "August 15, 2026" -- confirmed from recon.
    # Both padded and UNPADDED day forms are listed: strftime("%d") always
    # zero-pads, so a single-digit day would produce "August 05, 2026" and
    # match nothing, silently failing every run in the first nine days of a
    # month. Windows has no portable "%-d", so build those by hand.
    candidates = [
        day.strftime("%B %d, %Y"),                     # August 15, 2026
        f"{day:%B} {day.day}, {day.year}",             # August 5, 2026
        day.strftime("%b %d, %Y"),                     # Aug 15, 2026
        f"{day:%b} {day.day}, {day.year}",             # Aug 5, 2026
        day.strftime("%m/%d/%Y"),                      # 08/15/2026
        f"{day.month}/{day.day}/{day.year}",           # 8/15/2026
        day.isoformat(),                               # 2026-08-15
        day.strftime("%m/%d/%y"),                      # 08/15/26
    ]
    text = report_frame(page, markers).inner_text("body")
    if any(c in text for c in candidates):
        return

    raise RuntimeError(
        f"Report rendered but {day.isoformat()} does not appear anywhere in "
        f"the report frame -- the date almost certainly did not get set, so "
        f"this is the portal's default range, not the day requested. Check "
        f"the report_url dates and the date_field selector. "
        f"(Looked for: {', '.join(candidates)})"
    )


def download_export(page: Page, store: Store, start: date, end: date,
                    kind: str = "ds") -> pathlib.Path | None:
    """
    Click Export > CSV and keep the file.

    This is strictly better than reading numbers off the rendered page: the
    export is the portal's own data, already parsed into fields, with no risk
    of picking up a column header or a nav item that happens to contain the
    same words. Returns None if the export is unavailable, so the caller can
    fall back to scraping the page.

    Both clicks go INSIDE expect_download(). The download can fire before the
    second click returns, and a listener registered afterwards would miss it.

    WHY NOT JUST CLICK THE EXPORT BUTTON
    ------------------------------------
    Because clicking it does nothing at all, and traced all the way down the
    stack the reason is theirs, not ours:

        anchor.onclick = ReportTextButton_Click
        ReportTextButton_Click -> this.Exec()
        ToolbarButton_Exec     -> if (Enabled && Toolbar) Toolbar.OnCommand(this)
        ReportToolbar_OnCommand(b) { alert("OnCommand: " + b.CommandName); }

    That last one is the shipped default stub -- it pops an alert instead of
    exporting, and nothing in ReportForm.aspx ever overrides it. So a click
    either does nothing or raises a dialog, and Playwright auto-dismisses
    dialogs, which is precisely the silent no-op we spent a while chasing.

    The viewer object exposes the real mechanism:

        ReportViewer.prototype.ExportInternal = function (format) {
            if (this.exportUrl) window.open(this.exportUrl + "&ExportFormat=" + format, "_self");
        }

    So we read `exportUrl` off the live viewer and navigate to it directly.
    It MUST be read at run time -- it carries a per-render instanceID, and a
    cached one is stale the moment the report re-renders.
    """
    safe = re.sub(r"\W+", "_", store.name)
    frame = toolbar_frame(page)
    try:
        export_url = frame.evaluate("""() => {
            for (const k of Object.getOwnPropertyNames(window)) {
                let v;
                try { v = window[k]; } catch (e) { continue; }
                if (v && typeof v === 'object' && typeof v.exportUrl === 'string'
                    && v.exportUrl) {
                    return v.exportUrl;
                }
            }
            return null;
        }""")
        if not export_url:
            raise RuntimeError(
                "no ReportViewer object with an exportUrl in the toolbar frame "
                "-- the viewer may not have finished rendering")

        url = f"{export_url}&ExportFormat={SELECTORS['export_format_value']}"
        log.info("[%s] exporting via %s", store.name, url[:90])
        with page.expect_download(timeout=60_000) as dl_info:
            frame.evaluate("u => { window.location.href = u; }", url)
        download = dl_info.value

        suffix = pathlib.Path(download.suggested_filename).suffix or ".csv"
        span = (start.isoformat() if start == end
                else f"{start.isoformat()}_to_{end.isoformat()}")
        tag = "" if kind == "ds" else f"_{kind}"
        dest = EXPORTS / f"{span}_{safe}{tag}{suffix}"
        download.save_as(str(dest))
        log.info("[%s] export saved: %s (%d bytes)",
                 store.name, dest.name, dest.stat().st_size)
        return dest
    except Exception as e:
        log.warning("[%s] export unavailable (%s: %s) -- falling back to "
                    "reading the page", store.name, type(e).__name__, e)
        return None


def parse_export(path: pathlib.Path) -> dict[str, str]:
    """
    Pull metrics out of the downloaded CSV.

    ==========================================================================
    SHAPE CONFIRMED from a real DailySummary.csv. VALUES NOT YET CONFIRMED --
    the sample export was captured just after midnight for a day with no sales
    yet, so every figure in it was blank. The column names below are certain;
    which row the numbers land in is the part still to verify.
    ==========================================================================
    The export is one very wide header row with SEVERAL TABLES CONCATENATED
    SIDE BY SIDE -- the summary measures, then Payment Types, then Tax
    Categories, then Discount Names, and so on. Each table's rows start at
    different depths, so most cells in any given row are empty and the value
    for a column can sit a row or two below the header.

    So rather than assuming "the row under the header", we take the first
    non-empty cell in each wanted column. Column names are matched exactly,
    which matters because "Gross Sales Total" and "Gross Refunds Total" would
    both match a sloppier substring test.
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return {}

    metrics: dict[str, str] = {}

    # SHAPE 0: headerless positional export (what the viewer's export URL
    # returns). Detected by the absence of any known column name anywhere.
    has_header = any(c.strip() in EXPORT_COLUMNS for row in rows for c in row)
    if not has_header:
        row = rows[0]
        for idx, key in EXPORT_POSITIONS.items():
            if idx < len(row):
                v = clean_number(row[idx])
                if v:
                    metrics[key] = v
        ns = extract_no_sales(row)
        if ns is not None:
            metrics["no_sales"] = ns
        if metrics:
            log.info("parsed export as headerless positional (%d fields, "
                     "%d columns in row)", len(metrics), len(row))
            return metrics
        log.warning("headerless export had no usable values in %s", path.name)
        return {}

    # Locate the header row: the one containing the most known column names.
    best_i, best_hits = -1, {}
    for i, row in enumerate(rows):
        hits = {j: EXPORT_COLUMNS[c.strip()] for j, c in enumerate(row)
                if c.strip() in EXPORT_COLUMNS}
        if len(hits) > len(best_hits):
            best_i, best_hits = i, hits

    if best_hits:
        for j, key in best_hits.items():
            for row in rows[best_i + 1:]:
                if j < len(row) and row[j].strip():
                    metrics[key] = row[j].strip()
                    break
        if metrics:
            log.info("parsed export: header row %d, %d of %d columns populated",
                     best_i, len(metrics), len(best_hits))
            return metrics
        log.warning("export header found (%d known columns) but every value "
                    "cell was EMPTY -- is there any business on %s?",
                    len(best_hits), path.stem)
        return {}

    # Fallback: simple label/value rows, in case a different report is used
    # later. The digit test stops a header row matching as data.
    for row in rows:
        if len(row) < 2:
            continue
        label, value = row[0].strip(), row[1].strip()
        key = EXPORT_COLUMNS.get(label) or WANTED_FIELDS.get(label)
        if key and value and any(c.isdigit() for c in value):
            metrics[key] = value
    if metrics:
        log.info("parsed export as label/value rows (%d metrics)", len(metrics))
        return metrics

    log.warning("export at %s matched no known shape -- open the file and "
                "adjust EXPORT_COLUMNS / parse_export()", path.name)
    return {}


_ITEM_NAME_HEADERS = {
    "item name", "item", "name", "description", "menu item", "product",
}
_ITEM_QTY_HEADERS = {
    "sold quantity", "qty sold", "quantity sold", "sold qty", "qty",
    "quantity", "sold",
}
_ITEM_SALES_HEADERS = {
    "net sales amount", "net sales", "sales amount", "amount", "total",
    "net sales total",
}
_ITEM_CAT_HEADERS = {"category", "dept", "department"}
_ITEM_SKIP_NAMES = {
    "grand total", "total", "subtotal", "totals", "category total",
}


def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.replace(".", "").strip().lower())


def _money(cell: str) -> float | None:
    raw = clean_number(cell)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _parse_item_export_headerless(rows: list[list[str]],
                                  path: pathlib.Path) -> list[ItemSale]:
    """
    The layout Silver actually exports (confirmed 2026-10-20, both stores):
    no header row, a run of blank padding columns, then per item

        category, name, vendor, current price, avg price, sold qty,
        weight qty, amt, cost, margin, <category totals x4>,
        grand qty, grand weight, grand amt, ...

    Positions are trusted only because every row also carries the report's
    own grand total: the item amounts must add up to it to the cent, or this
    raises instead of ranking the wrong column.
    """
    items: list[ItemSale] = []
    grand_qty = grand_amt = None
    for row in rows:
        i = next((j for j, c in enumerate(row) if c.strip()), None)
        if i is None or len(row) < i + 17:
            continue
        cat, name, vendor = (c.strip() for c in row[i:i + 3])
        qty = _money(row[i + 5]) or 0.0
        amt = _money(row[i + 7])
        if not name or (qty == 0 and not amt):
            continue
        items.append(ItemSale(name=name, qty=qty, net_sales=amt, category=cat,
                              vendor=vendor, avg_price=_money(row[i + 4])))
        grand_qty, grand_amt = _money(row[i + 14]), _money(row[i + 16])

    if not items and not any(c.strip() for r in rows for c in r):
        return []                       # a day with no item sales at all
    if not items:
        preview = ", ".join(c.strip() for c in (rows[0] if rows else [])
                            if c.strip())[:200]
        raise RuntimeError(
            f"Item Sales export matched neither a header row nor the known "
            f"headerless layout. First row: {preview or '(empty)'}. "
            f"File kept at {path}")

    sum_qty = round(sum(it.qty for it in items), 2)
    sum_amt = round(sum(it.net_sales or 0 for it in items), 2)
    if grand_amt is None or abs(sum_amt - grand_amt) > 0.01 \
            or (grand_qty is not None and abs(sum_qty - grand_qty) > 0.01):
        raise RuntimeError(
            f"Item Sales export columns look shifted: items add up to "
            f"{sum_qty:g} / ${sum_amt:,.2f} but the report's grand total is "
            f"{grand_qty} / {grand_amt}. File kept at {path}")

    items.sort(key=lambda it: (-it.qty, -(it.net_sales or 0), it.name.lower()))
    log.info("parsed headerless item export: %d items, %g units, $%.2f in %s",
             len(items), sum_qty, sum_amt, path.name)
    return items


def parse_item_export(path: pathlib.Path) -> list[ItemSale]:
    """
    Pull per-item qty / net sales out of an Item Sales CSV.

    Silver's export has no header row (see _parse_item_export_headerless);
    the header-matching path stays for the day it grows one.
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return []

    header_i = -1
    name_j = qty_j = sales_j = cat_j = -1
    for i, row in enumerate(rows[:15]):
        norms = [_norm_header(c) for c in row]
        nj = next((j for j, n in enumerate(norms) if n in _ITEM_NAME_HEADERS), -1)
        qj = next((j for j, n in enumerate(norms) if n in _ITEM_QTY_HEADERS), -1)
        if nj < 0 or qj < 0:
            continue
        header_i, name_j, qty_j = i, nj, qj
        sales_j = next((j for j, n in enumerate(norms)
                        if n in _ITEM_SALES_HEADERS), -1)
        cat_j = next((j for j, n in enumerate(norms)
                      if n in _ITEM_CAT_HEADERS), -1)
        break

    if header_i < 0:
        return _parse_item_export_headerless(rows, path)

    items: list[ItemSale] = []
    for row in rows[header_i + 1:]:
        if name_j >= len(row) or qty_j >= len(row):
            continue
        name = row[name_j].strip()
        if not name or _norm_header(name) in _ITEM_SKIP_NAMES:
            continue
        qty_raw = clean_number(row[qty_j])
        try:
            qty = float(qty_raw) if qty_raw else 0.0
        except ValueError:
            continue
        if qty <= 0:
            continue
        net = None
        if sales_j >= 0 and sales_j < len(row):
            sraw = clean_number(row[sales_j])
            try:
                net = float(sraw) if sraw else None
            except ValueError:
                net = None
        cat = ""
        if cat_j >= 0 and cat_j < len(row):
            cat = row[cat_j].strip()
        items.append(ItemSale(name=name, qty=qty, net_sales=net, category=cat))

    items.sort(key=lambda it: (-it.qty, -(it.net_sales or 0), it.name.lower()))
    log.info("parsed item export: %d items from header row %d in %s",
             len(items), header_i, path.name)
    return items


def harvest(page: Page) -> tuple[dict[str, str], str]:
    """
    Pull labelled numbers out of the rendered report.

    Strategy: grab all the visible text, then for each label we care about,
    take the first money-or-number token that appears after it. This is
    deliberately dumb and forgiving -- it survives layout changes that would
    break precise cell-by-cell selectors.

    Reads the report frame, not the top document -- the outer page is an ExtJS
    nav shell and contains none of these numbers.
    """
    text = report_frame(page).inner_text("body")
    metrics: dict[str, str] = {}

    for label, key in WANTED_FIELDS.items():
        idx = text.find(label)
        if idx == -1:
            continue
        window = text[idx + len(label): idx + len(label) + 60]
        m = MONEY.search(window)
        if m:
            metrics[key] = m.group(0).strip()

    return metrics, text


def run_store(store: Store, start: date, end: date,
              headless: bool) -> StoreResult:
    span = (start.isoformat() if start == end
            else f"{start.isoformat()}..{end.isoformat()}")
    result = StoreResult(store=store.name, business_day=span)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000},
                                  accept_downloads=True)
        page = ctx.new_page()
        page.set_default_timeout(30_000)
        try:
            login(page, store)
            open_store_summary(page, start, end)

            # Always keep the page text: it is what we diagnose from when the
            # numbers look wrong, regardless of which path produced them.
            page_metrics, result.raw_text = harvest(page)

            export = download_export(page, store, start, end)
            if export is not None:
                result.export_file = str(export)
                result.metrics = parse_export(export)
                result.source = "export"

            if not result.metrics:
                # Either there was no export, or it parsed to nothing. The
                # scraped page is less trustworthy but far better than a blank
                # digest, so use it and say so in the log.
                result.metrics = page_metrics
                result.source = "page-text"
                if export is not None:
                    log.warning("[%s] export parsed to nothing -- using page text",
                                store.name)

            result.ok = bool(result.metrics)
            if not result.ok:
                result.error = ("Report loaded but neither the export nor the "
                                "page yielded any expected labels.")

            # The figures must agree with the report's own arithmetic. This is
            # what licenses reading a headerless file by column position: if
            # the layout shifts, the sums stop reconciling and we refuse the
            # numbers rather than filing a confident wrong answer.
            if result.ok and result.source == "export":
                problems = verify_totals(result.metrics)
                if problems:
                    result.ok = False
                    result.error = "Totals did not reconcile -- " + "; ".join(problems)
                    log.error("[%s] %s", store.name, result.error)

            log.info("[%s] captured %d metrics via %s",
                     store.name, len(result.metrics), result.source)
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
            log.error("[%s] FAILED: %s", store.name, result.error)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = re.sub(r"\W+", "_", store.name)
            try:
                page.screenshot(path=str(FAILURES / f"{stamp}_{safe}.png"),
                                full_page=True)
                (FAILURES / f"{stamp}_{safe}.html").write_text(page.content(),
                                                              encoding="utf-8")
                log.info("failure snapshot written to %s", FAILURES)
            except Exception:
                pass
        finally:
            ctx.close()
            browser.close()
    return result


def run_store_items(store: Store, start: date, end: date,
                    headless: bool) -> StoreResult:
    """
    Item Sales report for start..end. Never writes history.csv -- that file
    is one row per store per day of Daily Summary totals.
    """
    span = (start.isoformat() if start == end
            else f"{start.isoformat()}..{end.isoformat()}")
    result = StoreResult(store=store.name, business_day=span)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000},
                                  accept_downloads=True)
        page = ctx.new_page()
        page.set_default_timeout(30_000)
        try:
            login(page, store)
            open_report(page, start, end, report_type="is")
            try:
                result.raw_text = report_frame(
                    page, _report_markers("is")).inner_text("body")
            except Exception:
                result.raw_text = ""
            export = download_export(page, store, start, end, kind="is")
            if export is None:
                raise RuntimeError(
                    "Item Sales export did not download -- the viewer "
                    "exportUrl was missing or the click path failed")
            result.export_file = str(export)
            result.items = parse_item_export(export)
            result.source = "export"
            result.ok = True
            log.info("[%s] captured %d items via export",
                     store.name, len(result.items))
        except Exception as e:
            result.ok = False
            result.error = f"{type(e).__name__}: {e}"
            log.error("[%s] ITEMS FAILED: %s", store.name, result.error)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = re.sub(r"\W+", "_", store.name)
            try:
                page.screenshot(path=str(FAILURES / f"{stamp}_{safe}_items.png"),
                                full_page=True)
                (FAILURES / f"{stamp}_{safe}_items.html").write_text(
                    page.content(), encoding="utf-8")
            except Exception:
                pass
        finally:
            ctx.close()
            browser.close()
    return result


def build_items_digest(results: list[StoreResult], label: str,
                       limit: int = 15) -> tuple[str, str]:
    """Telegram subject + HTML body for a best-sellers pull."""
    subject = f"Items {label}"
    lines: list[str] = [f"<b>Items {notify._html_escape(label)}</b>"]
    any_ok = False
    for r in results:
        lines.append("")
        if not r.ok:
            lines.append(f"<b>{notify._html_escape(r.store)}</b> — FAILED")
            lines.append(notify._html_escape(r.error or "unknown error"))
            continue
        any_ok = True
        shown = r.items[:limit]
        units = sum(it.qty for it in r.items)
        net = sum(it.net_sales or 0 for it in r.items)
        lines.append(f"<b>{notify._html_escape(r.store)}</b> "
                     f"({len(r.items)} items, {units:g} sold, ${net:,.2f})")
        if not shown:
            lines.append("  (no item sales in this range — "
                         "open-price / unnamed rings will not appear)")
            continue
        block = []
        for it in shown:
            qty = f"{it.qty:g}"
            if it.net_sales is not None:
                money = f"${it.net_sales:,.2f}"
                block.append(f"{qty:>6}  {money:>10}  {it.name}")
            else:
                block.append(f"{qty:>6}  {it.name}")
        lines.append("<pre>" + notify._html_escape("\n".join(block)) + "</pre>")
    if not any_ok:
        subject = "[CHECK] " + subject
    return subject, "\n".join(lines)


def _items_grid(stores: list[StoreResult]) -> tuple[list[list], int]:
    """
    Stores side by side -- Item | Qty | Net Sales per store, one blank column
    between -- each list sorted by dollars, then a Total row for each store on
    one shared line under the longest list, and a both-stores total under it.
    Returns (rows, index of the store Total row). Values, not formulas, so
    the totals show in phone previews that never recalculate a sheet.
    """
    width = 4 * len(stores) - 1
    blank = lambda: [None] * width
    head, cols = blank(), blank()
    lists = []
    for k, r in enumerate(stores):
        c = 4 * k
        head[c] = r.store
        cols[c:c + 3] = ["Item", "Qty", "Net Sales"]
        lists.append(sorted(r.items, key=lambda it: (-(it.net_sales or 0),
                                                     -it.qty, it.name.lower())))
    rows = [head, cols]
    for i in range(max(len(l) for l in lists)):
        row = blank()
        for k, items in enumerate(lists):
            if i < len(items):
                it = items[i]
                row[4 * k:4 * k + 3] = [it.name, it.qty, it.net_sales]
        rows.append(row)
    total = blank()
    for k, items in enumerate(lists):
        total[4 * k:4 * k + 3] = ["Total", sum(it.qty for it in items),
                                  round(sum(it.net_sales or 0 for it in items), 2)]
    rows += [blank(), total]
    total_i = len(rows) - 1
    if len(stores) > 1:
        both = blank()
        both[0:3] = ["All shops", sum(total[4 * k + 1] for k in range(len(stores))),
                     round(sum(total[4 * k + 2] for k in range(len(stores))), 2)]
        rows.append(both)
    return rows, total_i


def write_items_file(results: list[StoreResult], start: date,
                     end: date) -> pathlib.Path | None:
    """
    Every item from every store that succeeded, in one spreadsheet. The
    Telegram digest only has room for the top few; this is the full list.
    Excel when openpyxl is installed, otherwise a CSV with the same layout.
    """
    ok = [r for r in results if r.ok and r.items]
    if not ok:
        return None
    span = (start.isoformat() if start == end
            else f"{start.isoformat()}_to_{end.isoformat()}")
    rows, total_i = _items_grid(ok)
    title = (f"Item Sales  {start:%m/%d/%Y}" if start == end
             else f"Item Sales  {start:%m/%d/%Y} - {end:%m/%d/%Y}")
    try:
        return _write_items_xlsx(rows, total_i, title,
                                 EXPORTS / f"{span}_items_all_stores.xlsx")
    except Exception as e:
        log.warning("items xlsx failed (%s: %s) -- sending CSV instead",
                    type(e).__name__, e)
    dest = EXPORTS / f"{span}_items_all_stores.csv"
    with dest.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([title])
        for row in rows:
            w.writerow(["" if v is None else f"{v:g}" if isinstance(v, float)
                        and v.is_integer() else v for v in row])
    return dest


def _write_items_xlsx(rows: list[list], total_i: int, title: str,
                      dest: pathlib.Path) -> pathlib.Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Item Sales"
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=14)
    for row in rows:
        ws.append(row)

    n_stores = (len(rows[0]) + 1) // 4
    bold, fill = Font(bold=True), PatternFill("solid", fgColor="DDEBF7")
    top = Border(top=Side(style="medium"))
    for k in range(n_stores):
        c = 4 * k + 1                     # 1-based first column of this store
        ws.merge_cells(start_row=2, start_column=c, end_row=2, end_column=c + 2)
        hdr = ws.cell(row=2, column=c)
        hdr.font, hdr.alignment = Font(bold=True, size=12), Alignment(horizontal="center")
        for j in range(3):
            cell = ws.cell(row=3, column=c + j)
            cell.font, cell.fill = bold, fill
            ws.column_dimensions[get_column_letter(c + j)].width = (
                38 if j == 0 else 9 if j == 1 else 13)
        ws.column_dimensions[get_column_letter(c + 3)].width = 3
        for r in range(4, ws.max_row + 1):
            ws.cell(row=r, column=c + 1).number_format = "#,##0.##"
            ws.cell(row=r, column=c + 2).number_format = '"$"#,##0.00'
        for j in range(3):
            cell = ws.cell(row=total_i + 2, column=c + j)
            cell.font, cell.border = bold, top
    for j in range(3):                    # the all-shops line, if present
        ws.cell(row=total_i + 3, column=1 + j).font = bold
    ws.freeze_panes = "A4"
    wb.save(dest)
    return dest


# ===========================================================================
#  RETENTION
# ===========================================================================
#  Nothing here is large today -- the data dir is tens of kilobytes. But it
#  only ever grows, and the two things that grow fastest are the ones you would
#  not notice: failure screenshots (~300KB per store per failed run, so a portal
#  outage that lasts a week quietly writes 4MB of near-identical PNGs) and the
#  logs (uncapped until setup_logging started rotating them).
#
#  history.csv is NEVER pruned. It is the one irreplaceable file here.
# ===========================================================================

def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


DATE_IN_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
STAMP_IN_NAME = re.compile(r"(\d{4})(\d{2})(\d{2})_\d{6}")


def _age_days(path: pathlib.Path, from_name: bool, today: date) -> int | None:
    """
    How old a file is, in days.

    `from_name` picks the business date out of the filename instead of using
    mtime, which matters because re-pulling an old day rewrites its mtime --
    age-by-mtime would keep that file forever while its untouched neighbours
    got pruned.
    """
    if from_name:
        m = DATE_IN_NAME.search(path.name) or STAMP_IN_NAME.search(path.name)
        if m:
            try:
                return (today - date(int(m.group(1)), int(m.group(2)),
                                     int(m.group(3)))).days
            except ValueError:
                pass
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).date()
        return (today - mtime).days
    except OSError:
        return None


def prune(dry_run: bool = True) -> list[str]:
    """
    Delete aged-out artefacts. Returns the list of actions (or would-be actions).

    Retention, all overridable in .env:
      KEEP_DAILY_EXPORTS_DAYS  450  a bit over a year, so year-on-year
                                    comparison still has the raw file
      KEEP_RANGE_EXPORTS_DAYS   14  ad-hoc query results; reproducible any time
                                    by asking the bot again
      KEEP_FAILURES_DAYS        30  long enough to debug, short enough not to pile up
      KEEP_HISTORY_BACKUPS       5  only written when the column set changes
    """
    today = date.today()
    keep_daily = _int_env("KEEP_DAILY_EXPORTS_DAYS", 450)
    keep_range = _int_env("KEEP_RANGE_EXPORTS_DAYS", 14)
    keep_fail = _int_env("KEEP_FAILURES_DAYS", 30)
    keep_baks = _int_env("KEEP_HISTORY_BACKUPS", 5)

    actions: list[str] = []

    def remove(path: pathlib.Path, why: str) -> None:
        actions.append(f"{'would delete' if dry_run else 'deleted'} {path.name} ({why})")
        if not dry_run:
            try:
                path.unlink()
            except OSError as e:
                actions[-1] = f"FAILED to delete {path.name}: {e}"

    for f in sorted(EXPORTS.glob("*.csv")):
        # A range export's filename holds the period that was QUERIED, not when
        # it was pulled -- a January 2025 query run today would look 20 months
        # old. So range files age by mtime, daily files age by their date.
        is_range = "_to_" in f.name
        limit = keep_range if is_range else keep_daily
        age = _age_days(f, from_name=not is_range, today=today)
        if age is not None and age > limit:
            remove(f, f"{'range' if is_range else 'daily'} export, {age}d old "
                      f"> {limit}d")

    for f in sorted(FAILURES.iterdir()) if FAILURES.exists() else []:
        if not f.is_file():
            continue
        age = _age_days(f, from_name=True, today=today)
        if age is not None and age > keep_fail:
            remove(f, f"failure artefact, {age}d old > {keep_fail}d")

    baks = sorted(DATA.glob("history.*.bak.csv"))
    for f in baks[:-keep_baks] if len(baks) > keep_baks else []:
        remove(f, f"old history backup, keeping newest {keep_baks}")

    if actions:
        for a in actions:
            log.info("cleanup: %s", a)
    else:
        log.info("cleanup: nothing to remove")
    return actions


# ===========================================================================
#  OUTPUT
# ===========================================================================

def append_history(results: list[StoreResult]) -> None:
    """
    Build your own longitudinal dataset. Silver's own history is limited and
    you do not control it -- this file is yours forever and makes year-over-year
    comparison possible later.
    """
    old_rows: list[dict] = []
    cols = (["business_day", "store", "captured_at", "source"]
            + METRIC_KEYS)

    # If the column set has changed since this file was created -- which
    # happens whenever a metric is added -- appending would silently write
    # rows that no longer line up with the header, quietly corrupting the one
    # file here that cannot be regenerated. So migrate it instead: keep every
    # old row, re-map it onto the new columns, and back up the original first.
    if HISTORY.exists():
        with HISTORY.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            existing_cols = reader.fieldnames or []
            old_rows = list(reader)
        if existing_cols != cols:
            backup = HISTORY.with_suffix(
                f".{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak.csv")
            HISTORY.replace(backup)
            merged = list(dict.fromkeys(existing_cols + cols))
            with HISTORY.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=merged, extrasaction="ignore")
                w.writeheader()
                for row in old_rows:
                    w.writerow(row)
            cols = merged
            log.warning("history.csv columns changed -- migrated %d row(s), "
                        "original saved as %s", len(old_rows), backup.name)

    fresh = [
        {"business_day": r.business_day, "store": r.store,
         "captured_at": datetime.now().isoformat(timespec="seconds"),
         "source": r.source, **r.metrics}
        for r in results if r.ok
    ]
    if not fresh:
        return

    # Re-running a day REPLACES its row rather than adding a second one.
    # Without this, a backfill or a retry after a failed night quietly leaves
    # two rows for the same store and date, and every later total silently
    # double-counts. Keyed on (business_day, store).
    keys = {(row["business_day"], row["store"]) for row in fresh}
    kept = [row for row in (old_rows if HISTORY.exists() else [])
            if (row.get("business_day"), row.get("store")) not in keys]
    replaced = len(old_rows) - len(kept) if HISTORY.exists() else 0
    if replaced:
        log.info("replacing %d existing row(s) for the same day/store", replaced)

    # Written to a sibling temp file and then swapped into place. Deduping made
    # this a full rewrite rather than an append, and a crash partway through a
    # direct rewrite would truncate the one file in this project that cannot be
    # regenerated. os.replace is atomic on the same filesystem.
    tmp = HISTORY.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in kept + fresh:
            w.writerow(row)
    os.replace(tmp, HISTORY)
    log.info("history.csv now holds %d row(s)", len(kept) + len(fresh))


# Which metrics are currency. The CSV exports bare numbers ("1234.56"), so the
# digest adds the symbol -- otherwise a ticket count and a dollar total look
# identical at a glance on a phone.
MONEY_KEYS = {"gross_sales", "net_sales", "total_collected", "tax",
              "discounts", "returns", "tips", "avg_ticket"}


def fmt_metric(key: str, value: str) -> str:
    """Add a currency symbol to money fields, leaving counts alone."""
    v = (value or "").strip()
    if key not in MONEY_KEYS or not v or v.startswith("$"):
        return v
    if v.startswith("-"):
        return f"-${v[1:]}"
    if v.startswith("(") and v.endswith(")"):      # accounting negatives
        return f"-${v[1:-1]}"
    return f"${v}"


def build_digest(results: list[StoreResult], day: date,
                 label: str | None = None) -> tuple[str, str, str]:
    """
    Render the same run in two registers: (subject, short_body, long_body).

    The long body is the full breakdown and goes to email. The short body has
    to survive being read on a lock screen and billed by the segment, so it is
    one line per store plus a total -- nothing else. A failed store shows up in
    BOTH, because a digest that silently omits a broken store is worse than no
    digest at all.
    """
    subject = f"Sales {label or day.strftime('%a %m/%d')}"
    long_lines: list[str] = []
    short_lines: list[str] = []
    total = 0.0

    for r in results:
        if not r.ok:
            long_lines.append(f"{r.store}: CAPTURE FAILED -- {r.error}")
            long_lines.append("")
            short_lines.append(f"{r.store}: FAILED")
            continue
        gross = fmt_metric("gross_sales", r.metrics.get("gross_sales", "?"))
        # If the CSV export failed and we read the rendered page instead, say
        # so in the message. This used to be recorded only in history.csv,
        # which is not a place anyone looks until they already suspect a
        # problem -- the whole value of the signal is arriving before that.
        mark = "  (page-text)" if r.source == "page-text" else ""
        long_lines.append(f"{r.store}{mark}")
        long_lines.append(f"  {'Gross':<17}{gross}")
        for key in ("net_sales", "total_collected", "tax", "transactions",
                    "avg_ticket", "discounts", "returns", "tips", "no_sales"):
            if key in r.metrics:
                long_lines.append(
                    f"  {key.replace('_', ' ').title():<17}"
                    f"{fmt_metric(key, r.metrics[key])}")
        long_lines.append("")
        short_lines.append(f"{r.store} {gross}{mark}")
        try:
            total += float(re.sub(r"[^\d.\-]", "", gross))
        except ValueError:
            pass

    if total:
        subject += f"  ${total:,.0f}"
        long_lines.append(f"COMBINED GROSS  ${total:,.2f}")
        if len([r for r in results if r.ok]) > 1:
            short_lines.append(f"TOTAL ${total:,.2f}")

    return subject, "\n".join(short_lines), "\n".join(long_lines)


def deliver(results: list[StoreResult], day: date,
            quiet_unless_failed: bool = False) -> bool:
    """Build and send the digest. Returns True if every channel accepted it."""
    subject, short_body, long_body = build_digest(results, day)
    ok = all(r.ok for r in results)
    if not ok:
        subject = "[CHECK] " + subject

    # The 3:15am run re-pulls a day the evening run already reported, purely to
    # catch late-settling figures and to retry a failed evening. Sending a
    # second identical digest every night would train you to ignore the alerts,
    # so it stays silent when everything worked -- but a FAILURE always speaks,
    # because silence must never mean success.
    if quiet_unless_failed and ok:
        log.info("run succeeded and --quiet-unless-failed set -- "
                 "history updated, no message sent")
        return True

    errors = notify.send_all(subject, short_body, long_body, ok)
    for e in errors:
        log.error("DELIVERY ERROR %s", e)
    return not errors


# ===========================================================================
#  SETUP HELPERS
# ===========================================================================

def telegram_setup() -> int:
    """
    Find your TELEGRAM_CHAT_ID.

    Getting the token is easy; finding the chat id is the part that trips
    everyone up, because there is no page in the Telegram UI that shows it.
    The only way is to message the bot and then ask the API what came in.
    """
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("TELEGRAM_BOT_TOKEN is not set in .env yet.\n")
        print("Get one first:")
        print("  1. Open Telegram and message @BotFather")
        print("  2. Send:  /newbot   then follow the two prompts")
        print("  3. It replies with a token like 123456789:AAH...")
        print("  4. Put it in .env as TELEGRAM_BOT_TOKEN=...")
        print("  5. Run this again.")
        return 1

    try:
        me = notify.telegram_call("getMe", {})
        bot = me["result"]["username"]
        print(f"Bot token is valid: @{bot}\n")
    except Exception as e:
        print(f"Token rejected: {e}")
        return 1

    try:
        updates = notify.telegram_call("getUpdates", {})["result"]
    except Exception as e:
        print(f"Could not read updates: {e}")
        return 1

    # A bot cannot message you first -- Telegram requires the human to open the
    # conversation. So an empty result almost always means "you haven't texted
    # the bot yet", not that anything is broken.
    chats: dict[str, str] = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            label = (chat.get("title") or chat.get("username")
                     or " ".join(filter(None, [chat.get("first_name"),
                                               chat.get("last_name")])) or "?")
            chats[str(chat["id"])] = f"{label} ({chat.get('type')})"

    if not chats:
        print(f"No messages yet. Open Telegram, find @{bot}, and send it any")
        print("message ('hi' is fine) -- then run this command again.")
        print("\n(A bot can never start the conversation; you have to speak first.)")
        return 1

    print("Found these chats:\n")
    for cid, label in chats.items():
        print(f"    TELEGRAM_CHAT_ID={cid}      # {label}")
    print("\nCopy the line you want into .env, then run --test-notify.")
    return 0


# ===========================================================================
#  TEST PATH
# ===========================================================================

def test_notify() -> int:
    """
    Prove the delivery half works without touching the Back Office.

    This is deliberately runnable before the scraper is finished, because
    delivery and scraping fail for completely unrelated reasons and debugging
    them at the same time is miserable. Get a text on your phone from this
    first; then the only unknown left is the portal.
    """
    day = date.today() - timedelta(days=1)

    # Use the REAL store names from .env so the test message looks exactly
    # like the nightly one. A test that differs from production in a visible
    # way trains you to skim past the thing you are meant to be checking.
    try:
        names = [s.name for s in get_stores()]
    except SystemExit:
        names = ["Store One", "Store Two"]

    sample = [
        {"gross_sales": "$4,231.55", "net_sales": "$3,902.10",
         "transactions": "187", "avg_ticket": "$22.63",
         "discounts": "$112.00", "returns": "$41.20"},
        {"gross_sales": "$2,980.00", "net_sales": "$2,744.85",
         "transactions": "131", "avg_ticket": "$22.75",
         "discounts": "$88.50", "returns": "$0.00"},
    ]
    canned = [
        StoreResult(store=name, business_day=day.isoformat(), ok=True,
                    source="export",
                    metrics=sample[i % len(sample)])
        for i, name in enumerate(names)
    ]

    subject, short_body, long_body = build_digest(canned, day)
    channels = notify.configured_channels()
    log.info("channels: %s", ", ".join(channels) or "(none)")
    log.info("short body is %d chars / %d SMS segment(s)",
             len(short_body), notify.sms_segments(f"{subject}\n{short_body}"))

    # Show, without sending, what a broken capture looks like -- so you can see
    # the [CHECK] contract is intact without burning a second message.
    broken = canned[:-1] + [StoreResult(store=names[-1],
                                        business_day=day.isoformat(), ok=False,
                                        error="TimeoutError: login_success never appeared")]
    fs, fshort, _ = build_digest(broken, day)
    print("\n--- a FAILURE digest would read (not being sent) ---")
    print("[CHECK] " + fs)
    print(fshort)
    print("---------------------------------------------------\n")

    sent = deliver(canned, day)

    if not channels:
        print("NOTHING WAS SENT -- no delivery channels are configured.")
        print("Copy env.example to .env, fill it in, and run this again.")
        return 1
    if sent:
        print(f"TEST DIGEST SENT via {', '.join(channels)} -- check your phone/inbox.")
        return 0
    print("Delivery reported errors -- see the log above.")
    return 1


# ===========================================================================

def main() -> int:
    setup_logging(DATA / "silver_report.log")

    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="business day as YYYY-MM-DD")
    ap.add_argument("--today", action="store_true")
    ap.add_argument("--show", action="store_true", help="visible browser")
    ap.add_argument("--test-notify", action="store_true",
                    help="send a canned digest through every configured "
                         "channel without opening a browser")
    ap.add_argument("--telegram-setup", action="store_true",
                    help="look up your Telegram chat id")
    ap.add_argument("--cleanup", action="store_true",
                    help="prune aged exports/failures; previews unless "
                         "--force is also given")
    ap.add_argument("--force", action="store_true",
                    help="with --cleanup, actually delete")
    ap.add_argument("--quiet-unless-failed", action="store_true",
                    help="write history but only send a message if something "
                         "broke (used by the 3:15am retry run)")
    ap.add_argument("--items", action="store_true",
                    help="pull Item Sales (reportType=is) instead of Daily "
                         "Summary; prints top items, does not write history")
    ap.add_argument("--rotate-dry-run", action="store_true",
                    help="if Silver forces a password change, fill the form "
                         "and check Save enables, but do not click it")
    args = ap.parse_args()

    global ROTATE_DRY_RUN
    ROTATE_DRY_RUN = args.rotate_dry_run

    if args.telegram_setup:
        return telegram_setup()
    if args.cleanup:
        actions = prune(dry_run=not args.force)
        if actions and not args.force:
            print(f"\n{len(actions)} file(s) would be removed. "
                  f"Re-run with --force to actually delete.")
        return 0
    if args.test_notify:
        return test_notify()

    if args.date:
        day = date.fromisoformat(args.date)
    elif args.today:
        day = date.today()
    else:
        day = date.today() - timedelta(days=1)

    log.info("=== run for business day %s%s ===",
             day, " (items)" if args.items else "")

    if args.items:
        results = [run_store_items(s, day, day, headless=not args.show)
                   for s in get_stores()]
        subject, body = build_items_digest(results, day.isoformat())
        print(re.sub(r"<[^>]+>", "", body))
        for r in results:
            if not r.ok:
                return 1
        return 0

    results = [run_store(s, day, day, headless=not args.show)
               for s in get_stores()]
    append_history(results)

    # Keep the raw page text from the last run around. Written BEFORE delivery
    # so a send failure never costs us the capture we just paid for.
    (DATA / "last_raw.json").write_text(
        json.dumps({r.store: r.raw_text for r in results}, indent=2),
        encoding="utf-8")

    delivered = deliver(results, day,
                        quiet_unless_failed=args.quiet_unless_failed)

    # Housekeeping last, so a prune failure can never cost us the capture or
    # the digest that already succeeded.
    try:
        prune(dry_run=False)
    except Exception as e:
        log.warning("cleanup skipped: %s: %s", type(e).__name__, e)

    if not all(r.ok for r in results):
        return 1
    return 0 if delivered else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.critical("unhandled error\n%s", traceback.format_exc())
        sys.exit(2)
