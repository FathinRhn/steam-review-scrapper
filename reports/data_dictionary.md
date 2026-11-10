# Data Dictionary — Delta Force English Steam Reviews

One row per English review of *Delta Force* (Steam app **2507950**).

| Column | Type | Description |
|---|---|---|
| `review_id` | string | Steam `recommendationid` (API mode) or a deterministic `html-...` surrogate key (HTML fallback). Unique per row. |
| `app_id` | integer | Steam application ID (2507950). |
| `language` | string | Review language reported by Steam, or the language filter used in HTML mode. Always `english` in this dataset. |
| `review_text` | string | Original review text, untouched (may contain BBCode/HTML). |
| `review_clean` | string | Review text with BBCode/HTML stripped and whitespace normalised. Use this for NLP. |
| `voted_up` | boolean | `True` = Recommended, `False` = Not Recommended. |
| `votes_up` | integer | Number of users who marked the review helpful. `0` in HTML mode when the page shows no votes. |
| `votes_funny` | integer | Funny votes (API only; empty in HTML fallback). |
| `comment_count` | integer | Number of comments on the review. |
| `weighted_vote_score` | float | Steam's helpfulness score (API only; empty in HTML fallback). |
| `playtime_forever_hours` | float | Author's total playtime in hours at scrape time. |
| `playtime_at_review_hours` | float | Playtime when the review was written (API only). |
| `author_num_games_owned` | integer | Number of games owned by the author (when shown). |
| `author_num_reviews` | integer | Number of reviews written by the author (API only). |
| `steam_purchase` | boolean | Whether the copy came from a Steam purchase (API only). |
| `received_for_free` | boolean | Review marked "Product received for free". |
| `written_during_early_access` | boolean | Review written while the game was in Early Access. |
| `primarily_steam_deck` | boolean | Review written on Steam Deck (API only). |
| `created_at_utc` | ISO 8601 string | Review creation time (UTC). Exact in API mode; day-precision inference in HTML mode. |
| `updated_at_utc` | ISO 8601 string | Last edit time (API only). |
| `author_id_hash` | string | First 16 hex characters of SHA-256(SteamID). Used for dedup/privacy; the raw SteamID is not exported. |
| `scraped_at_utc` | ISO 8601 string | When the row was downloaded. |
| `date_precision` | string | `exact`, `exact_day`, `inferred_year`, `missing`, or `unparsed`. |
| `lang_guess` | string | Optional `langdetect` result; empty unless `--detect-language` is used. |
| `source` | string | `api` or `html` (which endpoint produced the row). |

## Notes

- Steam's public endpoint returns at most 100 reviews per request.
- In API mode the crawl uses `filter=recent`, so it walks the full history.
- The HTML fallback renders 10 reviews per request and does not expose exact
  timestamps or all author statistics; those columns are empty for `html` rows.
- `review_text` is kept for auditability. Do not publish it in full without
  checking Steam's terms and your institution's ethics rules.
