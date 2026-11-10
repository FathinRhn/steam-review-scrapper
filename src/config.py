"""Central configuration for the supported Steam review crawls.

All tunable values live here so the rest of the code stays simple.  Values can
be overridden with environment variables without editing this file.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Game / endpoint settings
# ---------------------------------------------------------------------------
GAMES = {
    "delta-force": (2507950, "Delta Force"),
    "battlefield-6": (2807960, "Battlefield 6"),
}
DEFAULT_GAME = os.getenv("STEAM_GAME", "delta-force")
APP_ID = int(os.getenv("STEAM_APP_ID", str(GAMES[DEFAULT_GAME][0])))
APP_NAME = GAMES.get(DEFAULT_GAME, (APP_ID, "Steam game"))[1]
APP_URL = f"https://store.steampowered.com/app/{APP_ID}/"

# "all" collects reviews in every language; set REVIEW_LANGUAGE to limit it.
LANGUAGE = os.getenv("REVIEW_LANGUAGE", "all")

# Current Steamworks reviews endpoint. A key is optional; an app-associated
# publisher key receives a higher rate limit. Never commit the key to source.
API_URL = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
API_KEY = os.getenv("STEAM_API_KEY", "").strip()
API_VERSION = 2
HTML_REVIEWS_URL = f"https://steamcommunity.com/app/{APP_ID}/reviews/"
HTML_MORE_URL = f"https://steamcommunity.com/app/{APP_ID}/homecontent/"

# API page size (Steam clamps anything above 100).
PAGE_SIZE = 100

# Community HTML pages always render 10 reviews per request.
HTML_PAGE_SIZE = 10

# ---------------------------------------------------------------------------
# Politeness / reliability settings
# ---------------------------------------------------------------------------
MIN_DELAY = float(os.getenv("SCRAPE_MIN_DELAY", "1.0"))
MAX_DELAY = float(os.getenv("SCRAPE_MAX_DELAY", "1.8"))
MAX_RETRIES = int(os.getenv("SCRAPE_MAX_RETRIES", "5"))
REQUEST_TIMEOUT = (10, 40)  # (connect, read) seconds

# 0 means "no explicit cap"; pass --max-pages for a short test run.
MAX_PAGES = int(os.getenv("SCRAPE_MAX_PAGES", "0"))

# Absolute safety valve so a broken cursor can never loop forever.
SAFETY_PAGE_CAP = 20_000

USER_AGENT = os.getenv(
    "SCRAPE_USER_AGENT",
    "UAS-DataMining-Scraper/1.0 (academic project; contact: student@example.com)",
)

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]

RAW_DIR = PROJECT_ROOT / "data" / "raw"
STATE_DIR = PROJECT_ROOT / "data" / "state"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
REPORT_DIR = PROJECT_ROOT / "reports"

RAW_PATH = RAW_DIR / "reviews_raw.jsonl"
STATE_PATH = STATE_DIR / f"crawl_state_{APP_ID}.json"
API_SUMMARY_PATH = STATE_DIR / f"api_summary_{APP_ID}.json"

CLEAN_CSV_PATH = PROCESSED_DIR / "steam_reviews_all_languages.csv"
LOG_PATH = REPORT_DIR / "scrape_log.txt"
DATA_DICTIONARY_PATH = REPORT_DIR / "data_dictionary.md"
VALIDATION_PATH = REPORT_DIR / "validation_summary.txt"
CLEANING_SUMMARY_PATH = REPORT_DIR / "cleaning_summary.txt"


def select_game(game: str) -> None:
    """Set endpoint and checkpoint paths for one configured game."""
    global APP_ID, APP_NAME, APP_URL, API_URL, HTML_REVIEWS_URL, HTML_MORE_URL
    global STATE_PATH, API_SUMMARY_PATH
    APP_ID, APP_NAME = GAMES[game]
    APP_URL = f"https://store.steampowered.com/app/{APP_ID}/"
    API_URL = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
    HTML_REVIEWS_URL = f"https://steamcommunity.com/app/{APP_ID}/reviews/"
    HTML_MORE_URL = f"https://steamcommunity.com/app/{APP_ID}/homecontent/"
    STATE_PATH = STATE_DIR / f"crawl_state_{APP_ID}.json"
    API_SUMMARY_PATH = STATE_DIR / f"api_summary_{APP_ID}.json"


def ensure_directories() -> None:
    """Create every folder the project writes to."""
    for path in (RAW_DIR, STATE_DIR, PROCESSED_DIR, REPORT_DIR):
        path.mkdir(parents=True, exist_ok=True)


def request_headers(*, ajax: bool = False) -> dict[str, str]:
    """Headers used for every HTTP request.

    A descriptive User-Agent is part of the ethical scraping rules for this
    project: it lets Steam identify and contact the bot owner if needed.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    }
    if ajax:
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Referer"] = (
            f"{HTML_REVIEWS_URL}?browsefilter=mostrecent&p=1"
            f"&filterLanguage={LANGUAGE}&l=english"
        )
    return headers
