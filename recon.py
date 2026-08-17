#!/usr/bin/env python3
"""
recon.py  --  PHASE 1: Find out what the Silver Back Office actually looks like.

This script does NOT automate anything yet. It opens a real, visible browser,
logs you in, then waits while YOU click through to the report. When you press
Enter in the terminal, it saves a snapshot of the page so we can figure out the
correct selectors for the real script.

It also captures the Export flow: any file the site downloads is saved to
./recon_output/downloads/ and, if it is a CSV, previewed on screen. That
preview is the single most useful thing this script produces -- the exact
column layout of the export is what the real parser gets written against.

Nothing is sent anywhere. Everything is written to ./recon_output/ on your machine.

RUN IT LIKE THIS:
    python recon.py

You will be prompted for your username and password (they are not stored).
"""

import getpass
import pathlib
import sys
from datetime import datetime

from playwright.sync_api import sync_playwright

LOGIN_URL = "https://mystore.ncrvoyix.com/app/Account/LogOn"
OUT = pathlib.Path("recon_output")
OUT.mkdir(exist_ok=True)
DOWNLOADS = OUT / "downloads"
DOWNLOADS.mkdir(exist_ok=True)


def on_download(download) -> None:
    """
    Save every file the site hands us, and preview it immediately.

    Registered as an event listener rather than wrapped around a specific
    click, because during recon YOU are doing the clicking and we cannot
    predict exactly when the download fires.
    """
    name = download.suggested_filename
    dest = DOWNLOADS / f"{datetime.now().strftime('%H%M%S')}_{name}"
    try:
        download.save_as(str(dest))
    except Exception as e:
        print(f"  !! download '{name}' failed to save: {e}")
        return

    print(f"\n  -> DOWNLOADED: {dest}  ({dest.stat().st_size:,} bytes)")

    # Preview text-ish formats inline. This is the single most useful thing
    # recon produces now -- the exact column layout of the export is what the
    # parser gets written against.
    if dest.suffix.lower() in (".csv", ".txt", ".tsv"):
        try:
            lines = dest.read_text(encoding="utf-8-sig",
                                   errors="replace").splitlines()
        except Exception as e:
            print(f"     (could not read: {e})")
            return
        preview = OUT / f"{dest.stem}_PREVIEW.txt"
        head = lines[:40]
        preview.write_text("\n".join(head), encoding="utf-8")
        print(f"     first {len(head)} of {len(lines)} lines "
              f"(also saved to {preview.name}):")
        for ln in head:
            print(f"       | {ln[:160]}")
    else:
        print(f"     ({dest.suffix} is not plain text -- send me the file itself)")


def inventory(ctx, lines: list) -> None:
    """
    Element inventory for one document -- a page OR a single iframe.

    Split out from snapshot() because this portal nests the actual report two
    frames deep, and query_selector_all on the top page cannot see into a
    frame. Running this only on the page produced two byte-identical snapshots
    and a completely empty export-menu capture, because everything we wanted
    was inside an iframe the whole time.
    """
    for tag in ("input", "select", "button", "a"):
        lines.append(f"----- <{tag}> elements -----")
        for el in ctx.query_selector_all(tag):
            try:
                attrs = {}
                for a in ("id", "name", "type", "value", "class", "href",
                          "placeholder"):
                    v = el.get_attribute(a)
                    if v:
                        attrs[a] = v[:80]
                text = (el.inner_text() or "").strip().replace("\n", " ")[:60]
                if attrs or text:
                    lines.append(f"  text={text!r}  {attrs}")
            except Exception:
                continue
        lines.append("")

    # ExtJS renders menu items as <span>/<div>, NOT as <a> or <button>, so the
    # loop above misses them entirely -- including the Export format menu that
    # step [3/4] exists to capture. These are the ones we actually need in
    # order to write a selector for "CSV".
    lines.append("----- ExtJS / toolbar items (the clickable bits) -----")
    try:
        ext = ctx.query_selector_all(
            "[class*='x-menu-item'], [class*='x-boundlist-item'], "
            "[class*='x-btn-inner'], [class*='x-tab-inner'], "
            "[class*='x-column-header-text'], [class*='trv-'], "
            "[role='menuitem'], [title]"
        )
    except Exception:
        ext = []
    for el in ext:
        try:
            text = ((el.inner_text() or "").strip()
                    or (el.get_attribute("title") or "").strip())
            if not text:
                continue
            text = text.replace("\n", " ")[:60]
            cls = (el.get_attribute("class") or "")[:60]
            eid = el.get_attribute("id") or ""
            # Flag ids that ExtJS auto-generates -- they change between
            # sessions and must never end up in a selector.
            warn = "  <-- AUTO-GENERATED ID, DO NOT USE" if eid.startswith(
                ("ext-comp", "ext-gen", "ext-element", "ext-button")) else ""
            lines.append(f"  text={text!r}  id={eid!r}  class={cls!r}{warn}")
        except Exception:
            continue
    lines.append("")


