# Delta Force — English Steam Review Scraper

A small, reproducible academic project that collects **English-language Steam
reviews for *Delta Force*** (Team Jade / TiMi Studio Group, App ID **2507950**)
and turns them into an analysis-ready CSV.

Built for the Data Mining final project (Proyek UAS). The code is deliberately
split into four short scripts so each stage can be run, checked, and explained
on its own.

---

## 1. What it produces

| File | Contents |
|---|---|
| `data/raw/reviews_raw.jsonl` | Append-only raw crawl, one API-shaped review object per line. |
| `data/state/crawl_state_<app_id>.json` | Separate cursor / progress checkpoint for each game. |
| `data/state/api_summary_<app_id>.json` | Steam's per-game `query_summary` totals. |
| `data/processed/steam_reviews_all_languages.csv` | Combined dataset with an `app_id` and language column. |
| `reports/data_dictionary.md` | Column-by-column documentation. |
| `reports/cleaning_summary.txt` | Counts of dropped/duplicate/non-English rows. |
| `reports/validation_summary.txt` | Pass/fail QA report with manual spot-check samples. |
| `reports/scrape_log.txt` | Timestamped crawl log. |

---

## 2. Requirements

- Python 3.10+ (tested on 3.13)
- Internet access to `store.steampowered.com` and `steamcommunity.com`
- Windows PowerShell, macOS, or Linux

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows
# source .venv/bin/activate          # macOS / Linux
pip install -r requirements.txt
```

If PowerShell blocks the activation script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

---

## 3. Quick start

Run every command from the project root.

```powershell
# 0) Confirm the configured App ID (Delta Force by default)
python src/verify_app.py

# 1) Crawl both games (auto-detects the endpoint separately per game)
python src/fetch_reviews.py

# Or crawl only one game
python src/fetch_reviews.py --game delta-force
python src/fetch_reviews.py --game battlefield-6

# 2) Build the clean CSV
python src/clean_reviews.py

# 3) Run QA checks
python src/validate_reviews.py
```

A short smoke test (3 pages) before committing to a full crawl:

```powershell
python src/fetch_reviews.py --max-pages 3
python src/clean_reviews.py
python src/validate_reviews.py
```

---

## 4. How the scraper works

### 4.1 API mode (preferred)

Steam's current `IUserReviewsService/GetAppReviews` endpoint returns up to 100
reviews per request. It replaces the deprecated Store endpoint. The API key is
optional for public reviews; an app-associated publisher key gets a higher
rate limit.

```
https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/
    ?key=<optional app-associated publisher key>
    &input_json={"appid":<app_id>,"filter":1,"languages":["all"],
                 "review_type":0,"purchase_type":1,
                 "filter_offtopic_activity":false,
                 "num_per_page":100,"cursor":"<cursor>"}
