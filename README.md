# DnD-Video Game

## Updates

The current game version is `v0.5.7-alpha`. It is shown in the corner of the page. The page checks the server version on connection and periodically while open. If a newer server version is available, a reload banner appears over the login page or game. After a server reconnect, signed-in players need to log in again. For each release, update `GAME_VERSION` in `backend/main.py`, `APP_VERSION` in `frontend/index.html`, and the visible version label to the same version.

## Accounts

Install the backend dependencies with `pip install -r backend/requirements.txt`, then run the backend with `python backend/main.py`. Players log in using either the `name` or `username` and the account's `password_hash` in `backend/account.csv`. Set the `ACCOUNT_FILE` environment variable to use an account CSV at a different path; the default is `backend/account.csv`. Passwords are stored as bcrypt hashes; a password reset still selects the account by its `Name` column and uses the one-time code flow.

Signed-in players can open **Settings** in the game to change their password. They must enter their current password and choose a new password between 8 and 72 UTF-8 bytes. The server stores the new bcrypt hash in the configured account CSV.

The reset flow selects an account by its `Name` column and appends a one-time name/code row to `backend/codes.csv` (or the path set by `CODES_FILE`). The `name` and `code` headers are matched case-insensitively, so either `name,code` or `Name,Code` works. The code is not returned to the browser; an administrator must retrieve it from the backend's file and send it to the account owner. The owner enters the code and a new password to replace the account's bcrypt hash. Reset codes are stored as plain text, so protect access to the codes file and verify the requester before sharing a code.

The backend uses `websockets` for game connections and serves HTTP health checks separately: `GET` and `HEAD` requests to `/healthz` return `200 OK` (and `/` remains supported). Health checks do not require a WebSocket upgrade. The frontend connects to this Codespace at `wss://fluffy-space-fortnight-g4pjw54x6rprhwwwr.github.dev`. Three.js remains loaded from its CDN in the browser. The backend writes timestamped server and player activity events to `backend/server.log` and also prints them to the console. The log rotates at 1 MB and keeps three backups (`server.log.1` through `server.log.3`), for a maximum of about 4 MB total. It records connection, login, game-entry, and password-reset events, but never logs passwords or reset codes.