"""
Delete local user_profiles / user_sessions rows that don't belong to any
real account in the connected Microsoft 365 tenant.

Problem:
    GET /api/users (main.py) used to show every row in `user_profiles` /
    `user_sessions` even when the row's email had nothing to do with the
    connected tenant (test/QA logins against a different tenant, guest
    "#ext#" accounts, etc.). The endpoint itself is now fixed to only
    surface those local-only rows as a fallback when Graph is unreachable
    -- but the stale rows are still sitting in the database.

What this does:
    1. Calls Microsoft Graph GET /users against the CURRENT tenant
       (same call and same "#ext#" guest filter as main.py's
       _fetch_tenant_users) to get the set of real member emails.
    2. Finds every `user_profiles` row whose email is NOT in that set.
    3. Finds every `user_sessions` row whose email is NOT in that set.
    4. Prints what it found (dry run by default).
    5. With --apply, deletes them:
         - user_profiles rows are deleted via the ORM so
           site_permissions / folder_permissions / activity_logs /
           emergency_contacts cascade with them.
         - user_sessions rows for the same emails are deleted directly
           (they only SET NULL on user_id, they don't cascade).
    Nothing in SharePoint or Microsoft Graph is touched -- this only
    changes rows in the app's own PostgreSQL database.

Run from the backend project root (same venv/.env the app uses):
    python cleanup_stale_user_profiles.py            # dry run (default)
    python cleanup_stale_user_profiles.py --apply     # actually delete
"""
import asyncio
import sys

from app.config import settings
from app.db.base import SessionLocal
from app.db import models
from app.graph.client import graph


async def fetch_tenant_emails() -> set[str]:
    if not settings.graph_configured:
        print("Graph is not configured (AZURE_TENANT_ID / GRAPH_CLIENT_ID / "
              "GRAPH_CLIENT_SECRET) -- refusing to guess who's real. Aborting.")
        sys.exit(1)

    g = graph()
    emails: set[str] = set()
    url = "/users?$select=id,mail,userPrincipalName&$top=999"
    while url:
        page = await g.get(url)
        for u in (page.get("value") or []) if isinstance(page, dict) else []:
            email = (u.get("mail") or u.get("userPrincipalName") or "").strip().lower()
            if not email or "#ext#" in email:
                continue
            emails.add(email)
        url = page.get("@odata.nextLink") if isinstance(page, dict) else None
    return emails


def main(apply: bool) -> None:
    tenant_emails = asyncio.run(fetch_tenant_emails())
    print(f"Tenant has {len(tenant_emails)} real member accounts.")
    print(f"Mode: {'APPLY (deleting stale rows)' if apply else 'DRY RUN (no changes)'}\n")

    with SessionLocal() as db:
        profiles = db.query(models.UserProfile).order_by(models.UserProfile.email.asc()).all()
        stale_profiles = [p for p in profiles if (p.email or "").strip().lower() not in tenant_emails]

        sessions = db.query(models.UserSession).all()
        stale_session_emails = {
            (s.email or "").strip().lower() for s in sessions
            if (s.email or "").strip().lower() not in tenant_emails
        }
        stale_sessions = [s for s in sessions if (s.email or "").strip().lower() in stale_session_emails]

    if not stale_profiles and not stale_sessions:
        print("Nothing to clean up -- every local row matches a real tenant account.")
        return

    if stale_profiles:
        print(f"Stale user_profiles rows ({len(stale_profiles)}):")
        for p in stale_profiles:
            print(f"  id={p.id:<5} {p.email:<55} role={p.role:<6} name={p.display_name}")
    if stale_sessions:
        print(f"\nStale user_sessions rows ({len(stale_sessions)}), by email:")
        for email in sorted(stale_session_emails):
            count = sum(1 for s in stale_sessions if (s.email or "").strip().lower() == email)
            print(f"  {email:<55} {count} session row(s)")

    if not apply:
        print("\nDry run only -- re-run with --apply to delete these rows.")
        return

    with SessionLocal() as db:
        if stale_sessions:
            ids = [s.id for s in stale_sessions]
            db.query(models.UserSession).filter(models.UserSession.id.in_(ids)).delete(
                synchronize_session=False
            )
        if stale_profiles:
            # Delete via the ORM (not a bulk .delete()) so the
            # cascade="all, delete-orphan" relationships on UserProfile
            # (site_permissions, folder_permissions, activity_logs,
            # emergency_contacts) actually fire.
            ids = [p.id for p in stale_profiles]
            for p in db.query(models.UserProfile).filter(models.UserProfile.id.in_(ids)).all():
                db.delete(p)
        db.commit()

    print(f"\nDeleted {len(stale_profiles)} user_profiles row(s) (with their "
          f"permissions/logs) and {len(stale_sessions)} user_sessions row(s).")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
