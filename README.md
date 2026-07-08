# Data Center Report App

This app looks for news about data centers.

Then it turns that news into an email report.

Then you can review the report and send it.

## What This App Does

You type in a company name.

The app:

1. looks for public web articles
2. keeps the ones about data centers and related topics
3. writes a report
4. shows the report in a small web portal
5. lets you send the report or schedule it

## What You Need

You need:

- Python
- a Tavily API key
- a Gemini API key
- a Mailtrap sandbox inbox for test email sending

## First-Time Setup

Open PowerShell in this folder and run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item config.example.json config.json
```

## Step 1: Fill In `.env`

Open [`.env`](C:\Users\karan\Webscraper-\.env)

Add your real values:

```text
GEMINI_API_KEY=your_gemini_key
GEMINI_MODEL=gemini-2.5-flash

TAVILY_API_KEY=your_tavily_key
FIRECRAWL_API_KEY=your_firecrawl_key

SMTP_USERNAME=your_mailtrap_username
SMTP_PASSWORD=your_mailtrap_password
SMTP_HOST=sandbox.smtp.mailtrap.io
SMTP_PORT=2525

PORTAL_USERNAME=your_username
PORTAL_PASSWORD_HASH=put_a_hash_here
PORTAL_SECRET_KEY=put_a_secret_here
PORTAL_JWT_SECRET=put_another_secret_here
PORTAL_JWT_EXP_MINUTES=480
```

Notes:

- `FIRECRAWL_API_KEY` is optional
- `SMTP_*` should come from your Mailtrap sandbox inbox

## Step 2: Make The Login Password

Run this:

```powershell
python -c "from werkzeug.security import generate_password_hash; import getpass; print(generate_password_hash(getpass.getpass('Portal password: ')))"
```

Paste the result into `.env` for:

```text
PORTAL_PASSWORD_HASH=
```

## Step 3: Make The Secret Keys

Run this:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Run it 2 times.

Put one result here:

```text
PORTAL_SECRET_KEY=
```

Put the other result here:

```text
PORTAL_JWT_SECRET=
```

## Step 4: Fill In `config.json`

Open [`config.json`](C:\Users\karan\Webscraper-\config.json)

You will see something like this:

```json
{
  "companies": [
    "Microsoft",
    "Amazon Web Services",
    "Google",
    "Meta"
  ],
  "recipients": [],
  "sender": "sandbox@example.com",
  "days_back": 7,
  "max_results_per_company": 5,
  "draft_only": false,
  "require_human_approval": true,
  "email_provider": "smtp",
  "db_path": "agent_state.sqlite3",
  "draft_dir": "drafts",
  "schedule_hour": 8,
  "schedule_minute": 0,
  "timezone": "America/New_York",
  "sender_name": "Data Center Intelligence"
}
```

Change the parts you want:

- `companies`: the companies you care about
- `sender`: the sender name/address you want to show in testing
- `schedule_hour`: what hour the daily run should happen
- `schedule_minute`: what minute the daily run should happen

Keep this as:

```json
"email_provider": "smtp"
```

## Step 5: Start The App

Run:

```powershell
python portal.py
```

Then open:

```text
http://127.0.0.1:8000
```

## How To Use It

### Daily report

The app can make reports on a schedule.

It uses the companies listed in `config.json`.

### Run now

Press `Run now` if you want a report right away.

### Unit Test

Open the `Unit Test` tab if you want to test one company.

You:

1. type the company name
2. type recipient emails
3. press the button

The app:

1. looks for news from the last 24 hours
2. if nothing qualifies, it can fall back to 3 days
3. writes the report
4. shows diagnostics so you can see what happened

### Review report

After a report is made, you can:

- read it
- send it now
- schedule it for later

## What The Diagnostics Mean

On the report page you may see a `Diagnostics` box.

This helps explain why a report looks the way it does.

- `Raw Tavily results`: how many search hits came back
- `Qualified results in requested window`: how many passed the date filter
- `Recent undated pages included`: how many had no clean date but looked recent enough
- `Fallback used`: whether the app widened the time window
- `Structured summary fallback used`: whether Gemini failed and the app built a simpler backup summary

## Where Test Emails Go

Right now this app is set up for **Mailtrap sandbox**.

That means test emails do **not** go to real inboxes.

They go to your Mailtrap inbox so you can inspect them safely.

## Helpful Commands

Start the portal:

```powershell
python portal.py
```

Generate a report from the command line:

```powershell
python Main.py
```

See which Gemini models your key can use:

```powershell
python scripts/list_gemini_models.py
```

Scan for secrets before committing:

```powershell
python scripts/scan_secrets.py
```

## Important Safety Note

Do not put real secrets in:

- `README.md`
- `.env.example`
- source code

Put real secrets only in:

- [`.env`](C:\Users\karan\Webscraper-\.env)

## If Something Breaks

If the report says no updates were found:

- the search may not have found enough recent pages
- the pages may not have usable dates
- the company may simply not have new public data center news right now

If the report uses a fallback summary:

- Gemini did not return clean JSON
- the app still made a backup structured report instead of failing

## In One Sentence

This app searches for data center news, turns it into an email report, and lets you test and send that report from a web page.
