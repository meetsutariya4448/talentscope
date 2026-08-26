#!/usr/bin/env python3
"""
CLI to log a job application and later update its outcome.

Usage:
    python scripts/log_application.py add --company Stripe --title "SWE Intern" \
        --source greenhouse --source-id 12345
    python scripts/log_application.py update --id 7 --outcome interview

Ghosting is modeled explicitly: `no_response` plus last_checked_at records a
right-censored observation ("applied N days ago, nothing yet") rather than
leaving the row null and indistinguishable from an application never logged.
"""
import argparse
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models import Application, Posting

OUTCOMES = ("pending", "no_response", "rejected", "oa", "interview", "offer", "withdrawn")


def log_new(args):
    db = SessionLocal()
    try:
        posting_id = args.posting_id
        if posting_id is None and args.source and args.source_id:
            posting = db.query(Posting).filter_by(source=args.source, source_id=args.source_id).first()
            posting_id = posting.id if posting else None

        app_row = Application(
            posting_id=posting_id,
            company_name=args.company,
            title=args.title,
            applied_at=datetime.now(timezone.utc),
            last_checked_at=datetime.now(timezone.utc),
            outcome="pending",
        )
        db.add(app_row)
        db.commit()
        print(f"Logged application id={app_row.id} for {args.company} — {args.title}"
              + (f" (linked to posting {posting_id})" if posting_id else " (no matching posting found)"))
    finally:
        db.close()


def update_outcome(args):
    db = SessionLocal()
    try:
        app_row = db.query(Application).filter_by(id=args.id).first()
        if app_row is None:
            print(f"No application with id={args.id}")
            return

        now = datetime.now(timezone.utc)
        if args.outcome != "pending" and app_row.first_response_at is None:
            app_row.first_response_at = now
        app_row.outcome = args.outcome
        app_row.outcome_at = now
        app_row.last_checked_at = now
        db.commit()
        print(f"Updated application id={app_row.id} -> {args.outcome}")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Log and update job applications")
    sub = parser.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add", help="Log a new application")
    add_p.add_argument("--company", required=True)
    add_p.add_argument("--title", required=True)
    add_p.add_argument("--posting-id", type=int, dest="posting_id", default=None)
    add_p.add_argument("--source", help="Posting source (greenhouse|lever|ashby|adzuna), to look up posting_id")
    add_p.add_argument("--source-id", dest="source_id")
    add_p.set_defaults(func=log_new)

    update_p = sub.add_parser("update", help="Update an application's outcome")
    update_p.add_argument("--id", type=int, required=True)
    update_p.add_argument("--outcome", required=True, choices=OUTCOMES)
    update_p.set_defaults(func=update_outcome)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