```

The first cursor is `*`; every response contains the cursor for the next page.
The crawl stops when Steam returns no reviews or the cursor stops changing.
The numeric filter values are Steam's `Recent`, `All`, and `All` enums.
This mode supplies exact timestamps, helpfulness scores, author statistics, and
the real `recommendationid`.

### 4.2 HTML fallback (automatic)

Some networks (including data-centre/VPN egress IPs) are redirected from the
JSON endpoint to the store home page. When that happens, `--mode auto` switches
to the public community pages:

```
https://steamcommunity.com/app/<app_id>/reviews/?browsefilter=mostrecent&p=1&filterLanguage=all
```

Pagination uses the hidden "next page" form, which posts to:

```
https://steamcommunity.com/app/<app_id>/homecontent/?userreviewscursor=...&userreviewsoffset=...
```

The fallback renders **10 reviews per request**, so it is slower, and a few
fields do not exist on those pages (exact timestamps, funny votes, playtime at
review, author review counts). The CSV keeps the same schema; unavailable
fields are empty and `source` is set to `html`.

Force a mode explicitly if needed:

```powershell
python src/fetch_reviews.py --mode api
python src/fetch_reviews.py --mode html --reset-state
```

The old API cursor is reset automatically when the scraper detects a state
created with the deprecated API. Existing raw review IDs remain and are skipped.

### 4.3 Resume after an interruption

Every page is written to disk and that game's cursor is checkpointed before
the next request. `Ctrl+C`, a timeout, or a closed laptop loses at most the
page that was in flight. Rerun the same command; existing IDs are skipped.

To start over deliberately:

```powershell
python src/fetch_reviews.py --reset-state
```

This deletes the selected game's checkpoint(s); existing raw rows stay in
place and their IDs are skipped. Delete `data/raw/reviews_raw.jsonl` manually
if you want a truly fresh crawl.

---

## 5. Politeness and rate limits

The scraper uses Steam's current public review Web API. A key is optional, but
an app-associated publisher key raises its rate limit. It requests reviews in
pages of 100 and follows Steam's cursor until there are no more pages.

- One request at a time, with a random 1.0–1.8 s delay.
- Descriptive `User-Agent` (set `SCRAPE_USER_AGENT` to include your e-mail).
- Automatic retry with exponential backoff on HTTP 429/5xx.
- A blocked API is **not** hammered: auto mode falls back to HTML after one
  redirect; explicit API mode stops with a clear message.
- No login, no CAPTCHA solving, no age-gate bypass.
- SteamIDs are hashed in the exported CSV. Keep the raw file private and do not
  republish full review text without checking Steam's terms and your
  institution's ethics rules.

---

## 6. Configuration

All settings live in `src/config.py` and can be overridden with environment
variables:

| Variable | Default | Meaning |
|---|---|---|
| `STEAM_GAME` | `delta-force` | Default game (`delta-force` or `battlefield-6`) |
| `REVIEW_LANGUAGE` | `all` | Steam language filter (`all` collects every language) |
| `STEAM_API_KEY` | empty | Optional Steamworks publisher key; higher rate limit when associated with the app |
| `SCRAPE_MIN_DELAY` | `1.0` | Minimum seconds between requests |
| `SCRAPE_MAX_DELAY` | `1.8` | Maximum seconds between requests |
| `SCRAPE_MAX_RETRIES` | `5` | Retries per request |
| `SCRAPE_MAX_PAGES` | `0` | Page cap (`0` = no cap) |
| `SCRAPE_USER_AGENT` | academic project string | HTTP User-Agent |

Set the key in the shell before running the crawler. In PowerShell:

```powershell
$env:STEAM_API_KEY = "your-key"
python src/fetch_reviews.py
```

---

## 7. Cleaning rules

`clean_reviews.py` performs these steps, in order, and reports every count in
`reports/cleaning_summary.txt`:

1. Skip malformed JSONL lines.
2. Drop records without a review ID.
3. Deduplicate review IDs (keep the most recently scraped copy).
4. Deduplicate identical text by the same author.
5. Keep reviews in every language (the API supplies a language label; HTML fallback may use `unknown`).
6. Drop reviews with fewer than 3 characters of text.
7. Strip Steam BBCode/HTML into `review_clean` (the raw text is preserved).
8. Convert timestamps to UTC, playtime to hours, votes to integers.
9. Hash SteamIDs (`author_id_hash`) and remove the raw ID from the CSV.
10. Sort newest-first and write UTF-8 CSV (with BOM so Excel opens it cleanly).

Optional slow check:

```powershell
python src/clean_reviews.py --detect-language
```

This adds a `lang_guess` column using `langdetect`. It is meant to **flag**
possibly mis-tagged reviews, not to delete them automatically.

---

## 8. Validation checklist

`validate_reviews.py` verifies:

- All expected columns exist and the dataset is not empty.
- `review_id` is unique.
- Every row is English and belongs to App ID 2507950 or 2807960.
- Required fields are present; dates are parseable and inside the game's
  lifetime.
- No duplicate author+text pairs.
- Row count is within 2% of Steam's own `total_reviews` (API mode only).
- Optional language guesses are mostly `en`.
- Prints three random rows for a manual spot-check.

A non-zero exit code means a critical check failed; read
`reports/validation_summary.txt` for details.

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Steam redirected the review API...` | Network is blocked from the JSON endpoint. Auto mode already switches to HTML. |
| `HTTP 429` repeatedly | You are being rate-limited. Stop, wait 30–60 minutes, raise `SCRAPE_MIN_DELAY`. |
| Crawl stops with "Cursor did not advance" | Steam returned a loop; progress is saved. Rerun later. |
| CSV has empty `votes_funny` / `playtime_at_review_hours` | Expected for `source=html` rows. |
| Dates are day-precision | Expected for HTML rows; Steam hides exact times on community pages. |
| `UnicodeEncodeError` in an old console | Run `chcp 65001` or set `PYTHONUTF8=1`. |
| `python` not found | Install Python and enable "Add Python to PATH". |

---

## 10. Project layout

```
Proyek UAS/
├─ src/
│  ├─ config.py            # constants and paths
│  ├─ common.py            # logging, JSON state, text/date helpers
│  ├─ verify_app.py        # confirm App ID + endpoint availability
│  ├─ fetch_reviews.py     # API crawler + HTML fallback, resumable
│  ├─ clean_reviews.py     # raw JSONL -> clean CSV + data dictionary
│  └─ validate_reviews.py  # QA checks and summary report
├─ data/                   # raw, state, processed (created at runtime)
├─ reports/                # logs, dictionary, summaries (created at runtime)
├─ requirements.txt
└─ README.md
```

---

## 11. Definition of done

- [ ] `verify_app.py` confirms the App ID and shows the expected game.
- [ ] Full crawls complete; each game's `crawl_state_<app_id>.json` has `"finished": true`.
- [ ] `steam_reviews_all_languages.csv` exists and includes both app IDs.
- [ ] `validation_summary.txt` reports **PASS** (warnings explained).
- [ ] Crawl date, row count, mode (`api`/`html`), and API parameters are
      recorded for the report.
- [ ] Raw JSONL is retained locally for auditability.
