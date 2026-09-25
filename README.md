# bellhaven-sync

Keeps the CRM's picture of **which facilities Bellhaven Senior Living operates** in line with
Bellhaven's public website. It runs daily, proposes changes with evidence, and writes to the CRM
**only when a reviewer approves a proposal**.

Pure Python 3.10+ standard library: no `pip install` needed.

```bash
echo "BH_API_TOKEN=<token>" > .env
python -m bellhaven_sync run        # scrape + match + queue proposals (read-only against CRM)
python -m bellhaven_sync serve      # review app on http://127.0.0.1:8765
python -m bellhaven_sync status     # queue summary
python -m unittest discover tests   # offline tests (fake CRM)
```

## Pipeline

| Step | Module | Notes |
|---|---|---|
| Scrape | `scraper.py` | Crawls instead of just paging the directory. The directory lists **34** communities, but the homepage says "35" and links to **Bellhaven Meadows of Findlay**, which is not in the directory. The crawler collects every `/communities/<slug>` link from the homepage and all directory pages, then parses each detail page (name, street, city, state, zip, care offerings, plus administrator and phone as match evidence). |
| Match | `matcher.py`, `normalize.py` | Scores every location against every live CRM account (see below) and emits proposals. |
| Queue | `store.py` | SQLite ledger of runs, proposals, decisions, and an audit log. |
| Review | `review_app.py` | Each card shows the website record, the CRM record(s) with match signals, the CHOW SOP check, near misses that were rejected, and a field diff (current vs proposed). Approve or reject needs a reviewer name. |
| Apply | `apply.py` | Runs only from an approval. Before writing anything, it re-reads each target record and checks the proposal's `expect` values. If the CRM drifted, nothing is written and the proposal is marked `conflict`. |

### Matching

Normalization handles cosmetic differences: `Ave/Avenue`, `NW/Northwest`, `Pk/Pike`,
`Rehab/Rehabilitation`, `&/and`, `Centre/Center`, `Healthcare/Health Care`, `at/of/the`, punctuation.

A location **matches** an account when either:
1. **Address match:** the same normalized street and state, plus the same zip *or* city. A building
   doesn't move, so this is the primary key. It correctly links renamed facilities
   (Sunny Acres Retirement Home → Bellhaven Willow Creek) and catches duplicates.
2. **Corroborated name match:** distinctive-name similarity ≥ 0.85 in the same city/state, **and**
   at least one independent corroborator: phone matches the website, a CRM contact is the website's
   administrator, or the account is already under Bellhaven. This catches Ashtabula, whose street is
   stored as a PO Box.

Name similarity alone is never enough. That rule rejected three lookalikes, which are shown to the
reviewer as "near misses":
- **Amberly Manor** (Hudson, OH) vs the CRM's Amberly Manor in Colorado Springs (Juniper Point).
- **Bellhaven at Union Square** (118 Union Square Dr) vs Union Square Senior Living (240 Market St,
  Juniper Point). Same city, but a different address, phone, and administrator (Dale Croft vs Phil Holloway).
- **Bellhaven of Carlisle, PA** vs Bellhaven of New Carlisle, OH.

When several accounts match one location, one **survivor** is picked deterministically, ranked by:
active → already under Bellhaven → billing history → has the website's administrator as a contact →
number of contacts → phone match → name similarity → exact street text → has any parent.

Accounts that already have `duplicate_of_account` or `chow_current_account` set count as history
and are excluded from matching. That's what makes a re-run after approval come back clean.

### Proposal types

| Kind | When | CRM writes |
|---|---|---|
| `create` | Location has no CRM account | New account under Bellhaven with website data and a note |
| `reparent` | Survivor is under another parent (or none), and **not** (revenue > 0 AND AR > 0) | Patch `parent_id` (plus name/address fixes) and append a note |
| `chow` | Survivor must change parent **and** has revenue > 0 AND AR > 0 | Create a new account under Bellhaven, then set `chow_current_account` on the old account. **Nothing else on the old account is touched**: not parent, name, status, or note. |
| `rename` / `field_fix` | Name, street, zip, city, care type, or status differs from the website | Patch the differing fields and append a note |
| `duplicate` | Extra accounts describing the same building | `status=Inactive`, `duplicate_of_account=<survivor>`, append a note. Active contacts on the loser are re-homed to the survivor. The loser's parent is left alone because it's historical. |
| `not_on_website` | Account under Bellhaven that the website no longer lists, with no successor at that address | `status=Needs Review` plus a note. The parent is **not** changed, because we don't know whether it was sold or closed. |
| `chow` (to existing) | Same as above, but another operator already has an account at that address, and the old account has revenue + AR | Only `chow_current_account = <that operator's account>`. The old account is otherwise untouched, and no third record is created. |

## Idempotency / daily re-runs

Two independent layers:
1. **State-based.** Every run reads the CRM fresh. Applied changes no longer show up as differences,
   and duplicate or CHOW'd accounts drop out of the candidate pool.
2. **Decision ledger.** Each proposal has a fingerprint: kind + subject + intended writes, excluding
   dated note text. A fingerprint that was ever `applied` or `rejected` is never queued again, so
   **a rejection sticks** until the underlying facts change, which produces a different fingerprint.
   Pending proposals that a later run no longer produces become `superseded`.

Safety details:
- Approvals are claimed atomically (`pending → applying`), so a double click can't write twice.
- Creates are idempotent: before a create, the apply step looks for an identical account (same name,
  street, zip, and parent) so a crash mid-apply can't produce duplicates.
