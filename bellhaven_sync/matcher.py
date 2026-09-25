"""Match website locations to CRM accounts and turn the differences into proposals.

A proposal is a reviewable unit: a kind, a subject, human-readable evidence, and an
ordered list of operations that will be sent to the CRM *only* after approval.

Operations:
  {"op": "create",  "ref": "new", "fields": {...}}
  {"op": "update",  "account_id": ID, "set": {...}, "expect": {...}}
  {"op": "update_contact", "contact_id": ID, "set": {...}, "expect": {...}}
A value of "$new" in `set` is replaced by the id returned from the create op.
`expect` is checked against live CRM data right before writing; if the record drifted
since the proposal was made, nothing is written and the proposal is marked conflict.
"""
import datetime as dt
from collections import defaultdict

from . import config
from .normalize import (
    is_po_box, name_similarity, norm_city, norm_name, norm_phone, norm_street, norm_zip,
)

TODAY = dt.date.today().isoformat()


def _note(existing, text):
    line = f"[{TODAY} bellhaven-sync] {text}"
    return f"{existing}\n{line}" if existing else line


def _care_types(loc):
    return [config.CARE_TYPE_MAP.get(c.lower(), c) for c in loc["care_offerings"]]


def has_billing_hold(acct):
    """SOP: revenue history AND outstanding AR -> billing needs the old account preserved."""
    return (acct.get("lifetime_revenue") or 0) > 0 and (acct.get("outstanding_ar") or 0) > 0


