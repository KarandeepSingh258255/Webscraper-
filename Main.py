from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from openai import OpenAI


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"
DEFAULT_DB_PATH = ROOT / "agent_state.sqlite3"
DEFAULT_DRAFT_DIR = ROOT / "drafts"


DATA_CENTER_TOPICS = [
    "new data center projects",
    "canceled or paused data center projects",
    "data center site locations",
    "utility or power constraints",
    "leadership changes related to infrastructure",
    "earnings call mentions of data centers or AI infrastructure",
    "new data center deals or partnerships",
    "company struggles, delays, financing risks, or regulatory risks",
]


SUMMARY_SCHEMA = {
    "name": "data_center_intelligence_email",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "email_subject": {"type": "string"},
            "email_text": {"type": "string"},
            "email_html": {"type": "string"},
            "companies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "company": {"type": "string"},
                        "summary": {"type": "string"},
                        "relevance": {
                            "type": "string",
                            "enum": ["high", "medium", "low", "none"],
                        },
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "claim": {"type": "string"},
                                    "why_it_matters": {"type": "string"},
                                    "category": {
                                        "type": "string",
                                        "enum": [
                                            "project",
                                            "canceled_or_paused",
                                            "site_location",
                                            "utility_power",
                                            "leadership",
                                            "earnings",
                                            "deal_or_partnership",
                                            "struggle_or_risk",
                                            "other",
                                        ],
                                    },
                                    "source_title": {"type": "string"},
                                    "source_url": {"type": "string"},
                                    "source_date": {"type": "string"},
                                },
                                "required": [
                                    "claim",
                                    "why_it_matters",
                                    "category",
                                    "source_title",
                                    "source_url",
                                    "source_date",
                                ],
                            },
                        },
                    },
                    "required": ["company", "summary", "relevance", "items"],
                },
            },
            "approval_notes": {"type": "string"},
        },
        "required": ["email_subject", "email_text", "email_html", "companies", "approval_notes"],
    },
    "strict": True,
}


