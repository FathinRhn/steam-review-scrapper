"""Confirm that the configured Steam App ID is the correct Delta Force game.

Run this once before a full crawl:

    python src/verify_app.py

It checks the public store metadata and probes the review endpoint, printing a
short report.  No data is written.
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402


def main() -> int:
    session = requests.Session()
    session.headers.update(config.request_headers())

    print(f"Checking app {config.APP_ID}...")
    try:
        response = session.get(
            "https://store.steampowered.com/api/appdetails",
            params={"appids": config.APP_ID, "l": "english"},
            timeout=config.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json().get(str(config.APP_ID), {})
    except (requests.RequestException, ValueError) as exc:
        print(f"[FAIL] Could not read store metadata: {exc}")
        return 1

    if not payload.get("success"):
        print("[FAIL] Steam has no data for this App ID.")
        return 1

    data = payload["data"]
    print(f"  Name       : {data.get('name')}")
    print(f"  Type       : {data.get('type')}")
    print(f"  Release    : {data.get('release_date', {}).get('date')}")
    print(f"  Developers : {', '.join(data.get('developers', []))}")
    print(f"  Publishers : {', '.join(data.get('publishers', []))}")
    print(f"  Free       : {data.get('is_free')}")

    expected = "delta force"
    name_matches = str(data.get("name", "")).strip().lower() == expected
    release = str(data.get("release_date", {}).get("date", ""))
    release_matches = "2024" in release

    if not name_matches:
        print(f"[WARN] Expected the name to be exactly {config.APP_NAME!r}.")
    if not release_matches:
        print("[WARN] Expected a 2024 release date.")

    try:
        probe = session.get(
            config.API_URL,
            params={
                "json": 1,
                "language": config.LANGUAGE,
                "filter": "recent",
                "review_type": "all",
                "purchase_type": "all",
                "num_per_page": 1,
                "cursor": "*",
            },
            timeout=config.REQUEST_TIMEOUT,
            allow_redirects=False,
        )
        if probe.is_redirect or probe.status_code != 200:
            print(
                f"[WARN] Review API not reachable from this network "
                f"(HTTP {probe.status_code}). fetch_reviews.py will use the HTML fallback."
            )
        else:
            try:
                body = probe.json()
            except ValueError:
                print("[WARN] Review API returned non-JSON content; HTML fallback will be used.")
            else:
                summary = body.get("query_summary", {})
                print(f"  API OK      : success={body.get('success')}")
                print(
                    "  English total: "
                    f"{summary.get('total_reviews')} reviews "
                    f"({summary.get('total_positive')} positive / "
                    f"{summary.get('total_negative')} negative)"
                )
    except requests.RequestException as exc:
        print(f"[WARN] Review API probe failed: {exc}")

    if name_matches and release_matches:
        print("[PASS] App ID looks correct.")
        return 0

    print("[WARN] App ID could not be fully confirmed. Check the store page manually.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
