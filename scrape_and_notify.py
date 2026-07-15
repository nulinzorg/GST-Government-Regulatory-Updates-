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

# Catches worded dates like "31st March 2026", "9th February 2018", "24 June
# 2026" — the ticker text mixes this style with the numeric "dated DD.MM.YYYY"
# style above, and previously only the numeric one was recognized. A missed
# date silently fell back to today's scrape date instead of the real
# notification date — this covers the other common phrasing.
MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
WORDED_DATE_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTH_NAMES.keys()) + r")\.?,?\s+(\d{4})",
    re.IGNORECASE,
)


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


def parse_worded_date(match):
    if not match:
        return None
    dd, month_name, yyyy = match.groups()
    mm = MONTH_NAMES[month_name.lower()]
    return f"{yyyy}-{mm:02d}-{int(dd):02d}"


def extract_date_from_text(text):
    """Tries the numeric 'dated DD.MM.YYYY' pattern first, then the worded
    '31st March 2026' style, returning the first real date found, or None
    if neither matches — in which case the caller falls back to today's
    scrape date rather than guessing further."""
    numeric_match = ADVISORY_DATE_RE.search(text)
    if numeric_match:
        return parse_dd_mm_yyyy(numeric_match)
    worded_match = WORDED_DATE_RE.search(text)
    if worded_match:
        return parse_worded_date(worded_match)
    return None


# ---------------------------------------------------------------------------
# Priority & accounting-software-impact classification
# ---------------------------------------------------------------------------
# Rules (as specified):
#   HIGH   — a genuinely new implementation, a rule change, or anything that
#            affects accounting/return-filing software (e.g. Tally) — new
#            mandatory fields, schema/API changes, deadline changes, rate
#            changes, new registration/return requirements.
#   LOW    — regular circulars, clarifications, or informational content
#            with no impact on returns, software, existing behavior, or the
#            taxpayer (e.g. revenue statistics, press releases, a changed
#            helpdesk phone number, website policy pages).
#   MEDIUM — everything that can't clearly be determined as either.
#
# This is keyword-based, not AI-based — it's a real, testable, deterministic
# rule, not a judgment call made by a language model reading the text. It
# will misclassify some edge cases; treat it as a solid first pass, not a
# guarantee.

# ---------------------------------------------------------------------------
# Rules (as restated, simplified):
#   HIGH   — a validation change, a rule change, or an API change — including
#            any new rule implemented for an API.
#   LOW    — the default. A regular circular detailing an explanation, or
#            any other notification not related to a High-priority change.
#   MEDIUM — reserved for genuine ambiguity that can't be resolved from a
#            title alone. In practice this bucket is used almost entirely by
#            the AI analysis path (ai_analyze(), which reads the FULL
#            content) — the keyword-only fallback below defaults to Low
#            whenever no High signal is found, since "cannot be determined"
#            isn't something a short title can reliably signal on its own.
# ---------------------------------------------------------------------------

HIGH_KEYWORDS = [
    # Validation changes
    "validation", "validation rule", "validation check", "new validation", "revised validation",
    # Rule changes (including rate/procedure/deadline changes — all are rule changes)
    "rule change", "new rule", "revised rule", "amended rule", "amendment", "amended",
    "notification no", "rate change", "rate revision", "mandatory", "mandate",
    "w.e.f", "with effect from", "effective from", "extension of", "deadline",
    "due date", "last date", "closure", "revised procedure", "new provision",
    # API changes, including new rules implemented specifically for an API
    "api change", "api update", "new api", "api mandatory", "schema change", "schema update", "schema",
]

LOW_KEYWORDS = [
    "gross and net gst revenue", "revenue collections", "press release",
    "toll free number", "toll-free number", "helpdesk", "helpline",
    "council structure", "website polic", "terms and condition", "hyperlink polic",
    "system requirement", "knowledge portal", "grievance",
    "gst collections", "gross gst", "net gst revenue", "gst revenue",
]

SOFTWARE_IMPACT_KEYWORDS = [
    "e-invoice", "e-invoicing", "irn", "e-way bill", "eway bill", "ewb",
    "gstr-1", "gstr-3b", "gstr-9", "gstr1", "gstr3b", "gstr9", "gstr-2b",
    "return filing", "return-filing", "filing procedure", "schema", "api",
    "ship-to", "ship to gstin", "mandatory capture", "validation",
    "threshold", "rate change", "rate revision", "hsn", "classification",
    "registration", "aato", "aggregate annual turnover", "input tax credit", "itc",
    "invoice reference number", "two-factor authentication", "2fa",
    "portal login", "login procedure", "offline utility", "ims offline",
]


