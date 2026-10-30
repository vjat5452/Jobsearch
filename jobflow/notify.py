"""Tell you when something needs you: a Windows toast and/or an email.
Both are best effort - a failed notification never stops a run."""
from __future__ import annotations

import os
import smtplib
import subprocess
from email.message import EmailMessage
from html import escape

from .config import section

NO_WINDOW = 0x08000000


def toast(title: str, message: str) -> None:
    """Windows balloon notification via PowerShell (no extra modules needed)."""
    if os.name != "nt":
        return

    def lit(s: str) -> str:
        return s.replace("'", "''")[:200]

    ps = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
          "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
          f"$n.BalloonTipTitle = '{lit(title)}'; $n.BalloonTipText = '{lit(message)}'; "
          "$n.Visible = $true; $n.ShowBalloonTip(8000); Start-Sleep -Seconds 9; $n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


CSS = """
body{font:15px/1.55 -apple-system,'Segoe UI',sans-serif;color:#1a1a1a;background:#f6f7f9;margin:0;padding:24px}
.wrap{max-width:640px;margin:0 auto;background:#fff;border-radius:12px;padding:28px;border:1px solid #e3e5e9}
h1{font-size:20px;margin:0 0 4px}.sub{color:#6b7280;font-size:13px;margin:0 0 20px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;margin:24px 0 8px;
   padding-bottom:6px;border-bottom:1px solid #eceef2}
.job{padding:10px 0;border-bottom:1px solid #f1f2f5}.t{font-weight:600;text-decoration:none;color:#0b5cff}
.m{color:#6b7280;font-size:13px}.score{display:inline-block;background:#eef4ff;color:#0b5cff;border-radius:5px;
padding:1px 7px;font-size:12px;font-weight:600;margin-right:6px}.why{color:#4b5563;font-size:13px;font-style:italic}
"""


def render_jobs(rows) -> str:
    return "".join(
        f'<div class="job">{"<span class=score>%s/10</span>" % r["score"] if r.get("score") is not None else ""}'
        f'<a class="t" href="{escape(r.get("url") or "#")}">{escape(r["title"])}</a>'
        f'<div class="m">{escape(r.get("company") or "")} · {escape(r.get("location") or "")} · {escape(r["platform"])}</div>'
        f'{"<div class=why>%s</div>" % escape(r["reason"]) if r.get("reason") else ""}</div>'
        for r in rows)


def email(subject: str, html_body: str, text: str) -> bool:
    cfg = section("email")
    if not cfg.get("enabled"):
        return False
    password = (os.environ.get("JOBFLOW_SMTP_PASSWORD") or "").replace(" ", "").strip()
    if not password:
        raise RuntimeError("JOBFLOW_SMTP_PASSWORD is not set (env var or secrets.txt)")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["from"], cfg["to"]
    msg.set_content(text)
    msg.add_alternative(f"<html><head><meta charset='utf-8'><style>{CSS}</style></head>"
                        f"<body><div class='wrap'>{html_body}</div></body></html>", subtype="html")
    try:
        with smtplib.SMTP(cfg.get("smtp_host", "smtp.gmail.com"), int(cfg.get("smtp_port", 587)), timeout=30) as s:
            s.starttls()
            s.login(cfg["from"], password)
            s.send_message(msg)
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected) as e:
        # Gmail hangs up rather than answering when the app password is wrong.
        raise RuntimeError(f"the mail server rejected the login for {cfg['from']} - check the app "
                           f"password and that email.from is the account that made it") from e
    return True


def daily_summary(store) -> None:
    """One email a day: what was applied, what waits for you."""
    from datetime import date

    today = date.today().isoformat()
    applied = [dict(r) for r in store.db.execute(
        "SELECT * FROM jobs WHERE status = 'applied' AND applied_at LIKE ? ORDER BY score DESC",
        (f"{today}%",)).fetchall()]
    review = store.jobs(["review"], limit=25)
    questions = store.pending_questions()
    if not (applied or review or questions):
        return
    body = (f"<h1>JobFlow</h1><p class='sub'>{today} · {len(applied)} applied · {len(review)} waiting for "
            f"approval · {len(questions)} question(s) for you</p>"
            f"<h2>Applied today</h2>{render_jobs(applied) or '<p class=m>none</p>'}"
            f"<h2>Waiting for your approval</h2>{render_jobs(review) or '<p class=m>none</p>'}"
            + (f"<h2>Questions</h2>" + "".join(f"<div class='job'>{escape(q['question'])}</div>" for q in questions)
               if questions else "")
            + "<p class='m'>Approve, skip and answer in the dashboard: python main.py web</p>")
    text = f"{len(applied)} applied, {len(review)} waiting, {len(questions)} questions"
    email(f"JobFlow — {len(applied)} applied, {len(review)} to review", body, text)
