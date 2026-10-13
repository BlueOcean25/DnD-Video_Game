import argparse
import asyncio
import csv
import hashlib
import hmac
import json
import logging
import re
from logging.handlers import RotatingFileHandler
import os
import secrets
import tempfile
from pathlib import Path

import bcrypt
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

BACKEND_DIR = Path(__file__).resolve().parent


def load_database_url():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    env_file = BACKEND_DIR / ".env"
    if not env_file.exists():
        return None

    with env_file.open("r", encoding="utf-8") as env_stream:
        for line in env_stream:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if separator and key.strip() == "DATABASE_URL":
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                return value or None
    return None


DATABASE_URL = load_database_url()
ACCOUNT_FILE = Path(os.environ.get("ACCOUNT_FILE", BACKEND_DIR / "account.csv"))
CODES_FILE = Path(os.environ.get("CODES_FILE", BACKEND_DIR / "codes.csv"))
LOG_FILE = BACKEND_DIR / "server.log"
GAME_VERSION = "v0.5.27-alpha"
CSV_ORDER_COLUMN = "__csv_sync_order"
connected_players = set()


def database_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            RotatingFileHandler(
                LOG_FILE,
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
        force=True,
    )


def read_csv(path, required_fields):
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames or not set(required_fields).issubset(
            reader.fieldnames
        ):
            raise ValueError(f"{path.name} is missing required CSV columns")
        return reader.fieldnames, list(reader)


def write_csv(path, fieldnames, rows):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as csv_file:
            temporary_path = Path(csv_file.name)
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def csv_data_files():
    protected_paths = {ACCOUNT_FILE.resolve(), CODES_FILE.resolve()}
    return sorted(
        (
            path
            for path in BACKEND_DIR.glob("*.csv")
            if path.resolve() not in protected_paths
            and path.name not in {"account.csv", "codes.csv"}
        ),
        key=lambda path: path.name,
    )


def csv_table_name(filename):
    stem = Path(filename).stem.lower()
    slug = re.sub(r"[^a-z0-9_]+", "_", stem).strip("_")
    if not slug:
        raise ValueError(
            f"CSV filename cannot be converted to a Neon table name: {filename}"
        )
    suffix = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:8]
    return f"csv_{slug[:45]}_{suffix}"


def csv_column_map(fieldnames, existing_map=None):
    if not fieldnames or len(set(fieldnames)) != len(fieldnames):
        raise ValueError("CSV files must have a non-empty header with unique columns")

    existing_map = existing_map or {}
    column_map = {}
    used_columns = set()
    for index, field in enumerate(fieldnames, start=1):
        existing_column = existing_map.get(field)
        if (
            isinstance(existing_column, str)
            and existing_column
            and existing_column.lower() != CSV_ORDER_COLUMN
            and existing_column not in used_columns
        ):
            column_map[field] = existing_column
            used_columns.add(existing_column)
            continue

        slug = re.sub(r"[^a-zA-Z0-9_]+", "_", field).strip("_").lower()
        if not slug or slug[0].isdigit():
            slug = f"field_{slug}" if slug else f"column_{index}"
        slug = slug[:50]
        column = slug
        suffix = 2
        while column in used_columns or column == CSV_ORDER_COLUMN:
            column = f"{slug[:45]}_{suffix}"
            suffix += 1
        column_map[field] = column
        used_columns.add(column)
    return column_map


