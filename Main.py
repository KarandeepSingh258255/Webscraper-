from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import smtplib
import sqlite3
import sys
import threading
from dataclasses import dataclass, replace
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


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
    sender_name: str
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
            f"Missing {path}. Copy config.example.json to config.json and edit the company list."
        )

    raw = json.loads(path.read_text(encoding="utf-8"))
    email_provider = normalize_email_provider(raw.get("email_provider", "smtp"))

    return Config(
        companies=raw.get("companies", []),
        recipients=raw.get("recipients", []),
        sender=raw.get("sender", os.environ.get("SMTP_USERNAME", "sandbox@example.com")),
        sender_name=raw.get("sender_name", "Data Center Intelligence"),
        days_back=int(raw.get("days_back", 7)),
        max_results_per_company=int(raw.get("max_results_per_company", 5)),
        draft_only=bool(raw.get("draft_only", False)),
        email_provider=email_provider,
        require_human_approval=bool(raw.get("require_human_approval", True)),
        db_path=Path(os.environ.get("PORTAL_DB_PATH", raw.get("db_path", DEFAULT_DB_PATH))),
        draft_dir=Path(os.environ.get("PORTAL_DRAFT_DIR", raw.get("draft_dir", DEFAULT_DRAFT_DIR))),
        schedule_hour=int(raw.get("schedule_hour", 8)),
        schedule_minute=int(raw.get("schedule_minute", 0)),
        timezone=raw.get("timezone", "America/New_York"),
    )


