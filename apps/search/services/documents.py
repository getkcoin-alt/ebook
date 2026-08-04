"""Mapping from a books-service record to an index document.

The index is denormalised on purpose: a result card must render with no further
network calls, so author names, category names and the cover URL are copied in
rather than referenced. That is also why the reconciliation task exists — a
denormalised copy drifts, and something has to notice.

Every function here is defensive about missing keys. This service consumes another
service's JSON; a field that has not shipped yet must produce a slightly poorer
document, never a ``KeyError`` that dead-letters the event.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import orjson

#: Price buckets, in minor units (paise). Meilisearch facets on exact values, so
#: a "price range" facet needs a precomputed bucket attribute — faceting on
#: `price_minor` itself would return one bucket per distinct price.
PRICE_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("free", 0, 0),
    ("under_199", 1, 19_900),
    ("199_499", 19_901, 49_900),
    ("499_999", 49_901, 99_900),
    ("over_999", 99_901, None),
)


def price_bucket(price_minor: int) -> str:
    for label, low, high in PRICE_BUCKETS:
        if price_minor >= low and (high is None or price_minor <= high):
            return label
    return "over_999"


PRICE_BUCKET_LABELS: dict[str, str] = {
    "free": "Free",
    "under_199": "Under ₹199",
    "199_499": "₹199 – ₹499",
    "499_999": "₹499 – ₹999",
    "over_999": "Over ₹999",
}


def _timestamp(value: Any) -> int | None:
    """ISO string or datetime to a Unix timestamp.

    Meilisearch sorts numerically; an ISO string sorts lexicographically, which
    happens to be correct for UTC ISO-8601 and silently wrong the moment an
    offset appears. Store both: the string to display, the number to sort.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())


def _people(raw: Any) -> list[dict[str, str]]:
    """Normalise an authors/categories list, tolerating several shapes."""
    if not isinstance(raw, Iterable) or isinstance(raw, str | bytes | Mapping):
        return []
    people: list[dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            name = str(entry.get("name") or entry.get("title") or "").strip()
            if not name:
                continue
            people.append(
                {
                    "id": str(entry.get("id", "")),
                    "slug": str(entry.get("slug", "")),
                    "name": name,
                }
            )
        elif isinstance(entry, str) and entry.strip():
            people.append({"id": "", "slug": "", "name": entry.strip()})
    return people


def _strings(raw: Any) -> list[str]:
    if not isinstance(raw, Iterable) or isinstance(raw, str | bytes | Mapping):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def build_book_document(book: Mapping[str, Any]) -> dict[str, Any]:
    """Render one book into its index document."""
    authors = _people(book.get("authors"))
    categories = _people(book.get("categories"))
    price_minor = int(book.get("price_minor") or 0)
    rating_average = book.get("rating_average")
    rating_count = int(book.get("rating_count") or 0)
    published_at = book.get("published_at")

    document: dict[str, Any] = {
        "id": str(book.get("id", "")),
        "slug": str(book.get("slug", "")),
        "title": str(book.get("title") or ""),
        "subtitle": book.get("subtitle"),
        "description": str(book.get("description") or ""),
        "summary": book.get("summary"),
        "authors": authors,
        "author_names": [person["name"] for person in authors],
        "author_slugs": [person["slug"] for person in authors if person["slug"]],
        "author_ids": [person["id"] for person in authors if person["id"]],
        "categories": categories,
        "category_names": [category["name"] for category in categories],
        "category_slugs": [category["slug"] for category in categories if category["slug"]],
        "category_ids": [category["id"] for category in categories if category["id"]],
        "tags": _strings(book.get("tags")),
        "formats": _strings(book.get("formats")),
        "language": str(book.get("language") or "en"),
        "publisher": book.get("publisher"),
        "isbn": book.get("isbn"),
        "price_minor": price_minor,
        "currency": str(book.get("currency") or "INR"),
        "compare_at_price_minor": book.get("compare_at_price_minor"),
        "is_free": price_minor == 0,
        "price_bucket": price_bucket(price_minor),
        # Meilisearch cannot filter on null with a numeric comparison, so an
        # unrated book is 0.0 — and the API's rating filter is documented as
        # "at least", which reads correctly for it.
        "rating_average": float(rating_average) if rating_average is not None else 0.0,
        "rating_count": rating_count,
        "review_count": int(book.get("review_count") or rating_count),
        "cover_url": book.get("cover_url"),
        "page_count": book.get("page_count"),
        "reading_minutes": book.get("reading_minutes"),
        "status": str(book.get("status") or "published"),
        "published_at": published_at,
        "published_at_ts": _timestamp(published_at),
        "created_at_ts": _timestamp(book.get("created_at")),
        "popularity": _popularity(book),
    }
    return document


def _popularity(book: Mapping[str, Any]) -> float:
    """A single number the ranking rules can tie-break on.

    Built from what the catalogue already denormalises. Ratings are weighted by
    how many there are, because a lone five-star review is not evidence: the
    ``log1p`` damps a book with 10,000 ratings from burying everything else.
    """
    import math

    rating = float(book.get("rating_average") or 0.0)
    ratings = int(book.get("rating_count") or 0)
    views = int(book.get("view_count") or 0)
    purchases = int(book.get("purchase_count") or 0)
    return round(
        rating * math.log1p(ratings) * 2.0 + math.log1p(views) + math.log1p(purchases) * 3.0,
        4,
    )


def build_author_document(author: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(author.get("id", "")),
        "slug": str(author.get("slug", "")),
        "name": str(author.get("name") or ""),
        "bio": author.get("bio"),
        "avatar_url": author.get("avatar_url"),
        "book_count": int(author.get("book_count") or 0),
        "rating_average": float(author.get("rating_average") or 0.0),
        "is_featured": bool(author.get("is_featured", False)),
    }


def build_category_document(category: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(category.get("id", "")),
        "slug": str(category.get("slug", "")),
        "name": str(category.get("name") or ""),
        "description": category.get("description"),
        "parent_slug": category.get("parent_slug"),
        "icon": category.get("icon"),
        "book_count": int(category.get("book_count") or 0),
        "position": int(category.get("position") or 0),
        "is_active": bool(category.get("is_active", True)),
    }


def checksum(document: Mapping[str, Any]) -> str:
    """Stable hash of a document.

    ``sort_keys`` is what makes it stable — without it two structurally identical
    documents hash differently depending on dict insertion order, every event
    looks like a change, and the idempotency ledger stops preventing anything.
    """
    return hashlib.sha256(orjson.dumps(document, option=orjson.OPT_SORT_KEYS)).hexdigest()
