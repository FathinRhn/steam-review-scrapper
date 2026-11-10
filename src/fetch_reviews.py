"""Download Steam reviews for Delta Force and Battlefield 6.

Design
------
1. Try Steam's public JSON review endpoint (fast, rich fields).
2. If that endpoint is blocked (Steam sometimes redirects it to the store home
   page for certain networks), fall back to the public community review pages,
   which render the same reviews as HTML.

Both modes append to the same ``data/raw/reviews_raw.jsonl`` file in an
API-like shape, so ``clean_reviews.py`` does not need to care where the data
came from. Every page is checkpointed in a per-game state file;
rerunning the script resumes from the last saved position.

Usage (from the project root):
    python src/fetch_reviews.py                 # scrape both games, resume each
    python src/fetch_reviews.py --game delta-force
    python src/fetch_reviews.py --game battlefield-6
    python src/fetch_reviews.py --mode api
    python src/fetch_reviews.py --mode html
    python src/fetch_reviews.py --max-pages 3   # quick smoke test
    python src/fetch_reviews.py --reset-state   # start over, keep raw data
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# Allow "python src/fetch_reviews.py" as well as "python -m src.fetch_reviews".
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from common import (  # noqa: E402
    atomic_write_json,
    load_json,
    parse_posted_date,
    polite_sleep,
    setup_logging,
    sha256_short,
    to_float,
    to_int,
    utc_now,
    utc_now_iso,
)

LOGGER = setup_logging()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ApiBlockedError(RuntimeError):
    """The JSON API is unavailable (redirect/HTML/captcha), try HTML instead."""


class ApiError(RuntimeError):
    """A temporary or permanent API failure that is not an explicit block."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def default_state() -> dict[str, Any]:
    return {
        "app_id": config.APP_ID,
        "app_name": config.APP_NAME,
        "language": config.LANGUAGE,
        "api_version": config.API_VERSION,
        "mode": None,
        "cursor": "*",
        "next_params": None,
        "pages_done": 0,
        "reviews_saved": 0,
        "started_at": None,
        "last_run": None,
        "finished": False,
        "query_summary": {},
    }


def load_state() -> dict[str, Any]:
    state = default_state()
    saved = load_json(config.STATE_PATH, default={})
    # Migrate the original Delta Force checkpoint transparently.
    if not saved and config.APP_ID == 2507950:
        saved = load_json(config.STATE_DIR / "crawl_state.json", default={})
    if isinstance(saved, dict):
        state.update(saved)
    if state.get("app_id") != config.APP_ID:
        # A state file from another game must never be reused.
        LOGGER.warning(
            "State file belongs to app %s, expected %s. Starting fresh.",
            state.get("app_id"),
            config.APP_ID,
        )
        state = default_state()
    elif state.get("language") != config.LANGUAGE:
        LOGGER.info(
            "Review language changed from %s to %s; restarting this game's cursor. "
            "Existing IDs will still be skipped.",
            state.get("language"),
            config.LANGUAGE,
        )
        state = default_state()
    elif state.get("mode") == "api" and state.get("api_version") != config.API_VERSION:
        LOGGER.info(
            "Steam review API changed; restarting the cursor for %s. "
            "Previously saved review IDs will be skipped.",
            config.APP_NAME,
        )
        state["cursor"] = "*"
        state["pages_done"] = 0
        state["finished"] = False
        state["query_summary"] = {}
        state["api_version"] = config.API_VERSION
    if not state.get("started_at"):
        state["started_at"] = utc_now_iso()
    return state


def save_state(state: dict[str, Any]) -> None:
    state["last_run"] = utc_now_iso()
    atomic_write_json(config.STATE_PATH, state)


# ---------------------------------------------------------------------------
# Raw output
# ---------------------------------------------------------------------------
def append_raw(records: list[dict[str, Any]], source: str) -> int:
    """Append reviews to the raw JSONL file and force them to disk."""
    if not records:
        return 0

    scraped_at = utc_now_iso()
    written = 0
    with config.RAW_PATH.open("a", encoding="utf-8") as handle:
        for record in records:
            record = dict(record)
            record["_source"] = source
            record["_app_id"] = config.APP_ID
            record["_app_name"] = config.APP_NAME
            record["_scraped_at"] = scraped_at
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
        handle.flush()
        os.fsync(handle.fileno())
    return written


