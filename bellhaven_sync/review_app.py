"""Local review app. The only code path that writes to the CRM is an approval here."""
import html
import json
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .apply import Conflict, apply_ops
from .crm import CRM
from .store import Store

STORE = None
CRM_CLIENT = None
e = lambda s: html.escape("" if s is None else str(s))

KIND_LABEL = {
    "create": "New account", "reparent": "Re-parent", "chow": "CHOW", "rename": "Rename",
    "field_fix": "Field fix", "duplicate": "Duplicate", "not_on_website": "Not on website",
}
STATUS_TABS = ["pending", "applied", "rejected", "failed", "conflict", "superseded"]

CSS = """
:root{--bg:#f6f5f1;--card:#fff;--ink:#23231f;--mute:#6f6c62;--line:#e3e0d6;--accent:#2e5d50;
--ok:#2f7d4f;--bad:#b3413a;--warn:#9a6b00;--chip:#efece3;--add:#e6f4ea;--del:#fbe9e7}
@media (prefers-color-scheme:dark){:root{--bg:#171815;--card:#20211d;--ink:#e9e7df;--mute:#a19e93;
--line:#34352f;--accent:#7fc0aa;--ok:#6cc58f;--bad:#ef8a80;--warn:#e0b44c;--chip:#2b2c27;--add:#1d3326;--del:#3a2220}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:16px 24px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
header h1{font-size:18px;margin:0}header .meta{color:var(--mute);font-size:13px}
main{max-width:1100px;margin:0 auto;padding:16px}
.tabs a{display:inline-block;padding:6px 12px;margin:0 4px 8px 0;border-radius:999px;background:var(--chip);color:var(--ink);text-decoration:none;font-size:13px}
.tabs a.on{background:var(--accent);color:var(--bg)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin:12px 0}
.card h2{font-size:15px;margin:0 0 6px}.kind{font-size:11px;text-transform:uppercase;letter-spacing:.06em;padding:2px 8px;border-radius:4px;background:var(--chip);margin-right:8px}
.kind.chow{background:var(--warn);color:var(--bg)}.kind.create{background:var(--ok);color:var(--bg)}.kind.duplicate{background:var(--bad);color:var(--bg)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:760px){.grid{grid-template-columns:1fr}}
.box{border:1px solid var(--line);border-radius:6px;padding:10px;font-size:13px;overflow-wrap:anywhere}
.box h3{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute)}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:500}.old{background:var(--del)}.new{background:var(--add)}
ul.why{margin:6px 0;padding-left:18px}.sop{border-left:3px solid var(--warn);padding:6px 10px;margin:8px 0;background:var(--chip)}
.sig{display:inline-block;font-size:11px;padding:1px 6px;border-radius:3px;margin:2px 4px 0 0;background:var(--chip)}.sig.y{background:var(--add)}
form.act{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}form.act input[type=text]{flex:1;min-width:180px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
button{padding:7px 14px;border-radius:6px;border:0;font-weight:600;cursor:pointer}.approve{background:var(--ok);color:#fff}.reject{background:var(--bad);color:#fff}
.flash{padding:10px 14px;border-radius:6px;margin:10px 0;background:var(--chip)}.flash.err{border-left:4px solid var(--bad)}.flash.ok{border-left:4px solid var(--ok)}
details summary{cursor:pointer;color:var(--mute)}pre{white-space:pre-wrap;font-size:12px;background:var(--chip);padding:8px;border-radius:6px}
.muted{color:var(--mute)}a{color:var(--accent)}
"""


def account_lookup(p):
    out = {}
    for a in (p["evidence"] or {}).get("crm", []):
        out[a["account_id"]] = a
    return out


def render_changes(p):
    accts = account_lookup(p)
    rows = []
    for o in p["ops"]:
        if o["op"] == "create":
            rows.append("<tr><th colspan=3>Create new account</th></tr>")
            for k, v in o["fields"].items():
                rows.append(f"<tr><td>{e(k)}</td><td></td><td class=new>{e(v)}</td></tr>")
        elif o["op"] == "update":
            a = accts.get(o["account_id"], {})
            rows.append(f"<tr><th colspan=3>Update {e(a.get('name', ''))} <span class=muted>{e(o['account_id'])}</span></th></tr>")
            for k, v in o["set"].items():
                old = a.get(k, (o.get("expect") or {}).get(k, ""))
                shown = "(id of the new account above)" if v == "$new" else v
                rows.append(f"<tr><td>{e(k)}</td><td class=old>{e(old)}</td><td class=new>{e(shown)}</td></tr>")
        elif o["op"] == "update_contact":
            rows.append(f"<tr><td>contact {e(o['contact_id'])}</td><td class=old>{e(o['expect'].get('account_id'))}</td>"
                        f"<td class=new>account_id → {e(o['set']['account_id'])}</td></tr>")
    return "<table><tr><th>Field</th><th>Current</th><th>Proposed</th></tr>" + "".join(rows) + "</table>"


