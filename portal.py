from __future__ import annotations

import hmac
import os
import datetime as dt
from functools import wraps
from pathlib import Path
from typing import Callable, TypeVar

import jwt
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
    generate_report_once,
    get_recipients,
    get_report,
    get_reports,
    init_db,
    load_config,
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
  button, .button { background: #0b5cab; border: 0; color: white; border-radius: 6px; padding: 10px 14px; text-decoration: none; cursor: pointer; }
  button.secondary, .button.secondary { background: #52616f; }
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
        <a class="button secondary" href="{{ url_for('index') }}">Reports</a>
        <a class="button secondary" href="{{ url_for('recipients') }}">Recipients</a>
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
    load_dotenv()
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
        return render("Reports", body)

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

    @app.route("/reports/<int:report_id>")
    @login_required
    def report_detail(report_id: int):
        report = get_report(config.db_path, report_id)
        recipients_list = get_recipients(config.db_path)
        token = csrf_token()
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
                  <form method="post" action="{{ url_for('send_report_route', report_id=report["id"]) }}">
                    <input type="hidden" name="csrf_token" value="{{ token }}">
                    <button type="submit">Send with Outlook</button>
                  </form>
                {% endif %}
              </div>
              <p><strong>Recipients:</strong> {{ recipients|join(", ") if recipients else "None added" }}</p>
            </section>
            <iframe srcdoc="{{ report["email_html"]|e }}"></iframe>
            """,
            report=report,
            recipients=recipients_list,
            token=token,
        )
        return render("Report", body)

    @app.route("/reports/<int:report_id>/send", methods=["POST"])
    @login_required
    def send_report_route(report_id: int):
        try:
            check_csrf()
            recipients_sent = send_report(config, report_id)
            flash(f"Sent report {report_id} to {len(recipients_sent)} recipient(s).")
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
        return render("Recipients", body)

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
    host = os.environ.get("PORTAL_HOST", "127.0.0.1")
    port = int(os.environ.get("PORTAL_PORT", "8000"))
    app.run(host=host, port=port, debug=False, use_reloader=False)
