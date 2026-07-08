from __future__ import annotations

import hmac
import json
import os
import datetime as dt
import sqlite3
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo

import jwt
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    g,
    make_response,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from Main import (
    add_recipient,
    generate_report,
    generate_report_once,
    get_recipients,
    get_report,
    get_reports,
    init_db,
    load_config,
    parse_recipient_list,
    remove_recipient,
    seed_recipients,
    send_report,
)


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
F = TypeVar("F", bound=Callable)
AUTH_COOKIE_NAME = "dc_portal_auth"


BASE_CSS = """
<style>
  :root { color-scheme: light; font-family: Arial, sans-serif; }
  body { margin: 0; background: #f6f7f9; color: #17202a; }
  header { background: #102033; color: white; padding: 18px 28px; display: flex; justify-content: space-between; align-items: center; }
  main { max-width: 1120px; margin: 0 auto; padding: 24px; }
  a { color: #0b5cab; }
  .panel { background: white; border: 1px solid #d9dee7; border-radius: 8px; padding: 18px; margin-bottom: 18px; }
  .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  .spacer { flex: 1; }
  input { padding: 10px 12px; border: 1px solid #b9c2cf; border-radius: 6px; min-width: 260px; }
  select, textarea { padding: 10px 12px; border: 1px solid #b9c2cf; border-radius: 6px; min-width: 260px; }
  textarea { min-height: 120px; width: 100%; }
  input[type="datetime-local"] { min-width: 220px; }
  button, .button { background: #0b5cab; border: 0; color: white; border-radius: 6px; padding: 10px 14px; text-decoration: none; cursor: pointer; }
  button.secondary, .button.secondary { background: #52616f; }
  .button.active { background: #123a61; }
  button.danger { background: #a42b2b; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; border-bottom: 1px solid #e3e7ee; padding: 10px 8px; vertical-align: top; }
  th { color: #52616f; font-size: 13px; }
  .message { background: #fff7d6; border: 1px solid #e0c45a; padding: 10px 12px; border-radius: 6px; margin-bottom: 12px; }
  .status { display: inline-block; border-radius: 999px; padding: 4px 8px; background: #e8eef7; font-size: 12px; }
  .status.sent { background: #dff3e6; }
  iframe { width: 100%; min-height: 620px; border: 1px solid #d9dee7; border-radius: 8px; background: white; }
  .muted { color: #667382; }
</style>
"""


LAYOUT = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }}</title>
  """ + BASE_CSS + """
</head>
<body>
  <header>
    <div><strong>Data Center Intelligence Portal</strong></div>
    {% if is_authenticated %}
      <nav class="row">
        <a class="button {% if current_page == 'reports' %}active{% else %}secondary{% endif %}" href="{{ url_for('index') }}">Reports</a>
        <a class="button {% if current_page == 'unit_test' %}active{% else %}secondary{% endif %}" href="{{ url_for('unit_test') }}">Unit Test</a>
        <a class="button {% if current_page == 'recipients' %}active{% else %}secondary{% endif %}" href="{{ url_for('recipients') }}">Recipients</a>
        <a class="button secondary" href="{{ url_for('logout') }}">Logout</a>
      </nav>
    {% endif %}
  </header>
  <main>
    {% for message in get_flashed_messages() %}
      <div class="message">{{ message }}</div>
    {% endfor %}
    {{ body|safe }}
  </main>