def render_account(a):
    sig = a.get("signals")
    sigs = ""
    if sig:
        sigs = "".join(
            f"<span class='sig {'y' if v else ''}'>{e(k)}: {e(v)}</span>" for k, v in sig.items())
        sigs = f"<div>{sigs}</div><div class=muted>{e(a.get('match_reason'))}</div>"
    return (
        f"<b>{e(a['name'])}</b> <span class=muted>{e(a['account_id'])}</span><br>"
        f"{e(a['billing_street'])}, {e(a['billing_city'])}, {e(a['billing_state'])} {e(a['billing_zip'])}<br>"
        f"Parent: {e(a['parent_name'] or '(none)')} · {e(a['care_type'])} · <b>{e(a['status'])}</b> · {e(a['phone'])}<br>"
        f"Revenue ${a['lifetime_revenue']:,} · AR ${a['outstanding_ar']:,}"
        + (f"<br>Contacts: {e(', '.join(a['contacts']))}" if a.get("contacts") else "")
        + (f"<br>Note: {e(a['note'])}" if a.get("note") else "") + sigs)


def render_card(p, reviewer):
    ev = p["evidence"] or {}
    w = ev.get("website")
    web = ("<div class=box><h3>Website</h3>"
           f"<b>{e(w['name'])}</b><br>{e(w['street'])}, {e(w['city'])}, {e(w['state'])} {e(w['zip'])}<br>"
           f"{e(', '.join(w['care_offerings']))}<br>Administrator: {e(w['administrator'])} · {e(w['phone'])}<br>"
           f"<a href='{e(w['url'])}' target=_blank>{e(w['url'])}</a>"
           + ("" if w.get("in_directory", True) else "<br><i>Found via homepage, not in directory</i>")
           + "</div>") if w else "<div class=box><h3>Website</h3><i>Not listed anywhere on the Bellhaven website.</i></div>"
    crm = "".join(f"<div class=box style='margin-bottom:6px'>{render_account(a)}</div>" for a in ev.get("crm", []))
    crm = f"<div><div class=box style='border:0;padding:0'><h3>CRM</h3></div>{crm or '<i>No matching account</i>'}</div>"
    sop = ev.get("sop")
    sop_html = (f"<div class=sop><b>CHOW SOP check</b>: revenue ${sop['lifetime_revenue']:,} · AR "
                f"${sop['outstanding_ar']:,} → {e(sop.get('decision', ''))}</div>") if sop else ""
    near = ev.get("near_misses") or []
    near_html = ("<details><summary>Near misses considered and rejected</summary>"
                 + "".join(f"<div class=box>{render_account(a)}</div>" for a in near) + "</details>") if near else ""
    why = "".join(f"<li>{e(r)}</li>" for r in ev.get("rationale", []))
    actions = ""
    if p["status"] in ("pending", "failed", "conflict"):
        actions = (f"<form class=act method=post action='/p/{p['id']}/decide'>"
                   f"<input type=text name=reviewer placeholder='Reviewer' value='{e(reviewer)}' required>"
                   f"<input type=text name=comment placeholder='Comment (optional)'>"
                   f"<button class=approve name=decision value=approve>Approve &amp; write to CRM</button>"
                   f"<button class=reject name=decision value=reject>Reject</button></form>")
    decided = ""
    if p["decided_by"]:
        decided = (f"<div class=muted>{e(p['status'])} by {e(p['decided_by'])} at {e(p['decided_at'])}"
                   + (f" · “{e(p['comment'])}”" if p["comment"] else "") + "</div>")
    result = f"<details><summary>CRM write result</summary><pre>{e(json.dumps(p['result'], indent=1))}</pre></details>" if p["result"] else ""
    return (f"<div class=card id=p{p['id']}><h2><span class='kind {e(p['kind'])}'>{e(KIND_LABEL.get(p['kind'], p['kind']))}</span>"
            f"#{p['id']} {e(p['title'])}</h2><ul class=why>{why}</ul>{sop_html}"
            f"<div class=grid>{web}{crm}</div>{near_html}<h3 class=muted style='font-size:12px;margin:12px 0 4px'>PROPOSED CHANGES</h3>"
            f"{render_changes(p)}{decided}{result}{actions}</div>")


