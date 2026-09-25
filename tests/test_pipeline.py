"""Offline tests: python -m unittest discover tests"""
import copy
import unittest

from bellhaven_sync.apply import Conflict, apply_ops
from bellhaven_sync.matcher import Matcher
from bellhaven_sync.normalize import name_similarity, norm_name, norm_street
from bellhaven_sync.store import Store

BH = "P_BH"


def acct(aid, name, street, city, zip_, parent=BH, rev=0, ar=0, status="Active", **kw):
    parent_names = {BH: "Bellhaven Senior Living (Parent Account)", "P_CT": "Cedar Trail Communities (Parent Account)",
                    "P_MS": "Millstone Health Partners (Parent Account)", "": ""}
    return {"account_id": aid, "name": name, "parent_id": parent, "parent_name": parent_names[parent],
            "billing_street": street, "billing_city": city, "billing_state": "OH", "billing_zip": zip_,
            "care_type": "Skilled Nursing", "status": status, "phone": kw.get("phone", ""),
            "lifetime_revenue": rev, "outstanding_ar": ar, "chow_current_account": "",
            "duplicate_of_account": "", "note": ""}


def loc(slug, name, street, city, zip_, admin="", phone=""):
    return {"slug": slug, "url": f"https://x/communities/{slug}", "name": name, "street": street, "city": city,
            "state": "OH", "zip": zip_, "care_offerings": ["Short-Term Rehabilitation & Nursing"],
            "administrator": admin, "phone": phone, "in_directory": True}


PARENTS = [acct(BH, "Bellhaven Senior Living (Parent Account)", "", "", "", parent=""),
           acct("P_CT", "Cedar Trail Communities (Parent Account)", "", "", "", parent=""),
           acct("P_MS", "Millstone Health Partners (Parent Account)", "", "", "", parent="")]


class FakeCRM:
    def __init__(self, accounts):
        self.a = {x["account_id"]: copy.deepcopy(x) for x in accounts}
        self.c = {}
        self.n = 0

    def accounts(self, q="", parent_id="", **_):
        return [x for x in self.a.values() if q.lower() in x["name"].lower() and (not parent_id or x["parent_id"] == parent_id)]

    def account(self, i):
        return self.a[i]

    def create_account(self, f):
        self.n += 1
        new = acct(f"NEW{self.n}", f["name"], f["billing_street"], f["billing_city"], f["billing_zip"], parent=f["parent_id"])
        self.a[new["account_id"]] = new
        return new

    def update_account(self, i, f):
        self.a[i].update(f)

    def contacts(self, **_):
        return list(self.c.values())

    def contact(self, i):
        return self.c[i]

    def update_contact(self, i, f):
        self.c[i].update(f)


class NormalizeTests(unittest.TestCase):
    def test_street(self):
        self.assertEqual(norm_street("4850 Northwest Sylvania Avenue"), norm_street("4850 NW Sylvania Ave"))
        self.assertEqual(norm_street("3313 Wilmington Pk"), norm_street("3313 Wilmington Pike"))

    def test_name_variants_are_same_name(self):
        self.assertEqual(norm_name("Bellhaven Rehab and Nursing of Grove City"),
                         norm_name("Bellhaven Rehabilitation & Nursing of Grove City"))
        self.assertEqual(norm_name("The Arbors at Bellhaven - Dayton"), norm_name("Arbors at Bellhaven Dayton"))
        self.assertEqual(norm_name("Bellhaven Healthcare Centre of Ashland"),
                         norm_name("Bellhaven Health Care Center of Ashland"))

    def test_carlisle_is_not_new_carlisle(self):
        self.assertLess(name_similarity("Bellhaven of Carlisle", "Bellhaven of New Carlisle"), 0.85)


