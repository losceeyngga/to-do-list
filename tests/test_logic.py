import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401  (sets up sys.path and library stand-ins)

from scheduling_logic import build_spec, compute_next_run, describe_schedule
from utils import UserError, parse_date, parse_interval, parse_time, parse_weekdays

TZ = ZoneInfo("Asia/Manila")


def at(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


def spec(**kw):
    base = dict(schedule_type="daily", time_hhmm="08:00", weekdays=[], month_day=None, run_date=None,
                interval_minutes=None, start_date=None, end_date=None)
    base.update(kw)
    return base


class NextRunTests(unittest.TestCase):
    def test_daily_before_and_after_time(self):
        self.assertEqual(compute_next_run(spec(), at(2026, 11, 6, 7, 0), TZ), at(2026, 11, 6, 8, 0))
        self.assertEqual(compute_next_run(spec(), at(2026, 11, 6, 8, 0), TZ), at(2026, 11, 7, 8, 0))
        self.assertEqual(compute_next_run(spec(), at(2026, 11, 6, 13, 5), TZ), at(2026, 11, 7, 8, 0))

    def test_weekly_mon_wed_fri(self):
        s = spec(schedule_type="weekly", weekdays=[0, 2, 4])
        # 2026-11-06 is a Friday
        self.assertEqual(compute_next_run(s, at(2026, 11, 6, 7), TZ), at(2026, 11, 6, 8))
        self.assertEqual(compute_next_run(s, at(2026, 11, 6, 9), TZ), at(2026, 11, 9, 8))   # Monday
        self.assertEqual(compute_next_run(s, at(2026, 11, 9, 9), TZ), at(2026, 11, 11, 8))  # Wednesday

    def test_monthly_clamps_to_short_months(self):
        s = spec(schedule_type="monthly", month_day=31)
        self.assertEqual(compute_next_run(s, at(2027, 1, 31, 9), TZ), at(2027, 2, 28, 8))
        self.assertEqual(compute_next_run(s, at(2027, 2, 28, 9), TZ), at(2027, 3, 31, 8))
        self.assertEqual(compute_next_run(spec(schedule_type="monthly", month_day=15),
                                          at(2026, 12, 20), TZ), at(2027, 1, 15, 8))

    def test_hourly_uses_minute(self):
        s = spec(schedule_type="hourly", time_hhmm="08:30")
        self.assertEqual(compute_next_run(s, at(2026, 11, 6, 13, 5), TZ), at(2026, 11, 6, 13, 30))
        self.assertEqual(compute_next_run(s, at(2026, 11, 6, 13, 30), TZ), at(2026, 11, 6, 14, 30))

    def test_once(self):
        s = spec(schedule_type="once", run_date="2026-11-20")
        self.assertEqual(compute_next_run(s, at(2026, 11, 6), TZ), at(2026, 11, 20, 8))
        self.assertIsNone(compute_next_run(s, at(2026, 11, 20, 8), TZ))
        self.assertIsNone(compute_next_run(s, at(2026, 11, 21), TZ))

    def test_custom_interval_is_anchored(self):
        s = spec(schedule_type="custom", interval_minutes=90, start_date="2026-11-06")
        self.assertEqual(compute_next_run(s, at(2026, 11, 6, 7), TZ), at(2026, 11, 6, 8))
        self.assertEqual(compute_next_run(s, at(2026, 11, 6, 8), TZ), at(2026, 11, 6, 9, 30))
        self.assertEqual(compute_next_run(s, at(2026, 11, 6, 9, 31), TZ), at(2026, 11, 6, 11))

    def test_start_and_end_dates(self):
        s = spec(start_date="2026-11-10", end_date="2026-11-12")
        self.assertEqual(compute_next_run(s, at(2026, 11, 6), TZ), at(2026, 11, 10, 8))
        self.assertEqual(compute_next_run(s, at(2026, 11, 11, 9), TZ), at(2026, 11, 12, 8))
        self.assertIsNone(compute_next_run(s, at(2026, 11, 12, 9), TZ))

    def test_describe(self):
        self.assertEqual(describe_schedule(spec()), "Every day at 8:00 AM")
        self.assertIn("Mon, Wed, Fri", describe_schedule(spec(schedule_type="weekly", weekdays=[0, 2, 4])))


class BuildSpecTests(unittest.TestCase):
    now = at(2026, 11, 6, 13, 0)

    def test_weekly_requires_weekdays(self):
        with self.assertRaises(UserError):
            build_spec(tz=TZ, now=self.now, frequency="weekly")

    def test_once_in_past_rejected(self):
        with self.assertRaises(UserError):
            build_spec(tz=TZ, now=self.now, frequency="once", run_date_text="2026-11-01")

    def test_end_before_start_rejected(self):
        with self.assertRaises(UserError):
            build_spec(tz=TZ, now=self.now, frequency="daily", start_text="2026-12-05", end_text="2026-12-01")

    def test_edit_keeps_unchanged_values(self):
        base = spec(schedule_type="weekly", weekdays=[0, 2, 4], time_hhmm="08:00")
        new, nxt = build_spec(tz=TZ, now=self.now, base=base, time_text="9:30 PM")
        self.assertEqual(new["weekdays"], [0, 2, 4])
        self.assertEqual(new["time_hhmm"], "21:30")
        self.assertEqual(nxt, at(2026, 11, 6, 21, 30))

    def test_changing_type_clears_irrelevant_fields(self):
        base = spec(schedule_type="weekly", weekdays=[0, 2, 4])
        new, _ = build_spec(tz=TZ, now=self.now, base=base, frequency="daily")
        self.assertIsNone(new["weekdays"] or None)


class ParsingTests(unittest.TestCase):
    def test_time(self):
        for text, want in [("8:00 AM", "08:00"), ("8am", "08:00"), ("12 PM", "12:00"), ("12:30 am", "00:30"),
                           ("20:30", "20:30"), ("9:05 p.m.", "21:05"), ("noon", "12:00")]:
            self.assertEqual(parse_time(text), want, text)
        for bad in ["25:00", "13 PM", "banana", "8:75", ""]:
            with self.assertRaises(UserError):
                parse_time(bad)

    def test_date(self):
        today = datetime(2026, 11, 6).date()
        for text in ["2026-11-20", "November 20, 2026", "Nov 20 2026", "11/20/2026", "20 November 2026", "November 20th, 2026"]:
            self.assertEqual(parse_date(text, today).isoformat(), "2026-11-20", text)
        self.assertEqual(parse_date("tomorrow", today).isoformat(), "2026-11-07")
        for bad in ["November 31, 2026", "soon", "2026-13-01", "November 20"]:
            with self.assertRaises(UserError):
                parse_date(bad, today)

    def test_weekdays(self):
        self.assertEqual(parse_weekdays("mon,wed,fri"), [0, 2, 4])
        self.assertEqual(parse_weekdays("Monday and Friday"), [0, 4])
        self.assertEqual(parse_weekdays("mon-fri"), [0, 1, 2, 3, 4])
        self.assertEqual(parse_weekdays("weekends"), [5, 6])
        with self.assertRaises(UserError):
            parse_weekdays("funday")

    def test_interval(self):
        self.assertEqual(parse_interval("30m"), 30)
        self.assertEqual(parse_interval("2h"), 120)
        self.assertEqual(parse_interval("1d"), 1440)
        self.assertEqual(parse_interval("1h30m"), 90)
        for bad in ["1", "2 weeks", "3m", "abc", "0h"]:
            with self.assertRaises(UserError):
                parse_interval(bad)


if __name__ == "__main__":
    unittest.main()
