# DnD-Video Game

## Updates

The current game version is `v0.5.7-alpha`. It is shown in the corner of the page. The page checks the server version on connection and periodically while open. If a newer server version is available, a reload banner appears over the login page or game. After a server reconnect, signed-in players need to log in again. For each release, update `GAME_VERSION` in `backend/main.py`, `APP_VERSION` in `frontend/index.html`, and the visible version label to the same version.

## World and controls

The grassy play area is a circle 128 game units in diameter. Drag the scene to orbit the camera. Hold WASD or the arrow keys to move relative to the camera; mobile players can use the on-screen joystick. Keyboard and joystick movement share the same maximum speed and support diagonal movement. Wind speed and direction vary over time, and both the sky and grass colors respond to the current wind.

## Accounts

Install the backend dependencies with `pip install -r backend/requirements.txt`, then run the backend with `python backend/main.py`. The CSV files in `backend/` are the authoritative data store; the server reads and writes them directly and does not pull from PostgreSQL or Neon at startup. `account.csv` is authoritative for login accounts and password hashes, and `codes.csv` stores one-time reset codes. Account and reset changes are written to those files immediately. `ACCOUNT_FILE` and `CODES_FILE` can override the account and reset-code paths.

`players.csv` stores character information. The server creates or updates its `x`, `y`, and `z` columns as players join and move, saving positions at most once per second and again when they disconnect; positions are restored on their next login. `stats.csv` and `abilities.csv` are reference/configuration data and are not changed by the current movement-only game. They will only change when a game feature explicitly updates those records. The former `backend/sync.py` Neon push/pull workflow is no longer used; changing a CSV is now the source-of-truth update.

The CSVs must be writable by the server process. On hosting services with an ephemeral filesystem, use a persistent disk and place the CSVs there (setting `ACCOUNT_FILE` and `CODES_FILE` as needed), and keep independent backups. Without persistent storage, local CSV changes can be lost when the host restarts or redeploys.

Signed-in players can open **Settings** in the game to change their password. They must enter their current password and choose a new password between 8 and 72 UTF-8 bytes. Passwords are stored as bcrypt hashes in the configured account CSV.

The reset flow selects an account by its name and saves a one-time code to `backend/codes.csv` (or `CODES_FILE`). The code is not returned to the browser; an administrator must retrieve it from the file and send it to the account owner. Reset codes are stored as plain text, so restrict file access and verify the requester before sharing a code.

The backend uses `websockets` for game connections and serves HTTP health checks separately: `GET` and `HEAD` requests to `/healthz` return `200 OK` (and `/` remains supported). Health checks do not require a WebSocket upgrade. The game server must still run on a host reachable by players; a database preserves accounts but does not keep the game server online. Three.js remains loaded from its CDN in the browser. The backend writes timestamped server and player activity events to `backend/server.log` and also prints them to the console. The log rotates at 1 MB and keeps three backups (`server.log.1` through `server.log.3`), for a maximum of about 4 MB total. It records connection, login, game-entry, and password-reset events, but never logs passwords or reset codes.