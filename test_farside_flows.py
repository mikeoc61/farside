"""Regression tests for :mod:`farside_flows`.

Stdlib ``unittest`` only, so ``python3 -m unittest`` runs them with no
dependencies; the ``parse_table`` cases need BeautifulSoup and skip without it.

The cases here pin the three distinctions this module keeps getting wrong,
each of which turns a bad row into a plausible-looking number rather than an
error: a day with no published ``Total`` is not a complete day, a reported
``0.0`` is not a missing cell, and a market closure is not a session.
"""

import unittest

import farside_flows as ff

CFG = {"asset": "test", "lead": "AAA", "funds": ("AAA", "BBB")}
FUNDS = CFG["funds"]


def row(date, aaa=None, bbb=None, total=None):
    return {"date": date, "AAA": aaa, "BBB": bbb, "Total": total}


def outflow_days(n, start=1, each=-20.0):
    """``n`` consecutive fully-reported outflow days of ``each``."""
    return [
        row(f"{start + i:02d} Jan 2026", aaa=each / 2, bbb=each / 2, total=each)
        for i in range(n)
    ]


class NoPublishedTotal(unittest.TestCase):
    """A day whose funds posted but whose ``Total`` cell stayed blank."""

    def setUp(self):
        # Fourteen outflow days, day 11 fund-complete with no Total.
        self.days = outflow_days(14)
        self.gap = self.days[10]
        self.gap["Total"] = None

    def summary(self, days=None, windows=ff.DEFAULT_WINDOWS):
        return ff.summarize(days if days is not None else self.days, CFG, windows)

    def test_excluded_from_days_complete(self):
        self.assertEqual(self.summary()["days_complete"], 13)

    def test_window_is_not_short_under_a_full_label(self):
        """The 5-day net must cover five *usable* days, or admit it does not."""
        w = self.summary(windows=5)["windows"][0]
        if w["covered"]:
            self.assertEqual(w["total"], -100.0)
            self.assertEqual(len(w["dates"]), 5)
            self.assertNotIn(self.gap["date"], w["dates"])
        else:
            self.assertIsNone(w["total"])

    def test_streak_counts_across_the_gap(self):
        s = self.summary()
        self.assertEqual(s["streak_sign"], "outflow")
        self.assertEqual(s["streak_days"], 13)

    def test_as_newest_row_it_falls_back_rather_than_reporting_no_total(self):
        """Documented behaviour: the day goes absent, never through as a None.

        It is deliberately not surfaced as ``partial`` -- ``partial_pending``
        means "a tracked fund is still to report", which is not what happened
        here. Absent-and-honest beats present-and-empty; surfacing it properly
        needs its own wording and the case has never occurred in real data.
        """
        days = outflow_days(3) + [row("09 Jan 2026", aaa=-5.0, bbb=-5.0)]
        s = self.summary(days)
        self.assertEqual(s["as_of"], "03 Jan 2026")
        self.assertIsNotNone(s["latest_total"])
        self.assertIsNotNone(s["latest_lead"])
        self.assertEqual(s["days_complete"], 3)
        self.assertFalse(s["partial_pending"])
        self.assertIsNone(s["partial"])

    def test_no_usable_day_reports_nothing_rather_than_zero(self):
        s = self.summary([row("01 Jan 2026", aaa=-5.0, bbb=-5.0)])
        self.assertEqual(s["days_complete"], 0)
        self.assertIsNone(s["as_of"])
        self.assertIsNone(s["latest_total"])
        self.assertEqual(s["streak_days"], 0)


class WindowCoverage(unittest.TestCase):
    def test_uncovered_window_reports_none_not_a_short_sum(self):
        w = ff._window_net(outflow_days(3), "AAA", 5)
        self.assertFalse(w["covered"])
        self.assertIsNone(w["total"])
        self.assertIsNone(w["lead"])
        self.assertEqual(w["days_available"], 3)

    def test_covered_window_sums_exactly_the_requested_days(self):
        w = ff._window_net(outflow_days(10), "AAA", 5)
        self.assertTrue(w["covered"])
        self.assertEqual(w["total"], -100.0)
        self.assertEqual(w["lead"], -50.0)