def classify_notification(title, summary=""):
    """Keyword-only fallback classifier (used when AI analysis is
    unavailable or fails). High = validation/rule/API change (see
    HIGH_KEYWORDS). Everything else defaults to Low, per the rule that Low
    covers "regular circulars / explanations / other notifications not
    related to High priority" — Medium is intentionally rare here, since a
    title alone rarely gives a reliable signal of genuine ambiguity; that
    nuance is handled by ai_analyze() reading the full text instead."""
    text = f"{title} {summary}".lower()

    software_impact = any(kw in text for kw in SOFTWARE_IMPACT_KEYWORDS)
    is_high = software_impact or any(kw in text for kw in HIGH_KEYWORDS)

    if is_high:
        return "High", software_impact
    return "Low", software_impact


def to_regulatory_update(title, href, dept, category, priority, item_date, needs_review=False):
    from datetime import date
    clean_title = title.strip()
    classified_priority, software_impact = classify_notification(clean_title)
    return {
        "department": dept,
        "category": category,
        "title": clean_title,
        "summary": clean_title,
        "priority": classified_priority,  # was: hardcoded "Medium" for every scraped item
        "softwareImpact": software_impact,
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
        extracted_date = extract_date_from_text(context)
        items.append(to_regulatory_update(
            title=context[:200], href=urljoin(CBIC_HOME, a["href"]),
            dept="CBIC", category="Order", priority="Medium",
            item_date=extracted_date, needs_review=extracted_date is None,
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


def capture_page_text(driver, url):
    """Navigates to url (a non-PDF page, e.g. a GSTN advisory detail page)
    and returns its visible text, for feeding to ai_analyze() below. Best
    effort — returns "" on failure rather than raising."""
    try:
        driver.get(url)
        time.sleep(3)
        return driver.find_element("tag name", "body").text
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"  [debug] page text capture failed for {url}: {exc}")
        return ""


def extract_pdf_text(url, max_chars=6000):
    """Downloads a PDF and extracts its text (first 5 pages, which is
    plenty for a notification/circular/advisory). Returns "" on any
    failure — a missing PDF text extraction falls back to keyword
    classification, it never breaks the run."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        import io
        import pdfplumber
        text_parts = []
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            for page in pdf.pages[:5]:
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        return "\n".join(text_parts)[:max_chars]
    except Exception as exc:  # noqa: BLE001 — best-effort; missing PDF text is fine
        print(f"  [debug] PDF text extraction failed for {url}: {exc}")
        return ""


# ---------------------------------------------------------------------------
# AI-based analysis — reads the FULL notification text (not just the
# title) and asks Claude to classify it and write a real summary. Falls
# back to the keyword-based classify_notification() if no API key is
# configured, the call fails, or the response can't be parsed — this is
# an upgrade layered on top of the keyword classifier, never a hard
# dependency on it.
# ---------------------------------------------------------------------------
def ai_analyze(title, full_text):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not full_text.strip():
        priority, impact = classify_notification(title, full_text)
        return {"priority": priority, "softwareImpact": impact, "summary": title, "keyChanges": [], "publishedDate": None, "source": "keyword-fallback"}

    prompt = f"""You are analyzing a GST/CBIC regulatory notification for an internal compliance dashboard used by a tax/accounting team.

Title: {title}

Full text of the notification:
{full_text[:6000]}

Based on the ACTUAL CONTENT above (not just the title), respond with ONLY a JSON object, no other text, no markdown code fences, with exactly these fields:
- "priority": "High", "Medium", or "Low". High = a validation change, a rule change, or an API change — including any new rule implemented for an API. Low = a regular circular detailing an explanation, or any other notification not related to a High-priority change (this is the default for anything that isn't clearly High). Medium = only when it genuinely cannot be determined as either Low or High after reading the full text.
- "softwareImpact": true or false. True only if this genuinely requires or is likely to require a change in accounting/return-filing software behavior (e.g. e-Invoice, e-Way Bill, GSTR forms, IRN, registration, schema, or validation changes).
- "summary": a genuine 2-3 sentence plain-English summary of what this notification actually says, based on the real content.
- "keyChanges": an array of up to 4 short strings describing the specific changes, if any are stated.
- "publishedDate": the date this notification was actually published/dated, in YYYY-MM-DD format, if the text states one (e.g. "dated 24.06.2026" or "31st March 2026"). Use null if no date is stated anywhere in the text.
"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw_text = resp.json()["content"][0]["text"].strip()
        # Strip markdown code fences if the model added them despite instructions.
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        parsed = json.loads(raw_text.strip())
        priority = parsed.get("priority") if parsed.get("priority") in {"High", "Medium", "Low"} else "Medium"
        # Validate the AI's date claim looks like a real YYYY-MM-DD string
        # before trusting it — never let a malformed value through.
        published_date = parsed.get("publishedDate")
        if published_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(published_date)):
            published_date = None
        return {
            "priority": priority,
            "softwareImpact": bool(parsed.get("softwareImpact", False)),
            "summary": (parsed.get("summary") or title).strip(),
            "keyChanges": list(parsed.get("keyChanges", []))[:4],
            "publishedDate": published_date,
            "source": "ai",
        }
    except Exception as exc:  # noqa: BLE001 — AI analysis is an enhancement, never a hard dependency
        print(f"  [debug] AI analysis failed for {title[:50]!r}, falling back to keywords: {exc}")
        priority, impact = classify_notification(title, full_text)
        return {"priority": priority, "softwareImpact": impact, "summary": title, "keyChanges": [], "publishedDate": None, "source": "keyword-fallback"}


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
    print("=== SCRIPT VERSION: v11-fix-view-details-and-dates ===")

    # Dedicated test mode — sends one fake item straight to send_email_alert(),
    # completely bypassing scraping. This is the reliable way to test the
    # actual email path, since "delete an entry and hope the scraper
    # rediscovers it" only works for entries still within CBIC/GSTN's
    # current live listing — older entries won't come back that way.
    if os.environ.get("TEST_EMAIL_MODE") == "1":
        print("=== TEST EMAIL MODE — sending one fake item, no real scraping ===")
        fake_item = {
            "department": "GSTN",
            "category": "Notification",
            "title": "TEST — this is a test email, not a real update",
            "summary": "TEST — this is a test email, not a real update",
            "priority": "Medium",
            "source": "https://services.gst.gov.in/services/advisory/advisoryandreleases",
            "keyChanges": [],
            "effectiveDate": None,
            "actionRequired": "This is a test — no action needed.",
            "date": "2026-07-13",
            "_needsReview": False,
        }
        send_email_alert([fake_item], {})
        return

    # One-time reclassification mode — retroactively runs the keyword
    # classifier (classify_notification) against every EXISTING item's
    # title, filling in priority and softwareImpact for entries scraped
    # before that logic existed. Uses the keyword classifier, not the full
    # AI analysis — re-running AI analysis on old items would mean
    # re-fetching every old PDF/page's content again, which is slower and
    # unnecessary just to backfill a missing field. New items scraped from
    # here on still get the full AI treatment as normal.
    if os.environ.get("RECLASSIFY_MODE") == "1":
        print("=== RECLASSIFY MODE — backfilling priority/softwareImpact on existing items, no scraping ===")
        existing_items = load_existing()
        changed = 0
        for item in existing_items:
            priority, impact = classify_notification(item.get("title", ""), item.get("summary", ""))
            if item.get("priority") != priority or item.get("softwareImpact") != impact:
                changed += 1
            item["priority"] = priority
            item["softwareImpact"] = impact
        save_data(existing_items)
        print(f"Reclassified {len(existing_items)} existing item(s), {changed} changed. data.json updated.")
        return

    # Full refresh mode — genuinely erases and redownloads. Instead of
    # merging new findings into the existing dataset (the normal
    # behavior), this ignores everything already saved and rebuilds
    # data.json from scratch using only what's currently live on
    # CBIC/GSTN right now. Every item goes through full AI analysis again
    # (not just genuinely-new ones), since nothing is treated as
    # "already known." This is more expensive than a normal run (more AI
    # calls, more screenshots) but still cheap in absolute terms at this
    # volume — see Cost_Analysis.docx for context. Note: this can ONLY
    # ever contain what CBIC/GSTN currently show as "latest" — it cannot
    # recover older items that have scrolled off those live pages, the
    # same limitation that caused data loss during earlier testing.
    full_refresh = os.environ.get("FULL_REFRESH_MODE") == "1"
    if full_refresh:
        print("=== FULL REFRESH MODE — erasing and redownloading everything from CBIC/GSTN live pages ===")

    existing = [] if full_refresh else load_existing()
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

    # Enrich each genuinely new item with full-text AI analysis (reading the
    # actual PDF/page content, not just the title) — BEFORE saving, so
    # data.json itself reflects the real classification and summary, not
    # just what gets shown in the email. Falls back to the keyword
    # classifier per-item if ANTHROPIC_API_KEY isn't set or a call fails.
    for item in deduped:
        source = item["source"]
        try:
            if source.lower().endswith(".pdf"):
                full_text = extract_pdf_text(source)
            else:
                if driver is None:
                    driver = create_driver()
                full_text = capture_page_text(driver, source)
        except Exception as exc:  # noqa: BLE001 — a failed text capture just means keyword fallback
            print(f"  [debug] could not capture full text for {source}: {exc}")
            full_text = ""

        analysis = ai_analyze(item["title"], full_text)
        item["priority"] = analysis["priority"]
        item["softwareImpact"] = analysis["softwareImpact"]
        item["summary"] = analysis["summary"]
        item["keyChanges"] = analysis["keyChanges"]
        if analysis.get("publishedDate"):
            item["date"] = analysis["publishedDate"]
            item["effectiveDate"] = item.get("effectiveDate") or analysis["publishedDate"]
        print(f"  [debug] analyzed {item['title'][:50]!r}: priority={analysis['priority']}, softwareImpact={analysis['softwareImpact']}, date={item['date']}, via={analysis['source']}")

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