def load_seen_ids() -> set[str]:
    """Read existing raw IDs so a resumed run never appends duplicates."""
    seen: set[str] = set()
    if not config.RAW_PATH.exists():
        return seen

    skipped = 0
    with config.RAW_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            review_id = record.get("recommendationid") or record.get("_html_key")
            app_id = record.get("_app_id", record.get("app_id", config.APP_ID))
            if review_id and str(app_id) == str(config.APP_ID):
                seen.add(str(review_id))

    if skipped:
        LOGGER.warning("Skipped %s corrupt line(s) while loading known IDs.", skipped)
    LOGGER.info("Loaded %s known review ID(s) from %s", len(seen), config.RAW_PATH.name)
    return seen


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
def api_params(cursor: str, num_per_page: int = config.PAGE_SIZE) -> dict[str, Any]:
    return {
        "appid": config.APP_ID,
        "filter": 1,                  # k_EUserReviewsAppReviewsFilter_Recent
        "languages": [config.LANGUAGE],
        "review_type": 0,            # k_EUserReviewsReviewType_All
        "purchase_type": 1,          # k_EUserReviewsPurchaseType_All
        "filter_offtopic_activity": False,
        "num_per_page": num_per_page,
        "cursor": cursor,
    }


def fetch_api_page(
    session: requests.Session,
    cursor: str,
    *,
    num_per_page: int = config.PAGE_SIZE,
    max_retries: int = config.MAX_RETRIES,
) -> dict[str, Any]:
    """Fetch one API page, retrying transient errors.

    Raises:
        ApiBlockedError: Steam redirected or served HTML instead of JSON.
        ApiError: retries exhausted or an unexpected HTTP status.
    """
    input_json = json.dumps(api_params(cursor, num_per_page), separators=(",", ":"))
    params: dict[str, Any] = {"input_json": input_json}
    if config.API_KEY:
        params["key"] = config.API_KEY

    for attempt in range(1, max_retries + 1):
        try:
            response = session.get(
                config.API_URL,
                params=params,
                timeout=config.REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise ApiError(f"Network error after {attempt} attempts: {exc}") from exc
            wait = min(60, 2**attempt)
            LOGGER.warning("API network error (%s). Retrying in %ss...", exc, wait)
            time.sleep(wait)
            continue

        if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "<unknown>")
            raise ApiBlockedError(
                f"Steam redirected the review API to {location!r}. "
                "This network is likely blocked from the JSON endpoint."
            )

        if response.status_code in (429, 500, 502, 503, 504):
            retry_after = response.headers.get("Retry-After")
            wait = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
            wait = min(wait, 120)
            if attempt >= max_retries:
                detail = (
                    ". Set STEAM_API_KEY to an app-associated publisher key for a higher rate limit"
                    if response.status_code == 429
                    else ""
                )
                raise ApiError(
                    f"Steam GetAppReviews returned HTTP {response.status_code} "
                    f"after {attempt} attempts{detail}"
                )
            LOGGER.warning(
                "API returned HTTP %s. Retrying in %ss (attempt %s/%s)...",
                response.status_code,
                wait,
                attempt,
                max_retries,
            )
            time.sleep(wait)
            continue

        if response.status_code != 200:
            detail = ""
            if response.status_code in (401, 403):
                detail = ". Check STEAM_API_KEY and that the key is associated with this app"
            raise ApiError(
                f"Steam GetAppReviews returned HTTP {response.status_code}{detail}"
            )

        content_type = response.headers.get("content-type", "").lower()
        body = response.text.lstrip()
        if "json" not in content_type and not body.startswith("{"):
            raise ApiBlockedError(
                "Steam returned HTML instead of JSON (anti-bot page or block)."
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ApiBlockedError("Review API response was not valid JSON.") from exc

        # Steam Web API service methods normally wrap results in `response`;
        # accept an unwrapped body as well for forward compatibility.
        payload = data.get("response", data)
        if not isinstance(payload, dict):
            raise ApiError("GetAppReviews response did not contain a JSON object")
        if "reviews" not in payload:
            api_error = data.get("error") or payload.get("error")
            raise ApiError(
                "GetAppReviews response did not include a reviews list"
                + (f": {api_error}" if api_error else "")
            )
        return payload

    raise ApiError("Retries exhausted while calling the review API")


def crawl_api(
    session: requests.Session,
    state: dict[str, Any],
    *,
    max_pages: int,
    seen: set[str],
) -> None:
    total = int(state.get("query_summary", {}).get("total_reviews") or 0)
    progress = tqdm(total=total or None, desc="API reviews", unit="rev")

    try:
        while True:
            if max_pages and state["pages_done"] >= max_pages:
                LOGGER.info("Reached --max-pages=%s; stopping. Rerun to continue.", max_pages)
                break

            cursor = state.get("cursor") or "*"
            data = fetch_api_page(session, cursor)

            summary = data.get("query_summary") or {}
            if summary:
                state["query_summary"] = summary
                atomic_write_json(config.API_SUMMARY_PATH, summary)
                if total == 0:
                    total = int(summary.get("total_reviews") or 0)
                    progress.total = total or None
                    progress.refresh()

            reviews = data.get("reviews") or []
            if not reviews:
                LOGGER.info("API returned no more reviews. Crawl complete.")
                state["finished"] = True
                break

            fresh: list[dict[str, Any]] = []
            for record in reviews:
                review_id = str(record.get("recommendationid", ""))
                if not review_id or review_id in seen:
                    continue
                fresh.append(record)
                seen.add(review_id)

            written = append_raw(fresh, "api")
            state["pages_done"] += 1
            state["reviews_saved"] += written
            progress.update(len(reviews))

            next_cursor = data.get("cursor")
            if not next_cursor or next_cursor == cursor:
                LOGGER.info("Cursor did not advance; stopping to avoid an infinite loop.")
                state["finished"] = True
                break

            state["cursor"] = next_cursor
            save_state(state)

            LOGGER.info(
                "API page %s | +%s reviews (page total %s) | saved %s | cursor %s...",
                state["pages_done"],
                written,
                len(reviews),
                state["reviews_saved"],
                str(next_cursor)[:12],
            )

            if state["pages_done"] >= config.SAFETY_PAGE_CAP:
                LOGGER.warning("Safety page cap (%s) reached. Stopping.", config.SAFETY_PAGE_CAP)
                break

            polite_sleep()
    finally:
        progress.close()
        save_state(state)


# ---------------------------------------------------------------------------
# HTML fallback
# ---------------------------------------------------------------------------
def html_page_params() -> dict[str, Any]:
    return {
        "browsefilter": "mostrecent",
        "p": 1,
        "filterLanguage": config.LANGUAGE,
        "l": "english",
    }


def fetch_html(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    *,
    ajax: bool,
) -> str:
    """Fetch an HTML page/fragment with retries, returning its text."""
    headers = config.request_headers(ajax=ajax)

    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            response = session.get(
                url,
                params=params,
                headers=headers,
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            if attempt >= config.MAX_RETRIES:
                raise ApiError(f"Network error after {attempt} attempts: {exc}") from exc
            wait = min(60, 2**attempt)
            LOGGER.warning("HTML network error (%s). Retrying in %ss...", exc, wait)
            time.sleep(wait)
            continue

        if response.status_code in (429, 500, 502, 503, 504):
            retry_after = response.headers.get("Retry-After")
            wait = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
            if attempt >= config.MAX_RETRIES:
                raise ApiError(f"HTTP {response.status_code} after {attempt} attempts")
            LOGGER.warning("HTML returned HTTP %s. Sleeping %ss...", response.status_code, wait)
            time.sleep(min(wait, 120))
            continue

        if response.status_code != 200:
            raise ApiError(f"Unexpected HTTP {response.status_code} from community page")

        return response.text

    raise ApiError("Retries exhausted while fetching community page")


def extract_next_params(soup: BeautifulSoup) -> dict[str, Any] | None:
    """Read the hidden "next page" form from a review page/fragment."""
    form = soup.select_one('form[action*="homecontent"]')
    if form is None:
        return None

    params: dict[str, Any] = {}
    for field in form.find_all("input"):
        name = field.get("name")
        if name:
            params[name] = field.get("value", "")

    if not params.get("userreviewscursor"):
        return None

    params.setdefault("browsefilter", "mostrecent")
    params.setdefault("filterLanguage", config.LANGUAGE)
    params.setdefault("l", "english")
    return params


def parse_html_card(card: Any, reference: Any) -> dict[str, Any] | None:
    """Convert one community review card into an API-like raw record."""
    text_container = card.select_one(".apphub_CardTextContent")
    if text_container is None:
        return None

    # Author identity
    author_anchor = card.select_one(".apphub_CardContentAuthorName a[href]")
    profile_url = author_anchor.get("href") if author_anchor else None
    steamid = None
    if profile_url:
        match = re.search(r"steamcommunity\.com/(?:profiles|id)/([^/?#]+)", profile_url)
        if match:
            steamid = match.group(1)

    # Recommendation and playtime
    title_el = card.select_one(".vote_header .title")
    voted_up = bool(title_el and title_el.get_text(strip=True).lower() == "recommended")

    hours_el = card.select_one(".vote_header .hours")
    hours_text = hours_el.get_text(" ", strip=True) if hours_el else ""
    hours_match = re.search(r"([\d.,]+)\s*(?:hrs?|hours?)", hours_text, re.IGNORECASE)
    playtime_hours = to_float(hours_match.group(1)) if hours_match else None

    # Date and text.  Remove the date node before reading the review body.
    date_el = text_container.select_one(".date_posted")
    posted_text = date_el.get_text(" ", strip=True) if date_el else None
    if date_el is not None:
        date_el.extract()
    review_text = text_container.get_text("\n", strip=True)

    created_at, precision = parse_posted_date(posted_text, reference)

    # Helpfulness / comments / account size
    helpful_el = card.select_one(".found_helpful")
    helpful_text = helpful_el.get_text(" ", strip=True) if helpful_el else ""
    helpful_match = re.search(
        r"([\d,]+)\s+(?:person|people)\s+found", helpful_text, re.IGNORECASE
    )
    votes_up = to_int(helpful_match.group(1)) if helpful_match else 0

    comment_el = card.select_one(".apphub_CardCommentCount")
    comment_count = to_int(comment_el.get_text(strip=True)) if comment_el else 0

    more_el = card.select_one(".apphub_CardContentMoreLink")
    more_text = more_el.get_text(" ", strip=True) if more_el else ""
    games_match = re.search(r"([\d,]+)\s+products?\s+in\s+account", more_text, re.IGNORECASE)
    num_games_owned = to_int(games_match.group(1)) if games_match else None

    lowered = review_text.lower()
    received_for_free = "received for free" in lowered
    early_access = "early access review" in lowered

    # The community page has no numeric recommendation ID in the card markup.
    # Build a stable surrogate so resumes/deduplication still work.
    key_source = f"{steamid or 'anon'}|{posted_text or ''}|{review_text}"
    review_id = "html-" + hashlib.sha256(key_source.encode("utf-8")).hexdigest()[:20]

    return {
        "recommendationid": review_id,
        "author": {
            "steamid": steamid,
            "num_games_owned": num_games_owned,
            "num_reviews": None,
            "playtime_forever": int(round(playtime_hours * 60)) if playtime_hours else None,
            "playtime_at_review": None,
        },
        "language": config.LANGUAGE if config.LANGUAGE != "all" else "unknown",
        "review": review_text,
        "voted_up": voted_up,
        "votes_up": votes_up,
        "votes_funny": None,
        "weighted_vote_score": None,
        "comment_count": comment_count,
        "timestamp_created": int(created_at.timestamp()) if created_at else None,
        "timestamp_updated": None,
        "timestamp_created_text": posted_text,
        "timestamp_created_precision": precision,
        "playtime_forever": int(round(playtime_hours * 60)) if playtime_hours else None,
        "playtime_at_review": None,
        "steam_purchase": None,
        "received_for_free": received_for_free,
        "written_during_early_access": early_access,
        "primarily_steam_deck": None,
        "developer_response": None,
        "_html_key": review_id,
        "_html_profile_url": profile_url,
    }


def parse_html_reviews(html_text: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    soup = BeautifulSoup(html_text, "lxml")
    reference = utc_now()

    records: list[dict[str, Any]] = []
    for card in soup.select(".apphub_Card"):
        record = parse_html_card(card, reference)
        if record is not None:
            records.append(record)

    return records, extract_next_params(soup)


def crawl_html(
    session: requests.Session,
    state: dict[str, Any],
    *,
    max_pages: int,
    seen: set[str],
) -> None:
    """Walk the community review pages via the "homecontent" fragments."""
    progress = tqdm(desc="HTML reviews", unit="rev")

    try:
        while True:
            if max_pages and state["pages_done"] >= max_pages:
                LOGGER.info("Reached --max-pages=%s; stopping. Rerun to continue.", max_pages)
                break

            next_params = state.get("next_params")
            if next_params:
                html_text = fetch_html(
                    session, config.HTML_MORE_URL, next_params, ajax=True
                )
                page_label = next_params.get("p", state["pages_done"] + 1)
            else:
                html_text = fetch_html(
                    session, config.HTML_REVIEWS_URL, html_page_params(), ajax=False
                )
                page_label = 1

            records, following_params = parse_html_reviews(html_text)
            if not records:
                LOGGER.info("No review cards on page %s. Crawl complete.", page_label)
                state["finished"] = True
                break

            fresh: list[dict[str, Any]] = []
            for record in records:
                review_id = str(record.get("recommendationid", ""))
                if not review_id or review_id in seen:
                    continue
                fresh.append(record)
                seen.add(review_id)

            written = append_raw(fresh, "html")
            state["pages_done"] += 1
            state["reviews_saved"] += written
            progress.update(len(records))

            LOGGER.info(
                "HTML page %s | +%s reviews (page total %s) | saved %s",
                page_label,
                written,
                len(records),
                state["reviews_saved"],
            )

            if not following_params:
                LOGGER.info("No further-page form found. Crawl complete.")
                state["finished"] = True
                break

            old_cursor = next_params.get("userreviewscursor") if next_params else None
            new_cursor = following_params.get("userreviewscursor")
            if old_cursor and new_cursor == old_cursor:
                LOGGER.info("Cursor did not advance; stopping to avoid an infinite loop.")
                state["finished"] = True
                break

            state["next_params"] = following_params
            save_state(state)

            if state["pages_done"] >= config.SAFETY_PAGE_CAP:
                LOGGER.warning("Safety page cap (%s) reached. Stopping.", config.SAFETY_PAGE_CAP)
                break

            polite_sleep()
    finally:
        progress.close()
        save_state(state)


# ---------------------------------------------------------------------------
# Mode selection / CLI
# ---------------------------------------------------------------------------
def api_available(session: requests.Session) -> bool:
    """Probe the JSON API once; a redirect means this network is blocked."""
    try:
        fetch_api_page(session, "*", num_per_page=1, max_retries=1)
        return True
    except ApiBlockedError as exc:
        LOGGER.warning("JSON review API unavailable: %s", exc)
        return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Steam reviews for supported games.")
    parser.add_argument(
        "--mode",
        choices=("auto", "api", "html"),
        default="auto",
        help="api = JSON endpoint, html = community pages, auto = try API then fall back.",
    )
    parser.add_argument(
        "--game",
        choices=("all", *config.GAMES.keys()),
        default="all",
        help="Which game to crawl (default: all; each game has a separate checkpoint).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=config.MAX_PAGES,
        help="Stop after N pages (0 = no cap). Useful for a quick smoke test.",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Delete the selected game's checkpoint(s) and start from page 1. Raw data is kept.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config.ensure_directories()
    games = list(config.GAMES) if args.game == "all" else [args.game]
    for game in games:
        config.select_game(game)
        if args.reset_state and config.STATE_PATH.exists():
            config.STATE_PATH.unlink()
            LOGGER.info("Deleted checkpoint for %s.", config.APP_NAME)

        state = load_state()
        if state.get("finished"):
            LOGGER.info("%s is already complete (%s saved).", config.APP_NAME,
                        state.get("reviews_saved"))
            continue

        session = requests.Session()
        session.headers.update(config.request_headers())
        mode = args.mode
        if mode == "auto":
            mode = state.get("mode") if state.get("mode") in {"api", "html"} else None
            if mode is None:
                mode = "api" if api_available(session) else "html"
                LOGGER.info("Selected mode for %s: %s", config.APP_NAME, mode.upper())

        state["mode"] = mode
        state["api_version"] = config.API_VERSION if mode == "api" else None
        save_state(state)
        LOGGER.info("Starting %s crawl for %s (App ID %s); saved so far=%s",
                    mode.upper(), config.APP_NAME, config.APP_ID, state.get("reviews_saved", 0))
        seen = load_seen_ids()
        try:
            if mode == "api":
                crawl_api(session, state, max_pages=args.max_pages, seen=seen)
            else:
                crawl_html(session, state, max_pages=args.max_pages, seen=seen)
        except ApiBlockedError as exc:
            LOGGER.error("API blocked for %s and no fallback was attempted: %s", config.APP_NAME, exc)
            LOGGER.error("Rerun with: python src/fetch_reviews.py --game %s --mode html", game)
            save_state(state)
            return 2
        except ApiError as exc:
            LOGGER.error("Crawl stopped for %s: %s. Progress is saved; rerun to resume.", config.APP_NAME, exc)
            save_state(state)
            return 1
        except KeyboardInterrupt:
            LOGGER.warning("Interrupted. Progress for %s has been saved; rerun to resume.", config.APP_NAME)
            save_state(state)
            return 130
        LOGGER.info("Done: %s | mode=%s | pages=%s | reviews saved=%s",
                    config.APP_NAME, mode, state.get("pages_done"), state.get("reviews_saved"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
