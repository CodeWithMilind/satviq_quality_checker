"""SATVIQ -> PostgreSQL (schema V2): setup check + bulk import.

Usage (from the project root, with SATVIQ_DATABASE_URL set — a line
in .env or the environment):

    python scripts/import_to_postgres.py --check
        Verify the connection and that the V2 tables exist.

    python scripts/import_to_postgres.py --import
        Import every stored record from results/ into PostgreSQL
        (upsert by resume_id — safe to re-run).

    python scripts/import_to_postgres.py --create-schema
        Create the V2 schema in an EMPTY database using the bundled
        database/schema_v2.sql. Never run this against the
        organization's existing database — that one takes the
        ALTER-based migration instead.

Setup on Windows (one time):
    1. Install PostgreSQL 16 from postgresql.org, remember the
       postgres password you set during installation.
    2. In pgAdmin or psql:  CREATE DATABASE satviq;
    3. Add to the project's .env:
       SATVIQ_DATABASE_URL=postgresql://postgres:<password>@localhost:5432/satviq
    4. pip install "psycopg[binary]"
    5. python scripts/import_to_postgres.py --create-schema
       python scripts/import_to_postgres.py --import
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load_env() -> None:
    """Reuse the project's own .env loader when available."""
    try:
        from muse_ai.config import load_env_file
        load_env_file()
    except Exception:
        env_file = PROJECT_ROOT / ".env"
        if env_file.exists():
            import os
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--import", dest="do_import", action="store_true")
    group.add_argument("--create-schema", action="store_true")
    args = parser.parse_args()

    _load_env()
    from api import db

    if args.create_schema:
        sql = (PROJECT_ROOT / "database" / "schema_v2.sql").read_text(
            encoding="utf-8")
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        print("Schema V2 created.")
        return 0

    if args.check:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public' ORDER BY 1")
                tables = [row["table_name"] for row in cur.fetchall()]
                cur.execute("SELECT count(*) AS n FROM candidates")
                count = cur.fetchone()["n"]
        print("Connected. Tables:", ", ".join(tables))
        print("candidates rows:", count)
        return 0

    if args.do_import:
        result = db.import_all(PROJECT_ROOT / "results")
        print(f"Imported: {result['imported']}")
        for failure in result["failed"]:
            print("FAILED:", failure["file"], "-", failure["error"])
        return 1 if result["failed"] else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
