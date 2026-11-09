# Running on an Intel MacBook

Run these commands in Terminal:

```sh
cd /Users/mac/Downloads/eventsys
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
sh run_local.sh
```

Open http://127.0.0.1:5001/login and sign in with your existing admin
account. Open Users in the menu to create users, reset passwords and unlock
accounts, or delete users. Only an Admin assigned to All events can manage
users. You cannot delete your own account.

The entry point is `flask_app.py` in the project root. The older app under
`mysite/` and `templates/manage_users.html` are not used by this entry point.
The users route renders `templates/admin_user.html`.

The launcher enables HTTP session cookies for local development and uses port
5001 to avoid a possible conflict with macOS AirPlay on port 5000. Stop with
Control-C. Keep the server bound to your Mac for local use.

SQLite is included with Python; no MySQL server is needed. The app uses the
existing `brandit_tickets_v1.db`. Back it up before changing real user or ticket
data. Application import runs database initialization and migrations.

For a new database only, set ADMIN_PASSWORD before the first launch to choose
the initial admin password. This variable does not reset an existing account.
If unset on a new database, the generated password appears in the startup log.

Online payments require PAYNOW_ID and PAYNOW_KEY. Email requires SMTP_EMAIL
and SMTP_PASSWORD (and optionally SMTP_SERVER and SMTP_PORT). Set these as
environment variables; local user management works without them. Real payment
callbacks need a publicly reachable DOMAIN_URL.

## Gate scanner

Open http://127.0.0.1:5001/scanner while signed in as Admin or Bouncer. Allow
camera access in the browser and in macOS System Settings → Privacy & Security
→ Camera. A phone accessing a plain HTTP LAN address cannot use the camera;
use the HTTPS hosted site on phones. Manual ticket entry remains available
when camera access is denied. The QR decoder is served from `static/` and
does not require a CDN connection. Ticket verification still needs the server.

## External event listings

The reviewed November–December 2026 listings are in
`data/zimbabwe_events_2026.json`. They appear in admin with an EXTERNAL badge
and on the homepage with links to the original provider. Imported listings
have no local tickets or prices. See `data/README.md` for sources and import
instructions.

Checks: `.venv/bin/python -m unittest discover -s tests` and
`node tests/scanner_ui_test.cjs` (Node is only needed for the UI checks).
