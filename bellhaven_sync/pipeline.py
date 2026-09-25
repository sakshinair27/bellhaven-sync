"""Daily job: scrape -> read CRM -> match -> queue proposals. Never writes to the CRM."""
import json
import logging

from . import config
from .crm import CRM
from .matcher import Matcher
from .scraper import scrape
from .store import Store

log = logging.getLogger("bellhaven_sync")


def run(store=None, crm=None):
    store = store or Store()
    crm = crm or CRM()
    run_id = store.start_run()
    meta = {}
    try:
        locations, meta["scrape"] = scrape()
        log.info("scraped %d locations", len(locations))

        prev = store.last_good_run()
        prev_n = prev and prev["meta"].get("scrape", {}).get("locations_found")
        if prev_n and len(locations) < prev_n * config.MIN_SCRAPE_RATIO:
            raise RuntimeError(f"scrape found {len(locations)} locations vs {prev_n} last run; "
                               "refusing to generate proposals from a possibly broken scrape")
        claimed = meta["scrape"].get("homepage_claimed_count")
        if claimed and claimed != len(locations):
            meta["warning"] = f"homepage claims {claimed} communities, crawl found {len(locations)}"

        accounts, contacts = crm.accounts(), crm.contacts()
        m = Matcher(locations, accounts, contacts)
        proposals = m.run()
        meta.update({
            "crm_accounts": len(accounts),
            "confirmed_matches": m.confirmed,
            "info": m.info,
            "proposals_generated": len(proposals),
            "locations": locations,
        })
        meta["queue"] = store.sync_proposals(run_id, proposals)
        store.finish_run(run_id, "ok", meta)
        log.info("run %s: %s", run_id, json.dumps(meta["queue"]))
        return run_id, meta
    except Exception as e:
        meta["error"] = repr(e)
        store.finish_run(run_id, "error", meta)
        raise
