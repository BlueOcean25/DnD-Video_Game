import json
import os
from pathlib import Path
import uuid

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


def ensure_schema(
    connection,
    datasets=None,
    fields_by_dataset=None,
    include_defaults=True,
):
    selected_datasets = datasets or DATASETS
    fields_by_dataset = fields_by_dataset or {}
    for dataset in selected_datasets:
        definition = DATASETS[dataset]
        table = sql.Identifier(definition["table_name"])
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
        requested_fields = fields_by_dataset.get(dataset, [])
        if not existing_fields:
            fields = list(
                dict.fromkeys(
                    (definition["default_fields"] if include_defaults else [])
                    + requested_fields
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
                    "CREATE TABLE {} ({}, PRIMARY KEY ({}))"
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
        for field in requested_fields:
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


def unsupported_constraint_names(constraints):
    return [
        name
        for name, constraint_type in constraints
        if constraint_type not in {"p", "n"}
    ]


def column_rebuild_properties(column_row):
    _, data_type, default_value, not_null, _, _ = column_row
    return data_type, default_value, not_null


def reorder_dataset_columns(connection, dataset, ordered_fields):
    definition = DATASETS[dataset]
    table_name = definition["table_name"]
    table = sql.Identifier(table_name)
    column_rows = connection.execute(
        """
        SELECT
            a.attname,
            pg_catalog.format_type(a.atttypid, a.atttypmod),
            pg_get_expr(d.adbin, d.adrelid),
            a.attnotnull,
            a.attidentity,
            a.attgenerated
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
        WHERE n.nspname = current_schema()
          AND c.relname = %s
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (table_name,),
    ).fetchall()
    if not column_rows:
        raise RuntimeError(f"Neon table {table_name!r} is missing")
    special_columns = connection.execute(
        """
        SELECT attname
        FROM pg_attribute
        WHERE attrelid = %s
          AND attnum > 0
          AND NOT attisdropped
          AND (attidentity <> '' OR attgenerated <> '')
        """,
        (connection.execute(
            "SELECT to_regclass(%s)::oid",
            (table_name,),
        ).fetchone()[0],),
    ).fetchall()
    if special_columns:
        names = ", ".join(row[0] for row in special_columns)
        raise RuntimeError(
            f"Cannot safely rebuild {table_name!r}; it has identity or "
            f"generated columns: {names}"
        )

    existing_by_lower = {row[0].lower(): row[0] for row in column_rows}
    final_fields = []
    for field in ordered_fields:
        actual = existing_by_lower.get(field.lower())
        if actual is None:
            raise ValueError(
                f"Cannot order {table_name!r}: column {field!r} is missing"
            )
        if actual not in final_fields:
            final_fields.append(actual)

    current_fields = [row[0] for row in column_rows]
    if current_fields == final_fields:
        return [], False

    dropped_fields = [
        field for field in current_fields if field not in final_fields
    ]
    for field in dropped_fields:
        has_data = connection.execute(
            sql.SQL(
                "SELECT EXISTS (SELECT 1 FROM {} "
                "WHERE {} IS NOT NULL AND {}::text <> '')"
            ).format(
                table,
                sql.Identifier(field),
                sql.Identifier(field),
            )
        ).fetchone()[0]
        if has_data:
            raise ValueError(
                f"Refusing to remove non-empty Neon column "
                f"{table_name}.{field}"
            )

    table_info = connection.execute(
        """
        SELECT c.oid, c.relowner, pg_get_userbyid(c.relowner)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relname = %s
        """,
        (table_name,),
    ).fetchone()
    table_oid, _, owner_name = table_info
    dependencies = connection.execute(
        """
        SELECT DISTINCT dependent.relname
        FROM pg_constraint constraint_row
        JOIN pg_class dependent ON dependent.oid = constraint_row.conrelid
        WHERE constraint_row.contype = 'f'
          AND constraint_row.confrelid = %s
        UNION
        SELECT DISTINCT dependent.relname
        FROM pg_depend dependency
        JOIN pg_rewrite rewrite_row ON rewrite_row.oid = dependency.objid
        JOIN pg_class dependent ON dependent.oid = rewrite_row.ev_class
        WHERE dependency.refobjid = %s
          AND dependent.oid <> %s
        """,
        (table_oid, table_oid, table_oid),
    ).fetchall()
    if dependencies:
        names = ", ".join(row[0] for row in dependencies)
        raise RuntimeError(
            f"Cannot safely reorder {table_name!r}; dependent database "
            f"objects reference it: {names}"
        )

    unsupported_constraints = connection.execute(
        """
        SELECT conname, contype
        FROM pg_constraint
        WHERE conrelid = %s
        """,
        (table_oid,),
    ).fetchall()
    unsupported_names = unsupported_constraint_names(unsupported_constraints)
    if unsupported_names:
        names = ", ".join(unsupported_names)
        raise RuntimeError(
            f"Cannot safely rebuild {table_name!r}; it has additional "
            f"constraints: {names}"
        )

    triggers = connection.execute(
        """
        SELECT tgname
        FROM pg_trigger
        WHERE tgrelid = %s AND NOT tgisinternal
        """,
        (table_oid,),
    ).fetchall()
    if triggers:
        names = ", ".join(row[0] for row in triggers)
        raise RuntimeError(
            f"Cannot safely rebuild {table_name!r}; it has triggers: {names}"
        )

    row_security = connection.execute(
        "SELECT relrowsecurity FROM pg_class WHERE oid = %s",
        (table_oid,),
    ).fetchone()[0]
    if row_security:
        raise RuntimeError(
            f"Cannot safely rebuild {table_name!r}; row-level security is enabled"
        )

    column_privileges = connection.execute(
        """
        SELECT attname
        FROM pg_attribute
        WHERE attrelid = %s AND attnum > 0 AND attacl IS NOT NULL
        """,
        (table_oid,),
    ).fetchall()
    if column_privileges:
        names = ", ".join(row[0] for row in column_privileges)
        raise RuntimeError(
            f"Cannot safely rebuild {table_name!r}; it has column-specific "
            f"privileges: {names}"
        )

    indexes = connection.execute(
        """
        SELECT index_class.relname
        FROM pg_index index_row
        JOIN pg_class table_class ON table_class.oid = index_row.indrelid
        JOIN pg_class index_class ON index_class.oid = index_row.indexrelid
        WHERE table_class.oid = %s
          AND index_class.relname NOT IN (%s, %s)
        """,
        (
            table_oid,
            f"{table_name}_pkey",
            f"dnd_sync_{table_name}_key",
        ),
    ).fetchall()
    if indexes:
        names = ", ".join(row[0] for row in indexes)
        raise RuntimeError(
            f"Cannot safely rebuild {table_name!r}; it has additional "
            f"indexes: {names}"
        )

    columns_by_name = {row[0]: row for row in column_rows}
    staging_name = f"{table_name[:38]}_sync_{uuid.uuid4().hex[:8]}"
    backup_name = f"{table_name[:38]}_old_{uuid.uuid4().hex[:8]}"
    staging = sql.Identifier(staging_name)
    backup = sql.Identifier(backup_name)
    primary_key = sql.SQL(", ").join(
        sql.Identifier(existing_by_lower[field.lower()])
        for field in definition["key_fields"]
    )
    column_definitions = []
    for field in final_fields:
        data_type, default_value, not_null = column_rebuild_properties(
            columns_by_name[field]
        )
        column_definition = sql.SQL("{} {}").format(
            sql.Identifier(field),
            sql.SQL(data_type),
        )
        if default_value is not None:
            column_definition += sql.SQL(" DEFAULT {}").format(
                sql.SQL(default_value)
            )
        if not_null:
            column_definition += sql.SQL(" NOT NULL")
        column_definitions.append(column_definition)
    connection.execute(
        sql.SQL("CREATE TABLE {} ({}, PRIMARY KEY ({}))").format(
            staging,
            sql.SQL(", ").join(column_definitions),
            primary_key,
        )
    )
    columns = sql.SQL(", ").join(
        sql.Identifier(field) for field in final_fields
    )
    connection.execute(
        sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {}").format(
            staging,
            columns,
            columns,
            table,
        )
    )
    connection.execute(
        sql.SQL("ALTER TABLE {} OWNER TO {}").format(
            staging,
            sql.Identifier(owner_name),
        )
    )
    grants = connection.execute(
        """
        SELECT grantee_role.rolname, privilege.privilege_type,
               privilege.is_grantable
        FROM pg_class table_class
        CROSS JOIN LATERAL aclexplode(
            COALESCE(table_class.relacl, acldefault('r', table_class.relowner))
        ) AS privilege
        LEFT JOIN pg_roles grantee_role ON grantee_role.oid = privilege.grantee
        WHERE table_class.oid = %s
          AND privilege.grantee <> table_class.relowner
        """,
        (table_oid,),
    ).fetchall()
    for grantee, privilege, is_grantable in grants:
        if privilege not in {
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        }:
            raise RuntimeError(
                f"Cannot safely preserve {privilege!r} privilege on "
                f"{table_name!r}"
            )
        grantee_identifier = (
            sql.SQL("PUBLIC")
            if grantee is None
            else sql.Identifier(grantee)
        )
        grant_statement = sql.SQL("GRANT {} ON TABLE {} TO {}{}").format(
            sql.SQL(privilege),
            staging,
            grantee_identifier,
            sql.SQL(" WITH GRANT OPTION") if is_grantable else sql.SQL(""),
        )
        connection.execute(grant_statement)
    connection.execute(
        sql.SQL("ALTER TABLE {} RENAME TO {}").format(
            table,
            backup,
        )
    )
    connection.execute(
        sql.SQL("ALTER TABLE {} RENAME TO {}").format(
            staging,
            sql.Identifier(table_name),
        )
    )
    connection.execute(
        sql.SQL("DROP TABLE {}").format(backup)
    )
    connection.execute(
        sql.SQL("ALTER INDEX {} RENAME TO {}").format(
            sql.Identifier(f"{staging_name}_pkey"),
            sql.Identifier(f"{table_name}_pkey"),
        )
    )
    return dropped_fields, True


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
    ensure_schema(
        connection,
        [dataset],
        {dataset: normalized_fields},
        include_defaults=False,
    )
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
    ensure_schema(
        connection,
        [dataset],
        {dataset: normalized_fields},
        include_defaults=False,
    )
    record_key(dataset, normalized)
    write_record(connection, dataset, normalized_fields, normalized)
