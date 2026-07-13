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
import time
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
GSTN_ADVISORY_URL = "https://services.gst.gov.in/services/advisory/advisoryandreleases"
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
    print(f"  [debug] CBIC response: HTTP {resp.status_code}, {len(resp.text)} chars")
    soup = BeautifulSoup(resp.text, "html.parser")
    items = []

    # Match "What's New" regardless of straight (') vs curly (') apostrophe —
    # confirmed via testing that an exact-match regex silently fails against
    # a curly apostrophe, which professionally designed sites often use.
    # \W matches any single non-word character (straight or curly apostrophe,
    # or nothing at all) between "What" and "s New".
    whats_new_heading = soup.find(string=re.compile(r"What\W?s\s+New", re.IGNORECASE))
    print(f"  [debug] 'What's New' heading found: {whats_new_heading is not None}")
    if whats_new_heading:
        ul = whats_new_heading.find_parent().find_next("ul")
        print(f"  [debug] <ul> found after heading: {ul is not None}, <li> count: {len(ul.find_all('li')) if ul else 0}")
        if ul:
            rejected_pdf = rejected_words = rejected_junk = 0
            sample_rejected_hrefs = []
            for li in ul.find_all("li"):
                a = li.find("a")
                if not a or not a.get("href"):
                    continue
                title_text = a.get_text().strip()
                href = a["href"]
                # BUG FOUND via live debug output: real hrefs are relative
                # paths like "pdf/Circular-No-250-2025.pdf" — no leading
                # slash before "pdf". The old check for "/pdf/" as a
                # substring never matched these, silently rejecting every
                # real item. Checking the file extension instead is far
                # more reliable regardless of relative/absolute path style.
                if not href.lower().endswith(".pdf"):
                    rejected_pdf += 1
                    if len(sample_rejected_hrefs) < 5:
                        sample_rejected_hrefs.append(href)
                    continue
                if len(title_text.split()) < 3:
                    rejected_words += 1
                    continue
                if title_text.lower() in {"english", "hindi", "हिंदी"}:
                    rejected_junk += 1
                    continue
                items.append(to_regulatory_update(
                    title=title_text, href=urljoin(CBIC_HOME, href),
                    dept="CBIC", category="Notification", priority="Medium",
                    item_date=None, needs_review=True,
                ))
            print(f"  [debug] What's New: {len(items)} accepted, rejected: {rejected_pdf} (no /pdf/), {rejected_words} (too few words), {rejected_junk} (english/hindi)")
            if sample_rejected_hrefs:
                print(f"  [debug] sample of actual href values that were rejected (to see what the page really served):")
                for h in sample_rejected_hrefs:
                    print(f"    {h!r}")

    # Same fix as above — match the .pdf extension, not a "/pdf/" path
    # segment that assumes a leading slash relative links don't have.
    ticker_links = soup.find_all("a", href=re.compile(r"\.pdf$", re.IGNORECASE))
    print(f"  [debug] total <a href*=/pdf/> tags on page: {len(ticker_links)}")
    ticker_added = 0
    for a in ticker_links:
        context = a.find_parent().get_text(" ", strip=True) if a.find_parent() else a.get_text()
        if not re.search(r"advisory|dated|maintenance", context, re.IGNORECASE):
            continue
        date_match = ADVISORY_DATE_RE.search(context)
        items.append(to_regulatory_update(
            title=context[:200], href=urljoin(CBIC_HOME, a["href"]),
            dept="CBIC", category="Order", priority="Medium",
            item_date=parse_dd_mm_yyyy(date_match), needs_review=date_match is None,
        ))
        ticker_added += 1
    print(f"  [debug] ticker section: {ticker_added} accepted")

    return items