@dataclass(frozen=True)
class Config:
    companies: list[str]
    recipients: list[str]
    sender: str
    days_back: int
    max_results_per_company: int
    draft_only: bool
    email_provider: str
    require_human_approval: bool
    db_path: Path
    draft_dir: Path
    schedule_hour: int
    schedule_minute: int
    timezone: str


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Copy config.example.json to config.json and edit companies/recipients."
        )

    raw = json.loads(path.read_text(encoding="utf-8"))
    return Config(
        companies=raw.get("companies", []),
        recipients=raw.get("recipients", []),
        sender=raw.get("sender", "datacenter-alerts@company.com"),
        days_back=int(raw.get("days_back", 7)),
        max_results_per_company=int(raw.get("max_results_per_company", 5)),
        draft_only=bool(raw.get("draft_only", True)),
        email_provider=raw.get("email_provider", "draft").lower(),
        require_human_approval=bool(raw.get("require_human_approval", True)),
        db_path=Path(raw.get("db_path", DEFAULT_DB_PATH)),
        draft_dir=Path(raw.get("draft_dir", DEFAULT_DRAFT_DIR)),
        schedule_hour=int(raw.get("schedule_hour", 8)),
        schedule_minute=int(raw.get("schedule_minute", 0)),
        timezone=raw.get("timezone", "America/New_York"),
    )


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                published_date TEXT,
                raw_score REAL,
                first_seen_utc TEXT NOT NULL,
                UNIQUE(company, url)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                recipients_json TEXT NOT NULL,
                draft_path TEXT,
                sent_utc TEXT,
                mode TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                email_text TEXT NOT NULL,
                email_html TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                source_count INTEGER NOT NULL,
                draft_path TEXT,
                status TEXT NOT NULL DEFAULT 'ready',
                created_utc TEXT NOT NULL,
                sent_utc TEXT,
                sent_recipients_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recipients (
                email TEXT PRIMARY KEY,
                created_utc TEXT NOT NULL
            )
            """
        )


def seed_recipients(config: Config) -> None:
    now = dt.datetime.now(dt.UTC).isoformat()
    with sqlite3.connect(config.db_path) as conn:
        for recipient in config.recipients:
            conn.execute(
                "INSERT OR IGNORE INTO recipients (email, created_utc) VALUES (?, ?)",
                (recipient.lower().strip(), now),
            )


def get_recipients(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT email FROM recipients ORDER BY email").fetchall()
    return [row[0] for row in rows]


def add_recipient(db_path: Path, email: str) -> None:
    email = email.lower().strip()
    if "@" not in email or "." not in email:
        raise ValueError("Enter a valid email address.")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO recipients (email, created_utc) VALUES (?, ?)",
            (email, dt.datetime.now(dt.UTC).isoformat()),
        )


def remove_recipient(db_path: Path, email: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM recipients WHERE email = ?", (email.lower().strip(),))


def tavily_search(company: str, config: Config) -> list[dict[str, Any]]:
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is required for web search.")

    query = (
        f'"{company}" data center OR datacenter OR "AI infrastructure" OR colocation '
        f'OR "power" OR utility OR campus recent'
    )
    response = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "search_depth": "advanced",
            "include_answer": False,
            "include_raw_content": False,
            "max_results": config.max_results_per_company,
            "days": config.days_back,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def firecrawl_scrape(url: str) -> str:
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return ""

    response = requests.post(
        "https://api.firecrawl.dev/v1/scrape",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
        timeout=45,
    )
    response.raise_for_status()
    data = response.json().get("data", {})
    return data.get("markdown") or data.get("content") or ""


def collect_sources(config: Config) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    now = dt.datetime.now(dt.UTC).isoformat()

    with sqlite3.connect(config.db_path) as conn:
        for company in config.companies:
            for result in tavily_search(company, config):
                url = result.get("url", "")
                if not url:
                    continue

                content = result.get("content") or ""
                try:
                    scraped = firecrawl_scrape(url)
                    if scraped:
                        content = scraped[:12000]
                except requests.RequestException:
                    pass

                source = {
                    "company": company,
                    "title": result.get("title", "Untitled source"),
                    "url": url,
                    "published_date": result.get("published_date") or result.get("date") or "",
                    "score": result.get("score"),
                    "content": content[:12000],
                }
                sources.append(source)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO source_results
                    (company, title, url, published_date, raw_score, first_seen_utc)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source["company"],
                        source["title"],
                        source["url"],
                        source["published_date"],
                        source["score"],
                        now,
                    ),
                )

    return sources


def build_summary(config: Config, sources: list[dict[str, Any]]) -> dict[str, Any]:
    if not sources:
        return {
            "email_subject": "Data center intelligence: no public updates found",
            "email_text": "No relevant public data-center updates were found for the selected companies.",
            "email_html": "<p>No relevant public data-center updates were found for the selected companies.</p>",
            "companies": [],
            "approval_notes": "No sources were available to summarize.",
        }

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
    today = dt.date.today().isoformat()
    source_payload = json.dumps(sources, ensure_ascii=False)

    completion = client.chat.completions.create(
        model=model,
        temperature=0.2,
        response_format={"type": "json_schema", "json_schema": SUMMARY_SCHEMA},
        messages=[
            {
                "role": "system",
                "content": (
                    "You write concise data center intelligence emails from public web sources only. "
                    "Do not infer private company information. Include a source URL for every major claim. "
                    "Exclude items that are not related to data centers, AI infrastructure, power, sites, "
                    "leadership, earnings, partnerships, or material business risks."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Today is {today}. Build a morning intelligence email for these companies: "
                    f"{', '.join(config.companies)}.\n\n"
                    f"Focus areas: {', '.join(DATA_CENTER_TOPICS)}.\n\n"
                    "Use only the source payload below. If a claim lacks a URL, omit it. "
                    "Keep the email scannable for sales and market intelligence readers. "
                    "Mention when no relevant public update was found for a company.\n\n"
                    f"SOURCES_JSON:\n{source_payload}"
                ),
            },
        ],
    )

    content = completion.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned an empty summary.")
    return json.loads(content)


def write_draft(config: Config, summary: dict[str, Any]) -> Path:
    config.draft_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.draft_dir / f"data-center-intel-{stamp}.eml"

    path.write_text(build_email_message(config, summary).as_string(), encoding="utf-8")
    return path


def build_email_message(config: Config, summary: dict[str, Any]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = config.sender
    msg["To"] = ", ".join(config.recipients)
    msg["Subject"] = summary["email_subject"]
    msg.set_content(summary["email_text"])
    msg.add_alternative(summary["email_html"], subtype="html")
    return msg


def send_with_graph(
    config: Config, summary: dict[str, Any], recipients: list[str] | None = None
) -> None:
    tenant_id = os.environ["MS_GRAPH_TENANT_ID"]
    client_id = os.environ["MS_GRAPH_CLIENT_ID"]
    client_secret = os.environ["MS_GRAPH_CLIENT_SECRET"]
    recipients = recipients or config.recipients

    token_response = requests.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    token_response.raise_for_status()
    access_token = token_response.json()["access_token"]

    payload = {
        "message": {
            "subject": summary["email_subject"],
            "body": {"contentType": "HTML", "content": summary["email_html"]},
            "toRecipients": [
                {"emailAddress": {"address": recipient}} for recipient in recipients
            ],
        },
        "saveToSentItems": True,
    }
    send_response = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{config.sender}/sendMail",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    send_response.raise_for_status()


def record_report(config: Config, summary: dict[str, Any], draft_path: Path, mode: str) -> None:
    with sqlite3.connect(config.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sent_reports (subject, recipients_json, draft_path, sent_utc, mode)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                summary["email_subject"],
                json.dumps(config.recipients),
                str(draft_path),
                dt.datetime.now(dt.UTC).isoformat() if mode == "sent" else None,
                mode,
            ),
        )


def store_report(
    config: Config, summary: dict[str, Any], draft_path: Path, source_count: int
) -> int:
    with sqlite3.connect(config.db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO reports
            (subject, email_text, email_html, summary_json, source_count, draft_path, status, created_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                summary["email_subject"],
                summary["email_text"],
                summary["email_html"],
                json.dumps(summary),
                source_count,
                str(draft_path),
                "ready",
                dt.datetime.now(dt.UTC).isoformat(),
            ),
        )
        return int(cursor.lastrowid)


def get_reports(db_path: Path, limit: int = 25) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT id, subject, source_count, status, created_utc, sent_utc, sent_recipients_json
            FROM reports
            ORDER BY created_utc DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def get_report(db_path: Path, report_id: int) -> sqlite3.Row:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if row is None:
        raise ValueError(f"Report {report_id} was not found.")
    return row


def generate_report(config: Config) -> int:
    init_db(config.db_path)
    seed_recipients(config)
    sources = collect_sources(config)
    summary = build_summary(config, sources)
    draft_path = write_draft(config, summary)
    report_id = store_report(config, summary, draft_path, len(sources))
    record_report(config, summary, draft_path, "draft")
    return report_id


def send_report(config: Config, report_id: int) -> list[str]:
    init_db(config.db_path)
    recipients = get_recipients(config.db_path)
    if not recipients:
        raise ValueError("Add at least one recipient before sending.")

    row = get_report(config.db_path, report_id)
    if row["sent_utc"]:
        raise ValueError("This report has already been sent.")

    summary = json.loads(row["summary_json"])
    send_with_graph(config, summary, recipients=recipients)
    sent_utc = dt.datetime.now(dt.UTC).isoformat()
    with sqlite3.connect(config.db_path) as conn:
        conn.execute(
            """
            UPDATE reports
            SET status = 'sent', sent_utc = ?, sent_recipients_json = ?
            WHERE id = ?
            """,
            (sent_utc, json.dumps(recipients), report_id),
        )
        conn.execute(
            """
            INSERT INTO sent_reports (subject, recipients_json, draft_path, sent_utc, mode)
            VALUES (?, ?, ?, ?, ?)
            """,
            (row["subject"], json.dumps(recipients), row["draft_path"], sent_utc, "sent"),
        )
    return recipients


_generation_lock = threading.Lock()


def generate_report_once(config: Config) -> int:
    if not _generation_lock.acquire(blocking=False):
        raise RuntimeError("A report generation job is already running.")
    try:
        return generate_report(config)
    finally:
        _generation_lock.release()


def run(config_path: Path) -> int:
    load_dotenv()
    config = load_config(config_path)

    if not config.companies:
        raise ValueError("config.json must contain at least one company.")
    if not config.recipients:
        raise ValueError("config.json must contain at least one recipient.")

    report_id = generate_report_once(config)
    print(f"Report drafted in portal with id: {report_id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily data center intelligence email agent.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)),
        help="Path to config.json.",
    )
    args = parser.parse_args()
    return run(args.config)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