def store_csv_table(connection, filename, fieldnames, rows, replace_rows):
    if Path(filename).name != filename or not filename.lower().endswith(".csv"):
        raise ValueError(f"Invalid CSV filename in Neon: {filename}")
    if filename in {"account.csv", "codes.csv"}:
        raise ValueError(f"Protected CSV cannot use the game-data sync: {filename}")
    if any(None in row and row[None] for row in rows):
        raise ValueError(f"{filename} contains rows with more values than its header")

    table_name = csv_table_name(filename)
    existing = connection.execute(
        """
        SELECT table_name, column_map
        FROM csv_file_registry
        WHERE file_name = %s
        """,
        (filename,),
    ).fetchone()
    if existing:
        table_name = existing["table_name"]
        column_map = csv_column_map(fieldnames, existing["column_map"])
        table_columns = {
            row["column_name"]
            for row in connection.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s
                """,
                (table_name,),
            ).fetchall()
        }
        if CSV_ORDER_COLUMN not in table_columns and "row_order" in table_columns:
            connection.execute(
                sql.SQL("ALTER TABLE {} RENAME COLUMN {} TO {}").format(
                    sql.Identifier(table_name),
                    sql.Identifier("row_order"),
                    sql.Identifier(CSV_ORDER_COLUMN),
                )
            )
        elif CSV_ORDER_COLUMN not in table_columns:
            raise ValueError(
                f"CSV table is missing its ordering column: {filename}"
            )
    else:
        name_collision = connection.execute(
            "SELECT file_name FROM csv_file_registry WHERE table_name = %s",
            (table_name,),
        ).fetchone()
        if name_collision:
            raise ValueError(
                f"{filename} and {name_collision['file_name']} map to the same Neon table"
            )
        column_map = csv_column_map(fieldnames)
        definitions = [
            sql.SQL("{} TEXT").format(sql.Identifier(column_map[field]))
            for field in fieldnames
        ]
        connection.execute(
            sql.SQL("CREATE TABLE {} ({} BIGINT PRIMARY KEY{})").format(
                sql.Identifier(table_name),
                sql.Identifier(CSV_ORDER_COLUMN),
                (
                    sql.SQL(", ") + sql.SQL(", ").join(definitions)
                    if definitions
                    else sql.SQL("")
                ),
            )
        )

    for field in fieldnames:
        connection.execute(
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} TEXT").format(
                sql.Identifier(table_name),
                sql.Identifier(column_map[field]),
            )
        )

    if replace_rows:
        connection.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table_name)))
        if rows:
            columns = [column_map[field] for field in fieldnames]
            insert = sql.SQL("INSERT INTO {} ({}, {}) VALUES ({})").format(
                sql.Identifier(table_name),
                sql.Identifier(CSV_ORDER_COLUMN),
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join(sql.Placeholder() for _ in range(len(columns) + 1)),
            )
            values = [
                (index, *(row.get(field) or "" for field in fieldnames))
                for index, row in enumerate(rows)
            ]
            with connection.cursor() as cursor:
                cursor.executemany(insert, values)

    connection.execute(
        """
        INSERT INTO csv_file_registry (file_name, table_name, fieldnames, column_map)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (file_name) DO UPDATE
        SET table_name = EXCLUDED.table_name,
            fieldnames = EXCLUDED.fieldnames,
            column_map = EXCLUDED.column_map,
            updated_at = CURRENT_TIMESTAMP
        """,
        (filename, table_name, Jsonb(fieldnames), Jsonb(column_map)),
    )
    return len(rows)


def push_csv_files_to_database():
    if not DATABASE_URL:
        raise RuntimeError("Set DATABASE_URL before pushing CSV files to Neon")

    pushed_rows = {}
    with database_connection() as connection:
        for path in csv_data_files():
            fieldnames, rows = read_csv(path, ())
            pushed_rows[path.name] = store_csv_table(
                connection, path.name, fieldnames, rows, replace_rows=True
            )

    sync_csv_files_from_database()
    return pushed_rows


def sync_csv_files_from_database():
    if not DATABASE_URL:
        return

    local_files = {path.name: path for path in csv_data_files()}
    if not local_files:
        return

    with database_connection() as connection:
        registry = connection.execute(
            """
            SELECT file_name, table_name, fieldnames, column_map
            FROM csv_file_registry
            WHERE file_name = ANY(%s)
            ORDER BY file_name
            """,
            (list(local_files),),
        ).fetchall()
        for entry in registry:
            fieldnames = entry["fieldnames"]
            column_map = entry["column_map"]
            if (
                not isinstance(fieldnames, list)
                or not all(isinstance(field, str) for field in fieldnames)
                or not isinstance(column_map, dict)
                or not all(
                    isinstance(field, str) and isinstance(column, str)
                    for field, column in column_map.items()
                )
            ):
                raise ValueError(
                    f"Invalid CSV table metadata stored in Neon: {entry['file_name']}"
                )
            if entry["table_name"] != csv_table_name(entry["file_name"]) or any(
                field not in column_map for field in fieldnames
            ):
                raise ValueError(
                    f"Invalid CSV table mapping stored in Neon: {entry['file_name']}"
                )

            columns = [column_map[field] for field in fieldnames]
            selection = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.Identifier(entry["table_name"]),
                sql.Identifier(CSV_ORDER_COLUMN),
            )
            rows = connection.execute(selection).fetchall()
            csv_rows = [
                {field: row[column_map[field]] or "" for field in fieldnames}
                for row in rows
            ]
            write_csv(local_files[entry["file_name"]], fieldnames, csv_rows)


def account_rows():
    fieldnames, accounts = read_csv(ACCOUNT_FILE, ("password_hash",))
    if not any(field.lower() == "name" for field in fieldnames):
        raise ValueError(f"{ACCOUNT_FILE.name} is missing the account name column")
    return fieldnames, accounts


def csv_field(fieldnames, expected_name, path):
    field = next(
        (field for field in fieldnames if field.lower() == expected_name.lower()),
        None,
    )
    if field is None:
        raise ValueError(f"{path.name} is missing the {expected_name} column")
    return field


def matching_csv_accounts(identifier, fieldnames, accounts):
    name_field = csv_field(fieldnames, "name", ACCOUNT_FILE)
    username_field = next(
        (field for field in fieldnames if field.lower() == "username"),
        None,
    )
    return [
        account
        for account in accounts
        if account.get(name_field) == identifier
        or (username_field is not None and account.get(username_field) == identifier)
    ]


def sync_accounts_to_csv():
    if not DATABASE_URL:
        return

    with database_connection() as connection:
        accounts = connection.execute("""
            SELECT name, username, rank, password_hash, extra_data
            FROM accounts
            ORDER BY id
            """).fetchall()

    if ACCOUNT_FILE.exists():
        fieldnames, _ = read_csv(ACCOUNT_FILE, ())
        name_field = csv_field(fieldnames, "name", ACCOUNT_FILE)
    else:
        fieldnames = ["name", "username", "rank", "password_hash"]
        name_field = "name"

    standard_fields = {
        "name": name_field,
        "password_hash": csv_field(fieldnames, "password_hash", ACCOUNT_FILE),
    }
    for field in ("username", "rank"):
        matching_field = next(
            (candidate for candidate in fieldnames if candidate.lower() == field),
            None,
        )
        if matching_field is None and any(
            account[field] is not None for account in accounts
        ):
            matching_field = field
            fieldnames.append(field)
        if matching_field is not None:
            standard_fields[field] = matching_field

    extra_fields = sorted(
        {
            field
            for account in accounts
            for field in (account["extra_data"] or {})
            if field.lower() not in standard_fields
            and all(existing.lower() != field.lower() for existing in fieldnames)
        }
    )
    fieldnames.extend(extra_fields)

    csv_accounts = []
    for account in accounts:
        row = {field: "" for field in fieldnames}
        row[standard_fields["name"]] = account["name"]
        row[standard_fields["password_hash"]] = account["password_hash"] or ""
        for field in ("username", "rank"):
            if field in standard_fields:
                row[standard_fields[field]] = account[field] or ""
        for field, value in (account["extra_data"] or {}).items():
            matching_field = next(
                (
                    candidate
                    for candidate in fieldnames
                    if candidate.lower() == field.lower()
                ),
                None,
            )
            if matching_field is not None:
                row[matching_field] = value
        csv_accounts.append(row)

    write_csv(ACCOUNT_FILE, fieldnames, csv_accounts)


def push_accounts_to_database():
    if not DATABASE_URL:
        raise RuntimeError("Set DATABASE_URL before pushing accounts to Neon")

    fieldnames, accounts = account_rows()
    name_field = csv_field(fieldnames, "name", ACCOUNT_FILE)
    password_field = csv_field(fieldnames, "password_hash", ACCOUNT_FILE)
    username_field = next(
        (field for field in fieldnames if field.lower() == "username"),
        None,
    )
    rank_field = next(
        (field for field in fieldnames if field.lower() == "rank"),
        None,
    )
    standard_fields = {
        field
        for field in (name_field, password_field, username_field, rank_field)
        if field is not None
    }

    inserted = 0
    updated = 0
    with database_connection() as connection:
        for account in accounts:
            name = account.get(name_field, "")
            if not name:
                raise ValueError(
                    f"{ACCOUNT_FILE.name} contains an account with no name"
                )

            username = account.get(username_field) if username_field else None
            if username:
                existing_accounts = connection.execute(
                    """
                    SELECT id, username, rank, extra_data
                    FROM accounts
                    WHERE name = %s OR username = %s
                    """,
                    (name, username),
                ).fetchall()
            else:
                existing_accounts = connection.execute(
                    """
                    SELECT id, username, rank, extra_data
                    FROM accounts
                    WHERE name = %s
                    """,
                    (name,),
                ).fetchall()

            if len(existing_accounts) > 1:
                raise ValueError(
                    f"Account {name!r} matches multiple Neon accounts; "
                    "resolve the duplicate before pushing"
                )

            csv_extra_data = {
                field: value
                for field, value in account.items()
                if field not in standard_fields
            }
            existing_extra_data = (
                existing_accounts[0]["extra_data"] or {} if existing_accounts else {}
            )
            extra_data = dict(existing_extra_data)
            for field, value in csv_extra_data.items():
                existing_field = next(
                    (
                        candidate
                        for candidate in extra_data
                        if candidate.lower() == field.lower()
                    ),
                    field,
                )
                extra_data[existing_field] = value

            if username_field is not None:
                database_username = username
            else:
                database_username = (
                    existing_accounts[0]["username"] if existing_accounts else None
                )
            if rank_field is not None:
                database_rank = account.get(rank_field)
            else:
                database_rank = (
                    existing_accounts[0]["rank"] if existing_accounts else None
                )
            values = (
                name,
                database_username,
                database_rank,
                account.get(password_field) or None,
                Jsonb(extra_data),
            )

            if existing_accounts:
                connection.execute(
                    """
                    UPDATE accounts
                    SET name = %s, username = %s, rank = %s,
                        password_hash = %s, extra_data = %s
                    WHERE id = %s
                    """,
                    (*values, existing_accounts[0]["id"]),
                )
                updated += 1
            else:
                connection.execute(
                    """
                    INSERT INTO accounts
                        (name, username, rank, password_hash, extra_data)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    values,
                )
                inserted += 1

    sync_accounts_to_csv()
    return inserted, updated


