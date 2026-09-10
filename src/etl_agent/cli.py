"""Command-line helpers for ETL-Agent development."""

from __future__ import annotations

from etl_agent.db.session import SessionLocal
from etl_agent.services.auth_service import seed_admin_user, seed_rbac_users


def main() -> None:
    """Create the default development admin user and tenant."""
    db = SessionLocal()
    try:
        seed_admin_user(db)
        credentials = seed_rbac_users(db)
        for username, password in credentials.items():
            print(f"{username} / {password}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