- A run aborts if the scrape returns under 80% of the last good run's location count. A broken scrape
  must not flag every Bellhaven account as "no longer on website".
- A run warns if the homepage's "serve N communities" claim disagrees with the crawl.

Scheduling: [`deploy/crontab`](deploy/crontab) is the primary setup (same host as the review app,
`flock` against overlap). [`.github/workflows/daily-sync.yml`](.github/workflows/daily-sync.yml) is
the Actions alternative; it persists `state.db` on a `sync-state` branch, because the ledger has to
survive between runs.

## What I found and did (all 25 proposals approved and applied)

Verified after applying: a third run produced **0 new proposals, 35/35 locations confidently matched**.

**Confident matches, no change (20).** These include name variants that normalize to the same name,
such as "Bellhaven Rehab and Nursing of Grove City" vs "Bellhaven Rehabilitation & Nursing of Grove City",
"Arbors at Bellhaven Dayton", "Bellhaven of/at Sycamore Ridge", and "Health Care Center/Healthcare Centre of Ashland".

**Re-parented to Bellhaven directly (4):**
- Bellhaven Crossings of Lima (from Harborview; revenue $47k, **AR $0**, so a direct move)
- Bellhaven Meadows of Findlay (no parent; revenue $22k, AR $0; this is the homepage-only community)
- Cedar Trail of Zanesville → renamed Bellhaven of Zanesville (from Cedar Trail; $0 / $0)
- Kettering Care Centre → renamed Bellhaven of Kettering (from Harborview; $0 / $0)

**CHOW, SOP applied (3):**
- **Bellhaven of Tiffin** (Cedar Trail, revenue $84,000, AR $12,400): old account untouched; new
  account `0019DE4E65240B645C` created under Bellhaven; old `chow_current_account` points to it.
- **Bellhaven of Marietta** (Cedar Trail, revenue $51,250, AR $3,800): same pattern; new account `00141A500574DC7C43`.
- **Bellhaven of Sandusky** (under Bellhaven, revenue $130,000, AR $5,200): not on the website, and
  **Millstone Care of Sandusky** sits at the same address under Millstone Health Partners, so the
  building was sold. With a billing hold the parent must not change, and a successor account
  already exists, so I set `chow_current_account` to the Millstone account instead of creating a
  third record for the same building.

**Duplicates marked Inactive with `duplicate_of_account` (7):**
- Port Clinton and Erie: the Harborview-era copies point to the Bellhaven accounts.
- Monroe: Cedar Trail of Monroe and Monroe Gardens Care Center (Harborview) both point to Bellhaven Gardens of Monroe.
- Kettering: Kettering Nursing & Rehabilitation (no parent) and Kettering Senior Campus (Cedar Trail) point to the surviving Kettering account.
- Owosso: two Bellhaven accounts at 1120 W Main St. The survivor has the phone that matches the website
  and the website's administrator as a contact. The loser's contact (Tricia Lindqvist) moved to the survivor.

**Renamed, outdated names (3):** Riverbend Manor Care Center → Bellhaven of Chagrin Falls;
Sunny Acres Retirement Home → Bellhaven Willow Creek; Chesterton Senior Commons → Bellhaven of Chesterton.
Zanesville and Kettering were also renamed, as part of their re-parent proposals.

**Field fixes (2):** Ashtabula street `PO Box 517` → `3156 W Prospect Rd`; Portsmouth zip `45626` → `45662`.

**Created (4):** Bellhaven of Batavia, Bellhaven of Carlisle (PA), Bellhaven at Union Square,
Amberly Manor (Hudson, OH). Each note names the lookalike account it is *not*.

**Flagged Needs Review (2):** Bellhaven Care Center of Alliance, Bellhaven of Coldwater. Both are under
Bellhaven but on no page of the website, and there's no successor record. I used Needs Review rather
than Inactive: disappearing from a website doesn't prove a closure, and moving them to "no parent"
would lose the only ownership clue we have.

## Judgment calls worth a second look

- **Kettering survivor.** Three accounts, none under Bellhaven, no billing, no contacts, and no phone
  match, so the tie-break decided it. It chose the Harborview account (the operator Bellhaven acquired in
  2025, per the About page) over an orphan and over a record with an abbreviated street ("Wilmington Pk").
  A reviewer who knows otherwise could reject #12–14, and the next run would propose the alternative.
- **CHOW "leave exactly as is".** For the CHOW'd old accounts I only set `chow_current_account`. No
  note and no status change, reading the SOP literally. The explanation lives in the new account's note.
- **Phones.** Many CRM phones differ from the website. The brief scoped the fields to name, address,
  and care offerings, so phones are used as match evidence but not overwritten. New accounts do get the
  website phone. Adding phone to the diff is a one-line change if wanted.
- **Care type.** The website can list several offerings; the CRM holds one. A CRM value that is any of
  the website's offerings counts as consistent. It changes only when it's none of them (no such case today).
- **Old operator parent accounts** (Harborview Care Group, Cedar Trail) were left alone. Cedar Trail
  still operates other communities, and the brief is about facility → parent links.
- **Re-running the SOP on duplicates.** Marking a duplicate doesn't change its parent, so the SOP isn't
  triggered, but the card warns if a duplicate carries billing history.

## Known limitations

- Survivor selection when the survivor itself needs CHOW: its duplicates point at the old account,
  which resolves through `chow_current_account`, not directly at the new one.
- If a multi-op proposal fails halfway, a retry may report `conflict` for the already-applied op.
  The audit log shows exactly what was written.
- The review app is single-user, local-only (binds 127.0.0.1, rejects cross-origin posts), with no auth.