def sync_codes_to_csv():
    if not DATABASE_URL:
        return

    with database_connection() as connection:
        codes = connection.execute(
            "SELECT name, code FROM password_reset_codes ORDER BY id"
        ).fetchall()

    if CODES_FILE.exists():
        fieldnames, _ = read_csv(CODES_FILE, ())
        name_field = csv_field(fieldnames, "name", CODES_FILE)
        code_field = csv_field(fieldnames, "code", CODES_FILE)
    else:
        fieldnames = ["Name", "Code"]
        name_field, code_field = "Name", "Code"

    write_csv(
        CODES_FILE,
        fieldnames,
        [
            {
                **{field: "" for field in fieldnames},
                name_field: row["name"],
                code_field: row["code"],
            }
            for row in codes
        ],
    )


def push_codes_to_database():
    if not DATABASE_URL:
        raise RuntimeError("Set DATABASE_URL before pushing CSV files to Neon")
    if not CODES_FILE.exists():
        return 0

    fieldnames, codes = read_csv(CODES_FILE, ())
    name_field = csv_field(fieldnames, "name", CODES_FILE)
    code_field = csv_field(fieldnames, "code", CODES_FILE)
    if any(None in row and row[None] for row in codes):
        raise ValueError(
            f"{CODES_FILE.name} contains rows with more values than its header"
        )

    with database_connection() as connection:
        connection.execute("DELETE FROM password_reset_codes")
        for row in codes:
            code = row.get(code_field)
            if code:
                connection.execute(
                    """
                    INSERT INTO password_reset_codes (name, code)
                    VALUES (%s, %s)
                    """,
                    (row.get(name_field, ""), code),
                )

    sync_codes_to_csv()
    return sum(bool(row.get(code_field)) for row in codes)


