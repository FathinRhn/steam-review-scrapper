"""Validate the cleaned Delta Force and Battlefield 6 review dataset.

Usage:
    python src/validate_reviews.py

Writes reports/validation_summary.txt and exits with:
    0 = all critical checks passed (warnings may exist)
    1 = at least one critical check failed
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from clean_reviews import COLUMNS  # noqa: E402
from common import load_json, setup_logging  # noqa: E402

LOGGER = setup_logging()

RELEASE_DATES = {
    2507950: datetime(2024, 12, 4, tzinfo=timezone.utc),
    2807960: datetime(2025, 10, 10, tzinfo=timezone.utc),
}


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.critical_failures = 0
        self.warnings = 0

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(title)
        self.lines.append("-" * len(title))

    def check(self, passed: bool, label: str, detail: str = "", *, critical: bool = True) -> None:
        status = "PASS" if passed else ("FAIL" if critical else "WARN")
        if not passed:
            if critical:
                self.critical_failures += 1
            else:
                self.warnings += 1
        line = f"[{status}] {label}"
        if detail:
            line += f" — {detail}"
        self.lines.append(line)
        print(line)

    def info(self, label: str, value: object) -> None:
        self.lines.append(f"       {label}: {value}")


def main() -> int:
    csv_path = config.CLEAN_CSV_PATH
    report = Report()

    report.lines.append("Delta Force + Battlefield 6 English Reviews — Validation Summary")
    report.lines.append("=" * 52)
    report.lines.append(f"Run at (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    report.lines.append(f"Dataset     : {csv_path}")

    if not csv_path.exists():
        print(f"[FAIL] Dataset not found: {csv_path}")
        return 1

    dataframe = pd.read_csv(csv_path, encoding="utf-8-sig")
    report.info("Rows", len(dataframe))
    report.info("Columns", len(dataframe.columns))
    report.info("Columns present", ", ".join(dataframe.columns))

    report.section("Schema")
    missing_columns = [column for column in COLUMNS if column not in dataframe.columns]
    report.check(not missing_columns, "All expected columns exist", str(missing_columns) if missing_columns else "")
    report.check(len(dataframe) > 0, "Dataset is not empty", critical=True)

    if dataframe.empty or missing_columns:
        return finish(report)

    report.section("Uniqueness & language")
    unique_ids = dataframe[["app_id", "review_id"]].drop_duplicates().shape[0]
    report.check(
        unique_ids == len(dataframe),
        "(app_id, review_id) is unique",
        f"{unique_ids} unique / {len(dataframe)} rows",
    )

    language_counts = dataframe["language"].fillna("").str.lower().value_counts().to_dict()
    report.check(bool(language_counts) and all(language_counts),
                 "Review language is recorded", str(language_counts))

    app_ids = sorted(dataframe["app_id"].dropna().unique().tolist())
    expected_app_ids = sorted(app_id for app_id, _ in config.GAMES.values())
    report.check(set(app_ids).issubset(set(expected_app_ids)), "Rows belong to supported apps", str(app_ids))

    report.section("Required fields")
    for column in ("review_id", "voted_up", "created_at_utc", "review_text", "review_clean"):
        missing = int(dataframe[column].isna().sum())
        report.check(missing == 0, f"No missing values in {column}", f"{missing} missing")

    invalid_votes = (~dataframe["voted_up"].isin([True, False, "True", "False", 1, 0])).sum()
    report.check(int(invalid_votes) == 0, "voted_up contains only booleans", f"{invalid_votes} invalid")

    short_rows = int((dataframe["review_clean"].astype(str).str.len() < 3).sum())
    report.check(short_rows == 0, "No reviews shorter than 3 characters", f"{short_rows} rows")

    report.section("Dates")
    parsed_dates = pd.to_datetime(dataframe["created_at_utc"], errors="coerce", utc=True)
    invalid_dates = int(parsed_dates.isna().sum())
    report.check(
        invalid_dates == 0,
        "created_at_utc is parseable",
        f"{invalid_dates} unparseable",
        critical=False,
    )
    invalid_share = invalid_dates / len(dataframe)
    report.check(
        invalid_share <= 0.05,
        "Unparseable dates stay below 5%",
        f"{invalid_share:.2%}",
        critical=True,
    )

    future_limit = datetime.now(timezone.utc).replace(microsecond=0)
    release_limits = dataframe["app_id"].map(RELEASE_DATES)
    too_early = int((parsed_dates < release_limits).fillna(False).sum())
    too_late = int((parsed_dates > future_limit).sum())
    report.check(too_early == 0, "No reviews before the game's release", f"{too_early} rows")
    report.check(too_late == 0, "No reviews dated in the future", f"{too_late} rows")
    report.info("Date range", f"{parsed_dates.min()} -> {parsed_dates.max()}")

    precision_counts = dataframe["date_precision"].fillna("missing").value_counts().to_dict()
    report.info("Date precision", precision_counts)

    report.section("Duplicates")
    duplicate_text = int(
        dataframe.duplicated(subset=["app_id", "author_id_hash", "review_clean"], keep="first").sum()
    )
    report.check(duplicate_text == 0, "No duplicate author+text pairs", f"{duplicate_text} duplicates", critical=False)

    report.section("Class balance")
    positive = int((dataframe["voted_up"] == True).sum())  # noqa: E712
    negative = int((dataframe["voted_up"] == False).sum())  # noqa: E712
    report.info("Positive", f"{positive} ({positive / len(dataframe):.1%})")
    report.info("Negative", f"{negative} ({negative / len(dataframe):.1%})")

    report.section("Source & API reconciliation")
    sources = dataframe["source"].fillna("unknown").value_counts().to_dict()
    report.info("Sources", sources)

    for app_id, app_name in config.GAMES.values():
        state = load_json(config.STATE_DIR / f"crawl_state_{app_id}.json", default={})
        api_summary = load_json(config.STATE_DIR / f"api_summary_{app_id}.json", default={})
        mode = state.get("mode") if isinstance(state, dict) else None
        total_reviews = int(api_summary.get("total_reviews") or 0) if isinstance(api_summary, dict) else 0
        count = int((dataframe["app_id"] == app_id).sum())
        if mode == "api" and total_reviews:
            difference = abs(count - total_reviews) / total_reviews
            report.check(difference <= 0.02, f"{app_name} count matches Steam total (within 2%)",
                         f"dataset={count}, Steam={total_reviews}, diff={difference:.2%}", critical=False)
        else:
            report.info(f"{app_name} API total", "not available (HTML fallback or no query_summary)")

    report.section("Optional language detection")
    guesses = dataframe["lang_guess"].fillna("").str.strip()
    guessed = guesses[guesses != ""]
    if guessed.empty:
        report.info("lang_guess", "not run (use --detect-language in clean_reviews.py)")
    else:
        non_english_guesses = int((guessed != "en").sum())
        share = non_english_guesses / len(guessed)
        report.check(
            share <= 0.05,
            "Less than 5% of reviews are guessed non-English",
            f"{non_english_guesses} of {len(guessed)} ({share:.2%})",
            critical=False,
        )
        report.info("Top guesses", guessed.value_counts().head(5).to_dict())

    report.section("Manual spot-check")
    sample = dataframe.sample(n=min(3, len(dataframe)), random_state=42)
    for _, row in sample.iterrows():
        snippet = str(row["review_clean"])[:120].replace("\n", " ")
        report.lines.append(f"  - {row['review_id']} | {row['created_at_utc']} | {snippet}")
    report.info(
        "How to verify",
        "open https://store.steampowered.com/app/2507950/ and search the review text; "
        "API rows can also be matched with their recommendationid",
    )

    return finish(report)


def finish(report: Report) -> int:
    status = "FAIL" if report.critical_failures else ("PASS (with warnings)" if report.warnings else "PASS")
    report.lines.insert(3, f"Status      : {status}")
    report.lines.append("")
    report.lines.append(
        f"Critical failures: {report.critical_failures} | Warnings: {report.warnings}"
    )

    config.VALIDATION_PATH.write_text("\n".join(report.lines) + "\n", encoding="utf-8-sig")
    print(f"\nStatus: {status}")
    print(f"Report: {config.VALIDATION_PATH}")
    return 1 if report.critical_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
