"""Pure interval-service tests: union semantics prevent double-counting."""

import unittest
from datetime import datetime, timedelta, timezone

from sla.intervals import clip, covered_seconds, total_seconds, union

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
H = timedelta(hours=1)


def iv(a, b):
    return (T0 + a * H, T0 + b * H)


class UnionTest(unittest.TestCase):
    def test_disjoint_intervals_are_preserved(self):
        self.assertEqual(union([iv(0, 2), iv(5, 7)]), [iv(0, 2), iv(5, 7)])

    def test_overlapping_intervals_merge(self):
        self.assertEqual(union([iv(0, 4), iv(2, 6)]), [iv(0, 6)])

    def test_contained_interval_is_absorbed(self):
        self.assertEqual(union([iv(0, 10), iv(2, 5)]), [iv(0, 10)])

    def test_touching_intervals_merge(self):
        self.assertEqual(union([iv(0, 3), iv(3, 6)]), [iv(0, 6)])

    def test_unsorted_input_and_empty_intervals(self):
        self.assertEqual(union([iv(8, 9), iv(0, 2), iv(4, 4), iv(1, 3)]),
                         [iv(0, 3), iv(8, 9)])

    def test_overlaps_are_counted_once(self):
        # 30h + 30h pauses overlapping by 20h must deduct 40h, not 60h.
        ivs = [iv(10, 40), iv(20, 50)]
        self.assertEqual(total_seconds(union(ivs)), 40 * 3600)

    def test_clip_to_window(self):
        self.assertEqual(clip([iv(0, 10)], T0 + 2 * H, T0 + 5 * H), [iv(2, 5)])

    def test_covered_seconds_within_window(self):
        ivs = [iv(0, 4), iv(2, 6), iv(20, 30)]
        self.assertEqual(covered_seconds(ivs, T0 + 1 * H, T0 + 5 * H), 4 * 3600)


if __name__ == "__main__":
    unittest.main()
