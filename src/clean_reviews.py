"""Turn the raw JSONL crawl into an analysis-ready CSV.

Run after ``fetch_reviews.py``:

    python src/clean_reviews.py
    python src/clean_reviews.py --detect-language   # optional slow check

Outputs
-------
data/processed/delta_force_reviews_english.csv
reports/data_dictionary.md
reports/cleaning_summary.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from common import (  # noqa: E402
    clean_review_text,
    parse_posted_date,
    safe_bool,
    setup_logging,
    sha256_short,
    to_float,
    to_int,
)

LOGGER = setup_logging()

COLUMNS = [
    "review_id",
    "app_id",
    "language",
    "review_text",
    "review_clean",
    "voted_up",
    "votes_up",
    "votes_funny",
    "comment_count",
    "weighted_vote_score",
    "playtime_forever_hours",
    "playtime_at_review_hours",
    "author_num_games_owned",
    "author_num_reviews",
    "steam_purchase",
    "received_for_free",
    "written_during_early_access",
    "primarily_steam_deck",
    "created_at_utc",
    "updated_at_utc",
    "author_id_hash",
    "scraped_at_utc",
    "date_precision",
    "lang_guess",
    "source",
]

DATA_DICTIONARY = """# Data Dictionary — Delta Force English Steam Reviews

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
"""


def read_raw(path: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed = 0
    if not path.exists():
        return records, malformed

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(value, dict):
                records.append(value)
            else:
                malformed += 1
    return records, malformed


def build_row(record: dict[str, Any], reference: datetime) -> dict[str, Any] | None:
    author = record.get("author")
    if not isinstance(author, dict):
        author = {}

    review_text = record.get("review") or ""
    review_clean = clean_review_text(review_text)

    review_id = record.get("recommendationid") or record.get("_html_key")
    if not review_id:
        return None

    steamid = author.get("steamid") or record.get("author_steamid")

    timestamp = record.get("timestamp_created")
    created_at: datetime | None = None
    precision = record.get("timestamp_created_precision") or "missing"
    if timestamp not in (None, ""):
        try:
            created_at = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
            if precision == "missing":
                precision = "exact"
        except (ValueError, OSError, TypeError):
            created_at = None

    if created_at is None:
        created_at, precision = parse_posted_date(
            record.get("timestamp_created_text"), reference
        )

    updated_at: datetime | None = None
    updated_raw = record.get("timestamp_updated")
    if updated_raw not in (None, ""):
        try:
            updated_at = datetime.fromtimestamp(float(updated_raw), tz=timezone.utc)
        except (ValueError, OSError, TypeError):
            updated_at = None

    playtime_forever = author.get("playtime_forever")
    if playtime_forever is None:
        playtime_forever = record.get("playtime_forever")

    playtime_at_review = author.get("playtime_at_review")
    if playtime_at_review is None:
        playtime_at_review = record.get("playtime_at_review")

    developer_response = record.get("developer_response")
    if isinstance(developer_response, dict):
        developer_response = developer_response.get("response") or json.dumps(
            developer_response, ensure_ascii=False
        )

    playtime_hours = to_float(playtime_forever)
    playtime_at_review_hours = to_float(playtime_at_review)

    return {
        "review_id": str(review_id),
        "app_id": config.APP_ID,
        "language": (record.get("language") or "").strip().lower(),
        "review_text": review_text,
        "review_clean": review_clean,
        "voted_up": safe_bool(record.get("voted_up")),
        "votes_up": to_int(record.get("votes_up"), default=0),
        "votes_funny": to_int(record.get("votes_funny"), default=None),
        "comment_count": to_int(record.get("comment_count"), default=0),
        "weighted_vote_score": to_float(record.get("weighted_vote_score")),
        "playtime_forever_hours": round(playtime_hours / 60, 2) if playtime_hours is not None else None,
        "playtime_at_review_hours": round(playtime_at_review_hours / 60, 2)
        if playtime_at_review_hours is not None
        else None,
        "author_num_games_owned": to_int(author.get("num_games_owned"), default=None),
        "author_num_reviews": to_int(author.get("num_reviews"), default=None),
        "steam_purchase": safe_bool(record.get("steam_purchase")),
        "received_for_free": safe_bool(record.get("received_for_free")),
        "written_during_early_access": safe_bool(record.get("written_during_early_access")),
        "primarily_steam_deck": safe_bool(record.get("primarily_steam_deck")),
        "created_at_utc": created_at.isoformat(timespec="seconds") if created_at else None,
        "updated_at_utc": updated_at.isoformat(timespec="seconds") if updated_at else None,
        "author_id_hash": sha256_short(steamid, 16),
        "scraped_at_utc": record.get("_scraped_at"),
        "date_precision": precision,
        "lang_guess": "",
        "source": record.get("_source") or "unknown",
    }


def detect_languages(series: pd.Series) -> pd.Series:
    from langdetect import DetectorFactory, LangDetectException, detect

    DetectorFactory.seed = 0

    def guess(text: str) -> str:
        if not text or len(text) < 10:
            return ""
        try:
            return detect(text[:2000])
        except LangDetectException:
            return ""

    return series.map(guess)


def write_summary(
    *,
    raw_count: int,
    malformed: int,
    missing_id: int,
    duplicate_ids: int,
    duplicate_authors: int,
    non_english: int,
    too_short: int,
    final_count: int,
    dataframe: pd.DataFrame,
) -> None:
    positive = int((dataframe["voted_up"] == True).sum())  # noqa: E712
    negative = int((dataframe["voted_up"] == False).sum())  # noqa: E712
    sources = dataframe["source"].value_counts().to_dict()
    languages = dataframe["lang_guess"].replace("", pd.NA).dropna().value_counts().head(10).to_dict()

    lines = [
        "Delta Force English Review — Cleaning Summary",
        "=" * 46,
        f"Run at (UTC)              : {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Raw records read          : {raw_count}",
        f"Malformed JSON lines      : {malformed}",
        f"Dropped (missing ID)      : {missing_id}",
        f"Duplicate review IDs      : {duplicate_ids}",
        f"Duplicate author+text     : {duplicate_authors}",
        f"Dropped (non-English)     : {non_english}",
        f"Dropped (text < 3 chars)  : {too_short}",
        f"Final rows                : {final_count}",
        "",
        f"Positive (recommended)    : {positive}",
        f"Negative (not recommended): {negative}",
        f"Sources                   : {sources}",
        f"Date range (UTC)          : {dataframe['created_at_utc'].min()} -> {dataframe['created_at_utc'].max()}",
        "",
        "Optional language guesses :",
        f"{languages if languages else '(not run; pass --detect-language)'}",
        "",
    ]
    config.CLEANING_SUMMARY_PATH.write_text("\n".join(lines), encoding="utf-8-sig")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean raw Delta Force reviews into a CSV.")
    parser.add_argument("--input", type=Path, default=config.RAW_PATH)
    parser.add_argument("--output", type=Path, default=config.CLEAN_CSV_PATH)
    parser.add_argument(
        "--detect-language",
        action="store_true",
        help="Run langdetect on every review (slow, optional).",
    )
    args = parser.parse_args(argv)

    config.ensure_directories()

    if not args.input.exists():
        LOGGER.error("Raw file not found: %s. Run fetch_reviews.py first.", args.input)
        return 1

    records, malformed = read_raw(args.input)
    LOGGER.info("Read %s raw record(s) (%s malformed line(s)).", len(records), malformed)
    if not records:
        LOGGER.error("No usable records in %s", args.input)
        return 1

    reference = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    missing_id = 0
    for record in records:
        row = build_row(record, reference)
        if row is None:
            missing_id += 1
            continue
        rows.append(row)

    dataframe = pd.DataFrame(rows)
    raw_count = len(dataframe)

    # 1. Deduplicate review IDs (keep the most recently scraped copy).
    if "scraped_at_utc" in dataframe.columns:
        dataframe = dataframe.sort_values("scraped_at_utc", na_position="first")
    before = len(dataframe)
    dataframe = dataframe.drop_duplicates(subset=["review_id"], keep="last")
    duplicate_ids = before - len(dataframe)

    # 2. Deduplicate identical text by the same author (copy-paste spam guard).
    duplicate_author_mask = dataframe.duplicated(
        subset=["author_id_hash", "review_clean"], keep="first"
    ) & dataframe["author_id_hash"].notna()
    duplicate_authors = int(duplicate_author_mask.sum())
    dataframe = dataframe[~duplicate_author_mask]

    # 3. Defensive English-only filter.
    language_normalised = dataframe["language"].fillna("").str.strip().str.lower()
    non_english = int((language_normalised != "english").sum())
    dataframe = dataframe[language_normalised == "english"].copy()

    # 4. Drop empty one-word noise rows.
    too_short_mask = dataframe["review_clean"].str.len() < 3
    too_short = int(too_short_mask.sum())
    dataframe = dataframe[~too_short_mask]

    if args.detect_language and not dataframe.empty:
        LOGGER.info("Running optional langdetect on %s reviews...", len(dataframe))
        dataframe["lang_guess"] = detect_languages(dataframe["review_clean"])

    dataframe = dataframe.sort_values("created_at_utc", ascending=False, na_position="last")
    dataframe = dataframe[COLUMNS]
    dataframe.to_csv(args.output, index=False, encoding="utf-8-sig")

    config.DATA_DICTIONARY_PATH.write_text(DATA_DICTIONARY, encoding="utf-8-sig")
    write_summary(
        raw_count=raw_count,
        malformed=malformed,
        missing_id=missing_id,
        duplicate_ids=duplicate_ids,
        duplicate_authors=duplicate_authors,
        non_english=non_english,
        too_short=too_short,
        final_count=len(dataframe),
        dataframe=dataframe,
    )

    LOGGER.info("Wrote %s row(s) to %s", len(dataframe), args.output)
    LOGGER.info("Data dictionary: %s", config.DATA_DICTIONARY_PATH)
    LOGGER.info("Cleaning summary: %s", config.CLEANING_SUMMARY_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
