#!/usr/bin/env python3
"""Tests for the FAA DOF parser and ASR linking.  Run: python3 -m unittest test_dof -v"""

import datetime as dt
import unittest

import dof

# Real lines from DOF.DAT (currency 09/27/26)
RIG = "01-001307 O US AL DAUPHIN ISLAND   30 10 45.00N 088 04 39.00W RIG                1 00236 00236 R 5 D M 1990ASO01578OE C 2014138 "
TOWER = "01-063487 O US AL FORT MORGAN      30 14 14.32N 087 52 00.73W TOWER              1 00259 00271 D 1 A N 2023ASO02773OE C 2024057 "
UNVERIFIED = "01-061332 U US AL GULF SHORES      30 14 43.32N 087 42 12.20W BLDG               1 00059 00067 N 4 D N 2018ASO25793OE A 2020230 "


class ParseTests(unittest.TestCase):
    def test_parses_a_detail_line(self):
        o = dof.parse_line(TOWER)
        self.assertEqual(o["id"], "01-063487")
        self.assertEqual((o["ver"], o["co"], o["state"], o["city"]), ("O", "US", "AL", "FORT MORGAN"))
        self.assertAlmostEqual(o["lat"], 30 + 14 / 60 + 14.32 / 3600, places=5)
        self.assertAlmostEqual(o["lon"], -(87 + 52 / 60 + 0.73 / 3600), places=5)   # west is negative
        self.assertEqual((o["type"], o["qty"], o["agl_ft"], o["amsl_ft"]), ("TOWER", 1, 259, 271))
        self.assertEqual((o["light"], o["hacc"], o["vacc"], o["mark"]), ("D", "1", "A", "N"))
        self.assertEqual((o["study"], o["action"], o["updated"]), ("2023ASO02773OE", "C", "2024-02-26"))

    def test_unverified_flag_and_other_lines(self):
        self.assertEqual(dof.parse_line(UNVERIFIED)["ver"], "U")
        self.assertEqual(dof.parse_line(RIG)["type"], "RIG")

    def test_header_and_junk_lines_are_ignored(self):
        for line in ("  CURRENCY DATE = 09/27/26", "OAS#      V CO ST CITY", "-" * 127, "", "short"):
            self.assertIsNone(dof.parse_line(line))

    def test_currency_date(self):
        self.assertEqual(dof.currency_date("  CURRENCY DATE = 09/27/26"), "2026-09-27")
        self.assertIsNone(dof.currency_date("nope"))

    def test_julian_dates(self):
        self.assertEqual(dof.julian_to_iso("2014138"), "2014-05-18")
        self.assertEqual(dof.julian_to_iso("2024001"), "2024-01-01")
        self.assertEqual(dof.julian_to_iso("2024366"), "2024-12-31")   # leap year
        self.assertIsNone(dof.julian_to_iso(""))
        self.assertIsNone(dof.julian_to_iso("20xx138"))

    def test_southern_and_eastern_hemispheres(self):
        # not in the real US data, but the parser shouldn't assume N/W
        line = TOWER.replace("30 14 14.32N", "30 14 14.32S").replace("087 52 00.73W", "087 52 00.73E")
        o = dof.parse_line(line)
        self.assertLess(o["lat"], 0)
        self.assertGreater(o["lon"], 0)


class StudyKeyTests(unittest.TestCase):
    def test_fcc_and_dof_spellings_meet(self):
        self.assertEqual(dof.study_key("2013-AGL-9135-OE"), "2013AGL09135OE")
        self.assertEqual(dof.study_key("2013AGL09135OE"), "2013AGL09135OE")
        self.assertEqual(dof.study_key("2013-AGL-9135-OE"), dof.study_key("2013AGL09135OE"))

    def test_two_digit_years(self):
        self.assertEqual(dof.study_key("96-AGL-1964-OE"), "1996AGL01964OE")
        self.assertEqual(dof.study_key("01-ASO-2903-OE"), "2001ASO02903OE")
        self.assertEqual(dof.study_key("26-AGL-1-OE"), "2026AGL00001OE")
        self.assertEqual(dof.study_key("27-AGL-1-OE"), "1927AGL00001OE")

    def test_blank_and_na(self):
        for v in (None, "", "N/A", "   ", "garbage", "2013-AGL-OE"):
            self.assertIsNone(dof.study_key(v))

    def test_display_roundtrip(self):
        self.assertEqual(dof.study_display("2013AGL09135OE"), "2013-AGL-9135-OE")
        self.assertIsNone(dof.study_display(None))


