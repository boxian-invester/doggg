# 台股智研 AI v6.1 — Supabase PostgreSQL + incremental scan

This release retains **all v5 endpoints and frontend files**. Adds official TWSE/TPEx company universe import, bounded Yahoo Finance adjusted daily OHLCV ingestion, PostgreSQL storage, database-backed score scan and GitHub Actions scheduling. **It does not yet deliver full-history full-market coverage, intraday signals, next-day probabilities or verified day-trading win rates.**

## 1. Create Supabase database

1. Create a Supabase project at https://supabase.com and keep its database password private.
2. Open **SQL Editor** → New query. Paste and run `sql/001_init.sql` once.
3. Open **Connect** → choose a PostgreSQL connection string. Prefer a compatible **session pooler** URI if your environment cannot reach IPv6 direct DB addresses. Include `sslmode=require` and substitute the actual database password. Supabase connection details may differ by project.
4. Do **not** paste the DB password into the website, repository, or chat. Do not use a public `anon` API key as a DB password.

## 2. GitHub Actions secret and run

1. GitHub repository → **Settings → Secrets and variables → Actions → New repository secret**.
2. Name: `DATABASE_URL`. Value: your PostgreSQL URI from Supabase Connect.
3. Upload **all files**, including hidden `.github/workflows/daily_scan.yml`, `sql/`, `scripts/`, `db.py`, `static/`. In Finder press `Command + Shift + .` to reveal hidden files. For reliable hidden folder upload, use `git` CLI instead (see below).
4. **Actions → Taiwan Stock Daily Batch → Run workflow**, mode `all`, batch_size `30` initially. Check the log. The first run only imports official stock names and at most 30 symbols' history. Subsequent runs incrementally expand coverage.
5. Scheduled workflow runs 15:40 and 08:15 Taipei time on weekdays, but GitHub Actions schedules can be late or skipped; there is **no guaranteed 08:45 report**. Weekend/holiday checks and market calendar validation are future work.

### Scaling and free-plan limitations

- Default 60 stocks per run (two scheduled runs on weekdays). Thousands of stocks will require many runs, and free database storage/API quotas may be exhausted. Track `/api/v6/status` and Supabase disk usage.
- Historical bars from Yahoo Finance are unofficial **adjusted** daily prices. Their OHLCV and corporate action handling must be validated against official data before trading use. Avoid mass parallel scraping; respect provider rate limits and licensing.
- The TWSE/TPEx open-data schemas and endpoints may change. The pipeline reports failures rather than inventing missing stocks. Do not treat an incomplete imported universe as exhaustive.
- Scan requires >=70 valid daily bars and data within 4 calendar days of the latest stored bar. A stock with missing or stale data is excluded, not ranked.
- Ranking is a deterministic 0–100 **technical condition score**. It is not a forecast probability.
- Daily batch ingestion and scanning may take longer than free GitHub Actions limits. Reduce batch size if needed.

## 3. Render environment and deployment

1. Keep the existing Render Docker Web Service and GitHub repository.
2. Render → service → **Environment** → add `DATABASE_URL` with the same private PostgreSQL URI. Save and redeploy.
3. Confirm Dockerfile is `./Dockerfile`; root directory should be empty if files are at repository root.
4. Test:
   - `https://doggg.onrender.com/api/health` — original health endpoint
   - `https://doggg.onrender.com/api/v5/scan?limit=5` — original v5 scan
   - `https://doggg.onrender.com/api/v6/status` — DB coverage
   - `https://doggg.onrender.com/api/v6/scan?limit=5` — latest DB-backed rank
5. The v5 homepage is unchanged. v6 status and scan endpoints are available via `/docs`; future v6.2 can integrate a dashboard widget.

## 4. Mac terminal upload (reliable way to preserve .github)

```bash
cd ~/Downloads
unzip taiwan-stock-intelligence-v6.1.zip -d tsi-v6
cd tsi-v6
# Upload files to your EXISTING GitHub repository using git, not a ZIP rename.
# If you already have the repo cloned, copy these files into its root, then:
git add -A
git commit -m "Add Supabase batch scanner v6.1"
git push origin main
```

**Important:** `git add` only works in a Git repository. If the extracted folder is not your cloned repo, first clone the existing repository, then copy the extracted contents into the clone (including `.github`), commit and push. Never push `.env` or DB credentials.

## 5. Manual local execution

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql://...private-connection-string...'
python -m scripts.pipeline --mode universe
python -m scripts.pipeline --mode ingest --limit 30 --period 1y
python -m scripts.pipeline --mode scan
uvicorn main:app --reload
```

## 6. Known gaps / operational security

- No automatic reliable 08:45 publishing, no alerting, no model of next-day direction, no verified day-trading win probability, no executable intraday order prices.
- v6 APIs are read-only. No endpoint permits arbitrary SQL or triggers bulk downloads. Supabase RLS is enabled with no public policies; PostgreSQL server credentials stay in Render and GitHub Secrets.
- Do not publish the DATABASE_URL or share screenshots showing its password. Rotate it immediately if exposed.
- Public read endpoints show research results and may need rate limiting if traffic grows.
