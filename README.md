# DnD-Video Game

## Updates

The current game version is `v0.6.10-alpha`. It is shown in the corner of the page. The page checks the server version on connection and periodically while open. If a newer server version is available, a reload banner appears over the login page or game. After a server reconnect, signed-in players need to log in again. The page starts its server connection before loading the 3D game and reports browser startup errors on the login screen. For each release, update `GAME_VERSION` in `backend/main.py`, `APP_VERSION` in `frontend/index.html`, and the visible version label to the same version.

## World and controls

The grassy play area is a circle 128 game units in diameter. Drag the scene to orbit the camera. Hold WASD or the arrow keys to move relative to the camera; mobile players can use the on-screen joystick. Keyboard and joystick movement share the same maximum speed and support diagonal movement. Wind speed and direction vary over time, and both the sky and grass colors respond to the current wind.

## Accounts

Install the backend dependencies with `pip install -r backend/requirements.txt`, then run the backend with `python backend/main.py`. When `DATABASE_URL` is set, the server reads and writes game data in Neon. It uses tables named after the CSV files (`account`, `codes`, `players`, `stats`, `abilities`) with columns matching their headers, plus `dnd_sync_state` for the sync baseline. Before the first Render deploy, add `DATABASE_URL` as a secret environment variable in Render and import the existing CSV data from the Codespace with `python backend/sync.py`. The connection URL in the Codespace `.env` is not available to Render automatically.

The CSV files remain editable copies. Run `python backend/sync.py` from the Codespace after editing CSVs, or to bring Neon changes back into the files. The sync compares both sides to the last successful sync, automatically propagates one-sided edits, and prompts you to choose CSV or Neon when both sides changed the same record. The CSV header order becomes the leading Neon column order; extra Neon columns are removed only when all their values are empty, while populated extras are retained after CSV columns. Column reordering rebuilds a table while preserving primary-key and `NOT NULL` constraints, so the sync stops rather than rebuilding tables with other constraints, extra indexes, triggers, or dependent objects. Use `python backend/sync.py --dry-run` to preview pending changes without applying schema changes. Keep the CSVs available when running the sync tool; it refuses to treat missing files as empty and will not silently delete Neon data. `ACCOUNT_FILE` and `CODES_FILE` can override the account and reset-code CSV paths.

Player positions are stored in Neon and restored on the next login. `stats.csv` and `abilities.csv` are synchronized too, although the current movement-only game does not modify them.

Without `DATABASE_URL`, the server uses the CSV files directly, so they must be writable and persistent on the host. With Neon enabled, the server does not need those files; the sync command does, and should be run from the Codespace where you edit them. Keep independent backups of both the CSVs and Neon.

Signed-in players can open **Settings** in the game to change their password. They must enter their current password and choose a new password between 8 and 72 UTF-8 bytes. Passwords are stored as bcrypt hashes in Neon when configured, or in the account CSV in file-backed mode.

The reset flow selects an account by its name and saves a one-time code to Neon when configured, or `backend/codes.csv` (or `CODES_FILE`) in file-backed mode. The code is not returned to the browser; an administrator must retrieve it from the configured store and send it to the account owner. Reset codes are stored as plain text, so restrict access and verify the requester before sharing a code.

The backend uses `websockets` for game connections and serves HTTP health checks separately: `GET` and `HEAD` requests to `/healthz` return `200 OK` (and `/` remains supported). Health checks do not require a WebSocket upgrade. The game server must still run on a host reachable by players; a database preserves accounts but does not keep the game server online. Three.js remains loaded from its CDN in the browser. The backend writes timestamped server and player activity events to `backend/server.log` and also prints them to the console. The log rotates at 1 MB and keeps three backups (`server.log.1` through `server.log.3`), for a maximum of about 4 MB total. It records connection, login, game-entry, and password-reset events, but never logs passwords or reset codes.