def normalize_email_provider(value: Any) -> str:
    provider = str(value or "smtp").strip().lower()
    aliases = {
        "graph": "smtp",
        "microsoft_graph": "smtp",
        "ms_graph": "smtp",
        "outlook": "smtp",
        "mailersend": "smtp",
        "gmail": "smtp",
        "gmail smtp": "smtp",
        "gmail_smtp": "smtp",
        "mailtrap": "smtp",
        "mailtrap_sandbox": "smtp",
    }
    return aliases.get(provider, provider)


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
        _ensure_column(conn, "reports", "target_recipients_json", "TEXT")
        _ensure_column(conn, "reports", "scheduled_send_utc", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recipients (
                email TEXT PRIMARY KEY,
                created_utc TEXT NOT NULL
            )
            """
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


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


def parse_recipient_list(raw: str | None) -> list[str]:
    if not raw:
        return []

    recipients = [piece.strip().lower() for piece in re.split(r"[\s,;]+", raw) if piece.strip()]
    validated: list[str] = []
    for email in recipients:
        if "@" not in email or "." not in email:
            raise ValueError(f"Enter a valid email address: {email}")
        validated.append(email)
    return list(dict.fromkeys(validated))


def tavily_search(company: str, config: Config, days_back: int | None = None) -> list[dict[str, Any]]:
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is required for web search.")

    search_days = config.days_back if days_back is None else days_back
    query = (
        f'"{company}" '
        f'("data center" OR datacenter OR "AI infrastructure" OR colocation OR site OR campus '
        f'OR lease OR power OR substation OR "data center expansion" OR capacity OR utility) recent'
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
            "days": search_days,
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


def collect_sources(
    config: Config,
    companies: list[str] | None = None,
    days_back: int | None = None,
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    now = dt.datetime.now(dt.UTC).isoformat()
    target_companies = companies or config.companies

    with sqlite3.connect(config.db_path) as conn:
        for company in target_companies:
            for result in tavily_search(company, config, days_back=days_back):
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


def parse_source_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None

    raw = value.strip()
    if not raw:
        return None

    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            parsed = dt.datetime.strptime(raw, fmt)
            return parsed.replace(tzinfo=dt.UTC)
        except ValueError:
            continue

    try:
        parsed = parsedate_to_datetime(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except (TypeError, ValueError):
        return None


def filter_sources_by_time_window(
    sources: list[dict[str, Any]],
    days_back: int,
) -> list[dict[str, Any]]:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days_back)
    filtered: list[dict[str, Any]] = []
    for source in sources:
        published_at = parse_source_datetime(source.get("published_date"))
        if published_at is not None and published_at >= cutoff:
            filtered.append(source)
            continue
        if published_at is None and is_probably_recent_undated_source(source, days_back):
            filtered.append(source)
    return filtered


def count_recent_undated_sources(
    sources: list[dict[str, Any]],
    days_back: int,
) -> int:
    return sum(
        1
        for source in sources
        if parse_source_datetime(source.get("published_date")) is None
        and is_probably_recent_undated_source(source, days_back)
    )


def is_probably_recent_undated_source(source: dict[str, Any], days_back: int) -> bool:
    if days_back > 3:
        return False

    combined = " ".join(
        str(part or "")
        for part in (source.get("title"), source.get("content"), source.get("url"))
    ).lower()
    if not combined:
        return False

    current_year = str(dt.datetime.now().year)
    current_month = dt.datetime.now().strftime("%B").lower()
    short_month = dt.datetime.now().strftime("%b").lower()
    recent_markers = (
        "today",
        "yesterday",
        "this week",
        "hours ago",
        "hour ago",
        "minutes ago",
        "minute ago",
        "breaking",
        current_year,
        current_month,
        short_month,
    )
    infrastructure_markers = (
        "data center",
        "datacenter",
        "ai infrastructure",
        "campus",
        "lease",
        "power",
        "substation",
        "expansion",
        "capacity",
    )
    return any(marker in combined for marker in recent_markers) and any(
        marker in combined for marker in infrastructure_markers
    )


def build_summary(
    config: Config,
    sources: list[dict[str, Any]],
    companies: list[str] | None = None,
    time_window_label: str | None = None,
) -> dict[str, Any]:
    company_list = companies or config.companies
    window_text = time_window_label or f"the last {config.days_back} day(s)"
    if not sources:
        summary = {
            "email_subject": "Data center intelligence: no public updates found",
            "email_text": "No relevant public data-center updates were found for the selected companies.",
            "email_html": "<p>No relevant public data-center updates were found for the selected companies.</p>",
            "companies": [
                {
                    "company": company,
                    "summary": "No relevant public update was found.",
                    "relevance": "none",
                    "items": [],
                }
                for company in company_list
            ],
            "approval_notes": "No sources were available to summarize.",
        }
        return finalize_summary_payload(summary, company_list=company_list, window_text=window_text)

    prompt_context = build_summary_prompt_context(
        company_list=company_list,
        window_text=window_text,
        sources=sources,
    )
    summary = build_summary_with_gemini(prompt_context)
    return finalize_summary_payload(summary, company_list=company_list, window_text=window_text)


def append_approval_note(summary: dict[str, Any], note: str) -> dict[str, Any]:
    existing = str(summary.get("approval_notes", "") or "").strip()
    summary["approval_notes"] = f"{existing}\n\n{note}".strip() if existing else note
    return summary


def build_summary_prompt_context(
    company_list: list[str],
    window_text: str,
    sources: list[dict[str, Any]],
) -> dict[str, str]:
    today = dt.date.today().isoformat()
    source_payload = json.dumps(sources, ensure_ascii=False)
    return {
        "today": today,
        "company_list": ", ".join(company_list),
        "window_text": window_text,
        "source_payload": source_payload,
    }


def compact_sources_for_summary(
    sources: list[dict[str, Any]],
    limit: int = 8,
) -> list[dict[str, Any]]:
    ranked = sorted(
        sources,
        key=lambda source: (
            float(source.get("score") or 0),
            len(str(source.get("content") or "")),
        ),
        reverse=True,
    )
    compacted: list[dict[str, Any]] = []
    for source in ranked[:limit]:
        compacted.append(
            {
                "company": source.get("company", ""),
                "title": str(source.get("title", ""))[:180],
                "url": source.get("url", ""),
                "published_date": source.get("published_date", ""),
                "score": source.get("score"),
                "content": str(source.get("content", ""))[:1200],
            }
        )
    return compacted


def build_summary_prompt(context: dict[str, str]) -> tuple[str, str]:
    system_prompt = (
        "You write concise data center intelligence emails from public web sources only. "
        "Do not infer private company information. Include a source URL for every major claim. "
        "Exclude items that are not related to data centers, AI infrastructure, power, sites, "
        "leadership, earnings, partnerships, or material business risks. "
        "Return a single JSON object only with these exact top-level keys: "
        "email_subject, email_text, email_html, companies, approval_notes."
    )
    user_prompt = (
        f"Today is {context['today']}. Build a concise intelligence email for these companies: "
        f"{context['company_list']}.\n"
        f"Focus on public information from {context['window_text']}.\n\n"
        f"Focus areas: {', '.join(DATA_CENTER_TOPICS)}.\n\n"
        "Use only the source payload below. If a claim lacks a URL, omit it. "
        "Keep the email scannable for sales and market intelligence readers. "
        "Mention when no relevant public update was found for a company.\n"
        "The response must be valid JSON only, with no markdown or commentary.\n\n"
        f"SOURCES_JSON:\n{context['source_payload']}"
    )
    return system_prompt, user_prompt


def build_summary_with_gemini(context: dict[str, str]) -> dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")

    model = normalize_gemini_model_name(os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
    system_prompt, user_prompt = build_summary_prompt(context)
    text = request_gemini_summary_text(api_key, model, system_prompt, user_prompt)
    try:
        return validate_summary_payload(json.loads(extract_json_text(text)))
    except (json.JSONDecodeError, RuntimeError):
        compact_context = dict(context)
        compact_context["source_payload"] = json.dumps(
            compact_sources_for_summary(json.loads(context["source_payload"]), limit=8),
            ensure_ascii=False,
        )
        compact_system_prompt, compact_user_prompt = build_summary_prompt(compact_context)
        retry_text = request_gemini_summary_text(
            api_key,
            model,
            compact_system_prompt,
            (
                f"{compact_user_prompt}\n\n"
                "Be brief. Use at most 3 items per company. Keep every string short and plain. "
                "Return valid JSON only. No markdown fences, no commentary, no trailing commas. "
                "Use the exact top-level keys email_subject, email_text, email_html, companies, approval_notes."
            ),
            temperature=0.0,
        )
        try:
            return validate_summary_payload(json.loads(extract_json_text(retry_text)))
        except (json.JSONDecodeError, RuntimeError):
            return build_fallback_summary_from_sources(
                json.loads(context["source_payload"]),
                company_list=[part.strip() for part in context["company_list"].split(",") if part.strip()],
            )


def build_fallback_summary_from_sources(
    sources: list[dict[str, Any]],
    company_list: list[str],
) -> dict[str, Any]:
    companies: list[dict[str, Any]] = []
    for company in company_list:
        company_sources = [source for source in sources if source.get("company") == company][:5]
        items: list[dict[str, Any]] = []
        for source in company_sources:
            title = str(source.get("title") or "Untitled source").strip()
            items.append(
                {
                    "claim": title,
                    "why_it_matters": "Recent public coverage may affect data center positioning, demand, or infrastructure planning.",
                    "category": "other",
                    "source_title": title,
                    "source_url": source.get("url", ""),
                    "source_date": source.get("published_date", "") or "Unknown date",
                }
            )
        companies.append(
            {
                "company": company,
                "summary": "Recent public sources were found and listed below.",
                "relevance": "medium" if items else "none",
                "items": items,
            }
        )

    return {
        "email_subject": "",
        "email_text": "",
        "email_html": "",
        "companies": companies,
        "approval_notes": "",
        "fallback_summary_used": True,
    }


def request_gemini_summary_text(
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
) -> str:
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        headers={
            "content-type": "application/json",
        },
        json={
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 2500,
                "responseMimeType": "application/json",
            },
        },
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(
            f"Gemini request failed with {response.status_code}: {response.text}"
        )
    data = response.json()
    text = extract_gemini_output_text(data).strip()
    if not text:
        raise RuntimeError("Gemini returned an empty summary.")
    return text


def extract_json_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)

    if not stripped.startswith("{"):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            stripped = stripped[start : end + 1]
    return stripped


def extract_gemini_output_text(payload: dict[str, Any]) -> str:
    candidates: list[str] = []

    for candidate in payload.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content", {})
        if isinstance(content, dict):
            for part in content.get("parts", []):
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    candidates.append(part["text"])

    if candidates:
        return "".join(candidates)

    return str(payload.get("text", "") or payload.get("output_text", ""))


def normalize_gemini_model_name(model: str) -> str:
    cleaned = model.strip()
    if cleaned.startswith("models/"):
        return cleaned.removeprefix("models/")
    return cleaned


def validate_summary_payload(summary: dict[str, Any]) -> dict[str, Any]:
    summary = normalize_summary_payload(summary)
    required = ["email_subject", "email_text", "email_html", "companies", "approval_notes"]
    missing = [key for key in required if key not in summary]
    if missing:
        raise RuntimeError(f"Model returned an invalid summary missing: {', '.join(missing)}")
    companies = summary.get("companies")
    if not isinstance(companies, list):
        raise RuntimeError("Model returned an invalid summary: companies must be a list.")
    for company in companies:
        if not isinstance(company, dict):
            raise RuntimeError("Model returned an invalid summary: companies entries must be objects.")
        for key in ("company", "summary", "relevance", "items"):
            if key not in company:
                raise RuntimeError(f"Model returned an invalid summary: company entry missing {key}.")
    return summary


def finalize_summary_payload(
    summary: dict[str, Any],
    company_list: list[str],
    window_text: str,
) -> dict[str, Any]:
    summary["email_subject"] = build_report_subject(company_list)
    summary["email_text"] = render_professional_email_text(summary, company_list, window_text)
    summary["email_html"] = render_professional_email_html(summary, company_list, window_text)
    return summary


def build_report_subject(company_list: list[str]) -> str:
    report_date = dt.datetime.now().strftime("%B %d, %Y")
    if len(company_list) == 1:
        return f"Data Center Report | {company_list[0]} | {report_date}"
    return f"Data Center Report | {', '.join(company_list)} | {report_date}"


def render_professional_email_text(
    summary: dict[str, Any],
    company_list: list[str],
    window_text: str,
) -> str:
    lines = [
        "Hello,",
        "",
        f'Please find below the {window_text} data center report for {", ".join(company_list)}.',
        "",
    ]

    companies = coerce_company_entries(summary.get("companies", []))
    if companies:
        for company_entry in companies:
            company_name = company_entry.get("company", "Company")
            lines.append(company_name)
            lines.append("-" * len(company_name))
            company_summary = company_entry.get("summary", "No summary provided.")
            lines.append(company_summary)
            items = coerce_item_entries(company_entry.get("items", []))
            if items:
                lines.append("")
                for item in items:
                    lines.append(f"- {item.get('claim', 'No claim provided.')}")
                    lines.append(f"  Why it matters: {item.get('why_it_matters', 'No context provided.')}")
                    lines.append(
                        f"  Source: {item.get('source_title', 'Unknown source')} | "
                        f"{item.get('source_date', 'Unknown date')} | {item.get('source_url', '')}"
                    )
            lines.append("")
    else:
        lines.append("No relevant public data-center updates were found.")
        lines.append("")

    approval_notes = summary.get("approval_notes", "").strip()
    if approval_notes:
        lines.append("Notes")
        lines.append("-----")
        lines.append(approval_notes)
        lines.append("")

    lines.extend(["Regards,", summary.get("sender_name", "Data Center Intelligence")])
    return "\n".join(lines).strip()


def render_professional_email_html(
    summary: dict[str, Any],
    company_list: list[str],
    window_text: str,
) -> str:
    intro = html.escape(
        f'Please find below the {window_text} data center report for {", ".join(company_list)}.'
    )
    sections: list[str] = []
    for company_entry in coerce_company_entries(summary.get("companies", [])):
        company_name = html.escape(company_entry.get("company", "Company"))
        company_summary = html.escape(company_entry.get("summary", "No summary provided."))
        items_html: list[str] = []
        for item in coerce_item_entries(company_entry.get("items", [])):
            claim = html.escape(item.get("claim", "No claim provided."))
            why = html.escape(item.get("why_it_matters", "No context provided."))
            source_title = html.escape(item.get("source_title", "Unknown source"))
            source_date = html.escape(item.get("source_date", "Unknown date"))
            source_url = html.escape(item.get("source_url", ""))
            items_html.append(
                "<li style=\"margin-bottom:12px;\">"
                f"<div style=\"font-weight:600; color:#17202a;\">{claim}</div>"
                f"<div style=\"margin-top:4px; color:#334155;\">{why}</div>"
                f"<div style=\"margin-top:6px; font-size:13px; color:#52616f;\">"
                f"<a href=\"{source_url}\" style=\"color:#0b5cab; text-decoration:none;\">{source_title}</a>"
                f" | {source_date}</div>"
                "</li>"
            )

        sections.append(
            "<section style=\"margin-top:24px;\">"
            f"<h2 style=\"margin:0 0 8px; font-size:20px; color:#102033;\">{company_name}</h2>"
            f"<p style=\"margin:0 0 12px; color:#334155; line-height:1.6;\">{company_summary}</p>"
            f"{('<ul style=\"padding-left:20px; margin:0;\">' + ''.join(items_html) + '</ul>') if items_html else '<p style=\"margin:0; color:#52616f;\">No relevant public updates were found.</p>'}"
            "</section>"
        )

    notes_html = ""
    approval_notes = summary.get("approval_notes", "").strip()
    if approval_notes:
        notes_html = (
            "<section style=\"margin-top:24px;\">"
            "<h2 style=\"margin:0 0 8px; font-size:18px; color:#102033;\">Notes</h2>"
            f"<p style=\"margin:0; color:#52616f; line-height:1.6;\">{html.escape(approval_notes)}</p>"
            "</section>"
        )

    if not sections:
        sections.append(
            "<section style=\"margin-top:24px;\">"
            "<p style=\"margin:0; color:#52616f;\">No relevant public data-center updates were found.</p>"
            "</section>"
        )

    return (
        "<!doctype html><html><body style=\"margin:0; padding:24px; background:#f6f7f9; color:#17202a;\">"
        "<div style=\"max-width:720px; margin:0 auto; background:#ffffff; border:1px solid #d9dee7; border-radius:12px; overflow:hidden;\">"
        "<div style=\"padding:24px 28px; background:#102033; color:#ffffff;\">"
        "<div style=\"font-size:12px; letter-spacing:0.08em; text-transform:uppercase; opacity:0.82;\">Data Center Intelligence</div>"
        f"<h1 style=\"margin:8px 0 0; font-size:28px; font-weight:700;\">{html.escape(summary['email_subject'])}</h1>"
        "</div>"
        "<div style=\"padding:28px; font-family:Arial, sans-serif;\">"
        "<p style=\"margin:0 0 16px; color:#334155; line-height:1.7;\">Hello,</p>"
        f"<p style=\"margin:0 0 18px; color:#334155; line-height:1.7;\">{intro}</p>"
        f"{''.join(sections)}"
        f"{notes_html}"
        "<p style=\"margin:28px 0 0; color:#334155; line-height:1.7;\">Regards,<br>Data Center Intelligence</p>"
        "</div></div></body></html>"
    )


def normalize_summary_payload(summary: dict[str, Any]) -> dict[str, Any]:
    normalized = summary

    if len(normalized) == 1:
        only_value = next(iter(normalized.values()))
        if isinstance(only_value, dict):
            normalized = only_value

    key_aliases = {
        "email_subject": ["email_subject", "subject", "emailSubject"],
        "email_text": ["email_text", "text", "emailText", "body_text", "bodyText"],
        "email_html": ["email_html", "html", "emailHtml", "body_html", "bodyHtml"],
        "companies": ["companies", "company_summaries", "companySummaries"],
        "approval_notes": ["approval_notes", "approvalNotes", "notes"],
    }

    remapped: dict[str, Any] = {}
    for target_key, aliases in key_aliases.items():
        for alias in aliases:
            if alias in normalized:
                remapped[target_key] = normalized[alias]
                break

    for key, value in normalized.items():
        if key not in remapped:
            remapped[key] = value

    return remapped


def coerce_company_entries(raw_companies: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_companies, list):
        return []

    companies: list[dict[str, Any]] = []
    for entry in raw_companies:
        if isinstance(entry, dict):
            company_name = entry.get("company") or entry.get("company_name") or entry.get("name") or "Company"
            items = entry.get("items")
            if items is None:
                items = entry.get("updates")
            companies.append(
                {
                    **entry,
                    "company": company_name,
                    "summary": entry.get("overview") or entry.get("summary") or "No relevant public updates were found.",
                    "relevance": entry.get("relevance") or "none",
                    "items": items if isinstance(items, list) else [],
                }
            )
        elif isinstance(entry, str):
            companies.append(
                {
                    "company": entry,
                    "summary": "No structured summary was returned.",
                    "relevance": "none",
                    "items": [],
                }
            )
    return companies


def coerce_item_entries(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []

    items: list[dict[str, Any]] = []
    for entry in raw_items:
        if isinstance(entry, dict):
            items.append(entry)
        elif isinstance(entry, str):
            items.append(
                {
                    "claim": entry,
                    "why_it_matters": "No additional context was returned.",
                    "source_title": "Unknown source",
                    "source_date": "Unknown date",
                    "source_url": "",
                }
            )
    return items


def write_draft(
    config: Config,
    summary: dict[str, Any],
    recipients: list[str] | None = None,
) -> Path:
    config.draft_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.draft_dir / f"data-center-intel-{stamp}.eml"

    path.write_text(
        build_email_message(config, summary, recipients=recipients).as_string(),
        encoding="utf-8",
    )
    return path


def build_email_message(
    config: Config,
    summary: dict[str, Any],
    recipients: list[str] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{config.sender_name} <{config.sender}>"
    msg["To"] = ", ".join(recipients or config.recipients)
    msg["Subject"] = summary["email_subject"]
    msg.set_content(summary["email_text"])
    msg.add_alternative(summary["email_html"], subtype="html")
    return msg


def validate_send_addresses(sender: str, recipients: list[str]) -> None:
    blocked_domains = {"company.com", "example.com"}
    sender_domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
    recipient_domains = {
        recipient.rsplit("@", 1)[-1].lower()
        for recipient in recipients
        if "@" in recipient
    }

    if sender_domain in blocked_domains:
        raise ValueError(
            f"Invalid sender address '{sender}'. Replace the placeholder sender in config.json with a real sender address."
        )
    if recipient_domains & blocked_domains:
        raise ValueError(
            "Invalid recipient address in config.json or the portal form. Replace example.com placeholder recipients with real email addresses."
        )


def send_with_smtp(
    config: Config, summary: dict[str, Any], recipients: list[str] | None = None
) -> None:
    recipients = recipients or config.recipients
    validate_send_addresses(config.sender, recipients)
    smtp_username = os.environ.get("SMTP_USERNAME", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "").strip()
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    if not smtp_username:
        raise ValueError("Missing SMTP_USERNAME in .env.")
    if not smtp_password:
        raise ValueError("Missing SMTP_PASSWORD in .env.")
    if not smtp_host:
        raise ValueError("Missing SMTP_HOST in .env.")

    message = build_email_message(config, summary, recipients=recipients)
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
    except smtplib.SMTPException as exc:
        raise RuntimeError(f"SMTP send failed: {exc}") from exc


def record_report(
    config: Config,
    summary: dict[str, Any],
    draft_path: Path,
    mode: str,
    recipients: list[str],
) -> None:
    with sqlite3.connect(config.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sent_reports (subject, recipients_json, draft_path, sent_utc, mode)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                summary["email_subject"],
                json.dumps(recipients),
                str(draft_path),
                dt.datetime.now(dt.UTC).isoformat() if mode == "sent" else None,
                mode,
            ),
        )


def store_report(
    config: Config,
    summary: dict[str, Any],
    draft_path: Path,
    source_count: int,
    recipients: list[str],
) -> int:
    with sqlite3.connect(config.db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO reports
            (subject, email_text, email_html, summary_json, source_count, draft_path, status, created_utc, target_recipients_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(recipients),
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


def generate_report(
    config: Config,
    companies: list[str] | None = None,
    recipients: list[str] | None = None,
    days_back: int | None = None,
    time_window_label: str | None = None,
) -> int:
    init_db(config.db_path)
    seed_recipients(config)
    target_companies = companies or config.companies
    target_recipients = recipients or config.recipients
    target_days_back = config.days_back if days_back is None else days_back
    effective_days_back = target_days_back
    effective_window_label = time_window_label or f"the last {target_days_back} day(s)"
    fallback_note = ""
    diagnostics: dict[str, Any] = {
        "requested_days_back": target_days_back,
        "raw_results_count": 0,
        "qualified_results_count": 0,
        "recent_undated_included_count": 0,
        "fallback_summary_used": False,
        "fallback_used": False,
        "fallback_days_back": None,
        "fallback_raw_results_count": 0,
        "fallback_qualified_results_count": 0,
        "fallback_recent_undated_included_count": 0,
    }
    scoped_config = replace(
        config,
        companies=target_companies,
        recipients=target_recipients,
        days_back=target_days_back,
    )
    sources = collect_sources(scoped_config, companies=target_companies, days_back=target_days_back)
    diagnostics["raw_results_count"] = len(sources)
    diagnostics["recent_undated_included_count"] = count_recent_undated_sources(sources, target_days_back)
    sources = filter_sources_by_time_window(sources, target_days_back)
    diagnostics["qualified_results_count"] = len(sources)
    if not sources and target_days_back == 1:
        fallback_days_back = 3
        fallback_sources = collect_sources(
            scoped_config,
            companies=target_companies,
            days_back=fallback_days_back,
        )
        diagnostics["fallback_raw_results_count"] = len(fallback_sources)
        diagnostics["fallback_recent_undated_included_count"] = count_recent_undated_sources(
            fallback_sources,
            fallback_days_back,
        )
        fallback_sources = filter_sources_by_time_window(fallback_sources, fallback_days_back)
        diagnostics["fallback_qualified_results_count"] = len(fallback_sources)
        if fallback_sources:
            sources = fallback_sources
            effective_days_back = fallback_days_back
            diagnostics["fallback_used"] = True
            diagnostics["fallback_days_back"] = fallback_days_back
            effective_window_label = (
                "the last 3 days "
                "(fallback after no qualifying updates were found in the last 24 hours)"
            )
            fallback_note = (
                "No qualifying updates were found in the last 24 hours. "
                "This report automatically expanded the search window to the last 3 days."
            )
    scoped_config = replace(scoped_config, days_back=effective_days_back)
    summary = build_summary(
        scoped_config,
        sources,
        companies=target_companies,
        time_window_label=effective_window_label,
    )
    diagnostics["fallback_summary_used"] = bool(summary.get("fallback_summary_used"))
    summary["diagnostics"] = diagnostics
    if fallback_note:
        summary = append_approval_note(summary, fallback_note)
    draft_path = write_draft(scoped_config, summary, recipients=target_recipients)
    report_id = store_report(
        config,
        summary,
        draft_path,
        len(sources),
        recipients=target_recipients,
    )
    record_report(config, summary, draft_path, "draft", recipients=target_recipients)
    return report_id


def send_report(
    config: Config, report_id: int, recipients: list[str] | None = None
) -> list[str]:
    init_db(config.db_path)
    row = get_report(config.db_path, report_id)
    if row["sent_utc"]:
        raise ValueError("This report has already been sent.")

    summary = json.loads(row["summary_json"])
    target_recipients = recipients
    if target_recipients is None:
        stored_recipients = row["target_recipients_json"] if "target_recipients_json" in row.keys() else None
        target_recipients = json.loads(stored_recipients) if stored_recipients else get_recipients(config.db_path)

    if not target_recipients:
        raise ValueError("Add at least one recipient before sending.")

    provider = normalize_email_provider(config.email_provider)
    if provider != "smtp":
        raise ValueError(f"Unsupported email_provider: {config.email_provider}")

    send_with_smtp(config, summary, recipients=target_recipients)
    sent_utc = dt.datetime.now(dt.UTC).isoformat()
    with sqlite3.connect(config.db_path) as conn:
        conn.execute(
            """
            UPDATE reports
            SET status = 'sent', sent_utc = ?, sent_recipients_json = ?, scheduled_send_utc = NULL
            WHERE id = ?
            """,
            (sent_utc, json.dumps(target_recipients), report_id),
        )
        conn.execute(
            """
            INSERT INTO sent_reports (subject, recipients_json, draft_path, sent_utc, mode)
            VALUES (?, ?, ?, ?, ?)
            """,
            (row["subject"], json.dumps(target_recipients), row["draft_path"], sent_utc, "sent"),
        )
    return target_recipients


_generation_lock = threading.Lock()


def generate_report_once(
    config: Config,
    companies: list[str] | None = None,
    recipients: list[str] | None = None,
    days_back: int | None = None,
    time_window_label: str | None = None,
) -> int:
    if not _generation_lock.acquire(blocking=False):
        raise RuntimeError("A report generation job is already running.")
    try:
        return generate_report(
            config,
            companies=companies,
            recipients=recipients,
            days_back=days_back,
            time_window_label=time_window_label,
        )
    finally:
        _generation_lock.release()


def run(config_path: Path) -> int:
    load_dotenv()
    config = load_config(config_path)

    if not config.companies:
        raise ValueError("config.json must contain at least one company.")

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
