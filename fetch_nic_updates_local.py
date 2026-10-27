"""
fetch_nic_updates_local.py
-------------------------------------------------------------------------
Standalone companion to scrape_and_notify.py, for NIC e-Invoice and
e-Way Bill announcements specifically.

WHY THIS IS SEPARATE FROM scrape_and_notify.py: that script runs on
GitHub Actions' hosted runners, and both NIC hosts block those runners
outright —
    - docs.ewaybillgst.gov.in: Akamai's edge WAF returns a literal
      "Access Denied" page (errors.edgesuite.net) to the runner's IP,
      even from a real Selenium/Chrome browser — almost certainly an
      IP-reputation block against well-known datacenter ranges.
    - einv-apisandbox.nic.in: the connection times out at the TCP level
      — no response at all, consistent with IP-allowlisting on a
      developer "sandbox" subdomain meant for registered GSPs/ASPs.
Both are confirmed whole-domain blocks (announcements.html AND
release-notes.html fail identically), not anything scraper code can
route around from that environment.

This script exists to run somewhere that ISN'T blocked — your own
machine, an office network, or a self-hosted GitHub Actions runner —
reusing the actual parsing logic from scrape_and_notify.py (imported,
not duplicated) so there's one source of truth for how a NIC
announcement gets turned into a dashboard item.

USAGE
    python fetch_nic_updates_local.py                 # dry run: scrape + report, don't touch data.json
    python fetch_nic_updates_local.py --apply          # scrape + write data.json locally
    python fetch_nic_updates_local.py --apply --push   # also git commit + push the result

REQUIREMENTS
    Same as scrape_and_notify.py: pip install -r requirements.txt
    Plus a real Chrome/Chromium installed locally — Selenium 4's
    built-in Selenium Manager will download a matching chromedriver
    automatically the first time this runs, as long as this machine
    has normal internet access (unlike the GitHub Actions runner, it
    should for these two specific hosts).

SCHEDULING IT DAILY
    Linux/Mac (cron) — run at 8:15am every day, from the repo directory:
        15 8 * * * cd /path/to/GST-Government-Regulatory-Updates- && /usr/bin/python3 fetch_nic_updates_local.py --apply --push >> fetch_nic_updates.log 2>&1

    Windows (Task Scheduler):
        Program/script:  python
        Arguments:        fetch_nic_updates_local.py --apply --push
        Start in:         C:\\path\\to\\GST-Government-Regulatory-Updates-
        Trigger:          Daily, at a time of your choosing

    Either way, this needs to run against a checkout that can push to
    whichever branch the live dashboard actually reads data.json from
    (this repo's hourly automation pushes straight to `main` — see
    scrape.yml — so for parity, point this at a `main` checkout and
    pass --push, the same direct-to-main convention the hourly job
    already uses).
-------------------------------------------------------------------------
"""

import argparse
import subprocess
import sys

from scrape_and_notify import (
    NIC_EINVOICE_ANNOUNCEMENTS_URL,
    NIC_EWAYBILL_ANNOUNCEMENTS_URL,
    create_driver,
    load_existing,
    save_data,
    scrape_nic_announcements,
)


def assign_ids(existing_items, new_items):
    """Same logic as scrape_and_notify.main()'s nested assign_ids — kept
    in sync by hand since that one isn't importable (it's nested)."""
    used_ids = {u["id"] for u in existing_items if u.get("id") is not None}
    next_id = (max(used_ids) + 1) if used_ids else 1
    for u in existing_items:
        if u.get("id") is None:
            u["id"] = next_id
            used_ids.add(next_id)
            next_id += 1
    for u in new_items:
        u["id"] = next_id
        used_ids.add(next_id)
        next_id += 1


def run_git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    print(f"  [git] {' '.join(args)}: {result.returncode}")
    if result.stdout.strip():
        print(f"  [git stdout] {result.stdout.strip()}")
    if result.stderr.strip():
        print(f"  [git stderr] {result.stderr.strip()}")
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write results to data.json (default: dry run only).")
    parser.add_argument("--push", action="store_true", help="git add/commit/push data.json after writing it. Implies --apply.")
    args = parser.parse_args()
    apply_changes = args.apply or args.push

    existing = load_existing()
    existing_sources = {u["source"] for u in existing}
    print(f"Loaded {len(existing)} existing item(s) from data.json.")

    driver = None
    try:
        driver = create_driver()
    except Exception as exc:  # noqa: BLE001 — fall back to requests-only if Chrome isn't set up locally
        print(f"Could not start a local Chrome driver ({exc}); e-Way Bill will fall back to requests.get(), which may not be enough on its own.")

    new_items = []
    try:
        einvoice_items = scrape_nic_announcements(NIC_EINVOICE_ANNOUNCEMENTS_URL, "NIC e-Invoice")
        print(f"NIC e-Invoice: found {len(einvoice_items)} item(s) on the page.")
        new_items += einvoice_items
    except Exception as exc:  # noqa: BLE001 — one source failing shouldn't stop the other
        print(f"NIC e-Invoice scrape failed: {exc}")

    try:
        ewaybill_items = scrape_nic_announcements(NIC_EWAYBILL_ANNOUNCEMENTS_URL, "NIC e-Way Bill", driver=driver)
        print(f"NIC e-Way Bill: found {len(ewaybill_items)} item(s) on the page.")
        new_items += ewaybill_items
    except Exception as exc:  # noqa: BLE001 — one source failing shouldn't stop the other
        print(f"NIC e-Way Bill scrape failed: {exc}")
    finally:
        if driver is not None:
            driver.quit()

    deduped = [item for item in new_items if item["source"] not in existing_sources]
    assign_ids(existing, deduped)
    merged = existing + deduped

    print(f"Added {len(deduped)} new item(s). data.json would have {len(merged)} total.")
    for item in deduped:
        print(f"  NEW: [{item['department']}] {item['date']} — {item['title']}")

    if not deduped:
        print("Nothing new — exiting.")
        return

    if not apply_changes:
        print("Dry run (no --apply) — data.json NOT written. Re-run with --apply to write it, or --apply --push to also commit.")
        return

    save_data(merged)
    print("data.json written.")

    if args.push:
        run_git("add", "data.json")
        committed = run_git("commit", "-m", f"Add {len(deduped)} NIC e-Invoice/e-Way Bill update(s)")
        if committed:
            if not run_git("push"):
                print("Push failed — check git output above (likely needs `git pull` first, or auth isn't configured on this machine).")
                sys.exit(1)
        else:
            print("Nothing to commit (data.json may already have matched what's in git).")


if __name__ == "__main__":
    main()
