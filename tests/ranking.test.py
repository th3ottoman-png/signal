#!/usr/bin/env python3
"""Tests for story ranking: the per-source floor and cadence-aware recency.

Both exist because recency-weighted ranking structurally starves slow-cadence
sources. Without them a configured source can fetch perfectly and still never
render a single story.

Run: python3 tests/ranking.test.py
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fetch as F


def mk(source, score, slug=""):
    """A minimal ranked item. `slug` keeps urls distinct."""
    return {
        "source": source,
        "score": score,
        "title": f"{source} {slug or score}",
        "url": f"https://example.test/{source}/{slug or score}",
        "published": "2026-01-01T00:00:00+00:00",
        "weight": 3,
    }


def by_score(items):
    return sorted(items, key=lambda x: x["score"], reverse=True)


def stamps(source, count, gap_hours, start=(2026, 1, 1)):
    """`count` posts from one source, `gap_hours` apart."""
    base = datetime(*start, tzinfo=timezone.utc)
    return [{"source": source,
             "published": F.iso(base + timedelta(hours=gap_hours * i))}
            for i in range(count)]


class SourceFloor(unittest.TestCase):
    """apply_source_floor: every configured source gets a slot."""

    def test_a_source_that_loses_the_cap_still_gets_in(self):
        # THE regression. A naive items[:cap] drops "slow" entirely, which is
        # exactly the bug this function exists to prevent.
        items = by_score([mk("fast", 9.0 - i) for i in range(10)]
                         + [mk("slow", 0.5)])
        out = F.apply_source_floor(items, floor=1, cap=5)
        self.assertIn("slow", {i["source"] for i in out})

    def test_cap_is_never_exceeded(self):
        items = by_score([mk("fast", 9.0 - i) for i in range(10)]
                         + [mk("slow", 0.5)])
        self.assertLessEqual(len(F.apply_source_floor(items, 1, 5)), 5)

    def test_pageship_full_when_no_source_needs_rescuing(self):
        # If every source is already inside the budget, the reserved slots go
        # unused and must be handed back, or the feed ships short.
        items = by_score([mk("a", 9.0 - i * 0.1) for i in range(3)]
                         + [mk("b", 8.0 - i * 0.1) for i in range(3)])
        self.assertEqual(len(F.apply_source_floor(items, 1, 6)), 6)

    def test_floor_takes_the_sources_best_item(self):
        items = by_score([mk("fast", 9.0 - i) for i in range(10)]
                         + [mk("slow", 0.4, "worst"), mk("slow", 0.5, "best")])
        out = F.apply_source_floor(items, 1, 5)
        slow = [i for i in out if i["source"] == "slow"]
        self.assertEqual(len(slow), 1)
        self.assertEqual(slow[0]["score"], 0.5)

    def test_floor_of_two_takes_the_two_best(self):
        items = by_score([mk("fast", 9.0 - i) for i in range(10)]
                         + [mk("slow", s, str(s)) for s in (0.1, 0.5, 0.3)])
        out = F.apply_source_floor(items, 2, 6)
        self.assertEqual(sorted(i["score"] for i in out if i["source"] == "slow"),
                         [0.3, 0.5])

    def test_output_stays_sorted_by_score(self):
        items = by_score([mk("a", 9.0 - i * 0.3) for i in range(6)]
                         + [mk("b", 8.0 - i * 0.3) for i in range(6)]
                         + [mk("c", 0.2)])
        out = F.apply_source_floor(items, 1, 8)
        self.assertEqual([i["score"] for i in out],
                         sorted((i["score"] for i in out), reverse=True))

    def test_zero_floor_is_a_plain_cap(self):
        items = by_score([mk("a", 9.0 - i * 0.1) for i in range(20)])
        self.assertEqual(F.apply_source_floor(items, 0, 7), items[:7])

    def test_fewer_items_than_the_cap_passes_through(self):
        items = by_score([mk("a", 3.0), mk("b", 2.0)])
        self.assertEqual(len(F.apply_source_floor(items, 1, 400)), 2)

    def test_no_items(self):
        self.assertEqual(F.apply_source_floor([], 1, 400), [])

    def test_single_source_never_exceeds_cap(self):
        items = by_score([mk("only", 9.0 - i) for i in range(50)])
        self.assertEqual(len(F.apply_source_floor(items, 1, 10)), 10)


class CadenceTau(unittest.TestCase):
    """source_cadence_tau: slow sources get a longer recency horizon."""

    def test_daily_source_keeps_the_default(self):
        tau = F.source_cadence_tau(stamps("daily", 10, 6))
        self.assertEqual(tau.get("daily"), F.RECENCY_TAU_HOURS)

    def test_monthly_source_gets_a_longer_horizon(self):
        tau = F.source_cadence_tau(stamps("monthly", 10, 720))
        self.assertGreater(tau.get("monthly"), F.RECENCY_TAU_HOURS)

    def test_horizon_is_capped(self):
        # A yearly poster must not get a year-long horizon, or genuinely
        # ancient posts float back into the feed.
        tau = F.source_cadence_tau(stamps("yearly", 10, 8760))
        self.assertEqual(tau["yearly"], F.RECENCY_TAU_MAX_HOURS)

    def test_slower_source_outranks_faster_one(self):
        tau = F.source_cadence_tau(stamps("weekly", 8, 168)
                                   + stamps("daily", 8, 6))
        self.assertGreater(tau["weekly"], tau["daily"])

    def test_too_little_history_is_left_alone(self):
        # Two posts is one gap, and one gap is not a cadence.
        tau = F.source_cadence_tau(stamps("sparse", 2, 720))
        self.assertNotIn("sparse", tau)

    def test_unparseable_dates_are_left_alone(self):
        items = [{"source": "junk", "published": "not a date"} for _ in range(5)]
        self.assertNotIn("junk", F.source_cadence_tau(items))

    def test_identical_timestamps_are_left_alone(self):
        # Every gap is zero, so there is no rhythm to measure.
        self.assertNotIn("flat", F.source_cadence_tau(stamps("flat", 5, 0)))

    def test_missing_published_is_left_alone(self):
        items = [{"source": "nodate"} for _ in range(5)]
        self.assertNotIn("nodate", F.source_cadence_tau(items))

    def test_sources_are_measured_independently(self):
        tau = F.source_cadence_tau(stamps("a", 6, 6) + stamps("b", 6, 720))
        self.assertEqual(tau["a"], F.RECENCY_TAU_HOURS)
        self.assertGreater(tau["b"], F.RECENCY_TAU_HOURS)


class Scoring(unittest.TestCase):
    """score(): a longer horizon helps old items, never fresh ones."""

    def setUp(self):
        self.now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def test_old_item_scores_higher_with_a_longer_horizon(self):
        old = {"published": F.iso(self.now - timedelta(days=60)), "weight": 3}
        self.assertGreater(F.score(old, 1, self.now, 720),
                           F.score(old, 1, self.now))

    def test_fresh_item_is_unaffected_by_the_horizon(self):
        # age_hours is 0, so exp(0) is 1.0 whatever tau is. Anything else here
        # would mean the change leaks into the fast feeds, which it must not.
        fresh = {"published": F.iso(self.now), "weight": 3}
        self.assertEqual(F.score(fresh, 1, self.now, 720),
                         F.score(fresh, 1, self.now))

    def test_a_long_horizon_does_not_resurrect_ancient_posts(self):
        ancient = {"published": F.iso(self.now - timedelta(days=900)), "weight": 3}
        fresh = {"published": F.iso(self.now - timedelta(days=1)), "weight": 3}
        self.assertGreater(F.score(fresh, 1, self.now, 720),
                           F.score(ancient, 1, self.now, 720))

    def test_weight_still_matters(self):
        it = {"published": F.iso(self.now - timedelta(days=30)), "weight": 5}
        lo = dict(it, weight=1)
        self.assertGreater(F.score(it, 1, self.now, 720),
                           F.score(lo, 1, self.now, 720))


if __name__ == "__main__":
    # A suite that runs nothing is a failure, not a pass. Same trap the JS
    # runner guards against: a typo in a discovery pattern reports green while
    # checking absolutely nothing.
    result = unittest.main(exit=False, verbosity=2).result
    if result.testsRun == 0:
        print("FATAL: ran 0 tests, the suite did not execute", file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if result.wasSuccessful() else 1)
