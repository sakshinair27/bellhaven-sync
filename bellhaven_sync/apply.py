"""Execute an approved proposal against the CRM. Only ever called from an approval."""
from .http import HttpError
from .normalize import norm_street, norm_zip


class Conflict(Exception):
    pass


def _check_expect(record, expect, label):
    drift = {k: {"expected": v, "actual": record.get(k)}
             for k, v in (expect or {}).items() if (record.get(k) or "") != (v or "")}
    if drift:
        raise Conflict(f"{label} changed since this proposal was generated: {drift}")


def _find_existing(crm, fields):
    """Idempotent create: if a previous apply created the account but crashed before
    finishing, reuse it instead of creating a second copy."""
    for a in crm.accounts(q=fields["name"], parent_id=fields["parent_id"]):
        if (a["name"] == fields["name"]
                and norm_street(a["billing_street"]) == norm_street(fields["billing_street"])
                and norm_zip(a["billing_zip"]) == norm_zip(fields["billing_zip"])):
            return a
    return None


def apply_ops(crm, ops):
    # 1. verify every precondition before writing anything
    for o in ops:
        if o["op"] == "update":
            _check_expect(crm.account(o["account_id"]), o.get("expect"), f"account {o['account_id']}")
        elif o["op"] == "update_contact":
            _check_expect(crm.contact(o["contact_id"]), o.get("expect"), f"contact {o['contact_id']}")

    # 2. write in order, resolving $new references
    refs, log = {}, []
    for o in ops:
        try:
            if o["op"] == "create":
                existing = _find_existing(crm, o["fields"])
                if existing:
                    refs["$" + o["ref"]] = existing["account_id"]
                    log.append({"op": "create", "reused": existing["account_id"]})
                    continue
                created = crm.create_account(o["fields"])
                refs["$" + o["ref"]] = created["account_id"]
                log.append({"op": "create", "account_id": created["account_id"]})
            else:
                body = {k: refs.get(v, v) if isinstance(v, str) else v for k, v in o["set"].items()}
                if o["op"] == "update":
                    crm.update_account(o["account_id"], body)
                    log.append({"op": "update", "account_id": o["account_id"], "set": body})
                else:
                    crm.update_contact(o["contact_id"], body)
                    log.append({"op": "update_contact", "contact_id": o["contact_id"], "set": body})
        except HttpError as e:
            log.append({"op": o["op"], "error": str(e)})
            raise RuntimeError({"completed": log, "error": str(e)}) from None
    return log