class Matcher:
    def __init__(self, locations, accounts, contacts):
        self.locations = locations
        self.accounts = accounts
        self.by_id = {a["account_id"]: a for a in accounts}
        self.contacts = defaultdict(list)
        for c in contacts:
            self.contacts[c["account_id"]].append(c)
        parent_ids = {a["parent_id"] for a in accounts if a["parent_id"]}
        self.parent_ids = parent_ids | {
            a["account_id"] for a in accounts if a["name"].endswith("(Parent Account)")
        }
        targets = [a for a in accounts if a["name"] == config.TARGET_PARENT_NAME]
        if len(targets) != 1:
            raise RuntimeError(f"expected exactly one '{config.TARGET_PARENT_NAME}' account, found {len(targets)}")
        self.target = targets[0]
        self.tid = self.target["account_id"]
        # Accounts that are the live record for a facility. Accounts already resolved as a
        # duplicate or superseded via CHOW are history, never match candidates. This is
        # what makes a re-run after approvals come back clean.
        self.pool = [
            a for a in accounts
            if a["account_id"] not in self.parent_ids
            and not a["duplicate_of_account"]
            and not a["chow_current_account"]
        ]
        self.proposals = []
        self.confirmed = []   # confident matches needing no change (shown, never written)
        self.info = []

    # ------------------------------------------------------------------ signals
    def signals(self, loc, a):
        street_eq = norm_street(loc["street"]) == norm_street(a["billing_street"])
        state_eq = loc["state"].upper() == (a["billing_state"] or "").upper()
        city_eq = norm_city(loc["city"]) == norm_city(a["billing_city"])
        zip_eq = norm_zip(loc["zip"]) == norm_zip(a["billing_zip"])
        admins = {c["name"].lower() for c in self.contacts[a["account_id"]]}
        s = {
            "street_eq": street_eq, "city_eq": city_eq, "state_eq": state_eq, "zip_eq": zip_eq,
            "name_sim": name_similarity(loc["name"], a["name"]),
            "name_eq": norm_name(loc["name"]) == norm_name(a["name"]),
            "phone_eq": bool(loc["phone"]) and norm_phone(loc["phone"]) == norm_phone(a["phone"]),
            "admin_contact_eq": bool(loc["administrator"]) and loc["administrator"].lower() in admins,
            "under_target": a["parent_id"] == self.tid,
        }
        s["addr_match"] = street_eq and state_eq and (zip_eq or city_eq)
        corroborated = s["phone_eq"] or s["admin_contact_eq"] or s["under_target"]
        s["name_match"] = s["name_sim"] >= 0.85 and city_eq and state_eq and corroborated
        s["matched"] = s["addr_match"] or s["name_match"]
        s["score"] = (
            (3 if s["addr_match"] else 0) + 2 * s["name_sim"] + s["phone_eq"] + s["admin_contact_eq"]
        )
        return s

    @staticmethod
    def match_reason(s):
        bits = []
        if s["addr_match"]:
            bits.append("same street address" + (" + zip" if s["zip_eq"] else " + city"))
        elif s["name_match"]:
            bits.append(f"name match ({s['name_sim']:.2f}) in same city")
        if s["phone_eq"]:
            bits.append("phone matches website")
        if s["admin_contact_eq"]:
            bits.append("CRM contact is the website's administrator")
        return ", ".join(bits)

    def survivor_key(self, loc, a, s):
        """Higher sorts first. Deterministic so daily runs agree with each other."""
        raw_street_eq = (a["billing_street"] or "").strip().lower() == loc["street"].strip().lower()
        return (
            a["status"] == "Active",
            s["under_target"],
            (a["lifetime_revenue"] or 0) > 0,
            s["admin_contact_eq"],
            len(self.contacts[a["account_id"]]),
            s["phone_eq"],
            s["name_sim"],
            raw_street_eq,
            bool(a["parent_id"]),
        )

    # ---------------------------------------------------------------- helpers
    def acct_view(self, a, s=None):
        v = {k: a[k] for k in (
            "account_id", "name", "parent_id", "parent_name", "billing_street", "billing_city",
            "billing_state", "billing_zip", "care_type", "status", "phone", "lifetime_revenue",
            "outstanding_ar", "chow_current_account", "duplicate_of_account", "note")}
        v["contacts"] = [f'{c["name"]} ({c["title"]})' for c in self.contacts[a["account_id"]]]
        if s is not None:
            v["signals"] = {k: s[k] for k in (
                "addr_match", "name_match", "name_sim", "phone_eq", "admin_contact_eq", "under_target")}
            v["match_reason"] = self.match_reason(s)
        return v

    def add(self, kind, subject, title, ops, evidence):
        self.proposals.append({
            "kind": kind, "subject": subject, "title": title, "ops": ops, "evidence": evidence,
        })

    def new_account_fields(self, loc, note):
        return {
            "name": loc["name"], "parent_id": self.tid, "status": "Active",
            "billing_street": loc["street"], "billing_city": loc["city"],
            "billing_state": loc["state"], "billing_zip": loc["zip"],
            "care_type": (_care_types(loc) or [""])[0], "phone": loc["phone"], "note": note,
        }

    # -------------------------------------------------------------------- run
    def run(self):
        # 1. score every (location, account) pair
        matches = {}
        near = defaultdict(list)
        claimed = defaultdict(list)
        for loc in self.locations:
            ms = []
            for a in self.pool:
                s = self.signals(loc, a)
                if s["matched"]:
                    ms.append((a, s))
                    claimed[a["account_id"]].append((s["score"], loc["slug"]))
                elif s["name_sim"] >= 0.8 or (s["city_eq"] and s["state_eq"] and s["name_sim"] >= 0.5):
                    near[loc["slug"]].append(self.acct_view(a, s))
            matches[loc["slug"]] = ms

        # an account can only be one building: keep it with its best-scoring location
        for aid, cl in claimed.items():
            if len(cl) > 1:
                best = max(cl)[1]
                for _, slug in cl:
                    if slug != best:
                        matches[slug] = [(a, s) for a, s in matches[slug] if a["account_id"] != aid]
                self.info.append(f"Account {aid} matched several locations {[c[1] for c in cl]}; kept with {best}")

        matched_ids = set()
        for loc in self.locations:
            ms = matches[loc["slug"]]
            if not ms:
                self.propose_create(loc, near[loc["slug"]])
                continue
            ms.sort(key=lambda x: self.survivor_key(loc, *x), reverse=True)
            survivor, s = ms[0]
            matched_ids.update(a["account_id"] for a, _ in ms)
            self.propose_survivor(loc, survivor, s, ms)
            for loser, ls in ms[1:]:
                self.propose_duplicate(loc, loser, ls, survivor)

        # 2. accounts under the operator that the website no longer lists
        for a in self.pool:
            if a["parent_id"] == self.tid and a["account_id"] not in matched_ids:
                self.propose_orphan(a)
        return self.proposals

    # --------------------------------------------------------------- proposers
    def propose_create(self, loc, near_misses):
        why = ["No CRM account shares this street address, and no same-city account with a "
               "similar name is corroborated by phone, administrator contact, or parent."]
        if near_misses:
            why.append("Similar-looking accounts exist but were rejected as different facilities "
                       "(see near misses: different address/state, no shared phone or staff).")
        note = f"Created from Bellhaven website listing {loc['url']}."
        if near_misses:
            note += " Not the same facility as " + ", ".join(
                f"{n['name']} ({n['account_id']})" for n in near_misses) + " (different address)."
        self.add(
            "create", f"loc:{loc['slug']}", f"Create account: {loc['name']} ({loc['city']}, {loc['state']})",
            [{"op": "create", "ref": "new", "fields": self.new_account_fields(loc, note)}],
            {"website": loc, "crm": [], "near_misses": near_misses, "rationale": why},
        )

    def propose_survivor(self, loc, a, s, cluster):
        ev = {"website": loc, "crm": [self.acct_view(x, xs) for x, xs in cluster], "rationale": []}
        if len(cluster) > 1:
            ev["rationale"].append(
                f"{len(cluster)} CRM accounts describe this building; keeping {a['account_id']} "
                f"({a['name']}) as the surviving record (ranked by: active, already under Bellhaven, "
                "billing history, staff contacts, phone, name similarity).")

        fix, reasons = {}, []
        if not s["name_eq"]:
            fix["name"] = loc["name"]
            reasons.append(f"name '{a['name']}' is outdated; website calls it '{loc['name']}'")
        if not s["street_eq"]:
            fix["billing_street"] = loc["street"]
            extra = " (PO Box, not the facility's street address)" if is_po_box(a["billing_street"]) else ""
            reasons.append(f"street '{a['billing_street']}'{extra} -> '{loc['street']}'")
        if not s["city_eq"]:
            fix["billing_city"] = loc["city"]
            reasons.append(f"city '{a['billing_city']}' -> '{loc['city']}'")
        if not s["state_eq"]:
            fix["billing_state"] = loc["state"]
            reasons.append(f"state '{a['billing_state']}' -> '{loc['state']}'")
        if not s["zip_eq"]:
            fix["billing_zip"] = loc["zip"]
            reasons.append(f"zip '{a['billing_zip']}' -> '{loc['zip']}'")
        cts = _care_types(loc)
        if cts and a["care_type"] not in cts:
            fix["care_type"] = cts[0]
            reasons.append(f"care type '{a['care_type']}' not offered per website ({', '.join(loc['care_offerings'])})")
        if a["status"] != "Active":
            fix["status"] = "Active"
            reasons.append(f"status '{a['status']}' but the community is listed on the website")

        needs_parent = a["parent_id"] != self.tid
        rev, ar = a["lifetime_revenue"] or 0, a["outstanding_ar"] or 0
        if needs_parent:
            ev["sop"] = {
                "lifetime_revenue": rev, "outstanding_ar": ar,
                "billing_hold": has_billing_hold(a),
                "decision": ("CHOW: keep old account untouched, create new account under Bellhaven, "
                             "link via chow_current_account" if has_billing_hold(a)
                             else "Re-parent existing account directly (no revenue history or no outstanding AR)"),
            }

        if needs_parent and has_billing_hold(a):
            ev["rationale"].append(
                f"Listed on the Bellhaven website but parented to {a['parent_name'] or '(none)'}. "
                f"Account has revenue ${rev:,.0f} and outstanding AR ${ar:,.0f}, so per SOP the old "
                "account is preserved exactly as-is and a new account is created under Bellhaven.")
            note = (f"CHOW: facility now operated by Bellhaven per {loc['url']}. Prior account "
                    f"{a['account_id']} (under {a['parent_name']}) retained for billing.")
            ops = [
                {"op": "create", "ref": "new", "fields": self.new_account_fields(loc, note)},
                {"op": "update", "account_id": a["account_id"], "set": {"chow_current_account": "$new"},
                 "expect": {"parent_id": a["parent_id"], "chow_current_account": a["chow_current_account"]}},
            ]
            self.add("chow", f"acct:{a['account_id']}",
                     f"CHOW: {loc['name']} moves {a['parent_name']} -> Bellhaven (new account)", ops, ev)
            return

        if needs_parent:
            fix["parent_id"] = self.tid
            reasons.insert(0, f"parent is {a['parent_name'] or '(none)'}, website shows it is a Bellhaven community")
        if not fix:
            self.confirmed.append({
                "location": loc["name"], "slug": loc["slug"], "account_id": a["account_id"],
                "account_name": a["name"], "reason": self.match_reason(s),
            })
            return

        kind = "reparent" if needs_parent else ("rename" if "name" in fix else "field_fix")
        ev["rationale"].append("; ".join(reasons))
        fix["note"] = _note(a["note"], "Updated to match Bellhaven website: " + "; ".join(reasons) + ".")
        ops = [{"op": "update", "account_id": a["account_id"], "set": fix,
                "expect": {k: a[k] for k in fix if k != "note"}}]
        title = {"reparent": "Re-parent to Bellhaven", "rename": "Rename", "field_fix": "Fix fields"}[kind]
        self.add(kind, f"acct:{a['account_id']}", f"{title}: {a['name']} ({loc['city']}, {loc['state']})", ops, ev)

    def propose_duplicate(self, loc, loser, ls, survivor):
        reason = (f"Same building as surviving account {survivor['account_id']} ({survivor['name']}): "
                  f"{self.match_reason(ls)}.")
        ops = [{"op": "update", "account_id": loser["account_id"],
                "set": {"status": "Inactive", "duplicate_of_account": survivor["account_id"],
                        "note": _note(loser["note"], f"Duplicate of {survivor['account_id']} ({loc['name']}); "
                                                     f"same address per {loc['url']}.")},
                "expect": {"status": loser["status"], "duplicate_of_account": loser["duplicate_of_account"]}}]
        moved = []
        for c in self.contacts[loser["account_id"]]:
            if c["is_active"]:
                ops.append({"op": "update_contact", "contact_id": c["contact_id"],
                            "set": {"account_id": survivor["account_id"]},
                            "expect": {"account_id": loser["account_id"]}})
                moved.append(f"{c['name']} ({c['title']})")
        rationale = [reason, "Marked Inactive with duplicate_of_account (no merge/delete in the API). "
                             "Parent left unchanged: it is historical, not the live record."]
        if moved:
            rationale.append("Active contacts re-homed to the survivor so reps still see them: " + ", ".join(moved))
        if (loser["lifetime_revenue"] or 0) or (loser["outstanding_ar"] or 0):
            rationale.append(f"Note: duplicate carries billing history (rev ${loser['lifetime_revenue']:,}, "
                             f"AR ${loser['outstanding_ar']:,}); it is kept, only flagged.")
        self.add("duplicate", f"acct:{loser['account_id']}",
                 f"Mark duplicate: {loser['name']} ({loser['parent_name'] or 'no parent'}) -> {survivor['account_id']}", ops,
                 {"website": loc, "crm": [self.acct_view(survivor), self.acct_view(loser, ls)],
                  "rationale": rationale})

    def propose_orphan(self, a):
        """Account under Bellhaven that the website doesn't list."""
        if a["status"] != "Active":
            self.info.append(f"{a['name']} ({a['account_id']}) not on website but already {a['status']}; skipped")
            return
        # Has another operator taken over this building? Look for a live account at the same address.
        successors = [
            b for b in self.pool
            if b["account_id"] != a["account_id"] and b["parent_id"] and b["parent_id"] != self.tid
            and b["status"] == "Active"
            and norm_street(b["billing_street"]) == norm_street(a["billing_street"])
            and norm_zip(b["billing_zip"]) == norm_zip(a["billing_zip"])
        ]
        ev = {"website": None, "crm": [self.acct_view(a)] + [self.acct_view(b) for b in successors]}
        rev, ar = a["lifetime_revenue"] or 0, a["outstanding_ar"] or 0
        if successors:
            b = successors[0]
            ev["sop"] = {"lifetime_revenue": rev, "outstanding_ar": ar, "billing_hold": has_billing_hold(a)}
            if has_billing_hold(a):
                ev["sop"]["decision"] = ("CHOW: old account preserved exactly as-is; linked to the existing "
                                         "account under the new operator (no new account needed).")
                ev["rationale"] = [
                    f"Not on the Bellhaven website. {b['name']} ({b['account_id']}) under {b['parent_name']} "
                    "occupies the same street address, so the building changed operator.",
                    f"Revenue ${rev:,.0f} and outstanding AR ${ar:,.0f}: per SOP the parent is NOT changed; "
                    "chow_current_account points at the new operator's account instead of creating a third record."]
                ops = [{"op": "update", "account_id": a["account_id"],
                        "set": {"chow_current_account": b["account_id"]},
                        "expect": {"parent_id": a["parent_id"], "chow_current_account": a["chow_current_account"]}}]
                self.add("chow", f"acct:{a['account_id']}",
                         f"CHOW: {a['name']} now {b['name']} ({b['parent_name']})", ops, ev)
            else:
                ev["sop"]["decision"] = "No billing hold; the new operator's account is the live record."
                ev["rationale"] = [
                    f"Not on the Bellhaven website; {b['name']} under {b['parent_name']} is at the same address "
                    "and is the live record. This account is marked a duplicate of it."]
                ops = [{"op": "update", "account_id": a["account_id"],
                        "set": {"status": "Inactive", "duplicate_of_account": b["account_id"],
                                "note": _note(a["note"], f"No longer a Bellhaven community; building now "
                                                         f"{b['name']} ({b['account_id']}).")},
                        "expect": {"status": a["status"], "duplicate_of_account": a["duplicate_of_account"]}}]
                self.add("duplicate", f"acct:{a['account_id']}",
                         f"Mark superseded: {a['name']} -> {b['name']}", ops, ev)
            return

        ev["rationale"] = [
            "Parented to Bellhaven but absent from every page of the Bellhaven website (directory and homepage).",
            "No other CRM account at this address, so the new owner (or closure) is unknown. Status set to "
            "Needs Review with a note; parent left as-is until someone confirms what happened."]
        ops = [{"op": "update", "account_id": a["account_id"],
                "set": {"status": "Needs Review",
                        "note": _note(a["note"], "Not listed on the Bellhaven website; confirm whether the "
                                                 "facility was sold or closed before re-parenting.")},
                "expect": {"status": a["status"]}}]
        self.add("not_on_website", f"acct:{a['account_id']}",
                 f"Flag for review: {a['name']} no longer on Bellhaven website", ops, ev)
