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
from email.mime.image import MIMEImage
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
def scrape_gstn_advisories(driver):
    from selenium.webdriver.common.by import By

    items = []
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
    # Confirmed real structure via debug output: advisory titles live in
    # an <h3> with a class containing "news-item" and "header" (the
    # exact class name showed a double hyphen — "news-item--header" —
    # in one debug dump; using contains() here instead of an exact match
    # so a minor naming variation like that doesn't silently break this
    # again), dates in a nearby <p> with "news-item" and "date" in its
    # class. There is NO <a href> anywhere in this list — confirmed via
    # debug output — so we click each item to capture its real URL.
    headers = driver.find_elements(
        By.XPATH,
        "//h3[contains(@class, 'news-item') and contains(@class, 'header')]"
    )
    print(f"  [debug] news-item header elements found: {len(headers)}")

    MAX_ITEMS = 15  # cap to keep run time reasonable
    for i in range(min(len(headers), MAX_ITEMS)):
        try:
            # Re-find each time — clicking/navigating invalidates old
            # element references (StaleElementReferenceException).
            headers = driver.find_elements(
                By.XPATH,
                "//h3[contains(@class, 'news-item') and contains(@class, 'header')]"
            )
            header_el = headers[i]
            title = header_el.text.strip()
            if not title or len(title.split()) < 3:
                continue

            # Click the header itself (or its nearest clickable ancestor)
            clickable = header_el
            try:
                clickable.click()
            except Exception:
                # Some Angular Material list items need the parent
                # container clicked instead of the text element itself.
                parent = header_el.find_element(By.XPATH, "./..")
                parent.click()

            time.sleep(3)
            real_url = driver.current_url
            if "advisoryandreleases" in real_url and real_url != GSTN_ADVISORY_URL:
                items.append(to_regulatory_update(
                    title=title, href=real_url, dept="GSTN", category="Notification",
                    priority="Medium", item_date=None, needs_review=True,
                ))
            driver.back()
            time.sleep(3)
        except Exception as exc:  # noqa: BLE001 — one bad item shouldn't stop the rest
            print(f"  [debug] item {i} failed: {exc}")
            continue

    print(f"  [debug] items captured with real URLs: {len(items)}")
    return items


def create_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1024,700")
    return webdriver.Chrome(options=options)


def capture_screenshot(driver, url):
    """Navigates to url and returns a PNG screenshot as bytes, or None on
    failure. Used to embed a real preview of each new item's source page
    in the email digest. A failure here (e.g. a PDF that doesn't render,
    a slow page) should never break the rest of the run — caller treats
    None as "no screenshot for this one", not an error."""
    try:
        driver.get(url)
        time.sleep(3)
        return driver.get_screenshot_as_png()
    except Exception as exc:  # noqa: BLE001 — best-effort; missing a screenshot is fine
        print(f"  [debug] screenshot failed for {url}: {exc}")
        return None


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
DEPT_COLORS = {
    "GST": "#1E3A5F", "CBIC": "#0F766E", "NIC e-Way Bill": "#3730A3",
    "NIC e-Invoice": "#6D28D9", "PIB": "#854D0E", "GSTN": "#0369A1",
}
PRIORITY_COLORS = {
    "High": ("#FEF2F2", "#B91C1C"), "Medium": ("#FFFBEB", "#B45309"), "Low": ("#F0FDF4", "#15803D"),
}


def format_digest_text(new_items):
    """Plain-text fallback for mail clients that don't render HTML."""
    lines = [f"{len(new_items)} new GST/CBIC update(s) found:\n"]
    for item in new_items:
        lines.append(f"- [{item['department']}] {item['title']}")
        lines.append(f"  Priority: {item['priority']} | Date: {item['date']}")
        lines.append(f"  Source: {item['source']}\n")
    lines.append("— Sent automatically by the GST Regulatory Dashboard (GitHub Actions).")
    return "\n".join(lines)


