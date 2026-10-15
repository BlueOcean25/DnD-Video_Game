import json
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import sql

BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(BACKEND_DIR / ".env")

DATABASE_URL = os.environ.get("DATABASE_URL")

DATASETS = {
    "accounts": {
        "table_name": "account",
        "default_fields": ["name", "username", "rank", "password_hash"],
        "key_fields": ["name"],
    },
    "codes": {
        "table_name": "codes",
        "default_fields": ["name", "code"],
        "key_fields": ["name", "code"],
    },
    "players": {
        "table_name": "players",
        "default_fields": [
            "name",
            "race",
            "class",
            "current",
            "max",
            "gain",
            "loss",
            "modifier",
            "x",
            "y",
            "z",
        ],
        "key_fields": ["name"],
    },
    "stats": {
        "table_name": "stats",
        "default_fields": ["race", "class", "health"],
        "key_fields": ["race", "class"],
    },
    "abilities": {
        "table_name": "abilities",
        "default_fields": [
            "abilities",
            "attack_dice",
            "knockback",
            "area_of_effect",
            "energy_requirement",
        ],
        "key_fields": ["abilities"],
    },
}


def connect():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Configure it in backend/.env locally "
            "or as an environment variable on the hosting service."
        )
    return psycopg.connect(DATABASE_URL)


def ensure_schema(connection, datasets=None, fields_by_dataset=None):
    selected_datasets = datasets or DATASETS
    fields_by_dataset = fields_by_dataset or {}
    for dataset in selected_datasets:
        definition = DATASETS[dataset]
        table = sql.Identifier(definition["table_name"])
        fields = list(
            dict.fromkeys(
                definition["default_fields"] + fields_by_dataset.get(dataset, [])
            )
        )
        columns = sql.SQL(", ").join(
            sql.SQL("{} TEXT").format(sql.Identifier(field))
            for field in fields
        )
        primary_key = sql.SQL(", ").join(
            sql.Identifier(field) for field in definition["key_fields"]
        )
        connection.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} ({}, PRIMARY KEY ({}))"
            ).format(table, columns, primary_key)
        )
        existing_fields = connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s
            """,
            (definition["table_name"],),
        ).fetchall()
        existing_by_lower = {
            field[0].lower(): field[0]
            for field in existing_fields
        }
        for field in fields:
            if field.lower() not in existing_by_lower:
                connection.execute(
                    sql.SQL("ALTER TABLE {} ADD COLUMN {} TEXT").format(
                        table,
                        sql.Identifier(field),
                    )
                )
                existing_by_lower[field.lower()] = field
        actual_keys = [
            existing_by_lower[field.lower()]
            for field in definition["key_fields"]
        ]
        index = sql.Identifier(f"dnd_sync_{definition['table_name']}_key")
        key_columns = sql.SQL(", ").join(
            sql.Identifier(field) for field in actual_keys
        )
        connection.execute(
            sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} ({})").format(
                index,
                table,
                key_columns,
            )
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS dnd_sync_state (
            dataset TEXT NOT NULL,
            record_key TEXT NOT NULL,
            last_hash TEXT,
            PRIMARY KEY (dataset, record_key)
        )
        """
    )


def normalize_row(row):
    return {
        str(key): "" if value is None else str(value)
        for key, value in row.items()
        if key is not None and str(key)
    }


def record_key(dataset, row):
    fields = DATASETS[dataset]["key_fields"]
    normalized = {name.lower(): value for name, value in row.items()}
    values = []
    for field in fields:
        value = normalized.get(field.lower())
        if not value:
            raise ValueError(
                f"Dataset {dataset!r} has a record without key field {field!r}"
            )
        values.append(value)
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def fetch_dataset(connection, dataset):
    table_name = DATASETS[dataset]["table_name"]
    fields = [
        row[0]
        for row in connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table_name,),
        ).fetchall()
    ]
    if not fields:
        raise RuntimeError(f"Neon table {table_name!r} is missing")
    statement = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(sql.Identifier(field) for field in fields),
        sql.Identifier(table_name),
    )
    result = connection.execute(statement).fetchall()
    rows = {}
    for values in result:
        row = normalize_row(dict(zip(fields, values)))
        key = record_key(dataset, row)
        if key in rows:
            raise ValueError(f"Duplicate key in Neon table {table_name!r}: {key}")
        rows[key] = row
    return fields, rows


def write_record(connection, dataset, fields, row):
    definition = DATASETS[dataset]
    table_name = definition["table_name"]
    existing_fields = connection.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s
        """,
        (table_name,),
    ).fetchall()
    actual_by_lower = {field[0].lower(): field[0] for field in existing_fields}
    db_fields = [actual_by_lower[field.lower()] for field in fields]
    normalized = normalize_row(row)
    values_by_lower = {field.lower(): value for field, value in normalized.items()}
    values = [values_by_lower.get(field.lower(), "") for field in db_fields]
    conflict_keys = [
        actual_by_lower[field.lower()]
        for field in definition["key_fields"]
    ]
    update_fields = [
        field for field in db_fields if field not in conflict_keys
    ]
    if update_fields:
        conflict_action = sql.SQL("DO UPDATE SET {}").format(
            sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(
                    sql.Identifier(field),
                    sql.Identifier(field),
                )
                for field in update_fields
            )
        )
    else:
        conflict_action = sql.SQL("DO NOTHING")
    statement = sql.SQL(
        "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) {}"
    ).format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(sql.Identifier(field) for field in db_fields),
        sql.SQL(", ").join(sql.Placeholder() for _ in db_fields),
        sql.SQL(", ").join(sql.Identifier(field) for field in conflict_keys),
        conflict_action,
    )
    connection.execute(statement, values)


def replace_dataset(connection, dataset, fields, rows):
    normalized_fields = [field for field in fields if field]
    ensure_schema(connection, [dataset], {dataset: normalized_fields})
    keyed_rows = {}
    for row in rows:
        normalized = normalize_row(row)
        key = record_key(dataset, normalized)
        if key in keyed_rows:
            raise ValueError(f"Duplicate key in {dataset!r}: {key}")
        keyed_rows[key] = normalized

    table_name = DATASETS[dataset]["table_name"]
    connection.execute(
        sql.SQL("DELETE FROM {}").format(sql.Identifier(table_name))
    )
    for row in keyed_rows.values():
        write_record(connection, dataset, normalized_fields, row)


def upsert_record(connection, dataset, fields, row):
    normalized_fields = [field for field in fields if field]
    normalized = normalize_row(row)
    ensure_schema(connection, [dataset], {dataset: normalized_fields})
    record_key(dataset, normalized)
    write_record(connection, dataset, normalized_fields, normalized)