# ---------------------------------------------------------------------------
# GSTN Advisory & Releases (services.gst.gov.in) — OPTIONAL, NOT run by
# default. This page is a JavaScript app (a plain requests.get() to it
# returns essentially no content — confirmed directly), so reading it for
# real needs a headless browser, unlike the CBIC scraper above.
#
# This has NOT been tested against the live rendered page — this
# environment can't render JS-heavy pages either. The selector below is a
# best-effort guess based on the known page structure of similar GSTN
# advisory listings, not something confirmed against this specific page's
# real DOM. Treat it as a starting point: run it once, and if it returns
# 0 items, inspect the live page with browser devtools and adjust the
# selector.
#
# To actually use this, you'd need to:
#   1. pip install selenium (add to requirements.txt)
#   2. Add a Chrome setup step to .github/workflows/scrape.yml
#      (e.g. browser-actions/setup-chrome)
#   3. Call scrape_gstn_advisories() from main() below
# It's deliberately NOT wired in yet, so it can't break the reliable CBIC
# scrape that's already running hourly.
# ---------------------------------------------------------------------------
def scrape_gstn_advisories():
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    items = []
    try:
        # Direct-loading the deep advisory URL was landing on the site's
        # generic homepage/shell instead of the actual advisory list —
        # confirmed via debug output (we found a nav link *to* this exact
        # URL sitting in the results, meaning we never actually left the
        # homepage). Angular-style apps sometimes don't correctly
        # initialize a deep route on a fresh page load. Loading the
        # homepage first and clicking through like a real visitor is more
        # reliable for this kind of app.
        driver.get("https://services.gst.gov.in/")
        time.sleep(6)
        nav_link = driver.find_element(By.XPATH, "//a[contains(@href, 'advisoryandreleases')]")
        nav_link.click()
        time.sleep(8)
        page_title = driver.title
        body_text_length = len(driver.find_element(By.TAG_NAME, "body").text)
        print(f"  [debug] page title: {page_title!r}, body text length: {body_text_length} chars")
        # Selector updated based on a confirmed real URL from this site
        # (services.gst.gov.in/services/advisoryandreleases/read/543, found
        # via web search) — individual advisories use a "/read/{number}"
        # path, not necessarily direct .pdf links on the listing page itself.
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/read/']")
        print(f"  [debug] links matching '/read/' found: {len(links)}")
        if len(links) == 0:
            # The page rendered real content but our selector guess didn't
            # match — dump a sample of what's actually there instead of
            # guessing blindly again. Scanning ALL links (not just the
            # first several), since the first batch turned out to be
            # header/nav links (Login, Register, Home) — the real advisory
            # list items are further down the page.
            all_links = driver.find_elements(By.TAG_NAME, "a")
            print(f"  [debug] total <a> tags on page: {len(all_links)}")
            sample_hrefs = []
            for link in all_links:
                href = link.get_attribute("href")
                text = link.text.strip()
                if href and text and len(text) > 15:  # skip short nav labels, keep real-looking titles
                    sample_hrefs.append(f"{text[:70]!r} -> {href}")
            print(f"  [debug] {len(sample_hrefs)} links with longer text (likely real content, not nav):")
            for s in sample_hrefs[:25]:
                print(f"    {s}")
        for link in links:
            title = link.text.strip()
            href = link.get_attribute("href")
            if not title or not href or len(title.split()) < 3:
                continue
            items.append(to_regulatory_update(
                title=title, href=href, dept="GSTN", category="Notification",
                priority="Medium", item_date=None, needs_review=True,
            ))
    finally:
        driver.quit()
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

    # GSTN Advisory & Releases — isolated in its own try/except so that if
    # this JS-heavy page's scraper breaks or its selectors need tuning, it
    # can NEVER take down the reliable CBIC scrape above.
    try:
        gstn_items = scrape_gstn_advisories()
        print(f"GSTN Advisory & Releases: found {len(gstn_items)} item(s) on the page.")
        new_items += gstn_items
    except Exception as exc:  # noqa: BLE001 — deliberately broad: this source is best-effort
        print(f"GSTN Advisory scrape failed (CBIC results above are unaffected): {exc}")

    deduped = [item for item in new_items if item["source"] not in existing_sources]
    merged = existing + deduped
    save_data(merged)
    print(f"Added {len(deduped)} new item(s). data.json now has {len(merged)} total.")

    send_email_alert(deduped)


if __name__ == "__main__":
    main()
