"""
scrape_and_notify.py
-------------------------------------------------------------------------
Runs on a GitHub Actions schedule instead of your own machine. Does three
things in one pass:

1. Scrapes cbic-gst.gov.in (plain HTML, reliable — confirmed against the
   live site) for new notifications/advisories.
2. Merges any new items into data.json (deduped by source URL).
3. Emails every address in subscribers.json when new items are found,
   using Brevo's free SMTP relay (300 emails/day, no card, no 2-Step
   Verification required — unlike Gmail, which forces 2FA + an App
   Password before it'll allow any SMTP script to send).

Credentials come from environment variables (set as GitHub Secrets, never
committed to the repo):
    BREVO_SMTP_LOGIN   — your Brevo account login email
    BREVO_SMTP_KEY     — your Brevo SMTP key (from Brevo dashboard → SMTP & API)
    BREVO_SENDER_EMAIL — the "from" address (must be a verified sender in Brevo)

If those secrets aren't set, the script still scrapes and updates
data.json — it just skips the email step (logged, not an error).
-------------------------------------------------------------------------
"""

import json
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data.json"
SUBSCRIBERS_FILE = ROOT / "subscribers.json"

CBIC_HOME = "https://cbic-gst.gov.in/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GSTDashboardBot/1.0; internal, non-commercial use)"}
ADVISORY_DATE_RE = re.compile(r"dated\s+(\d{1,2})[.\/](\d{1,2})[.\/](\d{2,4})", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Scraping (same logic as the local version — see scrape_gst_updates.py for
# the fuller, commented original this was adapted from)
# ---------------------------------------------------------------------------
def parse_dd_mm_yyyy(match):
    if not match:
        return None
    dd, mm, yy = match.groups()
    yyyy = f"20{yy}" if len(yy) == 2 else yy
    return f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}"


def to_regulatory_update(title, href, dept, category, priority, item_date, needs_review=False):
    from datetime import date
    return {
        "department": dept,
        "category": category,
        "title": title.strip(),
        "summary": title.strip(),
        "priority": priority,
        "source": href,
        "keyChanges": [],
        "effectiveDate": item_date,
        "actionRequired": "Review the source document for details.",
        "date": item_date or date.today().isoformat(),
        "_needsReview": needs_review,
    }


def scrape_cbic():
    resp = requests.get(CBIC_HOME, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    items = []

    whats_new_heading = soup.find(string=re.compile(r"^\s*What's New\s*$"))
    if whats_new_heading:
        ul = whats_new_heading.find_parent().find_next("ul")
        if ul:
            for li in ul.find_all("li"):
                a = li.find("a")
                if not a or not a.get("href"):
                    continue
                items.append(to_regulatory_update(
                    title=a.get_text(), href=urljoin(CBIC_HOME, a["href"]),
                    dept="CBIC", category="Notification", priority="Medium",
                    item_date=None, needs_review=True,
                ))

    for a in soup.find_all("a", href=re.compile(r"/pdf/", re.IGNORECASE)):
        context = a.find_parent().get_text(" ", strip=True) if a.find_parent() else a.get_text()
        if not re.search(r"advisory|dated|maintenance", context, re.IGNORECASE):
            continue
        date_match = ADVISORY_DATE_RE.search(context)
        items.append(to_regulatory_update(
            title=context[:200], href=urljoin(CBIC_HOME, a["href"]),
            dept="CBIC", category="Order", priority="Medium",
            item_date=parse_dd_mm_yyyy(date_match), needs_review=date_match is None,
        ))

    return items


# ---------------------------------------------------------------------------
# Data persistence
# ---------------------------------------------------------------------------
def load_existing():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except json.JSONDecodeError:
            return []
    return []


def save_data(items):
    DATA_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False))


def load_subscribers():
    if not SUBSCRIBERS_FILE.exists():
        return []
    try:
        return json.loads(SUBSCRIBERS_FILE.read_text()).get("emails", [])
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# Email via Brevo SMTP — no 2-Step Verification, no App Password, just an
# SMTP key generated from the Brevo dashboard.
# ---------------------------------------------------------------------------
def format_digest(new_items):
    lines = [f"{len(new_items)} new GST/CBIC update(s) found:\n"]
    for item in new_items:
        lines.append(f"- [{item['department']}] {item['title']}")
        lines.append(f"  Priority: {item['priority']} | Date: {item['date']}")
        lines.append(f"  Source: {item['source']}\n")
    lines.append("— Sent automatically by the GST Regulatory Dashboard (GitHub Actions).")
    return "\n".join(lines)


def send_email_alert(new_items):
    if not new_items:
        print("No new items — skipping email.")
        return

    login = os.environ.get("BREVO_SMTP_LOGIN")
    key = os.environ.get("BREVO_SMTP_KEY")
    sender = os.environ.get("BREVO_SENDER_EMAIL")
    if not (login and key and sender):
        print("Email not configured (BREVO_SMTP_LOGIN / BREVO_SMTP_KEY / BREVO_SENDER_EMAIL secrets not set) — skipping email.")
        return

    recipients = load_subscribers()
    if not recipients:
        print("No subscribers in subscribers.json — skipping email.")
        return

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["Subject"] = f"GST Dashboard: {len(new_items)} new update(s)"
    msg.attach(MIMEText(format_digest(new_items), "plain"))

    try:
        with smtplib.SMTP("smtp-relay.brevo.com", 587) as server:
            server.starttls()
            server.login(login, key)
            for recipient in recipients:
                msg["To"] = recipient
                server.sendmail(sender, recipient, msg.as_string())
        print(f"Emailed {len(recipients)} subscriber(s).")
    except Exception as exc:  # noqa: BLE001 — a failed email should never fail the whole scrape
        print(f"Email send failed: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    existing = load_existing()
    existing_sources = {u["source"] for u in existing}

    new_items = scrape_cbic()
    print(f"CBIC: found {len(new_items)} item(s) on the page.")

    deduped = [item for item in new_items if item["source"] not in existing_sources]
    merged = existing + deduped
    save_data(merged)
    print(f"Added {len(deduped)} new item(s). data.json now has {len(merged)} total.")

    send_email_alert(deduped)


if __name__ == "__main__":
    main()
