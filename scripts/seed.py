"""Create demo organisations and users (development only; runs as the database owner role).

Two organisations so tenant isolation can be demonstrated:
  Tanjung Kredit Berhad (fictional lender)   — one user per role
  Other Lender Sdn Bhd (fictional)           — a single admin

All demo users share the password given by --password (default: ModelAssure!2026).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "apps/api"), str(ROOT)]

from sqlalchemy import select  # noqa: E402

from db.models import Membership, Organization, User  # noqa: E402
from db.session import new_session  # noqa: E402
from modules.identity.auth import hash_password  # noqa: E402
from settings import get_settings  # noqa: E402

ORGS = {
    "Tanjung Kredit Berhad": [
        ("admin@tanjung.example", "Aisyah Rahman", ["ADMIN"]),
        ("risk@tanjung.example", "Daniel Lim", ["RISK_MANAGER"]),
        ("engineer@tanjung.example", "Priya Nair", ["ENGINEER"]),
        ("analyst@tanjung.example", "Hafiz Ismail", ["ANALYST"]),
        ("reviewer@tanjung.example", "Mei Ling Tan", ["REVIEWER"]),
        ("auditor@tanjung.example", "Farid Osman", ["AUDITOR"]),
    ],
    "Other Lender Sdn Bhd": [
        ("admin@otherlender.example", "Siti Aminah", ["ADMIN", "RISK_MANAGER", "ANALYST"]),
    ],
}


def seed(password: str, url: str | None = None) -> dict[str, str]:
    url = url or get_settings().migration_database_url
    out: dict[str, str] = {}
    with new_session(url=url) as db:
        for org_name, users in ORGS.items():
            org = db.scalar(select(Organization).where(Organization.name == org_name))
            if org is None:
                org = Organization(name=org_name, approved_destinations=[])
                db.add(org)
                db.flush()
            out[org_name] = str(org.id)
            for email, name, roles in users:
                user = db.scalar(select(User).where(User.email == email))
                if user is None:
                    user = User(email=email, name=name, password_hash=hash_password(password))
                    db.add(user)
                    db.flush()
                for role in roles:
                    exists = db.scalar(select(Membership).where(Membership.user_id == user.id,
                                                                Membership.organization_id == org.id, Membership.role == role))
                    if not exists:
                        db.add(Membership(user_id=user.id, organization_id=org.id, role=role))
        db.commit()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--password", default="ModelAssure!2026")
    args = ap.parse_args()
    for name, oid in seed(args.password).items():
        print(f"{name}: {oid}")
    print("Demo users: " + ", ".join(e for users in ORGS.values() for e, _, _ in users))
