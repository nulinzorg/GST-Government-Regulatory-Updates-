--- pristine/scrape_and_notify.py	2026-07-15 10:15:38.000000000 +0000
+++ scrape_and_notify.py	2026-07-15 11:12:22.306865322 +0000
@@ -51,16 +51,41 @@
 # style above, and previously only the numeric one was recognized. A missed
 # date silently fell back to today's scrape date instead of the real
 # notification date — this covers the other common phrasing.
+#
+# BUG FIX: this used to match a bare date-shaped substring ANYWHERE in the
+# text with no anchor word, so phrases like "...GST Council in its 55th
+# meeting held on 21st December, 2024..." or "...31st MARCH 2026 IS THE DUE
+# DATE FOR..." were misread as this notification's own date, when they're
+# actually a meeting date / due date mentioned in passing. Requiring the
+# same "dated" anchor as the numeric pattern above fixes this.
 MONTH_NAMES = {
     "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
     "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
 }
 WORDED_DATE_RE = re.compile(
-    r"(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTH_NAMES.keys()) + r")\.?,?\s+(\d{4})",
+    r"dated\s+(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTH_NAMES.keys()) + r")\.?,?\s+(\d{4})",
+    re.IGNORECASE,
+)
+
+# BUG FIX: even with the "dated" anchor above, a phrase like "Amendment to
+# Circular No. 31/05/2018-GST, dated 9th February, 2018" or "As per the
+# Notification No. 05/2025-Central Excise (N.T.) dated 31.12.2025" IS
+# anchored by "dated" — but the date belongs to the OLDER document being
+# cited/amended, not to this notification. Only skip the match when it's
+# introduced by an explicit reference phrase like these; a bare "Advisory
+# No. 11/2026 dated 24.06.2026" (no such lead-in) is the document
+# identifying itself, and its date should be trusted.
+REFERENCE_LEADIN_RE = re.compile(
+    r"(amendment\s+to|as\s+per\s+the|in\s+supersession\s+of|pursuant\s+to|read\s+with|in\s+terms\s+of|as\s+per\s+sub)",
     re.IGNORECASE,
 )
 
 
+def is_citation_date(text, match_start):
+    preceding = text[max(0, match_start - 90):match_start]
+    return bool(REFERENCE_LEADIN_RE.search(preceding))
+
+
 # ---------------------------------------------------------------------------
 # Scraping (same logic as the local version — see scrape_gst_updates.py for
 # the fuller, commented original this was adapted from)
@@ -83,14 +108,15 @@
 
 def extract_date_from_text(text):
     """Tries the numeric 'dated DD.MM.YYYY' pattern first, then the worded
-    '31st March 2026' style, returning the first real date found, or None
-    if neither matches — in which case the caller falls back to today's
-    scrape date rather than guessing further."""
+    'dated 31st March 2026' style, returning the first real date found, or
+    None if neither matches (or the only match found is a reference to a
+    different, cited document) — in which case the caller falls back to
+    today's scrape date rather than guessing further."""
     numeric_match = ADVISORY_DATE_RE.search(text)
-    if numeric_match:
+    if numeric_match and not is_citation_date(text, numeric_match.start()):
         return parse_dd_mm_yyyy(numeric_match)
     worded_match = WORDED_DATE_RE.search(text)
-    if worded_match:
+    if worded_match and not is_citation_date(text, worded_match.start()):
         return parse_worded_date(worded_match)
     return None
 
@@ -179,6 +205,14 @@
 
 
 def to_regulatory_update(title, href, dept, category, priority, item_date, needs_review=False):
+    # BUG FIX: this dict never included an "id" field at all, for any item,
+    # ever — every dashboard that opens the detail panel by looking up
+    # `ALL_UPDATES.find(x => x.id === id)` was matching against `undefined`
+    # for every single scraped item, so View Details always resolved to the
+    # first undefined-id item in the array regardless of which row was
+    # clicked. Real, unique ids are assigned once, at merge time (see
+    # assign_ids below), since that's the one place that can see the full
+    # existing dataset and guarantee no collisions.
     from datetime import date
     clean_title = title.strip()
     classified_priority, software_impact = classify_notification(clean_title)
@@ -241,10 +275,19 @@
                 if title_text.lower() in {"english", "hindi", "हिंदी"}:
                     rejected_junk += 1
                     continue
+                # BUG FIX: this used to hardcode item_date=None, which silently
+                # fell back to today's scrape date for every "What's New" item —
+                # even though most of these titles state the real date directly
+                # (e.g. "...Advisory No. 11/2026 dated 24.06.2026..."). Run the
+                # same regex extraction used for ticker items against the title
+                # text so the real notification date is captured whenever it's
+                # present, independent of whether AI analysis (which can also
+                # backfill this later) ever runs.
+                extracted_date = extract_date_from_text(title_text)
                 items.append(to_regulatory_update(
                     title=title_text, href=urljoin(CBIC_HOME, href),
                     dept="CBIC", category="Notification", priority="Medium",
-                    item_date=None, needs_review=True,
+                    item_date=extracted_date, needs_review=extracted_date is None,
                 ))
             print(f"  [debug] What's New: {len(items)} accepted, rejected: {rejected_pdf} (no /pdf/), {rejected_words} (too few words), {rejected_junk} (english/hindi)")
             if sample_rejected_hrefs:
@@ -735,6 +778,26 @@
 
     deduped = [item for item in new_items if item["source"] not in existing_sources]
 
+    # BUG FIX: backfill ids on any pre-existing items that predate this fix
+    # (e.g. items already sitting in data.json without one), then assign
+    # fresh sequential ids to genuinely new items. Never reuse or renumber
+    # an id that's already present — existing dashboards/bookmarks may
+    # depend on it staying stable across runs.
+    def assign_ids(existing_items, new_items_):
+        used_ids = {u["id"] for u in existing_items if u.get("id") is not None}
+        next_id = (max(used_ids) + 1) if used_ids else 1
+        for u in existing_items:
+            if u.get("id") is None:
+                u["id"] = next_id
+                used_ids.add(next_id)
+                next_id += 1
+        for u in new_items_:
+            u["id"] = next_id
+            used_ids.add(next_id)
+            next_id += 1
+
+    assign_ids(existing, deduped)
+
     # Enrich each genuinely new item with full-text AI analysis (reading the
     # actual PDF/page content, not just the title) — BEFORE saving, so
     # data.json itself reflects the real classification and summary, not
