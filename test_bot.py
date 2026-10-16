"""Date-phrase and item-export parsing. No browser, no network."""
from __future__ import annotations

import csv
import tempfile
from datetime import date
from pathlib import Path

import bot
import silver_report as sr


def test_last_n_days_inclusive():
    today = date(2026, 9, 2)
    s, e, label = bot.parse_range("last 10 days", today)
    assert s == date(2026, 8, 24)
    assert e == today
    assert "10" in label


def test_last_7d_shortcut():
    today = date(2026, 9, 2)
    s, e, _ = bot.parse_range("last 7d", today)
    assert s == date(2026, 8, 27)
    assert e == today


def test_today_yesterday_week():
    today = date(2026, 9, 2)  # Wednesday
    s, e, _ = bot.parse_range("today", today)
    assert s == e == today
    s, e, _ = bot.parse_range("yesterday", today)
    assert s == e == date(2026, 9, 1)
    s, e, _ = bot.parse_range("last week", today)
    assert s == date(2026, 8, 24)
    assert e == date(2026, 8, 30)


def test_bare_unknown_is_none():
    assert bot.parse_range("", date(2026, 9, 2)) is None
    assert bot.parse_range("best sellers", date(2026, 9, 2)) is None


def test_item_export_header_shape():
    d = Path(tempfile.mkdtemp(prefix="silver-test-"))
    p = d / "items.csv"
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Category", "Item Name", "Sold Quantity",
                    "Avg. Price", "Net Sales Amount"])
        w.writerow(["Disposables", "Geek Bar 15k", "12", "19.99", "239.88"])
        w.writerow(["Disposables", "Lost Mary", "8", "19.99", "159.92"])
        w.writerow(["", "Grand Total", "20", "", "399.80"])
        w.writerow(["Accessories", "Sleeve", "0", "2.99", "0.00"])
    items = sr.parse_item_export(p)
    assert [i.name for i in items] == ["Geek Bar 15k", "Lost Mary"]
    assert items[0].qty == 12
    assert items[0].net_sales == 239.88


def test_item_export_unknown_header_fails():
    d = Path(tempfile.mkdtemp(prefix="silver-test-"))
    p = d / "bad.csv"
    p.write_text("foo,bar,baz\n1,2,3\n", encoding="utf-8")
    try:
        sr.parse_item_export(p)
    except RuntimeError as e:
        assert "no recognisable header" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