def initialize_database(sync_accounts=True, sync_csv_files=True, sync_codes=True):
    if not DATABASE_URL:
        return

    with database_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name TEXT NOT NULL,
                username TEXT,
                rank TEXT,
                password_hash TEXT,
                extra_data JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_codes (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name TEXT NOT NULL,
                code TEXT NOT NULL UNIQUE
            )
            """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS storage_migrations (
                name TEXT PRIMARY KEY
            )
            """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS csv_file_registry (
                file_name TEXT PRIMARY KEY,
                table_name TEXT NOT NULL UNIQUE,
                fieldnames JSONB NOT NULL,
                column_map JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)
        migrated = connection.execute(
            "SELECT 1 FROM storage_migrations WHERE name = %s",
            ("csv_to_postgresql_v1",),
        ).fetchone()
        if not migrated:
            if ACCOUNT_FILE.exists():
                account_fields, accounts = read_csv(ACCOUNT_FILE, ("password_hash",))
                account_name_field = csv_field(account_fields, "name", ACCOUNT_FILE)
                username_field = next(
                    (field for field in account_fields if field.lower() == "username"),
                    None,
                )
                rank_field = next(
                    (field for field in account_fields if field.lower() == "rank"),
                    None,
                )
                password_field = csv_field(
                    account_fields, "password_hash", ACCOUNT_FILE
                )
                standard_fields = {
                    account_name_field,
                    password_field,
                    username_field,
                    rank_field,
                }
                for account in accounts:
                    connection.execute(
                        """
                        INSERT INTO accounts
                            (name, username, rank, password_hash, extra_data)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            account.get(account_name_field, ""),
                            account.get(username_field) if username_field else None,
                            account.get(rank_field) if rank_field else None,
                            account.get(password_field) or None,
                            Jsonb(
                                {
                                    field: value
                                    for field, value in account.items()
                                    if field not in standard_fields
                                }
                            ),
                        ),
                    )

            if CODES_FILE.exists():
                code_fields, codes = read_csv(CODES_FILE, ())
                code_name_field = csv_field(code_fields, "name", CODES_FILE)
                code_value_field = csv_field(code_fields, "code", CODES_FILE)
                for code in codes:
                    code_value = code.get(code_value_field)
                    if code_value:
                        connection.execute(
                            """
                            INSERT INTO password_reset_codes (name, code)
                            VALUES (%s, %s)
                            ON CONFLICT (code) DO NOTHING
                            """,
                            (code.get(code_name_field, ""), code_value),
                        )

            connection.execute(
                "INSERT INTO storage_migrations (name) VALUES (%s)",
                ("csv_to_postgresql_v1",),
            )

        table_snapshots_exist = connection.execute(
            "SELECT to_regclass('csv_file_snapshots') AS table_name"
        ).fetchone()["table_name"]
        snapshots_migrated = connection.execute(
            "SELECT 1 FROM storage_migrations WHERE name = %s",
            ("csv_snapshots_to_tables_v1",),
        ).fetchone()
        if table_snapshots_exist and not snapshots_migrated:
            legacy_snapshots = connection.execute("""
                SELECT file_name, fieldnames, rows
                FROM csv_file_snapshots
                ORDER BY file_name
                """).fetchall()
            for snapshot in legacy_snapshots:
                fieldnames = snapshot["fieldnames"]
                rows = snapshot["rows"]
                if (
                    not isinstance(fieldnames, list)
                    or not all(isinstance(field, str) for field in fieldnames)
                    or not isinstance(rows, list)
                    or not all(isinstance(row, dict) for row in rows)
                ):
                    raise ValueError(
                        f"Invalid legacy CSV snapshot in Neon: {snapshot['file_name']}"
                    )
                store_csv_table(
                    connection,
                    snapshot["file_name"],
                    fieldnames,
                    rows,
                    replace_rows=True,
                )

            connection.execute(
                "INSERT INTO storage_migrations (name) VALUES (%s)",
                ("csv_snapshots_to_tables_v1",),
            )
            connection.execute(
                "INSERT INTO storage_migrations (name) VALUES (%s) ON CONFLICT DO NOTHING",
                ("csv_files_to_postgresql_v1",),
            )
            connection.execute(
                sql.SQL("DROP TABLE {}").format(sql.Identifier("csv_file_snapshots"))
            )

        csv_files_migrated = connection.execute(
            "SELECT 1 FROM storage_migrations WHERE name = %s",
            ("csv_files_to_postgresql_v1",),
        ).fetchone()
        if not csv_files_migrated:
            for path in csv_data_files():
                fieldnames, rows = read_csv(path, ())
                store_csv_table(
                    connection,
                    path.name,
                    fieldnames,
                    rows,
                    replace_rows=True,
                )
            connection.execute(
                "INSERT INTO storage_migrations (name) VALUES (%s)",
                ("csv_files_to_postgresql_v1",),
            )

    if sync_accounts:
        sync_accounts_to_csv()
    if sync_csv_files:
        sync_csv_files_from_database()
    if sync_codes:
        sync_codes_to_csv()


def list_account_names():
    if DATABASE_URL:
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT name FROM accounts WHERE name <> '' ORDER BY id"
            ).fetchall()
        return [row["name"] for row in rows]

    fieldnames, accounts = account_rows()
    name_field = csv_field(fieldnames, "name", ACCOUNT_FILE)
    return [row[name_field] for row in accounts if row.get(name_field)]


def authenticate(identifier, password):
    if DATABASE_URL:
        with database_connection() as connection:
            accounts = connection.execute(
                """
                SELECT id, password_hash
                FROM accounts
                WHERE name = %s OR username = %s
                LIMIT 2
                """,
                (identifier, identifier),
            ).fetchall()
        if len(accounts) != 1 or not accounts[0]["password_hash"]:
            return False
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"),
                accounts[0]["password_hash"].encode("ascii"),
            )
        except (UnicodeEncodeError, ValueError):
            logging.warning(
                "Invalid bcrypt hash or password encoding for account %s",
                identifier,
            )
            return False

    fieldnames, accounts = account_rows()
    matching_accounts = matching_csv_accounts(identifier, fieldnames, accounts)
    if len(matching_accounts) != 1:
        return False
    account = matching_accounts[0]
    if not account.get("password_hash"):
        return False

    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), account["password_hash"].encode("ascii")
        )
    except (UnicodeEncodeError, ValueError):
        logging.warning(
            "Invalid bcrypt hash or password encoding for account %s", identifier
        )
        return False


def change_password(identifier, current_password, new_password):
    if len(new_password) < 8 or len(new_password.encode("utf-8")) > 72:
        return False
    if DATABASE_URL:
        with database_connection() as connection:
            accounts = connection.execute(
                """
                SELECT id, password_hash
                FROM accounts
                WHERE name = %s OR username = %s
                LIMIT 2
                """,
                (identifier, identifier),
            ).fetchall()
            if len(accounts) != 1 or not accounts[0]["password_hash"]:
                return False
            try:
                valid_password = bcrypt.checkpw(
                    current_password.encode("utf-8"),
                    accounts[0]["password_hash"].encode("ascii"),
                )
            except (UnicodeEncodeError, ValueError):
                logging.warning(
                    "Invalid bcrypt hash or password encoding for account %s",
                    identifier,
                )
                return False
            if not valid_password:
                return False
            new_hash = bcrypt.hashpw(
                new_password.encode("utf-8"), bcrypt.gensalt()
            ).decode("ascii")
            connection.execute(
                "UPDATE accounts SET password_hash = %s WHERE id = %s",
                (new_hash, accounts[0]["id"]),
            )
        sync_accounts_to_csv()
        return True

    if not authenticate(identifier, current_password):
        return False

    fieldnames, accounts = account_rows()
    matching_accounts = matching_csv_accounts(identifier, fieldnames, accounts)
    if len(matching_accounts) != 1:
        return False

    matching_accounts[0]["password_hash"] = bcrypt.hashpw(
        new_password.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("ascii")
    write_csv(ACCOUNT_FILE, fieldnames, accounts)
    return True


def create_reset_code(name):
    if DATABASE_URL:
        code = secrets.token_urlsafe(24)
        with database_connection() as connection:
            account = connection.execute(
                "SELECT 1 FROM accounts WHERE name = %s LIMIT 1",
                (name,),
            ).fetchone()
            if account is None:
                return None
            connection.execute(
                "INSERT INTO password_reset_codes (name, code) VALUES (%s, %s)",
                (name, code),
            )
        sync_codes_to_csv()
        return code

    fieldnames, accounts = account_rows()
    name_field = next(field for field in fieldnames if field.lower() == "name")
    if not any(row.get(name_field) == name for row in accounts):
        return None

    code = secrets.token_urlsafe(24)
    if CODES_FILE.exists():
        fieldnames, codes = read_csv(CODES_FILE, ())
    else:
        fieldnames, codes = ["Name", "Code"], []
    name_field = csv_field(fieldnames, "Name", CODES_FILE)
    code_field = csv_field(fieldnames, "Code", CODES_FILE)
    codes.append(
        {
            **{field: "" for field in fieldnames},
            name_field: name,
            code_field: code,
        }
    )
    write_csv(CODES_FILE, fieldnames, codes)
    return code


def reset_password(name, code, password):
    if len(password.encode("utf-8")) > 72:
        return False
    if DATABASE_URL:
        with database_connection() as connection:
            account = connection.execute(
                "SELECT id FROM accounts WHERE name = %s LIMIT 1 FOR UPDATE",
                (name,),
            ).fetchone()
            if account is None:
                return False
            reset_codes = connection.execute(
                """
                SELECT id, code
                FROM password_reset_codes
                WHERE name = %s
                FOR UPDATE
                """,
                (name,),
            ).fetchall()
            matching_code = next(
                (
                    row
                    for row in reset_codes
                    if hmac.compare_digest(
                        row["code"].encode("utf-8"), code.encode("utf-8")
                    )
                ),
                None,
            )
            if matching_code is None:
                return False
            password_hash = bcrypt.hashpw(
                password.encode("utf-8"), bcrypt.gensalt()
            ).decode("ascii")
            connection.execute(
                "UPDATE accounts SET password_hash = %s WHERE id = %s",
                (password_hash, account["id"]),
            )
            connection.execute(
                "DELETE FROM password_reset_codes WHERE id = %s",
                (matching_code["id"],),
            )
        sync_accounts_to_csv()
        sync_codes_to_csv()
        return True

    account_fields, accounts = account_rows()
    name_field = next(field for field in account_fields if field.lower() == "name")
    account = next((row for row in accounts if row.get(name_field) == name), None)
    if account is None:
        return False

    code_fields, codes = read_csv(CODES_FILE, ())
    code_name_field = csv_field(code_fields, "Name", CODES_FILE)
    code_value_field = csv_field(code_fields, "Code", CODES_FILE)
    matching_code_index = next(
        (
            index
            for index, row in enumerate(codes)
            if row.get(code_name_field) == name
            and hmac.compare_digest(
                (row.get(code_value_field) or "").encode("utf-8"),
                code.encode("utf-8"),
            )
        ),
        None,
    )
    if matching_code_index is None:
        return False

    account["password_hash"] = bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt()
    ).decode("ascii")
    write_csv(ACCOUNT_FILE, account_fields, accounts)

    del codes[matching_code_index]
    write_csv(CODES_FILE, code_fields, codes)
    return True


async def send_auth_error(websocket, message):
    await websocket.send(json.dumps({"type": "auth_error", "message": message}))


async def handle_auth_message(websocket, data):
    message_type = data.get("type")
    try:
        if message_type == "get_account_names":
            await websocket.send(
                json.dumps(
                    {
                        "type": "account_names",
                        "names": list_account_names(),
                    }
                )
            )
        elif message_type == "check_version":
            client_version = data.get("version")
            if isinstance(client_version, str) and client_version != GAME_VERSION:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "update_available",
                            "version": GAME_VERSION,
                        }
                    )
                )
        elif message_type == "login":
            identifier = data.get("identifier", "")
            password = data.get("password", "")
            valid = (
                isinstance(identifier, str)
                and isinstance(password, str)
                and authenticate(identifier, password)
            )
            if valid:
                websocket.authenticated = True
                websocket.account_identifier = identifier
                logging.info("Login succeeded for account identifier %r", identifier)
            else:
                logging.warning("Login failed for account identifier %r", identifier)
            await websocket.send(
                json.dumps(
                    {
                        "type": "login_result",
                        "success": bool(valid),
                    }
                )
            )
        elif message_type == "change_password":
            current_password = data.get("current_password", "")
            new_password = data.get("new_password", "")
            valid_fields = all(
                isinstance(value, str) for value in (current_password, new_password)
            )
            success = (
                websocket.authenticated
                and isinstance(websocket.account_identifier, str)
                and valid_fields
                and change_password(
                    websocket.account_identifier,
                    current_password,
                    new_password,
                )
            )
            if success:
                logging.info(
                    "Password changed for account identifier %r",
                    websocket.account_identifier,
                )
            else:
                logging.warning(
                    "Password change failed for account identifier %r",
                    getattr(websocket, "account_identifier", None),
                )
            await websocket.send(
                json.dumps(
                    {
                        "type": "change_password_result",
                        "success": bool(success),
                    }
                )
            )
        elif message_type == "request_reset_code":
            name = data.get("name", "")
            code = create_reset_code(name) if isinstance(name, str) else None
            if code is not None:
                logging.info("Password reset code requested for account %r", name)
            await websocket.send(
                json.dumps(
                    {
                        "type": "reset_code_result",
                        "success": code is not None,
                    }
                )
            )
        elif message_type == "reset_password":
            name = data.get("name", "")
            code = data.get("code", "")
            password = data.get("password", "")
            valid_fields = all(
                isinstance(value, str) for value in (name, code, password)
            )
            password_valid = (
                valid_fields
                and len(password) >= 8
                and len(password.encode("utf-8")) <= 72
            )
            success = reset_password(name, code, password) if password_valid else False
            if success:
                logging.info("Password reset completed for account %r", name)
            else:
                logging.warning("Password reset failed for account %r", name)
            await websocket.send(
                json.dumps(
                    {
                        "type": "password_reset_result",
                        "success": success,
                    }
                )
            )
    except (OSError, csv.Error, UnicodeError, ValueError, psycopg.Error):
        logging.exception("Account operation failed")
        await send_auth_error(websocket, "The account request could not be completed.")


async def handler(websocket):
    connected_players.add(websocket)
    websocket.player_id = None
    websocket.authenticated = False
    websocket.account_identifier = None
    logging.info("Client connected; active connections: %d", len(connected_players))

    try:
        async for websocket_message in websocket:
            if not isinstance(websocket_message, str):
                await send_auth_error(websocket, "Invalid message.")
                continue

            try:
                data = json.loads(websocket_message)
            except json.JSONDecodeError:
                logging.warning("Rejected invalid JSON from client")
                await send_auth_error(websocket, "Invalid message.")
                continue

            if not isinstance(data, dict):
                await send_auth_error(websocket, "Invalid message.")
                continue

            if data.get("type") in {
                "get_account_names",
                "check_version",
                "login",
                "change_password",
                "request_reset_code",
                "reset_password",
            }:
                await handle_auth_message(websocket, data)
                continue

            if not websocket.authenticated:
                continue

            if data.get("type") in {"join", "reply"} and "id" in data:
                websocket.player_id = data["id"]
                if data.get("type") == "join":
                    logging.info(
                        "Player entered the game (player_id=%r)", websocket.player_id
                    )

            for player in connected_players:
                if player != websocket and player.authenticated:
                    await send_game_message(player, json.dumps(data))

    finally:
        connected_players.discard(websocket)
        logging.info(
            "Client disconnected (player_id=%r); active connections: %d",
            websocket.player_id,
            len(connected_players),
        )

        if websocket.player_id:
            leave_notification = {
                "type": "leave",
                "id": websocket.player_id,
            }
            for player in connected_players:
                if player.authenticated:
                    await send_game_message(player, json.dumps(leave_notification))


async def send_game_message(websocket, message):
    try:
        await websocket.send(message)
    except ConnectionClosed:
        logging.warning("Unable to send game message to a closing WebSocket")


async def relay_stream(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


def health_response(method):
    body = b"OK\n"
    response_body = body if method == "GET" else b""
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + response_body
    )


async def handle_tcp_request(reader, writer, websocket_port):
    upstream_writer = None
    try:
        request_head = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"),
            timeout=10,
        )
        request_line, *header_lines = request_head.split(b"\r\n")
        method, path, _ = request_line.decode("ascii").split(" ", 2)

        if method == "HEAD" and path in {"/", "/healthz"}:
            writer.write(health_response(method))
            await writer.drain()
            return

        headers = {}
        for line in header_lines:
            if not line:
                continue
            name, value = line.split(b":", 1)
            headers[name.decode("ascii").lower()] = value.decode("ascii").strip()

        is_websocket = (
            method == "GET"
            and path == "/"
            and headers.get("upgrade", "").lower() == "websocket"
            and any(
                token.strip().lower() == "upgrade"
                for token in headers.get("connection", "").split(",")
            )
        )
        if method == "GET" and path == "/healthz":
            writer.write(health_response(method))
            await writer.drain()
            return
        if method == "GET" and path == "/" and not is_websocket:
            writer.write(health_response(method))
            await writer.drain()
            return
        if not is_websocket:
            writer.write(
                b"HTTP/1.1 404 Not Found\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            return

        upstream_reader, upstream_writer = await asyncio.open_connection(
            "127.0.0.1",
            websocket_port,
        )
        upstream_writer.write(request_head)
        await upstream_writer.drain()
        to_websocket = asyncio.create_task(relay_stream(reader, upstream_writer))
        to_client = asyncio.create_task(relay_stream(upstream_reader, writer))
        done, pending = await asyncio.wait(
            {to_websocket, to_client},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
    except (
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        UnicodeError,
        ValueError,
        OSError,
        asyncio.TimeoutError,
    ):
        logging.warning("Rejected malformed or incomplete HTTP request")
    finally:
        if upstream_writer is not None:
            upstream_writer.close()
            await upstream_writer.wait_closed()
        writer.close()
        await writer.wait_closed()


async def main():
    configure_logging()
    initialize_database()

    host = "0.0.0.0"
    public_port = int(os.environ.get("PORT", "10000"))

    async with serve(handler, "127.0.0.1", 0) as websocket_server:
        websocket_port = websocket_server.sockets[0].getsockname()[1]
        logging.info("Server running on port %s", public_port)

        public_server = await asyncio.start_server(
            lambda r, w: handle_tcp_request(r, w, websocket_port),
            host,
            public_port,
        )

        async with public_server:
            await public_server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--push-csv-to-neon",
        action="store_true",
        help="Push account, reset-code, and game-data CSV files to Neon",
    )
    arguments = parser.parse_args()
    if arguments.push_csv_to_neon:
        configure_logging()
        initialize_database(
            sync_accounts=False,
            sync_csv_files=False,
            sync_codes=False,
        )
        inserted, updated = push_accounts_to_database()
        code_count = push_codes_to_database()
        file_counts = push_csv_files_to_database()
        print(f"Pushed account CSV to Neon: {inserted} added, {updated} updated.")
        print(f"Pushed codes CSV to Neon: {code_count} reset codes.")
        for filename, row_count in file_counts.items():
            print(f"Pushed {filename} to Neon: {row_count} rows.")
    else:
        asyncio.run(main())
