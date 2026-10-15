import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from data_store import (
    BACKEND_DIR,
    DATASETS,
    connect,
    ensure_schema,
    fetch_dataset,
    normalize_row,
    record_key,
    replace_dataset,
)
from psycopg import sql

CSV_PATHS = {
    "accounts": Path(os.environ.get("ACCOUNT_FILE", BACKEND_DIR / "account.csv")),
    "codes": Path(os.environ.get("CODES_FILE", BACKEND_DIR / "codes.csv")),
    "players": BACKEND_DIR / "players.csv",
    "stats": BACKEND_DIR / "stats.csv",
    "abilities": BACKEND_DIR / "abilities.csv",
}


def row_hash(row):
    if row is None:
        return None
    serialized = json.dumps(
        normalize_row(row),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def read_csv_dataset(dataset, path):
    if not path.is_file():
        raise FileNotFoundError(
            f"Required CSV file {path} is missing; refusing to treat it as empty."
        )
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames:
            raise ValueError(f"{path.name} has no CSV header")
        fields = [field for field in reader.fieldnames if field]
        rows = {}
        for row in reader:
            normalized = normalize_row(row)
            key = record_key(dataset, normalized)
            if key in rows:
                raise ValueError(f"{path.name} contains duplicate key {key}")
            rows[key] = normalized
        return fields, rows


def align_rows_to_fields(rows, fields):
    aligned = {}
    for key, row in rows.items():
        values_by_lower = {field.lower(): value for field, value in row.items()}
        aligned[key] = {
            field: values_by_lower.get(field.lower(), "")
            for field in fields
        }
    return aligned


def choose_conflict(dataset, key, csv_row, neon_row):
    try:
        display_key = ", ".join(json.loads(key))
    except (json.JSONDecodeError, TypeError):
        display_key = key
    changed_fields = sorted(
        field
        for field in set(csv_row or {}) | set(neon_row or {})
        if (csv_row or {}).get(field) != (neon_row or {}).get(field)
    )
    print(
        f"\nConflict in {dataset} [{display_key}]: "
        "both CSV and Neon changed since the previous sync. "
        f"Different fields: {', '.join(changed_fields) or '(record deleted)'}."
    )
    while True:
        choice = input("Keep [c]sv or [n]eon? ").strip().lower()
        if choice in {"c", "csv"}:
            return "csv"
        if choice in {"n", "neon"}:
            return "neon"
        print("Enter 'c' for CSV or 'n' for Neon.")


def merge_dataset(dataset, csv_rows, neon_rows, baseline, resolver):
    result = {}
    conflicts = 0
    for key in sorted(set(csv_rows) | set(neon_rows) | set(baseline)):
        csv_row = csv_rows.get(key)
        neon_row = neon_rows.get(key)
        csv_hash = row_hash(csv_row)
        neon_hash = row_hash(neon_row)
        has_baseline = key in baseline
        previous_hash = baseline.get(key)

        if csv_hash == neon_hash:
            result[key] = csv_row
        elif not has_baseline:
            if csv_row is None:
                result[key] = neon_row
            elif neon_row is None:
                result[key] = csv_row
            else:
                conflicts += 1
                choice = resolver(dataset, key, csv_row, neon_row)
                if choice not in {"csv", "neon"}:
                    raise ValueError("Conflict resolver must choose CSV or Neon")
                result[key] = csv_row if choice == "csv" else neon_row
        elif csv_hash == previous_hash:
            result[key] = neon_row
        elif neon_hash == previous_hash:
            result[key] = csv_row
        else:
            conflicts += 1
            choice = resolver(dataset, key, csv_row, neon_row)
            if choice not in {"csv", "neon"}:
                raise ValueError("Conflict resolver must choose CSV or Neon")
            result[key] = csv_row if choice == "csv" else neon_row
    return result, conflicts


def write_csv_dataset(path, fields, rows):
    fields = [field for field in fields if field]
    if not fields:
        raise ValueError(f"Cannot write {path.name} without CSV columns")
    path.parent.mkdir(parents=True, exist_ok=True)
    old_mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
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
            writer = csv.DictWriter(csv_file, fieldnames=fields)
            writer.writeheader()
            for key in sorted(rows):
                row = rows[key]
                if row is not None:
                    writer.writerow({field: row.get(field, "") for field in fields})
        temporary_path.chmod(old_mode)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def sync(dry_run=False):
    csv_snapshots = {
        dataset: read_csv_dataset(dataset, path)
        for dataset, path in CSV_PATHS.items()
    }
    with connect() as connection:
        ensure_schema(
            connection,
            fields_by_dataset={
                dataset: fields
                for dataset, (fields, _) in csv_snapshots.items()
            },
        )
        neon_snapshots = {
            dataset: fetch_dataset(connection, dataset)
            for dataset in DATASETS
        }
        state_rows = connection.execute(
            "SELECT dataset, record_key, last_hash FROM dnd_sync_state"
        ).fetchall()
    baseline_by_dataset = {dataset: {} for dataset in DATASETS}
    for dataset, key, last_hash in state_rows:
        baseline_by_dataset[dataset][key] = last_hash

    plans = {}
    total_conflicts = 0
    resolver = choose_conflict
    if dry_run:
        resolver = lambda *args: "csv"
    elif not sys.stdin.isatty():
        def resolver(dataset, key, csv_row, neon_row):
            raise RuntimeError(
                "A CSV/Neon conflict requires a person to choose a side. "
                "Run sync.py in an interactive terminal."
            )

    for dataset in DATASETS:
        _, csv_rows = csv_snapshots[dataset]
        neon_fields, neon_rows = neon_snapshots[dataset]
        csv_rows = align_rows_to_fields(csv_rows, neon_fields)
        target_rows, conflicts = merge_dataset(
            dataset,
            csv_rows,
            neon_rows,
            baseline_by_dataset[dataset],
            resolver,
        )
        plans[dataset] = {
            "fields": neon_fields,
            "csv_rows": csv_rows,
            "neon_rows": neon_rows,
            "target_rows": target_rows,
        }
        total_conflicts += conflicts

    changes = 0
    for plan in plans.values():
        keys = set(plan["csv_rows"]) | set(plan["neon_rows"])
        changes += sum(
            row_hash(plan["csv_rows"].get(key))
            != row_hash(plan["target_rows"].get(key))
            for key in keys
        )
        changes += sum(
            row_hash(plan["neon_rows"].get(key))
            != row_hash(plan["target_rows"].get(key))
            for key in keys
        )
    if dry_run:
        print(
            f"Dry run: {changes} side(s) would change; "
            f"{total_conflicts} conflict(s) resolved for this run."
        )
        return

    current_csv_snapshots = {
        dataset: read_csv_dataset(dataset, CSV_PATHS[dataset])
        for dataset in DATASETS
    }
    for dataset in DATASETS:
        if current_csv_snapshots[dataset] != csv_snapshots[dataset]:
            raise RuntimeError(
                f"{CSV_PATHS[dataset].name} changed during sync; "
                "rerun to avoid overwriting edits."
            )

    with connect() as connection:
        ensure_schema(connection)
        tables = sql.SQL(", ").join(
            sql.Identifier(DATASETS[dataset]["table_name"])
            for dataset in DATASETS
        )
        connection.execute(
            sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(tables)
        )
        latest_neon_snapshots = {
            dataset: fetch_dataset(connection, dataset)
            for dataset in DATASETS
        }
        latest_state_rows = connection.execute(
            "SELECT dataset, record_key, last_hash FROM dnd_sync_state"
        ).fetchall()
        latest_state = {dataset: {} for dataset in DATASETS}
        for dataset, key, last_hash in latest_state_rows:
            latest_state[dataset][key] = last_hash
        if (
            latest_neon_snapshots != neon_snapshots
            or latest_state != baseline_by_dataset
        ):
            raise RuntimeError(
                "Neon changed while sync was waiting for conflict decisions; "
                "no changes were applied. Run sync.py again."
            )
        for dataset, plan in plans.items():
            replace_dataset(
                connection,
                dataset,
                plan["fields"],
                [
                    row
                    for row in plan["target_rows"].values()
                    if row is not None
                ],
            )

    for dataset, plan in plans.items():
        write_csv_dataset(
            CSV_PATHS[dataset],
            plan["fields"],
            plan["target_rows"],
        )

    with connect() as connection:
        ensure_schema(connection)
        for dataset, plan in plans.items():
            all_keys = set(plan["target_rows"]) | set(baseline_by_dataset[dataset])
            for key in all_keys:
                connection.execute(
                    """
                    INSERT INTO dnd_sync_state (dataset, record_key, last_hash)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (dataset, record_key)
                    DO UPDATE SET last_hash = EXCLUDED.last_hash
                    """,
                    (dataset, key, row_hash(plan["target_rows"].get(key))),
                )

    print(
        f"Sync complete: {changes} side(s) updated; "
        f"{total_conflicts} conflict(s) resolved."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile backend CSV files with the Neon game store."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show how many changes are pending without applying them.",
    )
    arguments = parser.parse_args()
    try:
        sync(dry_run=arguments.dry_run)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Sync failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())