class MatcherTests(unittest.TestCase):
    def run_matcher(self, locs, accts, contacts=()):
        m = Matcher(locs, PARENTS + accts, list(contacts))
        return m, m.run()

    def test_chow_when_revenue_and_ar(self):
        m, ps = self.run_matcher([loc("t", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "44883")],
                                 [acct("A1", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "44883", parent="P_CT", rev=10, ar=5)])
        self.assertEqual([p["kind"] for p in ps], ["chow"])
        ops = ps[0]["ops"]
        self.assertEqual(ops[0]["op"], "create")
        self.assertEqual(ops[1]["set"], {"chow_current_account": "$new"})  # old account otherwise untouched

    def test_direct_reparent_when_no_ar(self):
        for rev, ar in [(10, 0), (0, 5), (0, 0)]:
            _, ps = self.run_matcher([loc("l", "Bellhaven of Lima", "1 Main St", "Lima", "45801")],
                                     [acct("A1", "Bellhaven of Lima", "1 Main St", "Lima", "45801", parent="P_CT", rev=rev, ar=ar)])
            self.assertEqual(ps[0]["kind"], "reparent", (rev, ar))
            self.assertEqual(ps[0]["ops"][0]["set"]["parent_id"], BH)

    def test_duplicates_and_orphans(self):
        locs = [loc("o", "Bellhaven of Owosso", "1120 W Main St", "Owosso", "48867", admin="Gloria Lambert")]
        accts = [acct("A1", "Bellhaven of Owosso", "1120 West Main Street", "Owosso", "48867"),
                 acct("A2", "Bellhaven of Owosso", "1120 W Main St", "Owosso", "48867"),
                 acct("A3", "Bellhaven of Coldwater", "90 N Michigan Ave", "Coldwater", "49036"),
                 acct("A4", "Bellhaven of Sandusky", "2715 Columbus Ave", "Sandusky", "44870", rev=1, ar=1),
                 acct("A5", "Millstone Care of Sandusky", "2715 Columbus Ave", "Sandusky", "44870", parent="P_MS")]
        contacts = [{"contact_id": "C1", "account_id": "A2", "name": "Gloria Lambert", "title": "Administrator", "is_active": True}]
        _, ps = self.run_matcher(locs, accts, contacts)
        by = {p["subject"]: p for p in ps}
        self.assertEqual(by["acct:A1"]["kind"], "duplicate")  # A2 survives: has the website's administrator
        self.assertEqual(by["acct:A1"]["ops"][0]["set"]["duplicate_of_account"], "A2")
        self.assertEqual(by["acct:A3"]["ops"][0]["set"]["status"], "Needs Review")
        self.assertEqual(by["acct:A4"]["ops"][0]["set"], {"chow_current_account": "A5"})

    def test_lookalike_in_other_state_is_not_matched(self):
        _, ps = self.run_matcher([loc("am", "Amberly Manor", "4390 Darrow Rd", "Hudson", "44236")],
                                 [dict(acct("X", "Amberly Manor", "918 S Nevada Ave", "Colorado Springs", "80903", parent="P_CT"), billing_state="CO")])
        self.assertEqual(ps[0]["kind"], "create")
        self.assertEqual(ps[0]["evidence"]["near_misses"][0]["account_id"], "X")


class EndToEndTests(unittest.TestCase):
    def test_rerun_is_idempotent_and_rejections_stick(self):
        locs = [loc("t", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "44883"),
                loc("b", "Bellhaven of Batavia", "2000 Hospital Dr", "Batavia", "45103")]
        crm = FakeCRM(PARENTS + [acct("A1", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "44883", parent="P_CT", rev=10, ar=5)])
        store = Store(":memory:")

        def cycle(run_id):
            m = Matcher(locs, crm.accounts(), crm.contacts())
            return store.sync_proposals(run_id, m.run())

        self.assertEqual(cycle(1)["new"], 2)
        chow = next(p for p in store.list("pending") if p["kind"] == "chow")
        create = next(p for p in store.list("pending") if p["kind"] == "create")
        store.claim_for_apply(chow["id"])
        apply_ops(crm, chow["ops"])
        store.decide(chow["id"], "applied", "t", "")
        store.decide(create["id"], "rejected", "t", "")
        self.assertEqual(crm.a["A1"]["parent_id"], "P_CT")            # old account untouched
        self.assertTrue(crm.a["A1"]["chow_current_account"].startswith("NEW"))

        second = cycle(2)
        self.assertEqual(second["new"], 0)                            # nothing re-proposed
        self.assertEqual(second["already_decided"], 1)                # the rejected create stays rejected
        self.assertEqual(len(store.list("pending")), 0)
        self.assertFalse(store.claim_for_apply(chow["id"]))           # can't apply twice

    def test_conflict_writes_nothing(self):
        crm = FakeCRM(PARENTS + [acct("A1", "Old", "1 Main St", "Lima", "45801")])
        ops = [{"op": "update", "account_id": "A1", "set": {"name": "New"}, "expect": {"name": "Something else"}}]
        with self.assertRaises(Conflict):
            apply_ops(crm, ops)
        self.assertEqual(crm.a["A1"]["name"], "Old")


if __name__ == "__main__":
    unittest.main()
