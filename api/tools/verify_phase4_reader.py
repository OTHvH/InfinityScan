"""Verify the live Phase 4 reader contract against the release fixture."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    slug = fixture["slug"]
    endpoint = f"/reader/{slug}/chunks"

    with httpx.Client(base_url=args.api, timeout=10, follow_redirects=False) as client:
        initial_response = client.get(
            endpoint,
            params={"start_chapter_id": fixture["first_chapter_id"], "limit": 5},
        )
        initial_response.raise_for_status()
        initial = initial_response.json()
        assert len(initial["chapters"]) == 5
        assert initial["previous_cursor"] is None
        assert initial["next_cursor"]

        next_response = client.get(
            endpoint,
            params={"cursor": initial["next_cursor"], "direction": "next", "limit": 5},
        )
        next_response.raise_for_status()
        assert len(next_response.json()["chapters"]) == 5

        middle_response = client.get(
            endpoint,
            params={
                "start_chapter_id": fixture["middle_chapter_id"],
                "direction": "previous",
                "limit": 5,
            },
        )
        middle_response.raise_for_status()
        middle = middle_response.json()
        assert len(middle["chapters"]) == 5
        assert middle["previous_cursor"]
        previous_response = client.get(
            endpoint,
            params={
                "cursor": middle["previous_cursor"],
                "direction": "previous",
                "limit": 5,
            },
        )
        previous_response.raise_for_status()
        assert len(previous_response.json()["chapters"]) == 5

        traversed = list(initial["chapters"])
        cursor = initial["next_cursor"]
        request_count = 1
        while cursor:
            response = client.get(
                endpoint,
                params={"cursor": cursor, "direction": "next", "limit": 5},
            )
            response.raise_for_status()
            chunk = response.json()
            assert chunk["chapters"]
            traversed.extend(chunk["chapters"])
            cursor = chunk["next_cursor"]
            request_count += 1

        assert len(traversed) == fixture["ready_chapters"] == 200
        assert len({chapter["id"] for chapter in traversed}) == 200
        numbers = [Decimal(chapter["number"]) for chapter in traversed]
        assert numbers == sorted(numbers)
        assert any(number % 1 for number in numbers)
        assert any(right - left > 1 for left, right in zip(numbers, numbers[1:]))
        assert any(left == right for left, right in zip(numbers, numbers[1:]))

        serialized = json.dumps(traversed)
        for hidden_id in fixture["hidden_chapter_ids"]:
            assert hidden_id not in serialized
        assert fixture["pending_page_id"] not in serialized
        assert "object_key" not in serialized
        assert "phase4/" not in serialized
        assert "X-Amz" not in serialized
        assert "minio" not in serialized.lower()
        page_count = 0
        for chapter in traversed:
            assert chapter["page_count"] == 10
            assert len(chapter["pages"]) == 10
            for page in chapter["pages"]:
                assert page["media_path"] == f'/media/pages/{page["id"]}'
                page_count += 1
        assert page_count == fixture["verified_pages"] == 2000

        valid_cursor = initial["next_cursor"]
        tampered = valid_cursor[:-1] + ("A" if valid_cursor[-1] != "A" else "B")
        assert client.get(
            endpoint,
            params={"cursor": tampered, "direction": "next", "limit": 5},
        ).status_code == 400

        media_response = client.get(f'/media/pages/{fixture["sample_page_id"]}')
        assert media_response.status_code == 307
        assert media_response.headers.get("location")

    result = {
        "readyChapters": len(traversed),
        "verifiedPages": page_count,
        "cursorRequests": request_count,
        "mediaStatus": media_response.status_code,
    }
    args.output.write_text(json.dumps(result) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
