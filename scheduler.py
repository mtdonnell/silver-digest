#!/usr/bin/env python3
"""
scheduler.py  --  Runs the nightly job forever, inside the container.

Why not cron? Because cron in a container is a reliability trap: it runs with
a stripped environment, so the variables docker passes in are simply absent by
the time your job starts, and it fails silently at 3am with no output anywhere
you would think to look. A loop that sleeps is boring, has the real
environment, and logs to stdout where `docker compose logs` can see it.

Controlled by three environment variables:

    TZ           IANA zone name, e.g. America/Chicago. NOT an offset --
                 a fixed offset would be an hour wrong for half the year.
    RUN_AT       24-hour local time, default 03:15
    RUN_ON_START run once immediately at container start, for testing
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_AT = os.environ.get("RUN_AT", "03:15")
TZ_NAME = os.environ.get("TZ", "America/Chicago")


def say(msg: str) -> None:
    """Timestamped line to stdout, which is where docker logs looks."""
    print(f"[scheduler {datetime.now(tz()).isoformat(timespec='seconds')}] {msg}",
          flush=True)


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(TZ_NAME)
    except Exception:
        print(f"[scheduler] WARNING: unknown TZ {TZ_NAME!r}, falling back to UTC. "
              f"Is tzdata installed?", flush=True)
        return ZoneInfo("UTC")


def next_run(now: datetime) -> datetime:
    """Next occurrence of RUN_AT, today or tomorrow, in local time."""
    hh, mm = (int(x) for x in RUN_AT.split(":", 1))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run_once() -> int:
    say("starting silver_report.py")
    proc = subprocess.run([sys.executable, os.path.join(HERE, "silver_report.py")],
                          cwd=HERE)
    # Exit codes are meaningful here: 1 = a capture failed, 3 = captured fine
    # but delivery failed. Both are worth shouting about in the container log,
    # because a container nobody opens is exactly where silence hides.
    label = {0: "OK", 1: "CAPTURE FAILED", 2: "CRASHED",
             3: "DELIVERY FAILED"}.get(proc.returncode, "UNKNOWN")
    say(f"finished: exit {proc.returncode} ({label})")
    return proc.returncode


def main() -> None:
    say(f"up. timezone={TZ_NAME} run_at={RUN_AT}")

    if os.environ.get("RUN_ON_START", "").strip().lower() in ("1", "true", "yes"):
        say("RUN_ON_START set -- running immediately")
        run_once()

    while True:
        now = datetime.now(tz())
        target = next_run(now)
        wait = (target - now).total_seconds()
        say(f"next run {target.isoformat(timespec='seconds')} "
            f"(in {wait / 3600:.1f}h)")

        # Sleep in chunks rather than one long sleep: a single multi-hour
        # sleep does not notice a DST shift that happens while it is asleep,
        # and it makes the container slow to stop. Recomputing the target each
        # time means the clock change is picked up within the minute.
        while True:
            now = datetime.now(tz())
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(60, remaining))

        run_once()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("stopped")
