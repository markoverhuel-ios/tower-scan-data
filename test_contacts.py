"""Tests for the contact (phone) layer. Run: python3 -m unittest test_contacts"""

import io
import os
import sqlite3
import tempfile
import unittest
import zipfile

import build_db as b


def row(width: int, **cols) -> str:
    """A pipe-delimited FCC row of `width` fields with the given 0-based columns set."""
    f = [""] * width
    for k, v in cols.items():
        f[int(k[1:])] = str(v)
    return "|".join(f)


class PhoneAndOrgRules(unittest.TestCase):
    def test_clean_phone(self):
        self.assertEqual(b.clean_phone("(616) 555-0142"), "6165550142")
        self.assertEqual(b.clean_phone("1-616-555-0142"), "6165550142")
        self.assertIsNone(b.clean_phone(""))
        self.assertIsNone(b.clean_phone(None))
        self.assertIsNone(b.clean_phone("0000000000"))
        self.assertIsNone(b.clean_phone("1111111111"))
        self.assertIsNone(b.clean_phone("5551234"))      # too short
        self.assertIsNone(b.clean_phone("0165550142"))   # area code can't start with 0 or 1

    def test_individuals_are_excluded(self):
        self.assertFalse(b.is_organization("I", "", "", "SMITH JOHN"))
        self.assertFalse(b.is_organization("C", "John", "", "SMITH"))      # a first name means a person
        self.assertFalse(b.is_organization("L", "", "Smith", "JOHN"))
        self.assertFalse(b.is_organization("", "", "", "JOHN SMITH"))      # blank code, reads like a person

    def test_organizations_are_included(self):
        self.assertTrue(b.is_organization("C", "", "", "Crown Castle"))
        self.assertTrue(b.is_organization("L", "", "", "Pro-Comm Inc."))
        self.assertTrue(b.is_organization("G", "", "", "ISABELLA, COUNTY OF"))
        self.assertTrue(b.is_organization("", "", "", "ACME TOWER SERVICES LLC"))  # blank code, reads like a business


class LoaderBuildsContacts(unittest.TestCase):
    def make_zip(self, path):
        usi = "5001"
        files = {
            "HD.dat": row(10, c0="HD", c1=usi, c4="WTEST1", c5="A", c6="PW", c8="01/01/2030"),
            # licensee 5001: an organization with a phone; 5002: an individual with a phone
            "EN.dat": "\n".join([
                row(25, c0="EN", c1=usi, c5="L", c7="ACME RADIO INC", c12="(616) 555-0142", c22="0001", c23="C"),
                row(25, c0="EN", c1="5002", c5="L", c7="SMITH", c8="JOHN", c12="(616) 555-0199", c23="I"),
            ]),
            "LO.dat": "\n".join(
                row(45, c0="LO", c1=u, c6="F", c8="1", c11="1 MAIN", c12="TOWN", c14="MI",
                    c19="43", c20="0", c21="0", c22="N", c23="85", c24="0", c25="0", c26="W")
                for u in (usi, "5002")),
            "FR.dat": "\n".join(
                row(25, c0="FR", c1=u, c6="1", c8="FB2", c10="154.235", c15="100", c16="180")
                for u in (usi, "5002")),
            "EM.dat": "",
            "CP.dat": "\n".join([
                row(14, c0="CP", c1=usi, c8="LANSING", c9="MI", c10="(517) 555-0188"),
                row(14, c0="CP", c1=usi, c8="LANSING", c9="MI", c10="(517) 555-0188"),   # duplicate
                row(14, c0="CP", c1="5002", c8="DETROIT", c9="MI", c10="(313) 555-0123"),  # individual's license
            ]),
        }
        # Second license header (individual)
        files["HD.dat"] += "\n" + row(10, c0="HD", c1="5002", c4="WTEST2", c5="A", c6="PW", c8="01/01/2030")
        with zipfile.ZipFile(path, "w") as z:
            for name, text in files.items():
                z.writestr(name, text + "\n")

    def test_only_organization_contacts_reach_the_stage(self):
        d = tempfile.mkdtemp()
        zp = os.path.join(d, "l_test.zip")
        self.make_zip(zp)
        db = b.open_stage(os.path.join(d, "stage.sqlite"))
        b.load_uls(db, zp, "landMobile")
        rows = db.execute("SELECT kind, ref, phone FROM contact ORDER BY kind, phone").fetchall()
        self.assertEqual(rows, [("control", 5001, "5175550188"), ("licensee", 5001, "6165550142")])
        phones = {r[2] for r in rows}
        self.assertNotIn("6165550199", phones)   # individual licensee
        self.assertNotIn("3135550123", phones)   # control point of an individual's license


if __name__ == "__main__":
    unittest.main()
