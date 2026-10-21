import argparse
import json
import sys
from datetime import date

from .config import Config, connect
from .health import check
from .notify import send
from .reconcile import reconcile, repair
from .release import (PipelineBusy, PublicationUncertain, ReleaseRejected, SimulatedCrash,
                      recover, rollback, run_release)
from .source import DuplicatingSource, SyntheticSource


def _source(args):
    s = SyntheticSource(days_back=args.days, end=date.fromisoformat(args.end))
    if getattr(args, "lose_hits", None):
        for d in s.days()[-2:]:
            s.dropped_hits[d] = args.lose_hits  # demo: the export comes back short
    return s


def main(argv=None):
    p = argparse.ArgumentParser(prog="rp", description="Whole-release publishing demo for ClickHouse (synthetic data).")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--end", default="2026-01-31")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("release", help="build, test and publish a date range")
    r.add_argument("--lose-hits", type=int, default=0, help="demo: drop N hits on the last 2 days")
    r.add_argument("--crash-after", type=int, choices=(1, 2, 3), help="demo: crash after switching N tables")
    r.add_argument("--duplicate-visit", action="store_true", help="demo: feed a duplicate visit_id (dbt must reject)")
    sub.add_parser("recover", help="finish an interrupted switch")
    sub.add_parser("rollback", help="switch back to the previous release")
    sub.add_parser("reconcile", help="compare published data with the external totals")
    sub.add_parser("repair", help="reload days that reconcile found short")
    sub.add_parser("health", help="freshness check; exit 1 if stale")
    args = p.parse_args(argv)

    client, cfg, src = connect(), Config(), _source(args)
    days = src.days()
    try:
        if args.cmd == "release":
            feed = DuplicatingSource(src) if args.duplicate_visit else src
            rid = run_release(client, feed, days[0], days[-1], cfg, fail_after_step=args.crash_after)
            send(f"release {rid} published")
            print(f"published release {rid}")
        elif args.cmd == "recover":
            print("recovered", recover(client, cfg))
        elif args.cmd == "rollback":
            print("rolled back", rollback(client, cfg))
        elif args.cmd in ("reconcile", "repair"):
            rows = reconcile(client, src, days[0], days[-1], cfg)
            bad = [date.fromisoformat(r["date"]) for r in rows if not r["ok"]]
            for r in rows:
                print(("OK  " if r["ok"] else "DIFF"), json.dumps(r))
            if args.cmd == "repair":
                print("repaired by release", repair(client, src, bad, cfg))
            return 1 if bad and args.cmd == "reconcile" else 0
        elif args.cmd == "health":
            res = check(client, cfg)
            print(json.dumps(res, indent=2))
            if not res["ok"]:
                send("health check failed: " + "; ".join(res["problems"]))
            return 0 if res["ok"] else 1
    except ReleaseRejected as exc:
        send("release rejected by dbt tests; production untouched")
        print(exc, file=sys.stderr)
        return 2
    except SimulatedCrash as exc:
        print(f"{exc}. The lock is kept; run `rp health` and `rp recover`.", file=sys.stderr)
        return 4
    except (PipelineBusy, PublicationUncertain) as exc:
        send(f"pipeline needs attention: {exc}")
        print(exc, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