</body>
</html>
"""


def create_app() -> Flask:
    load_dotenv(override=True)
    config = load_config(CONFIG_PATH)
    init_db(config.db_path)
    seed_recipients(config)

    portal_username = os.environ.get("PORTAL_USERNAME")
    portal_password_hash = os.environ.get("PORTAL_PASSWORD_HASH")
    portal_secret_key = os.environ.get("PORTAL_SECRET_KEY")
    portal_jwt_secret = os.environ.get("PORTAL_JWT_SECRET")
    jwt_exp_minutes = int(os.environ.get("PORTAL_JWT_EXP_MINUTES", "480"))
    missing = [
        name
        for name, value in {
            "PORTAL_USERNAME": portal_username,
            "PORTAL_PASSWORD_HASH": portal_password_hash,
            "PORTAL_SECRET_KEY": portal_secret_key,
            "PORTAL_JWT_SECRET": portal_jwt_secret,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required portal environment variable(s): {', '.join(missing)}")

    app = Flask(__name__)
    app.secret_key = portal_secret_key
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("PORTAL_COOKIE_SECURE", "false").lower() == "true",
    )
    auth_cookie_secure = os.environ.get("PORTAL_COOKIE_SECURE", "false").lower() == "true"

    scheduler = BackgroundScheduler(timezone=config.timezone)
    scheduler.add_job(
        lambda: generate_report_once(config),
        trigger="cron",
        hour=config.schedule_hour,
        minute=config.schedule_minute,
        id="daily_data_center_report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    def schedule_report_send_job(report_id: int, scheduled_local: str) -> str:
        if not scheduled_local:
            raise ValueError("Choose a send time.")

        local_zone = ZoneInfo(config.timezone)
        scheduled_local_dt = dt.datetime.fromisoformat(scheduled_local)
        if scheduled_local_dt.tzinfo is None:
            scheduled_local_dt = scheduled_local_dt.replace(tzinfo=local_zone)
        scheduled_utc = scheduled_local_dt.astimezone(dt.UTC)
        if scheduled_utc <= dt.datetime.now(dt.UTC):
            raise ValueError("Scheduled send time must be in the future.")

        row = get_report(config.db_path, report_id)
        if row["sent_utc"]:
            raise ValueError("This report has already been sent.")

        job_id = f"send-report-{report_id}"
        scheduler.add_job(
            lambda rid=report_id: send_report(config, rid),
            trigger="date",
            run_date=scheduled_utc,
            id=job_id,
            replace_existing=True,
            misfire_grace_time=300,
        )
        with sqlite3.connect(config.db_path) as conn:
            conn.execute(
                """
                UPDATE reports
                SET status = 'scheduled', scheduled_send_utc = ?
                WHERE id = ?
                """,
                (scheduled_utc.isoformat(), report_id),
            )
        return scheduled_utc.isoformat()

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def render(title: str, body: str, **context):
        return render_template_string(
            LAYOUT,
            title=title,
            body=body,
            is_authenticated=bool(getattr(g, "current_user", None)),
            current_page=context.pop("current_page", ""),
            **context,
        )

    def create_auth_token(username: str) -> str:
        now = dt.datetime.now(dt.UTC)
        payload = {
            "sub": username,
            "iat": now,
            "exp": now + dt.timedelta(minutes=jwt_exp_minutes),
            "iss": "data-center-intelligence-portal",
        }
        return jwt.encode(payload, portal_jwt_secret, algorithm="HS256")

    def get_current_user() -> str | None:
        token = request.cookies.get(AUTH_COOKIE_NAME)
        if not token:
            return None
        try:
            payload = jwt.decode(
                token,
                portal_jwt_secret,
                algorithms=["HS256"],
                issuer="data-center-intelligence-portal",
            )
        except jwt.InvalidTokenError:
            return None

        subject = payload.get("sub")
        if isinstance(subject, str) and hmac.compare_digest(subject, portal_username):
            return subject
        return None

    @app.before_request
    def load_current_user() -> None:
        g.current_user = get_current_user()

    def login_required(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not g.current_user:
                return redirect(url_for("login"))
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    def check_csrf() -> None:
        token = request.form.get("csrf_token", "")
        if not token or not hmac.compare_digest(token, session.get("csrf_token", "")):
            raise ValueError("Invalid form token.")

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = os.urandom(24).hex()
            session["csrf_token"] = token
        return token

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            submitted_user = request.form.get("username", "")
            submitted_password = request.form.get("password", "")
            if hmac.compare_digest(submitted_user, portal_username) and check_password_hash(
                portal_password_hash, submitted_password
            ):
                session.clear()
                response = make_response(redirect(url_for("index")))
                response.set_cookie(
                    AUTH_COOKIE_NAME,
                    create_auth_token(submitted_user),
                    httponly=True,
                    secure=auth_cookie_secure,
                    samesite="Lax",
                    max_age=jwt_exp_minutes * 60,
                )
                return response
            flash("Invalid username or password.")

        body = """
        <section class="panel">
          <h1>Sign In</h1>
          <form method="post" class="row">
            <input name="username" autocomplete="username" placeholder="Username" required>
            <input name="password" type="password" autocomplete="current-password" placeholder="Password" required>
            <button type="submit">Sign in</button>
          </form>
        </section>
        """
        return render("Sign In", body)

    @app.route("/logout")
    def logout():
        session.clear()
        response = make_response(redirect(url_for("login")))
        response.delete_cookie(AUTH_COOKIE_NAME, samesite="Lax", secure=auth_cookie_secure)
        return response

    @app.route("/")
    @login_required
    def index():
        reports = get_reports(config.db_path)
        token = csrf_token()
        body = render_template_string(
            """
            <section class="panel">
              <div class="row">
                <div>
                  <h1>Reports</h1>
                  <p class="muted">A new report is generated every day at {{ hour }}:{{ minute }} {{ timezone }}.</p>
                </div>
                <div class="spacer"></div>
                <form method="post" action="{{ url_for('run_now') }}">
                  <input type="hidden" name="csrf_token" value="{{ token }}">
                  <button type="submit">Run now</button>
                </form>
              </div>
            </section>
            <section class="panel">
              <table>
                <thead>
                  <tr><th>ID</th><th>Subject</th><th>Created</th><th>Sources</th><th>Status</th><th></th></tr>
                </thead>
                <tbody>
                  {% for report in reports %}
                    <tr>
                      <td>{{ report["id"] }}</td>
                      <td>{{ report["subject"] }}</td>
                      <td>{{ report["created_utc"] }}</td>
                      <td>{{ report["source_count"] }}</td>
                      <td><span class="status {{ report["status"] }}">{{ report["status"] }}</span></td>
                      <td><a href="{{ url_for('report_detail', report_id=report["id"]) }}">Review</a></td>
                    </tr>
                  {% else %}
                    <tr><td colspan="6">No reports have been generated yet.</td></tr>
                  {% endfor %}
                </tbody>
              </table>
            </section>
            """,
            reports=reports,
            token=token,
            hour=f"{config.schedule_hour:02d}",
            minute=f"{config.schedule_minute:02d}",
            timezone=config.timezone,
        )
        return render("Reports", body, current_page="reports")

    @app.route("/run-now", methods=["POST"])
    @login_required
    def run_now():
        try:
            check_csrf()
            report_id = generate_report_once(config)
            flash(f"Generated report {report_id}.")
        except Exception as exc:
            flash(str(exc))
        return redirect(url_for("index"))

    @app.route("/unit-test", methods=["GET", "POST"])
    @login_required
    def unit_test():
        selected_company = request.form.get("company", "").strip()
        raw_recipients = request.form.get("recipients", "")

        if request.method == "POST":
            try:
                check_csrf()
                if not selected_company:
                    raise ValueError("Enter a company name.")
                recipients = parse_recipient_list(raw_recipients)
                if not recipients:
                    raise ValueError("Enter at least one recipient email.")
                unit_test_config = replace(config, max_results_per_company=25)
                report_id = generate_report_once(
                    unit_test_config,
                    companies=[selected_company],
                    recipients=recipients,
                    days_back=1,
                    time_window_label="the last 24 hours",
                )
                flash(f"Generated unit test report {report_id} for {selected_company}.")
                return redirect(url_for("report_detail", report_id=report_id))
            except Exception as exc:
                flash(str(exc))

        body = render_template_string(
            """
            <section class="panel">
              <div class="row">
                <div>
                  <h1>Unit Test</h1>
                  <p class="muted">Generate a one-company report from the last 24 hours and send it to the addresses you choose.</p>
                </div>
              </div>
              <form method="post">
                <input type="hidden" name="csrf_token" value="{{ token }}">
                <div class="row" style="align-items:flex-start;">
                  <div style="flex:1; min-width: 280px;">
                    <label for="company"><strong>Company</strong></label><br>
                    <input id="company" name="company" type="text" placeholder="Company name" value="{{ selected_company }}" required>
                  </div>
                  <div style="flex:2; min-width: 320px;">
                    <label for="recipients"><strong>Recipients</strong></label><br>
                    <textarea id="recipients" name="recipients" placeholder="name@company.com, other@company.com" required>{{ raw_recipients }}</textarea>
                    <div class="muted">Separate emails with commas, semicolons, or new lines.</div>
                  </div>
                </div>
                <div style="margin-top: 16px;">
                  <button type="submit">Generate 24 Hour Report</button>
                </div>
              </form>
            </section>
            """,
            token=csrf_token(),
            selected_company=selected_company,
            raw_recipients=raw_recipients,
        )
        return render("Unit Test", body, current_page="unit_test")

    @app.route("/reports/<int:report_id>")
    @login_required
    def report_detail(report_id: int):
        report = get_report(config.db_path, report_id)
        summary = json.loads(report["summary_json"])
        diagnostics = summary.get("diagnostics") if isinstance(summary, dict) else None
        recipients_list = (
            json.loads(report["target_recipients_json"])
            if report["target_recipients_json"]
            else get_recipients(config.db_path)
        )
        token = csrf_token()
        scheduled_send_utc = report["scheduled_send_utc"]
        body = render_template_string(
            """
            <section class="panel">
              <div class="row">
                <div>
                  <h1>{{ report["subject"] }}</h1>
                  <p class="muted">Report {{ report["id"] }} created {{ report["created_utc"] }}.</p>
                </div>
                <div class="spacer"></div>
                {% if report["sent_utc"] %}
                  <span class="status sent">Sent {{ report["sent_utc"] }}</span>
                {% else %}
                  <div class="row" style="justify-content:flex-end;">
                    <form method="post" action="{{ url_for('send_report_route', report_id=report["id"]) }}">
                      <input type="hidden" name="csrf_token" value="{{ token }}">
                      <button type="submit">Send now</button>
                    </form>
                    <form method="post" action="{{ url_for('schedule_report_route', report_id=report["id"]) }}" class="row">
                      <input type="hidden" name="csrf_token" value="{{ token }}">
                      <input type="datetime-local" name="scheduled_send_local" required>
                      <button class="secondary" type="submit">Schedule send</button>
                    </form>
                  </div>
                {% endif %}
              </div>
              <p><strong>Recipients:</strong> {{ recipients|join(", ") if recipients else "None added" }}</p>
              {% if scheduled_send_utc and not report["sent_utc"] %}
                <p class="muted"><strong>Scheduled:</strong> {{ scheduled_send_utc }}</p>
              {% endif %}
            </section>
            {% if diagnostics %}
            <section class="panel">
              <h2 style="margin-top:0;">Diagnostics</h2>
              <table>
                <tbody>
                  <tr><th style="text-align:left;">Requested window</th><td>{{ diagnostics["requested_days_back"] }} day(s)</td></tr>
                  <tr><th style="text-align:left;">Raw Tavily results</th><td>{{ diagnostics["raw_results_count"] }}</td></tr>
                  <tr><th style="text-align:left;">Qualified results in requested window</th><td>{{ diagnostics["qualified_results_count"] }}</td></tr>
                  <tr><th style="text-align:left;">Recent undated pages included</th><td>{{ diagnostics["recent_undated_included_count"] }}</td></tr>
                  <tr><th style="text-align:left;">Structured summary fallback used</th><td>{{ "Yes" if diagnostics["fallback_summary_used"] else "No" }}</td></tr>
                  <tr><th style="text-align:left;">Fallback used</th><td>{{ "Yes" if diagnostics["fallback_used"] else "No" }}</td></tr>
                  {% if diagnostics["fallback_used"] %}
                    <tr><th style="text-align:left;">Fallback window</th><td>{{ diagnostics["fallback_days_back"] }} day(s)</td></tr>
                    <tr><th style="text-align:left;">Fallback raw results</th><td>{{ diagnostics["fallback_raw_results_count"] }}</td></tr>
                    <tr><th style="text-align:left;">Fallback qualified results</th><td>{{ diagnostics["fallback_qualified_results_count"] }}</td></tr>
                    <tr><th style="text-align:left;">Fallback recent undated pages included</th><td>{{ diagnostics["fallback_recent_undated_included_count"] }}</td></tr>
                  {% endif %}
                </tbody>
              </table>
            </section>
            {% endif %}
            <iframe srcdoc="{{ report["email_html"]|e }}"></iframe>
            """,
            report=report,
            diagnostics=diagnostics,
            recipients=recipients_list,
            token=token,
            scheduled_send_utc=scheduled_send_utc,
        )
        return render("Report", body)

    @app.route("/reports/<int:report_id>/send", methods=["POST"])
    @login_required
    def send_report_route(report_id: int):
        try:
            check_csrf()
            report = get_report(config.db_path, report_id)
            recipients_override = (
                json.loads(report["target_recipients_json"])
                if report["target_recipients_json"]
                else None
            )
            try:
                scheduler.remove_job(f"send-report-{report_id}")
            except JobLookupError:
                pass
            recipients_sent = send_report(config, report_id, recipients=recipients_override)
            flash(f"Sent report {report_id} to {len(recipients_sent)} recipient(s).")
        except Exception as exc:
            flash(str(exc))
        return redirect(url_for("report_detail", report_id=report_id))

    @app.route("/reports/<int:report_id>/schedule-send", methods=["POST"])
    @login_required
    def schedule_report_route(report_id: int):
        try:
            check_csrf()
            scheduled_send_local = request.form.get("scheduled_send_local", "")
            scheduled_utc = schedule_report_send_job(report_id, scheduled_send_local)
            flash(f"Scheduled report {report_id} for {scheduled_utc}.")
        except Exception as exc:
            flash(str(exc))
        return redirect(url_for("report_detail", report_id=report_id))

    @app.route("/recipients", methods=["GET", "POST"])
    @login_required
    def recipients():
        if request.method == "POST":
            try:
                check_csrf()
                add_recipient(config.db_path, request.form.get("email", ""))
                flash("Recipient added.")
            except Exception as exc:
                flash(str(exc))
            return redirect(url_for("recipients"))

        token = csrf_token()
        recipient_rows = get_recipients(config.db_path)
        body = render_template_string(
            """
            <section class="panel">
              <h1>Recipients</h1>
              <form method="post" class="row">
                <input type="hidden" name="csrf_token" value="{{ token }}">
                <input name="email" type="email" placeholder="person@company.com" required>
                <button type="submit">Add email</button>
              </form>
            </section>
            <section class="panel">
              <table>
                <thead><tr><th>Email</th><th></th></tr></thead>
                <tbody>
                  {% for email in recipients %}
                    <tr>
                      <td>{{ email }}</td>
                      <td>
                        <form method="post" action="{{ url_for('delete_recipient', email=email) }}">
                          <input type="hidden" name="csrf_token" value="{{ token }}">
                          <button class="danger" type="submit">Remove</button>
                        </form>
                      </td>
                    </tr>
                  {% else %}
                    <tr><td colspan="2">No recipients configured.</td></tr>
                  {% endfor %}
                </tbody>
              </table>
            </section>
            """,
            recipients=recipient_rows,
            token=token,
        )
        return render("Recipients", body, current_page="recipients")

    @app.route("/recipients/<path:email>/delete", methods=["POST"])
    @login_required
    def delete_recipient(email: str):
        try:
            check_csrf()
            remove_recipient(config.db_path, email)
            flash("Recipient removed.")
        except Exception as exc:
            flash(str(exc))
        return redirect(url_for("recipients"))

    return app


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("PORTAL_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT") or os.environ.get("PORTAL_PORT", "8000"))
    app.run(host=host, port=port, debug=False, use_reloader=False)