def snapshot(page, label: str) -> None:
    """Save everything we might need to identify elements on this page."""
    stamp = datetime.now().strftime("%H%M%S")
    base = OUT / f"{stamp}_{label}"

    page.screenshot(path=f"{base}.png", full_page=True)
    (pathlib.Path(f"{base}.html")).write_text(page.content(), encoding="utf-8")

    # A condensed, human-readable inventory of every clickable / input element.
    # This is the part that matters most -- it's what we use to write selectors.
    lines = [f"URL: {page.url}", f"TITLE: {page.title()}", ""]

    lines.append("=" * 70)
    lines.append("===== TOP-LEVEL DOCUMENT =====")
    lines.append("=" * 70)
    inventory(page, lines)

    # Now every iframe. The report and its Export control live in here, not on
    # the page above -- this is the section that actually matters for this
    # portal, and the reason the .html file alone is not enough.
    frames = [f for f in page.frames if f is not page.main_frame]
    for i, f in enumerate(frames, 1):
        lines.append("=" * 70)
        lines.append(f"===== IFRAME {i}/{len(frames)} =====")
        lines.append(f"  url: {f.url}")
        lines.append("=" * 70)
        try:
            body = f.inner_text("body")[:400].replace("\n", " | ")
            lines.append(f"  text preview: {body}")
            lines.append("")
        except Exception as e:
            lines.append(f"  (could not read frame body: {type(e).__name__})")
            lines.append("")
        inventory(f, lines)

        # Dump each frame's HTML separately -- page.content() does not include
        # frame contents, so without this the markup is simply not captured.
        try:
            (OUT / f"{stamp}_{label}_frame{i}.html").write_text(
                f.content(), encoding="utf-8")
        except Exception:
            pass

    # Also dump any iframes -- old portals often nest reports inside them,
    # and if so, Playwright has to be pointed at the frame, not the page.
    frames = [f.url for f in page.frames]
    lines.append(f"===== FRAMES ({len(frames)}) =====")
    lines.extend(f"  {u}" for u in frames)

    (pathlib.Path(f"{base}.txt")).write_text("\n".join(lines), encoding="utf-8")
    print(f"  -> saved {base}.png / .html / .txt")


def main() -> None:
    print("NCR Silver recon. Credentials are used once and never written to disk.\n")
    user = input("Back Office username: ").strip()
    pw = getpass.getpass("Back Office password: ")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=250)
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000},
                                  accept_downloads=True)
        page = ctx.new_page()
        page.on("download", on_download)

        print(f"\nOpening {LOGIN_URL} ...")
        page.goto(LOGIN_URL, wait_until="load")
        snapshot(page, "01_login_page")

        # Best-effort auto-login. If the selectors are wrong it does not matter --
        # you can just log in by hand in the window that opened.
        # These three are confirmed against the live login page, not guesses.
        # It is an ExtJS app with no <form> element and a <button type="button">
        # -- the usual input[type=submit] would never have matched.
        try:
            page.fill("#login-user-name-inputEl", user, timeout=10000)
            page.fill("#password-inputEl", pw, timeout=10000)
            page.click("#Login-btnEl", timeout=10000)
            page.wait_for_load_state("networkidle", timeout=30000)
            print("Auto-login attempted.")
        except Exception as e:
            print(f"Auto-login did not work ({type(e).__name__}). Log in manually "
                  f"in the browser window -- that is completely fine.")

        input("\n[1/4] Log in if needed, then press Enter here to snapshot the "
              "landing page...")
        snapshot(page, "02_after_login")

        print("\n[2/4] Now click through to Results > Store Summary.")
        print("      *** SET THE DATE TO A BUSY DAY THAT ALREADY HAPPENED ***")
        print("      Not today. A day with real sales on it -- last Saturday is")
        print("      ideal. An export for a day with no business yet comes out")
        print("      with every figure blank, which tells us nothing about")
        print("      where the numbers actually land in the file.")
        input("      When real numbers are on screen, press Enter here...")
        snapshot(page, "03_store_summary")

        # The export is almost certainly a two-step interaction: a button that
        # opens a menu, then a format inside it. We need the DOM of the OPEN
        # menu to write a selector for the CSV entry, and that menu usually
        # vanishes on blur -- so it gets its own capture step before the click.
        input("\n[3/4] Click the Export button so the format menu OPENS, but do\n"
              "      NOT pick a format yet. With the menu open, press Enter...")
        snapshot(page, "04_export_menu_open")

        print("\n[4/4] Now click CSV. The download is captured automatically and")
        print("      previewed below -- you do not need to save it anywhere.")
        input("      Press Enter here once the download has finished...")
        snapshot(page, "05_after_export")

        ctx.close()
        browser.close()

    print(f"\nDone. Everything is in ./{OUT}/")
    print("\nSend me, in priority order:")
    print(f"  1. {OUT}/*_PREVIEW.txt        <- the CSV shape. Most important.")
    print(f"  2. {OUT}/*04_export_menu_open.txt  <- how to click CSV")
    print(f"  3. {OUT}/*.txt               <- the rest of the element inventories")
    print("The .png files help too. Skip the .html files unless I ask.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
