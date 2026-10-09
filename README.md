# DnD-Video Game

## Accounts

Install the backend dependencies with `pip install -r backend/requirements.txt`, then run the backend with `python backend/main.py`. Players must log in with the `username` and `password_hash` in `backend/account.csv`. Passwords are stored as bcrypt hashes; an account can set its password through the password-reset flow.

The reset flow selects an account by its `Name` column and appends a one-time `Name,Code` row to `backend/codes.csv`. The code is not returned to the browser; an administrator must retrieve it from that file and send it to the account owner. The owner enters the code and a new password to replace the account's bcrypt hash. Reset codes are stored as plain text, so protect access to `backend/codes.csv` and verify the requester before sharing a code.