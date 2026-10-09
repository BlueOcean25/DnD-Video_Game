import asyncio
import csv
import hmac
import json
import logging
import secrets
import tempfile
from pathlib import Path

import bcrypt
import websockets


ACCOUNT_FILE = Path(__file__).with_name("account.csv")
CODES_FILE = Path(__file__).with_name("codes.csv")
LOG_FILE = Path(__file__).with_name("server.log")
GAME_VERSION = "v0.5.3-alpha"
connected_players = set()


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )


def read_csv(path, required_fields):
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames or not set(required_fields).issubset(reader.fieldnames):
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


def account_rows():
    fieldnames, accounts = read_csv(ACCOUNT_FILE, ("password_hash",))
    if not any(field.lower() == "name" for field in fieldnames):
        raise ValueError(f"{ACCOUNT_FILE.name} is missing the account name column")
    return fieldnames, accounts


def list_account_names():
    fieldnames, accounts = account_rows()
    name_field = next(field for field in fieldnames if field.lower() == "name")
    return [row[name_field] for row in accounts if row.get(name_field)]


def authenticate(identifier, password):
    fieldnames, accounts = account_rows()
    name_field = next(field for field in fieldnames if field.lower() == "name")
    username_field = next(
        (field for field in fieldnames if field.lower() == "username"),
        None,
    )
    matching_accounts = [
        row
        for row in accounts
        if row.get(name_field) == identifier
        or (username_field is not None and row.get(username_field) == identifier)
    ]
    if len(matching_accounts) != 1:
        return False
    account = matching_accounts[0]
    if account is None or not account.get("password_hash"):
        return False

    try:
        return bcrypt.checkpw(password.encode("utf-8"), account["password_hash"].encode("ascii"))
    except (UnicodeEncodeError, ValueError):
        logging.warning("Invalid bcrypt hash or password encoding for account %s", identifier)
        return False


def create_reset_code(name):
    fieldnames, accounts = account_rows()
    name_field = next(field for field in fieldnames if field.lower() == "name")
    if not any(row.get(name_field) == name for row in accounts):
        return None

    code = secrets.token_urlsafe(24)
    if CODES_FILE.exists():
        fieldnames, codes = read_csv(CODES_FILE, ("Name", "Code"))
    else:
        fieldnames, codes = ["Name", "Code"], []
    codes.append({**{field: "" for field in fieldnames}, "Name": name, "Code": code})
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

    code_fields, codes = read_csv(CODES_FILE, ("Name", "Code"))
    matching_code_index = next(
        (
            index
            for index, row in enumerate(codes)
            if row.get("Name") == name
            and hmac.compare_digest(
                (row.get("Code") or "").encode("utf-8"),
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
            await websocket.send(json.dumps({
                "type": "account_names",
                "names": list_account_names(),
            }))
        elif message_type == "check_version":
            client_version = data.get("version")
            if isinstance(client_version, str) and client_version != GAME_VERSION:
                await websocket.send(json.dumps({
                    "type": "update_available",
                    "version": GAME_VERSION,
                }))
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
                logging.info("Login succeeded for account identifier %r", identifier)
            else:
                logging.warning("Login failed for account identifier %r", identifier)
            await websocket.send(json.dumps({
                "type": "login_result",
                "success": bool(valid),
            }))
        elif message_type == "request_reset_code":
            name = data.get("name", "")
            code = create_reset_code(name) if isinstance(name, str) else None
            if code is not None:
                logging.info("Password reset code requested for account %r", name)
            await websocket.send(json.dumps({
                "type": "reset_code_result",
                "success": code is not None,
            }))
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
            success = (
                reset_password(name, code, password)
                if password_valid
                else False
            )
            if success:
                logging.info("Password reset completed for account %r", name)
            else:
                logging.warning("Password reset failed for account %r", name)
            await websocket.send(json.dumps({
                "type": "password_reset_result",
                "success": success,
            }))
        return True
    except (OSError, csv.Error, UnicodeError, ValueError):
        logging.exception("Account operation failed")
        await send_auth_error(websocket, "The account request could not be completed.")
        return True


async def handler(websocket):
    connected_players.add(websocket)
    websocket.player_id = None
    websocket.authenticated = False
    logging.info("Client connected; active connections: %d", len(connected_players))

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
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
                "request_reset_code",
                "reset_password",
            }:
                await handle_auth_message(websocket, data)
                continue

            if not websocket.authenticated:
                continue

            if data.get("type") in ["join", "reply"] and "id" in data:
                websocket.player_id = data["id"]
                if data.get("type") == "join":
                    logging.info("Player entered the game (player_id=%r)", websocket.player_id)

            for player in connected_players:
                if player != websocket and player.authenticated:
                    await player.send(json.dumps(data))

    except websockets.ConnectionClosed:
        pass
    finally:
        connected_players.remove(websocket)
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
                    try:
                        await player.send(json.dumps(leave_notification))
                    except websockets.ConnectionClosed:
                        pass


async def main():
    configure_logging()
    logging.info("Game server starting on port 10000 (version %s)", GAME_VERSION)
    async with websockets.serve(handler, "0.0.0.0", 10000):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