def format_digest_html(new_items, screenshots):
    """Styled HTML version, matching the dashboard's own colors. For each
    item with a captured screenshot, embeds it inline via a cid: reference
    — the actual image bytes are attached separately in send_email_alert()."""
    cards = []
    for i, item in enumerate(new_items):
        dept_color = DEPT_COLORS.get(item["department"], "#6B7280")
        bg, text_color = PRIORITY_COLORS.get(item["priority"], ("#F0FDF4", "#15803D"))
        cid = f"screenshot{i}"
        has_screenshot = item["source"] in screenshots
        screenshot_html = (
            f'<img src="cid:{cid}" alt="Preview of source page" '
            f'style="width:100%;max-width:520px;border:1px solid #E5E7EB;border-radius:8px;margin-top:10px;display:block;">'
            if has_screenshot else ""
        )
        cards.append(f"""
        <div style="background:#F7F8FA;border-radius:12px;padding:16px;margin-bottom:14px;">
          <div style="display:inline-block;background:{dept_color};color:#fff;font-size:11px;font-weight:600;
                      padding:3px 10px;border-radius:999px;margin-bottom:8px;">{item['department']}</div>
          <div style="display:inline-block;background:{bg};color:{text_color};font-size:11px;font-weight:600;
                      padding:3px 10px;border-radius:999px;margin-bottom:8px;margin-left:6px;">{item['priority']} priority</div>
          <div style="font-size:15px;font-weight:600;color:#111827;margin:6px 0;">{item['title']}</div>
          <div style="font-size:12px;color:#6B7280;margin-bottom:6px;">Date: {item['date']}</div>
          <a href="{item['source']}" style="font-size:12px;color:{dept_color};font-weight:600;text-decoration:none;">View original source &rarr;</a>
          {screenshot_html}
        </div>""")

    return f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:600px;margin:0 auto;">
      <div style="background:#1E3A5F;color:#fff;padding:20px;border-radius:12px 12px 0 0;">
        <div style="font-size:17px;font-weight:700;">GST Regulatory Dashboard</div>
        <div style="font-size:13px;color:#CADCFC;">{len(new_items)} new update(s) found</div>
      </div>
      <div style="border:1px solid #E5E7EB;border-top:none;padding:20px;border-radius:0 0 12px 12px;">
        {''.join(cards)}
        <div style="font-size:11px;color:#9CA3AF;text-align:center;margin-top:8px;">
          Sent automatically by the GST Regulatory Dashboard (GitHub Actions)
        </div>
      </div>
    </div>
    """


def send_email_alert(new_items, screenshots=None):
    screenshots = screenshots or {}
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

    text_body = format_digest_text(new_items)
    html_body = format_digest_html(new_items, screenshots)
    subject = f"GST Dashboard: {len(new_items)} new update(s)"

    try:
        with smtplib.SMTP("smtp-relay.brevo.com", 587) as server:
            server.starttls()
            server.login(login, key)
            for recipient in recipients:
                # A fresh MIMEMultipart per recipient — same pattern as
                # before — so no subscriber ever sees another's address.
                msg = MIMEMultipart("related")
                msg["From"] = sender
                msg["To"] = recipient
                msg["Subject"] = subject

                alt = MIMEMultipart("alternative")
                alt.attach(MIMEText(text_body, "plain"))
                alt.attach(MIMEText(html_body, "html"))
                msg.attach(alt)

                for i, item in enumerate(new_items):
                    png_bytes = screenshots.get(item["source"])
                    if png_bytes:
                        img = MIMEImage(png_bytes)
                        img.add_header("Content-ID", f"<screenshot{i}>")
                        img.add_header("Content-Disposition", "inline", filename=f"screenshot{i}.png")
                        msg.attach(img)

                server.sendmail(sender, recipient, msg.as_string())
        print(f"Emailed {len(recipients)} subscriber(s).")
    except Exception as exc:  # noqa: BLE001 — a failed email should never fail the whole scrape
        print(f"Email send failed: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=== SCRIPT VERSION: v8-html-email-with-screenshots ===")
    existing = load_existing()
    existing_sources = {u["source"] for u in existing}

    new_items = scrape_cbic()
    print(f"CBIC: found {len(new_items)} item(s) on the page.")

    # One shared Chrome instance for GSTN scraping AND for screenshotting
    # new items afterward — avoids starting a second browser just for
    # screenshots. Isolated in its own try/except so that if this JS-heavy
    # page's scraper breaks, it can NEVER take down the reliable CBIC
    # scrape above.
    driver = None
    try:
        driver = create_driver()
        gstn_items = scrape_gstn_advisories(driver)
        print(f"GSTN Advisory & Releases: found {len(gstn_items)} item(s) on the page.")
        new_items += gstn_items
    except Exception as exc:  # noqa: BLE001 — deliberately broad: this source is best-effort
        print(f"GSTN Advisory scrape failed (CBIC results above are unaffected): {exc}")

    deduped = [item for item in new_items if item["source"] not in existing_sources]
    merged = existing + deduped
    save_data(merged)
    print(f"Added {len(deduped)} new item(s). data.json now has {len(merged)} total.")

    # Screenshot each genuinely new item's real source page, to embed in
    # the email — capped implicitly by len(deduped), which is normally
    # small. A failed screenshot for one item never blocks the others or
    # the email itself (capture_screenshot returns None on failure).
    screenshots = {}
    if deduped:
        try:
            if driver is None:
                driver = create_driver()
            for item in deduped:
                png = capture_screenshot(driver, item["source"])
                if png:
                    screenshots[item["source"]] = png
            print(f"  [debug] screenshots captured: {len(screenshots)} of {len(deduped)} new item(s)")
        except Exception as exc:  # noqa: BLE001 — screenshots are a bonus, never critical
            print(f"  [debug] screenshot capture step failed: {exc}")

    if driver:
        driver.quit()

    send_email_alert(deduped, screenshots)


if __name__ == "__main__":
    main()
