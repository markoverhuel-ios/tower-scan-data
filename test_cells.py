#!/usr/bin/env python3
"""Tests for the OpenCellID -> ASR matcher.  Run: python3 -m unittest test_cells -v"""

import math
import unittest

import cells

CARRIERS = {
    "310-410": "AT&T", "310-150": "AT&T", "311-480": "Verizon",
    "310-260": "T-Mobile", "313-100": "FirstNet (AT&T)",
}

LAT, LON = 42.9634, -85.6681


def offset(lat, lon, north_m=0.0, east_m=0.0):
    return (lat + north_m / 111_320.0,
            lon + east_m / (111_320.0 * math.cos(math.radians(lat))))


def row(mcc, mnc, lat, lon, samples=10, radio="LTE", rng=1000, updated=1_760_000_000):
    # radio,mcc,net,area,cell,unit,lon,lat,range,samples,changeable,created,updated,averageSignal
    return [radio, mcc, mnc, "1", "1", "", str(lon), str(lat), str(rng), str(samples),
            "1", "1600000000", str(updated), "0"]


def index(*towers):
    return cells.TowerIndex(list(towers))


class MatchTests(unittest.TestCase):
    def run_match(self, towers, rows, **kw):
        return cells.match_cells(rows, index(*towers), CARRIERS, **kw)

    def test_cell_on_tower_is_likely(self):
        agg, _ = self.run_match([(1, LAT, LON)], [row("310", "410", *offset(LAT, LON, 20, 10))])
        a = agg[(1, "AT&T")]
        self.assertEqual(cells.confidence(a), "likely")
        self.assertLess(a["dist"], 30)

    def test_far_cell_not_matched(self):
        agg, stats = self.run_match([(1, LAT, LON)], [row("310", "410", *offset(LAT, LON, 400, 0))])
        self.assertEqual(agg, {})
        self.assertEqual(stats["matched"], 0)

    def test_medium_distance_is_possible_not_likely(self):
        agg, _ = self.run_match([(1, LAT, LON)], [row("310", "410", *offset(LAT, LON, 120, 0))])
        self.assertEqual(cells.confidence(agg[(1, "AT&T")]), "possible")

    def test_low_samples_never_likely(self):
        agg, _ = self.run_match([(1, LAT, LON)], [row("310", "410", LAT, LON, samples=3)])
        self.assertEqual(cells.confidence(agg[(1, "AT&T")]), "possible")

    def test_single_measurement_cells_ignored(self):
        agg, _ = self.run_match([(1, LAT, LON)], [row("310", "410", LAT, LON, samples=1)])
        self.assertEqual(agg, {})

    def test_nearest_tower_wins(self):
        near = offset(LAT, LON, 30, 0)
        far = offset(LAT, LON, 180, 0)
        agg, _ = self.run_match([(1, *near), (2, *far)], [row("310", "410", LAT, LON)])
        self.assertIn((1, "AT&T"), agg)
        self.assertNotIn((2, "AT&T"), agg)

    def test_two_towers_side_by_side_both_get_it_as_possible(self):
        a = offset(LAT, LON, 10, 0)
        b = offset(LAT, LON, -10, 0)
        agg, _ = self.run_match([(1, *a), (2, *b)], [row("310", "410", LAT, LON + 0.00001)])
        self.assertEqual({k[0] for k in agg}, {1, 2})
        for a in agg.values():
            self.assertTrue(a["ambiguous"])
            self.assertEqual(cells.confidence(a), "possible")

    def test_neighbor_31m_away_is_not_left_empty(self):
        # The WOOD-TV case: an old registration 31 m from the real tower.
        t1 = (1, LAT, LON)
        t2 = (2, *offset(LAT, LON, 31, 0))
        agg, _ = self.run_match([t1, t2], [row("310", "410", *offset(LAT, LON, 8, 0))])
        self.assertEqual({k[0] for k in agg}, {1, 2})

    def test_neighbor_well_beyond_gap_is_excluded(self):
        t1 = (1, LAT, LON)
        t2 = (2, *offset(LAT, LON, 90, 0))
        agg, _ = self.run_match([t1, t2], [row("310", "410", LAT, LON)])
        self.assertEqual({k[0] for k in agg}, {1})
        self.assertEqual(cells.confidence(agg[(1, "AT&T")]), "likely")

    def test_cells_roll_up_per_carrier_and_merge_codes(self):
        rows = [
            row("310", "410", LAT, LON, samples=6, radio="LTE"),
            row("310", "150", LAT, LON, samples=7, radio="UMTS"),
            row("310", "410", LAT, LON, samples=5, radio="NR"),
            row("311", "480", LAT, LON, samples=20, radio="LTE"),
        ]
        agg, _ = self.run_match([(1, LAT, LON)], rows)
        att = agg[(1, "AT&T")]
        self.assertEqual(att["cells"], 3)
        self.assertEqual(att["samples"], 18)
        self.assertEqual(att["codes"], {"310-410", "310-150"})
        self.assertEqual(att["radios"], {"LTE", "UMTS", "NR"})
        self.assertIn((1, "Verizon"), agg)

    def test_non_us_ignored(self):
        agg, stats = self.run_match([(1, LAT, LON)], [row("302", "720", LAT, LON)])
        self.assertEqual(agg, {})
        self.assertEqual(stats["us"], 0)

    def test_mnc_leading_zero_lookup(self):
        carriers = {"310-004": "Verizon"}
        self.assertEqual(cells.carrier_for(carriers, "310", "4"), "Verizon")
        self.assertEqual(cells.carrier_for(carriers, "310", "999"), "Carrier 310-999")

    def test_rows_are_marked_with_source_and_tile(self):
        agg, _ = self.run_match([(1, LAT, LON)], [row("310", "410", LAT, LON)])
        out = cells.to_rows(agg, {1: (LAT, LON)}, "2026-10-16")
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual(r[10], "OpenCellID")       # source
        self.assertEqual(r[11], "2026-10-16")       # source_date
        self.assertEqual(r[-1], "21_-43")           # tile key matches build_db.tile_key
        self.assertEqual(r[3], "LTE")                # radios

    def test_header_row_skipped(self):
        import gzip, os, tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.csv.gz")
            with gzip.open(p, "wt") as f:
                f.write("radio,mcc,net,area,cell,unit,lon,lat,range,samples,changeable,created,updated,averageSignal\n")
                f.write(",".join(row("310", "410", LAT, LON)) + "\n")
            rows = list(cells.read_rows(p))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "310")

    def test_tile_key_agrees_with_build_db(self):
        import build_db
        for lat, lon in [(42.96, -85.67), (-0.5, 10.2), (61.2, -149.9), (21.3, -157.8)]:
            expect = build_db.tile_key(lat, lon)
            agg, _ = cells.match_cells([row("310", "410", lat, lon)], index((1, lat, lon)), CARRIERS)
            self.assertEqual(cells.to_rows(agg, {1: (lat, lon)}, "d")[0][-1], expect)


if __name__ == "__main__":
    unittest.main()