class ReportedFilter(unittest.TestCase):
    """Blankness marks a non-day; ``0.0`` is a number the site published."""

    def test_market_closure_is_dropped(self):
        closure = row("01 Jan 2026", total=0.0)  # funds blank, Total 0.0
        self.assertEqual(ff._reported([closure], FUNDS), [])

    def test_all_zero_trading_session_is_kept(self):
        session = row("02 Jan 2026", aaa=0.0, bbb=0.0, total=0.0)
        self.assertEqual(ff._reported([session], FUNDS), [session])

    def test_trailing_unpublished_row_is_dropped(self):
        self.assertEqual(ff._reported([row("03 Jan 2026")], FUNDS), [])

    def test_partially_reported_day_is_kept(self):
        partial = row("04 Jan 2026", aaa=-5.0, total=-5.0)
        self.assertEqual(ff._reported([partial], FUNDS), [partial])

    def test_closure_never_becomes_the_partial_headline(self):
        days = outflow_days(3) + [row("09 Jan 2026", total=0.0)]
        s = ff.summarize(days, CFG)
        self.assertFalse(s["partial_pending"])
        self.assertIsNone(s["partial"])
        self.assertEqual(s["as_of"], "03 Jan 2026")

    def test_all_zero_session_counts_as_a_complete_day(self):
        days = outflow_days(3) + [row("09 Jan 2026", aaa=0.0, bbb=0.0, total=0.0)]
        s = ff.summarize(days, CFG)
        self.assertEqual(s["days_complete"], 4)
        self.assertEqual(s["as_of"], "09 Jan 2026")
        self.assertEqual(s["latest_total"], 0.0)


class ParseFlow(unittest.TestCase):
    def test_reported_zero_is_distinct_from_a_blank_cell(self):
        self.assertEqual(ff.parse_flow("0.0"), 0.0)
        self.assertEqual(ff.parse_flow("0"), 0.0)
        self.assertIsNone(ff.parse_flow(""))
        self.assertIsNone(ff.parse_flow("-"))

    def test_formatting_quirks(self):
        self.assertEqual(ff.parse_flow("(444.5)"), -444.5)
        self.assertEqual(ff.parse_flow("1,234.5"), 1234.5)
        self.assertEqual(ff.parse_flow("–12.0"), -12.0)


def _cells(*vals):
    return "".join(f"<td><span>{v}</span></td>" for v in vals)


class ParseTable(unittest.TestCase):
    def setUp(self):
        try:
            import bs4  # noqa: F401
        except ImportError:
            self.skipTest("beautifulsoup4 not installed")

    def test_stray_row_does_not_duplicate_the_first_day(self):
        """Farside opens ``<tbody>`` with a stray empty ``<tr>``.

        The parser nests the real rows inside it, so that wrapper reports every
        descendant cell -- and its leading cells are the first row's, which
        surfaced as a duplicate of each asset's launch day.
        """
        html = (
            "<table>"
            "<thead><tr><th>Date</th><th>AAA</th><th>BBB</th><th>Total</th></tr></thead>"
            "<tbody><tr>"  # stray, as the live page emits it
            "<tr>" + _cells("01 Jan 2026", "1.0", "2.0", "3.0") + "</tr>"
            "<tr>" + _cells("02 Jan 2026", "4.0", "5.0", "9.0") + "</tr>"
            "</tbody></table>"
        )
        # The fixture must actually reproduce the nesting, or it proves nothing.
        from bs4 import BeautifulSoup
        trs = BeautifulSoup(html, "html.parser").find("tbody").find_all("tr")
        self.assertTrue(any(tr.find("tr") is not None for tr in trs))

        data = ff.parse_table(html, CFG)
        self.assertEqual([r["date"] for r in data], ["01 Jan 2026", "02 Jan 2026"])
        self.assertEqual(data[0]["Total"], 3.0)
        self.assertEqual(data[1]["Total"], 9.0)


if __name__ == "__main__":
    unittest.main()
