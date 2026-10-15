import asyncio
import csv
import hmac
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
import secrets
import tempfile
import time
from pathlib import Path

import bcrypt
from data_store import (
    DATABASE_URL,
    connect,
    ensure_schema,
    fetch_dataset,
    replace_dataset,
    upsert_record,
)
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

BACKEND_DIR = Path(__file__).resolve().parent

ACCOUNT_FILE = Path(os.environ.get("ACCOUNT_FILE", BACKEND_DIR / "account.csv"))
CODES_FILE = Path(os.environ.get("CODES_FILE", BACKEND_DIR / "codes.csv"))
PLAYERS_FILE = BACKEND_DIR / "players.csv"
LOG_FILE = BACKEND_DIR / "server.log"
GAME_VERSION = "v0.6.1-alpha"
POSITION_SAVE_INTERVAL = 1.0
MAP_RADIUS = 64.0
connected_players = set()


def dataset_for_path(path):
    candidate = Path(path)
    dataset_paths = {
        "accounts": ACCOUNT_FILE,
        "codes": CODES_FILE,
        "players": PLAYERS_FILE,
        "stats": BACKEND_DIR / "stats.csv",
        "abilities": BACKEND_DIR / "abilities.csv",
    }
    return next(
        (
            dataset
            for dataset, dataset_path in dataset_paths.items()
            if candidate == dataset_path
        ),
        None,
    )


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
    dataset = dataset_for_path(path)
    if DATABASE_URL and dataset:
        with connect() as connection:
            ensure_schema(connection)
            fieldnames, records = fetch_dataset(connection, dataset)
        if not set(required_fields).issubset(fieldnames):
            raise ValueError(f"{path.name} is missing required CSV columns")
        return fieldnames, list(records.values())

    with path.open("r", newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames or not set(required_fields).issubset(
            reader.fieldnames
        ):
            raise ValueError(f"{path.name} is missing required CSV columns")
        return reader.fieldnames, list(reader)


def write_csv(path, fieldnames, rows):
    dataset = dataset_for_path(path)
    if DATABASE_URL and dataset:
        with connect() as connection:
            ensure_schema(connection)
            replace_dataset(connection, dataset, fieldnames, rows)
        return

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


def account_name_for_identifier(identifier):
    fieldnames, accounts = account_rows()
    matches = matching_csv_accounts(identifier, fieldnames, accounts)
    if len(matches) != 1:
        raise ValueError("Account identifier does not match exactly one account")
    return matches[0][csv_field(fieldnames, "name", ACCOUNT_FILE)]


def player_position_from_message(data):
    coordinates = {}
    for axis in ("x", "y", "z"):
        value = data.get(axis)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        coordinate = float(value)
        if not math.isfinite(coordinate):
            return None
        coordinates[axis] = coordinate

    if (
        not 1 <= coordinates["y"] <= 100
        or math.hypot(coordinates["x"], coordinates["z"]) > MAP_RADIUS
    ):
        return None
    return coordinates


def saved_player_position(name):
    if not DATABASE_URL and not PLAYERS_FILE.exists():
        return None
    fieldnames, players = read_csv(PLAYERS_FILE, ())
    name_field = csv_field(fieldnames, "name", PLAYERS_FILE)
    coordinate_fields = {
        axis: next(
            (field for field in fieldnames if field.lower() == axis),
            None,
        )
        for axis in ("x", "y", "z")
    }
    if any(field is None for field in coordinate_fields.values()):
        return None

    matching_players = [
        player for player in players if player.get(name_field) == name
    ]
    if not matching_players:
        return None
    if len(matching_players) > 1:
        raise ValueError(f"{PLAYERS_FILE.name} has duplicate records for {name!r}")

    try:
        position = {
            axis: float(matching_players[0][field])
            for axis, field in coordinate_fields.items()
        }
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in position.values()):
        return None
    if (
        not 1 <= position["y"] <= 100
        or math.hypot(position["x"], position["z"]) > MAP_RADIUS
    ):
        return None
    return position


