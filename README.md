# GST Regulatory Dashboard — Fully Hosted on GitHub (No Machine Required)

This version needs **no computer of yours to stay on**. GitHub itself runs the
scraper on a schedule and hosts the dashboard. Once set up, it runs
indefinitely with zero ongoing action from you.

```
.github/workflows/scrape.yml   → runs scrape_and_notify.py every hour, automatically
scrape_and_notify.py            → scrapes cbic-gst.gov.in, updates data.json, emails alerts
data.json                       → the data — just a file, committed by the Action
subscribers.json                → who gets emailed — edit + commit to change
dashboard.html                  → the dashboard itself, served by GitHub Pages
```

## What's different from the local version

| | Local (`python app.py`) | This version |
|---|---|---|
| Needs your computer on | Yes | **No** — runs on GitHub's servers |
| "Refresh" button | Live re-scrape on click | Reloads the last scheduled result (scraping itself only happens on the hourly schedule) |
| Email subscribe | Self-service box in the UI | Edit `subscribers.json` directly and commit |
| Email provider | Your own Gmail (needs 2-Step Verification + App Password) | **Brevo** (free, no 2-Step Verification required) |

## One-time setup (about 15 minutes)

### 1. Create the repository
1. Go to **github.com** → sign in (or create a free account)
2. Click **New repository** → name it (e.g. `gst-dashboard`) → **Public** (required for free GitHub Pages + unlimited free Actions minutes) → Create
3. Upload every file in this folder to that repository (drag-and-drop works on the repo's main page, or use `git push` if you're comfortable with Git)

### 2. Turn on GitHub Pages
1. In the repo, go to **Settings → Pages**
2. Under "Build and deployment", set **Source** to "Deploy from a branch"
3. Branch: `main`, folder: `/ (root)` → Save
4. After a minute, your dashboard is live at:
   ```
   https://<your-username>.github.io/<repo-name>/dashboard.html
   ```
   That's your permanent link — bookmark it, share it.

### 3. Set up free email (Brevo — no 2-Step Verification)
1. Go to **brevo.com** → **Sign up free** (just an email + password — no forced 2FA)
2. Verify your email address (a normal confirmation link, not 2FA)
3. In Brevo, go to **SMTP & API** (usually under your account/company name menu, top right)
4. Under "SMTP", note your **SMTP login** and generate/copy your **SMTP key**
5. Go to **Senders** in Brevo and add/verify the email address you want alerts to come *from* (a simple email confirmation, not 2FA)

### 4. Add your Brevo credentials as GitHub Secrets
1. In your repo: **Settings → Secrets and variables → Actions → New repository secret**
2. Add three secrets:
   - `BREVO_SMTP_LOGIN` → your Brevo SMTP login
   - `BREVO_SMTP_KEY` → your Brevo SMTP key
   - `BREVO_SENDER_EMAIL` → the verified "from" address
3. These are encrypted by GitHub and never appear in logs or the repo itself.

### 5. Add who gets emailed
Edit `subscribers.json` directly on GitHub (click the file → pencil icon to edit) and commit:
```json
{
  "emails": ["you@example.com", "colleague@example.com"]
}
```

### 6. Turn on the schedule
1. Go to the **Actions** tab in your repo
2. If prompted, click **"I understand my workflows, go ahead and enable them"**
3. That's it — it now runs automatically every hour, forever, with no further action from you.

### 7. Test it right now instead of waiting an hour
1. Go to **Actions** → **Scrape GST/CBIC updates and email alerts** (left sidebar)
2. Click **Run workflow** → **Run workflow** (green button)
3. Watch it run (takes under a minute); check the logs for what it found
4. Refresh your dashboard URL from step 2 to see the result

## Cost

| Piece | Cost |
|---|---|
| GitHub repo + Actions (public repo) | $0 — 2,000+ free minutes/month, this uses ~1 min/hour = ~730 min/month, comfortably inside the free tier |
| GitHub Pages hosting | $0 — free, unlimited, for public repos |
| Email (Brevo) | $0 — 300 emails/day free, forever, no card |
| **Total** | **$0/month**, indefinitely, with nothing running on any machine of yours |

## Known limitations (same as the local version)

- Only the CBIC scraper runs here — the GST-portal (NIC advisories) scraper needs a headless browser and hasn't been adapted to this version. It can be added later as a second, separate job if useful.
- The "Refresh" button reloads the latest scheduled result — it does not trigger an instant live scrape (there's no server to ask for one). If you need to force an immediate check, use the manual "Run workflow" button in step 7 above.
- Subscribing is now a file edit + commit, not a self-service form — a trade-off of not having a live backend.