def page(body, title="Bellhaven CRM review"):
    return f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{e(title)}</title><style>{CSS}</style></head><body>{body}</body></html>"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reviewer(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return urllib.parse.unquote(c["reviewer"].value) if "reviewer" in c else ""

    def send(self, code, body, headers=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/":
            return self.send(404, page("<main>Not found</main>"))
        q = urllib.parse.parse_qs(u.query)
        status = q.get("status", ["pending"])[0]
        flash = q.get("msg", [""])[0]
        counts = STORE.counts()
        runs = STORE.runs(1)
        run = runs[0] if runs else None
        tabs = "".join(
            f"<a class='{'on' if s == status else ''}' href='/?status={s}'>{s} ({counts.get(s, 0)})</a>"
            for s in STATUS_TABS)
        items = STORE.list(status)
        cards = "".join(render_card(p, self.reviewer()) for p in items) or "<p class=muted>Nothing here.</p>"
        confirmed = ""
        if run and status == "pending":
            cm = run["meta"].get("confirmed_matches", [])
            rows = "".join(f"<tr><td>{e(c['location'])}</td><td>{e(c['account_name'])} <span class=muted>{e(c['account_id'])}</span></td><td>{e(c['reason'])}</td></tr>" for c in cm)
            confirmed = (f"<details class=card><summary>{len(cm)} confident matches, nothing to change (read-only)</summary>"
                         f"<table><tr><th>Website</th><th>CRM account</th><th>Evidence</th></tr>{rows}</table></details>")
            if run["meta"].get("warning"):
                confirmed = f"<div class='flash'>{e(run['meta']['warning'])}</div>" + confirmed
        runinfo = (f"Last run #{run['id']} {e(run['status'])} at {e(run['finished_at'])} · "
                   f"{run['meta'].get('scrape', {}).get('locations_found', '?')} website locations · "
                   f"{run['meta'].get('crm_accounts', '?')} CRM accounts") if run else "No runs yet: python -m bellhaven_sync run"
        fl = f"<div class='flash {'err' if flash.startswith('Error') or flash.startswith('Conflict') else 'ok'}'>{e(flash)}</div>" if flash else ""
        self.send(200, page(
            f"<header><h1>Bellhaven → CRM review queue</h1><span class=meta>{runinfo}</span></header>"
            f"<main>{fl}<div class=tabs>{tabs}</div>{confirmed}{cards}</main>"))

    def do_POST(self):
        parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "p" or parts[2] != "decide":
            return self.send(404, page("Not found"))
        # Local-only app: reject cross-site form posts.
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            return self.send(403, page("Cross-origin request refused"))
        pid = int(parts[1])
        n = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        who, comment, decision = form.get("reviewer", "").strip(), form.get("comment", ""), form.get("decision")
        msg = decide(pid, decision, who, comment)
        ck = f"reviewer={urllib.parse.quote(who)}; Path=/; SameSite=Strict"
        self.send(303, "", {"Location": f"/?msg={urllib.parse.quote(msg)}", "Set-Cookie": ck})


def decide(pid, decision, who, comment):
    p = STORE.get(pid)
    if not p or not who:
        return "Error: unknown proposal or missing reviewer name"
    if decision == "reject":
        if p["status"] not in ("pending", "failed", "conflict"):
            return f"#{pid} is already {p['status']}"
        STORE.decide(pid, "rejected", who, comment)
        return f"Rejected #{pid}. It will not be proposed again unless the underlying data changes."
    if decision != "approve":
        return "Error: bad decision"
    if not STORE.claim_for_apply(pid):
        return f"#{pid} is already {p['status']}"
    try:
        result = apply_ops(CRM_CLIENT, p["ops"])
        STORE.decide(pid, "applied", who, comment, result)
        return f"Approved #{pid} and wrote {len(result)} change(s) to the CRM."
    except Conflict as c:
        STORE.decide(pid, "conflict", who, comment, {"conflict": str(c)})
        return f"Conflict on #{pid}: nothing written. {c}"
    except Exception as x:
        STORE.decide(pid, "failed", who, comment, {"error": str(x)})
        return f"Error applying #{pid}: {x}"


def serve(host, port):
    global STORE, CRM_CLIENT
    STORE, CRM_CLIENT = Store(), CRM()
    print(f"Review app on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