def ob(study, lat, lon, oid="01-000001"):
    return {"id": oid, "study": study, "lat": lat, "lon": lon}


def metres_north(lat, m):
    return lat + m / 111_320.0


class LinkTests(unittest.TestCase):
    LAT, LON = 42.6874, -85.5097

    def asr(self, i, north_m, study="2017-AGL-16359-OE"):
        return (i, metres_north(self.LAT, north_m), self.LON, study)

    def test_study_match(self):
        o = ob("2017AGL16359OE", self.LAT, self.LON)
        dof.link_to_asr([o], [self.asr(1236861, 5)])
        self.assertEqual((o["asr"], o["match"]), (1236861, "study"))

    def test_nearest_wins_when_a_study_covers_several_structures(self):
        o = ob("2017AGL16359OE", self.LAT, self.LON)
        dof.link_to_asr([o], [self.asr(1, 400), self.asr(2, 20), self.asr(3, 700)])
        self.assertEqual(o["asr"], 2)

    def test_study_shared_with_a_far_structure_is_not_a_link(self):
        o = ob("2017AGL16359OE", self.LAT, self.LON)
        dof.link_to_asr([o], [self.asr(1, 3000)])
        self.assertEqual((o["asr"], o["match"]), (None, None))

    def test_proximity_when_no_study_number(self):
        o = ob(None, self.LAT, self.LON)
        dof.link_to_asr([o], [self.asr(7, 12, study="N/A")])
        self.assertEqual((o["asr"], o["match"]), (7, "near"))

    def test_proximity_limit(self):
        o = ob(None, self.LAT, self.LON)
        dof.link_to_asr([o], [self.asr(7, 60, study="N/A")])
        self.assertEqual((o["asr"], o["match"]), (None, None))

    def test_different_study_nearby_still_links_by_proximity(self):
        o = ob("2020AGL00001OE", self.LAT, self.LON)
        dof.link_to_asr([o], [self.asr(7, 10, study="2017-AGL-16359-OE")])
        self.assertEqual((o["asr"], o["match"]), (7, "near"))

    def test_unrelated_obstacle_stays_unlinked(self):
        o = ob("2020AGL00001OE", 30.0, -90.0)
        dof.link_to_asr([o], [self.asr(7, 10)])
        self.assertEqual((o["asr"], o["match"]), (None, None))


class FindFileTests(unittest.TestCase):
    PAGE = ('<a href="https://aeronav.faa.gov/Obst_Data/DOF_260802.zip">old</a>'
            '<a href="https://aeronav.faa.gov/Obst_Data/DOF_260927.zip">current</a>')

    def test_picks_newest_link_on_the_page(self):
        url = dof.find_zip_url(fetch_text=lambda u: self.PAGE, today=dt.date(2026, 10, 16))
        self.assertTrue(url.endswith("DOF_260927.zip"))

    def test_falls_back_to_cycle_arithmetic_when_page_is_blocked(self):
        def boom(_): raise OSError("403")
        seen = []
        def head(url): seen.append(url); return url.endswith("260927.zip")
        url = dof.find_zip_url(fetch_text=boom, today=dt.date(2026, 10, 16), head=head)
        self.assertTrue(url.endswith("DOF_260927.zip"))
        self.assertTrue(seen[0].endswith("260927.zip"))      # tries the newest cycle first

    def test_cycles_advance_every_56_days(self):
        urls = dof.cycle_urls(dt.date(2026, 11, 30))
        self.assertTrue(urls[0].endswith("DOF_261122.zip"))   # 2026-09-27 + 56 days
        self.assertTrue(urls[1].endswith("DOF_260927.zip"))

    def test_raises_when_nothing_is_found(self):
        def boom(_): raise OSError("403")
        with self.assertRaises(RuntimeError):
            dof.find_zip_url(fetch_text=boom, today=dt.date(2026, 10, 16), head=lambda u: False)


class RowTests(unittest.TestCase):
    def test_rows_convert_units_and_tile(self):
        o = dof.parse_line(TOWER)
        o["asr"], o["match"] = 1, "study"
        (row,) = dof.to_rows([o], 2)
        self.assertEqual(row[0], "01-063487")
        self.assertAlmostEqual(row[8], 78.9, places=1)      # 259 ft AGL in metres
        self.assertEqual(row[14], "2023-ASO-2773-OE")        # display form
        self.assertEqual(row[-1], dof.tile_key(o["lat"], o["lon"], 2))
        self.assertEqual(len(row), len(dof.OBSTACLE_COLUMNS.split(",")))


if __name__ == "__main__":
    unittest.main()
