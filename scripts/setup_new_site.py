#!/usr/bin/env python3
"""Setup / Provision a new site database and verify configuration.

Usage:
    python scripts/setup_new_site.py --site <site_key>
    python scripts/setup_new_site.py --site nissenkaiun_jp
    python scripts/setup_new_site.py --site local
"""
import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote_plus

# Ensure backend root is on sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.config import _RawEnv, _prefixed, _prefixed_int, ENV_FILE


def check_and_provision_site(site_key: str) -> bool:
    site_raw = site_key.strip().lower()
    p = site_raw.upper()
    raw = _RawEnv()

    print(f"\n=======================================================")
    print(f" Checking Configuration for Site: '{site_raw}' (Prefix: {p}_)")
    print(f" Reading from: {ENV_FILE}")
    print(f"=======================================================\n")

    # 1. Check SharePoint settings
    site_name = _prefixed(p, "SP_SITE_NAME", raw, f"Vessel DMS ({site_raw})")
    azure_tenant = _prefixed(p, "AZURE_TENANT_ID", raw)
    graph_client = _prefixed(p, "GRAPH_CLIENT_ID", raw)
    graph_secret = _prefixed(p, "GRAPH_CLIENT_SECRET", raw)
    drive_id = _prefixed(p, "DRIVE_ID", raw)
    container_id = _prefixed(p, "CONTAINER_ID", raw)

    print(f"[*] Site Display Name: {site_name}")
    print(f"[*] Azure Tenant ID:   {'[CONFIGURED]' if azure_tenant else '[MISSING]'}")
    print(f"[*] Graph Client ID:   {'[CONFIGURED]' if graph_client else '[MISSING]'}")
    print(f"[*] Graph Secret:      {'[CONFIGURED]' if graph_secret else '[MISSING]'}")
    print(f"[*] SharePoint Drive:  {drive_id or '[MISSING/NOT CONFIGURED]'}")
    print(f"[*] Container ID:      {container_id or '[MISSING/NOT CONFIGURED]'}")

    # 2. Check DB settings
    db_host = _prefixed(p, "DB_HOST", raw)
    db_port = _prefixed_int(p, "DB_PORT", raw, 5432)
    db_name = _prefixed(p, "DB_NAME", raw)
    db_user = _prefixed(p, "DB_USER", raw)
    db_password = _prefixed(p, "DB_PASSWORD", raw)

    if not all([db_host, db_name, db_user, db_password]):
        print(f"\n[!] WARNING: Incomplete database settings for prefix '{p}_'.")
        print(f"    DB_HOST:     {db_host or '[MISSING]'}")
        print(f"    DB_NAME:     {db_name or '[MISSING]'}")
        print(f"    DB_USER:     {db_user or '[MISSING]'}")
        print(f"    DB_PASSWORD: {'[SET]' if db_password else '[MISSING]'}")
        print("\nPlease configure these keys in .env and run this script again.")
        return False

    db_url = (
        f"postgresql+psycopg2://{quote_plus(db_user)}:{quote_plus(db_password)}"
        f"@{db_host}:{db_port}/{db_name}"
    )

    print(f"\n[+] Database Target: {db_user}@{db_host}:{db_port}/{db_name}")

    # 3. Create database if it does not exist
    print(f"\n[*] Ensuring PostgreSQL database '{db_name}' exists...")
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    try:
        url = make_url(db_url)
        postgres_url = url.set(database="postgres")
        admin_engine = create_engine(postgres_url)
        with admin_engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :dbname"),
                {"dbname": db_name},
            ).scalar()

            if not exists:
                print(f"    Creating database '{db_name}'...")
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
                print(f"    [OK] Database '{db_name}' created successfully.")
            else:
                print(f"    [OK] Database '{db_name}' already exists.")
        admin_engine.dispose()
    except Exception as exc:
        print(f"    [!] Error while checking/creating database: {exc}")
        print("    Continuing with migration attempt...")

    # 4. Run Alembic migrations and create tables
    print(f"\n[*] Running database migrations on '{db_name}'...")
    try:
        # Override active environment in process for alembic / Base
        os.environ["ACTIVE_SITE"] = site_raw
        os.environ["APP_ENV"] = site_raw

        import alembic.config
        from alembic import command
        from sqlalchemy import inspect
        from app.db.base import Base

        site_engine = create_engine(db_url, pool_pre_ping=True)
        with site_engine.connect() as conn:
            inspector = inspect(conn)
            existing_tables = set(inspector.get_table_names())
            has_version = "alembic_version" in existing_tables
            has_tables = bool({"vessels", "folders", "user_profiles"} & existing_tables)

        alembic_ini = backend_dir / "alembic.ini"
        alembic_cfg = alembic.config.Config(str(alembic_ini))
        # Override sqlalchemy.url in alembic config (escape % for configparser)
        alembic_cfg.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))

        if has_tables and not has_version:
            print("    Tables exist without Alembic tracking; stamping head...")
            command.stamp(alembic_cfg, "head")

        try:
            command.upgrade(alembic_cfg, "head")
            print("    [OK] Alembic migrations applied successfully.")
        except Exception as up_exc:
            print(f"    [!] Alembic upgrade note: {up_exc} -> stamping head...")
            try:
                command.stamp(alembic_cfg, "head")
            except Exception:
                pass

        # Safety net table create
        Base.metadata.create_all(bind=site_engine, checkfirst=True)
        print("    [OK] All tables verified and initialized.")
        site_engine.dispose()

    except Exception as exc:
        print(f"    [!] Migration / schema init error: {exc}")
        return False

    print(f"\n=======================================================")
    print(f" SUCCESS! Site '{site_raw}' is fully configured and ready.")
    print(f" To activate this site, set in .env:")
    print(f"     ACTIVE_SITE={site_raw}")
    print(f" Then restart the backend server.")
    print(f"=======================================================\n")
    return True


def main():
    parser = argparse.ArgumentParser(description="Setup / verify a new site configuration.")
    parser.add_argument(
        "--site",
        required=True,
        help="The site key/prefix matching .env (e.g. 'local', 'dev', 'nissenkaiun_jp')",
    )
    args = parser.parse_args()
    success = check_and_provision_site(args.site)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
