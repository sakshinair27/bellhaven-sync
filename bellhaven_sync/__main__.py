"""CLI.

  python -m bellhaven_sync run              # daily job: scrape, match, queue proposals
  python -m bellhaven_sync serve [--port N] # review app (approve/reject -> CRM writes)
  python -m bellhaven_sync status           # queue summary
"""
import argparse
import json
import logging

from . import pipeline, review_app
from .store import Store


def main():
    ap = argparse.ArgumentParser(prog="bellhaven_sync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")
    sub.add_parser("status")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "run":
        run_id, meta = pipeline.run()
        print(json.dumps({"run": run_id, "queue": meta["queue"],
                          "confirmed_no_change": len(meta["confirmed_matches"]),
                          "warning": meta.get("warning")}, indent=2))
    elif args.cmd == "serve":
        review_app.serve(args.host, args.port)
    elif args.cmd == "status":
        st = Store()
        print(json.dumps(st.counts(), indent=2))
        for p in st.list("pending"):
            print(f"  #{p['id']:<4} {p['kind']:<15} {p['title']}")


if __name__ == "__main__":
    main()
