# Data Center Intelligence Portal

This is a public-web-only MVP for a daily data center intelligence workflow.

Pipeline:

1. At 8:00 AM, search selected companies with Tavily
2. Optionally clean source pages with Firecrawl
3. Use Gemini to keep only data-center-relevant findings
4. Store the finished report in a login-protected portal
5. Let a user review the report, manage recipients, and click one button to send through MailerSend
6. Use the Unit Test tab to pick one company, generate a 24-hour report, and aim it at chosen recipients

The app does not read company inboxes. It only sends through MailerSend from the approved sending domain.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item config.example.json config.json
```

Edit `.env` with API keys and portal credentials. Edit `config.json` with your companies, initial recipients, sender, and timezone.

## Required Environment

```text
TAVILY_API_KEY=...
PORTAL_USERNAME=...
PORTAL_PASSWORD_HASH=...
PORTAL_SECRET_KEY=...
PORTAL_JWT_SECRET=...
PORTAL_JWT_EXP_MINUTES=480
MAILERSEND_API_KEY=...
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.0-flash
```

Firecrawl is optional but recommended:

```text
FIRECRAWL_API_KEY=...
```

Generate a password hash instead of storing a plaintext portal password:

```powershell
python -c "from werkzeug.security import generate_password_hash; import getpass; print(generate_password_hash(getpass.getpass('Portal password: ')))"
```

Generate a session secret:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Put those generated values into `.env` as `PORTAL_PASSWORD_HASH` and `PORTAL_SECRET_KEY`.

Generate a separate JWT signing secret by running the same command again:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Put that value into `.env` as `PORTAL_JWT_SECRET`. `PORTAL_JWT_EXP_MINUTES` controls how long the login cookie remains valid.

## Run The Secure Portal

```powershell
python portal.py
```

Open:

```text
http://127.0.0.1:8000
```

The portal:

- generates a report every day at `schedule_hour` / `schedule_minute` in `config.json`
- has a `Run now` button for manual report generation
- has a `Unit Test` tab for one-company, last-24-hours reports
- stores reports in `agent_state.sqlite3`
- lets you add or remove recipient emails
- sends the reviewed report through MailerSend with one button

For production, run the portal behind company SSO or a private VPN/reverse proxy with HTTPS. Set this when HTTPS is enabled:

```text
PORTAL_COOKIE_SECURE=true
```

## Secret Protection

Real credentials belong in `.env`, not `.env.example`, `README.md`, or source files. The repo ignores `.env`, local config, service-account JSON files, private keys, generated drafts, and the SQLite state database.

Run this before committing:

```powershell
python scripts/scan_secrets.py
```

If a real key was committed, rotate that key in the provider dashboard. Removing it from the latest file is not enough once it has existed in git history.

## MailerSend Setup

Use an approved sender such as:

```text
datacenter-alerts@company.com
```

Create a MailerSend API token in Settings -> API Tokens and verify the sending domain for the `sender` address in `config.json`. Keep the token limited to the account and domain your team approves.

## Manual CLI Generation

You can still generate a report without the portal:

```powershell
python Main.py
```

That stores a draft report in the same portal database.
