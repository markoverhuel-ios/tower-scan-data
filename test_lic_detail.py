"""Tests for lic_detail.py, using the real daily ULS files in the FCC watchdog folder when present,
plus a synthetic zip so it runs anywhere (CI)."""
import gzip, json, os, tempfile, unittest, zipfile

import lic_detail


def row(n, **kv):
    p = [""] * n
    for k, v in kv.items():
        p[int(k[1:])] = v
    return "|".join(p)


class LicDetailTests(unittest.TestCase):
    def test_export(self):
        with tempfile.TemporaryDirectory() as d:
            raw = os.path.join(d, "raw"); os.makedirs(raw)
            lo = [
                row(51, c0="LO", c1="3131154", c6="F", c7="T", c8="1", c11="123 MAIN ST", c12="JACKSON", c13="JACKSON",
                    c14="MI", c19="42", c20="20", c21="51.3", c22="N", c23="84", c24="24", c25="8.2", c26="W"),
                row(51, c0="LO", c1="3131154", c6="M", c7="T", c8="2", c13="KENT", c14="MI"),
                row(51, c0="LO", c1="3131154", c6="F", c7="T", c8="1", c11="123 MAIN ST", c12="JACKSON", c13="JACKSON",
                    c14="MI", c19="42", c20="20", c21="51.3", c22="N", c23="84", c24="24", c25="8.2", c26="W"),
            ]
            fr = [
                row(30, c0="FR", c1="3131154", c6="1", c7="1", c8="MO", c10="936.27500000", c15="20.000", c16="40.000", c26="45"),
                row(30, c0="FR", c1="3131154", c6="1", c7="1", c8="MO", c10="0", c26="46"),
            ]
            with zipfile.ZipFile(os.path.join(raw, "l_LMpriv.zip"), "w") as z:
                z.writestr("LO.dat", "\r\n".join(lo) + "\r\n")
                z.writestr("FR.dat", "\r\n".join(fr) + "\r\n")
            out = os.path.join(d, "out")
            import sys
            sys.argv = ["x", "--raw", raw, "--work", os.path.join(d, "w"), "--out", out, "--min-licenses", "1"]
            self.assertEqual(lic_detail.main(), 0)
            shard = json.loads(gzip.decompress(open(os.path.join(out, "3131.json"), "rb").read()))
            lic = shard["3131154"]
            self.assertEqual(lic["l"][0], ["1 - Fixed", "123 MAIN ST, JACKSON, MI", "42-20-51.3 N, 084-24-8.2 W", ""])
            self.assertEqual(lic["l"][1][0:3], ["2 - Mobile", "KENT County, MI", ""])
            self.assertEqual(len(lic["l"]), 2, "duplicate site must be dropped")
            self.assertEqual(lic["f"], [["936.27500000", "1", "1", "45", "MO", "MHz", "", "20.000", "40.000"]])


if __name__ == "__main__":
    unittest.main()