def save_player_position(name, position):
    if DATABASE_URL:
        with connect() as connection:
            ensure_schema(connection)
            fieldnames, records = fetch_dataset(connection, "players")
            name_field = csv_field(fieldnames, "name", PLAYERS_FILE)
            coordinate_fields = {}
            for axis in ("x", "y", "z"):
                field = next(
                    (
                        candidate
                        for candidate in fieldnames
                        if candidate.lower() == axis
                    ),
                    None,
                )
                if field is None:
                    field = axis
                    fieldnames.append(field)
                coordinate_fields[axis] = field

            matching_players = [
                player
                for player in records.values()
                if player.get(name_field) == name
            ]
            if len(matching_players) > 1:
                raise ValueError(
                    f"{PLAYERS_FILE.name} has duplicate records for {name!r}"
                )
            if matching_players:
                player = matching_players[0]
            else:
                player = {field: "" for field in fieldnames}
                player[name_field] = name
            for axis, field in coordinate_fields.items():
                player[field] = format(position[axis], ".15g")
            upsert_record(connection, "players", fieldnames, player)
        return

    if PLAYERS_FILE.exists():
        fieldnames, players = read_csv(PLAYERS_FILE, ())
    else:
        fieldnames = [
            "name",
            "race",
            "class",
            "current",
            "max",
            "gain",
            "loss",
            "modifier",
        ]
        players = []

    name_field = csv_field(fieldnames, "name", PLAYERS_FILE)
    coordinate_fields = {}
    for axis in ("x", "y", "z"):
        field = next(
            (candidate for candidate in fieldnames if candidate.lower() == axis),
            None,
        )
        if field is None:
            field = axis
            fieldnames.append(field)
        coordinate_fields[axis] = field

    matching_players = [
        player for player in players if player.get(name_field) == name
    ]
    if len(matching_players) > 1:
        raise ValueError(f"{PLAYERS_FILE.name} has duplicate records for {name!r}")
    if matching_players:
        player = matching_players[0]
    else:
        player = {field: "" for field in fieldnames}
        player[name_field] = name
        players.append(player)

    for axis, field in coordinate_fields.items():
        player[field] = format(position[axis], ".15g")
    write_csv(PLAYERS_FILE, fieldnames, players)












def list_account_names():
    fieldnames, accounts = account_rows()
    name_field = csv_field(fieldnames, "name", ACCOUNT_FILE)
    return [row[name_field] for row in accounts if row.get(name_field)]


def authenticate(identifier, password):
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
    fieldnames, accounts = account_rows()
    name_field = next(field for field in fieldnames if field.lower() == "name")
    if not any(row.get(name_field) == name for row in accounts):
        return None

    code = secrets.token_urlsafe(24)
    if DATABASE_URL or CODES_FILE.exists():
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
                account_name = account_name_for_identifier(identifier)
                websocket.authenticated = True
                websocket.account_identifier = identifier
                websocket.account_name = account_name
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
    except (OSError, csv.Error, UnicodeError, ValueError):
        logging.exception("Account operation failed")
        await send_auth_error(websocket, "The account request could not be completed.")


async def handler(websocket):
    connected_players.add(websocket)
    websocket.player_id = None
    websocket.authenticated = False
    websocket.account_identifier = None
    websocket.account_name = None
    websocket.last_position = None
    websocket.last_position_saved_at = 0.0
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

            message_type = data.get("type")
            if message_type in {"join", "reply"} and "id" in data:
                websocket.player_id = data["id"]
                if message_type == "join":
                    position = saved_player_position(websocket.account_name)
                    if position is None:
                        position = player_position_from_message(data)
                    if position is None:
                        logging.warning(
                            "Rejected invalid player position for account %r",
                            websocket.account_name,
                        )
                        continue
                    try:
                        save_player_position(websocket.account_name, position)
                    except (OSError, csv.Error, ValueError):
                        logging.exception(
                            "Could not save player position for account %r",
                            websocket.account_name,
                        )
                        await send_auth_error(
                            websocket,
                            "Your player data could not be saved. Please try again.",
                        )
                        continue
                    websocket.last_position = position
                    websocket.last_position_saved_at = time.monotonic()
                    data.update(position)
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "restore_position",
                                "position": position,
                            }
                        )
                    )
                    logging.info(
                        "Player entered the game (player_id=%r)", websocket.player_id
                    )
            elif message_type == "move" and data.get("id") == websocket.player_id:
                position = player_position_from_message(data)
                if position is None:
                    logging.warning(
                        "Rejected invalid player position for account %r",
                        websocket.account_name,
                    )
                    continue
                websocket.last_position = position
                now = time.monotonic()
                if now - websocket.last_position_saved_at >= POSITION_SAVE_INTERVAL:
                    try:
                        save_player_position(websocket.account_name, position)
                    except (OSError, csv.Error, ValueError):
                        logging.exception(
                            "Could not save player position for account %r",
                            websocket.account_name,
                        )
                        await send_auth_error(
                            websocket,
                            "Your player data could not be saved. Please try again.",
                        )
                    else:
                        websocket.last_position_saved_at = now

            for player in connected_players:
                if player != websocket and player.authenticated:
                    await send_game_message(player, json.dumps(data))

    finally:
        connected_players.discard(websocket)
        if websocket.account_name and websocket.last_position is not None:
            try:
                save_player_position(
                    websocket.account_name,
                    websocket.last_position,
                )
            except (OSError, csv.Error, ValueError):
                logging.exception(
                    "Could not save final player position for account %r",
                    websocket.account_name,
                )
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
    if DATABASE_URL:
        with connect() as connection:
            ensure_schema(connection)
    _, accounts = account_rows()
    if DATABASE_URL and not accounts:
        raise RuntimeError(
            "Neon contains no game accounts. Run `python backend/sync.py` "
            "from the Codespace to import and reconcile the CSV data."
        )

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
    asyncio.run(main())
