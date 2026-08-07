#!/usr/bin/env python3
"""Fill the catalogue with real, redistributable books.

Source is Project Gutenberg via the Gutendex API. Everything it serves is public
domain in the United States and explicitly free to redistribute *and* to sell, so
these are books the platform may lawfully hand to a paying customer — which is the
whole point: after checkout the download URL has to resolve to a real file, not a
placeholder.

The pipeline is the same one an operator performs by hand, driven over the public
API rather than by writing to the database:

    author  -> POST /v1/authors
    category-> POST /v1/categories          (from Gutenberg's subject headings)
    book    -> POST /v1/admin/books         (draft; no file yet)
    file    -> POST /v1/admin/books/uploads -> presigned POST -> object storage
    cover   -> same, as an image
    version -> POST /v1/admin/books/{id}/versions
    publish -> POST /v1/admin/books/{id}/publish

Publication is refused until a readable file exists, so a book only ever becomes
visible once its EPUB is genuinely in storage. That ordering is deliberate and is
why the script uploads before it publishes rather than after.

Usage:

    KOS_ADMIN_EMAIL=… KOS_ADMIN_PASSWORD=… python scripts/ingest-gutenberg.py --count 60
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass, field

import httpx

API = os.environ.get("KOS_GATEWAY", "https://api.allelearning.in")

#: Prices are integers of the minor unit — paise. ₹200 to ₹4,000.
PRICE_MIN_MINOR = 200_00
PRICE_MAX_MINOR = 4000_00

#: Gutenberg subject headings are long Library-of-Congress strings
#: ("Detective and mystery stories -- England -- Fiction"). The catalogue wants a
#: browsable shelf, so they are mapped onto a small closed set. Order matters: the
#: first match wins, so the more specific patterns come first.
SHELVES: list[tuple[str, tuple[str, ...]]] = [
    ("Detective & Mystery", ("detective", "mystery", "crime")),
    ("Science Fiction", ("science fiction", "utopia")),
    ("Horror & Gothic", ("horror", "ghost", "gothic", "vampire")),
    ("Adventure", ("adventure", "sea stories", "pirates", "voyages")),
    ("Romance", ("love stories", "romance", "courtship")),
    ("Historical Fiction", ("historical fiction", "history -- fiction")),
    ("Poetry", ("poetry", "poems")),
    ("Drama", ("drama", "tragedies", "comedies", "plays")),
    ("Philosophy", ("philosophy", "ethics", "logic", "metaphysics")),
    ("Psychology", ("psychology", "mind")),
    ("Economics & Politics", ("economics", "political", "government", "capital")),
    ("Science & Nature", ("science", "natural history", "biology", "astronomy", "evolution")),
    ("History", ("history", "biography", "autobiography")),
    ("Children's", ("children", "juvenile", "fairy tales")),
    ("Short Stories", ("short stories",)),
    ("Fiction", ("fiction",)),
]

LANGUAGE_NAMES = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "ru": "Russian",
    "nl": "Dutch",
    "fi": "Finnish",
    "hu": "Hungarian",
    "el": "Greek",
    "la": "Latin",
}


def normalise_title(value: str) -> str:
    """A title reduced to what makes two records the same work.

    Editions differ in punctuation and subtitle — "The strange case of Dr.
    Jekyll and Mr. Hyde" against "The Strange Case of Dr. Jekyll and Mr.
    Hyde" — so the comparison is on letters and digits alone, with any
    trailing subtitle after a colon or semicolon dropped.
    """
    head = re.split(r"[:;]", value, maxsplit=1)[0]
    return re.sub(r"[^a-z0-9]+", "", head.lower())


def slugify(value: str, *, max_length: int = 90) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:max_length].strip("-") or "untitled"


def shelf_for(subjects: list[str], bookshelves: list[str]) -> str:
    haystack = " | ".join(subjects + bookshelves).lower()
    for name, needles in SHELVES:
        if any(needle in haystack for needle in needles):
            return name
    return "General"


def price_for(book: dict) -> int:
    """A stable price in the requested band.

    Derived from the id so a re-run does not silently reprice the catalogue, and
    nudged upward by popularity so the well-known titles are not the cheapest — a
    catalogue where everything costs the same reads as fake.
    """
    digest = hashlib.sha256(str(book["id"]).encode()).digest()
    base = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # 0.0 to 1.0
    downloads = min(book.get("download_count", 0), 40_000) / 40_000  # 0.0 to 1.0
    weighted = 0.55 * base + 0.45 * downloads
    price = PRICE_MIN_MINOR + weighted * (PRICE_MAX_MINOR - PRICE_MIN_MINOR)
    return int(round(price / 5000) * 5000)  # to the nearest ₹50


@dataclass
class Totals:
    created: int = 0
    skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


class Ingester:
    def __init__(self, client: httpx.Client, headers: dict[str, str]) -> None:
        self.c = client
        self.h = headers
        self.authors: dict[str, str] = {}
        self.categories: dict[str, str] = {}
        self.existing: set[str] = set()
        #: Normalised titles already on the shelf. The slug carries the
        #: Gutenberg id, so two editions of one work produce two different
        #: slugs and the slug check waves both through — which is how the
        #: catalogue ended up listing Jekyll and Hyde twice, at two prices.
        self.existing_titles: set[str] = set()

    # ---- catalogue scaffolding -----------------------------------------

    def load_existing_slugs(self) -> None:
        cursor = None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self.c.get("/v1/admin/books", headers=self.h, params=params).json()
            for row in page.get("items", []):
                self.existing.add(row["slug"])
                self.existing_titles.add(normalise_title(row["title"]))
            cursor = page.get("next_cursor")
            if not cursor or not page.get("has_more"):
                break
        print(f"catalogue already holds {len(self.existing)} book(s)")

    def author_id(self, name: str) -> str | None:
        if name in self.authors:
            return self.authors[name]
        slug = slugify(name)
        r = self.c.post("/v1/authors", headers=self.h, json={"name": name, "slug": slug})
        if r.status_code in (200, 201):
            self.authors[name] = r.json()["id"]
        elif r.status_code == 409:
            # Already there from an earlier run; find it rather than giving up.
            found = self.c.get("/v1/authors", headers=self.h, params={"q": name, "limit": 5})
            for row in found.json().get("items", []) if found.status_code == 200 else []:
                if row.get("slug") == slug or row.get("name") == name:
                    self.authors[name] = row["id"]
                    break
        return self.authors.get(name)

    def category_id(self, name: str) -> str | None:
        if name in self.categories:
            return self.categories[name]
        slug = slugify(name)
        r = self.c.post("/v1/categories", headers=self.h, json={"name": name, "slug": slug})
        if r.status_code in (200, 201):
            self.categories[name] = r.json()["id"]
        elif r.status_code == 409:
            found = self.c.get("/v1/categories", headers=self.h, params={"limit": 100})
            for row in found.json().get("items", []) if found.status_code == 200 else []:
                if row.get("slug") == slug:
                    self.categories[name] = row["id"]
                    break
        return self.categories.get(name)

    # ---- object storage -------------------------------------------------

    def upload(self, category: str, filename: str, content_type: str, blob: bytes) -> str | None:
        """Mint a presigned target and put the bytes straight into storage.

        The file never passes through the API: proxying a 40MB download would
        occupy a worker for the whole transfer.
        """
        r = self.c.post(
            "/v1/admin/books/uploads",
            headers=self.h,
            json={"category": category, "filename": filename, "content_type": content_type},
        )
        if r.status_code not in (200, 201):
            return None
        target = r.json()
        with httpx.Client(timeout=180.0) as raw:
            put = raw.post(
                target["url"],
                data=target.get("fields") or {},
                files={"file": (filename, blob, content_type)},
            )
        return target["key"] if put.status_code in (200, 201, 204) else None

    # ---- one book -------------------------------------------------------

    def ingest(self, book: dict, totals: Totals) -> None:
        title = (book.get("title") or "").strip().replace("\n", " ")
        if not title:
            return
        slug = slugify(f"{title}-{book['id']}")
        key = normalise_title(title)
        if slug in self.existing or key in self.existing_titles:
            totals.skipped += 1
            return

        formats = book.get("formats", {})
        epub_url = next(
            (u for m, u in formats.items() if m.startswith("application/epub+zip")), None
        )
        if not epub_url:
            totals.skipped += 1
            return
        cover_url = next((u for m, u in formats.items() if m.startswith("image/jpeg")), None)

        try:
            epub = fetch(epub_url)
            cover = fetch(cover_url, required=False) if cover_url else None
        except Exception as exc:
            totals.failed.append((title, f"download: {type(exc).__name__}"))
            return

        if not epub or len(epub) < 1024:
            totals.failed.append((title, "empty epub"))
            return

        people = [a["name"] for a in book.get("authors", []) if a.get("name")]
        author_ids = [i for i in (self.author_id(n) for n in people[:3]) if i]
        shelf = shelf_for(book.get("subjects", []), book.get("bookshelves", []))
        category_ids = [i for i in [self.category_id(shelf)] if i]
        language = (book.get("languages") or ["en"])[0]

        # Gutenberg has no blurb, and inventing one would be inventing a fact about
        # a book. State what is true instead.
        author_line = ", ".join(_display_name(n) for n in people) or "an unknown hand"
        description = (
            f"{title} by {author_line}. A Project Gutenberg edition, in the public "
            f"domain and free to keep. Delivered as EPUB, readable in the browser or "
            f"on any e-reader."
        )

        payload = {
            "title": title[:500],
            "slug": slug,
            "description": description,
            "price_minor": price_for(book),
            "currency": "INR",
            "language": language,
            "formats": ["epub"],
            "meta_title": f"{title} — allelearning.in"[:255],
            "meta_description": description[:500],
        }
        if author_ids:
            payload["author_ids"] = [{"author_id": a, "role": "author"} for a in author_ids]
        if category_ids:
            payload["category_ids"] = category_ids

        created = self.c.post("/v1/admin/books", headers=self.h, json=payload)
        if created.status_code not in (200, 201):
            totals.failed.append((title, f"create {created.status_code}: {created.text[:110]}"))
            return
        book_id = created.json()["id"]

        epub_key = self.upload("book", f"{slug}.epub", "application/epub+zip", epub)
        if not epub_key:
            totals.failed.append((title, "epub upload failed"))
            return

        cover_key = None
        if cover:
            cover_key = self.upload("cover", f"{slug}.jpg", "image/jpeg", cover)
            if cover_key:
                self.c.patch(
                    f"/v1/admin/books/{book_id}",
                    headers=self.h,
                    json={"cover_key": cover_key, "thumbnail_key": cover_key},
                )

        version = self.c.post(
            f"/v1/admin/books/{book_id}/versions",
            headers=self.h,
            json={
                "epub_key": epub_key,
                "file_size_bytes": len(epub),
                "changelog": "Project Gutenberg edition.",
            },
        )
        if version.status_code not in (200, 201):
            totals.failed.append((title, f"version {version.status_code}: {version.text[:110]}"))
            return

        published = self.c.post(f"/v1/admin/books/{book_id}/publish", headers=self.h)
        if published.status_code not in (200, 201):
            totals.failed.append(
                (title, f"publish {published.status_code}: {published.text[:110]}")
            )
            return

        self.existing.add(slug)
        self.existing_titles.add(key)
        totals.created += 1
        price = payload["price_minor"] / 100
        print(
            f"  + {title[:44]:<44} ₹{price:>7,.0f}  {shelf:<20} "
            f"{len(epub) // 1024:>5}KB {'cover' if cover_key else '     '}"
        )


#: Gutenberg asks that automated clients identify themselves, and it drops
#: connections from anonymous ones under load — which is what the first run hit.
_UA = "allelearning.in catalogue ingester (+https://allelearning.in)"


def fetch(url: str, *, required: bool = True, attempts: int = 4) -> bytes | None:
    """Download with backoff.

    Gutenberg's mirrors reset connections when busy, and a single attempt lost five
    of nine books on the first run — including Pride and Prejudice and Sherlock
    Holmes, which is exactly the sort of absence nobody notices in a catalogue of
    sixty.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with httpx.Client(
                timeout=240.0, follow_redirects=True, headers={"User-Agent": _UA}
            ) as raw:
                response = raw.get(url)
                response.raise_for_status()
                return response.content
        except Exception as exc:  # connection reset, protocol error, 5xx
            last = exc
            time.sleep(2 * (attempt + 1))
    if required and last is not None:
        raise last
    return None


def _display_name(name: str) -> str:
    """Gutenberg stores "Austen, Jane"; a bookshelf says "Jane Austen"."""
    if "," in name:
        family, _, given = name.partition(",")
        given = given.split("(")[0].strip()
        if given:
            return f"{given} {family.strip()}"
    return name.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=60, help="books to publish")
    parser.add_argument("--languages", default="en", help="comma-separated ISO codes")
    args = parser.parse_args()

    with httpx.Client(base_url=API, timeout=90.0) as c:
        login = c.post(
            "/v1/auth/login",
            json={
                "email": os.environ["KOS_ADMIN_EMAIL"],
                "password": os.environ["KOS_ADMIN_PASSWORD"],
            },
        )
        if login.status_code != 200:
            print("login failed:", login.status_code, login.text[:200])
            return 1
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        ing = Ingester(c, headers)
        ing.load_existing_slugs()

        totals = Totals()
        page_url = "https://gutendex.com/books"
        params = {"languages": args.languages, "sort": "popular", "mime_type": "application/epub"}

        print(f"\npublishing up to {args.count} book(s)\n")
        with httpx.Client(timeout=90.0, follow_redirects=True) as feed:
            while totals.created < args.count and page_url:
                page = feed.get(page_url, params=params if "?" not in page_url else None).json()
                for book in page.get("results", []):
                    if totals.created >= args.count:
                        break
                    try:
                        ing.ingest(book, totals)
                    except Exception as exc:
                        totals.failed.append(
                            (book.get("title", "?"), f"{type(exc).__name__}: {exc}")
                        )
                    # The token lives 15 minutes; refresh well inside that.
                    time.sleep(0.5)
                    if totals.created and totals.created % 25 == 0:
                        again = c.post(
                            "/v1/auth/login",
                            json={
                                "email": os.environ["KOS_ADMIN_EMAIL"],
                                "password": os.environ["KOS_ADMIN_PASSWORD"],
                            },
                        )
                        if again.status_code == 200:
                            headers["Authorization"] = f"Bearer {again.json()['access_token']}"
                            time.sleep(1)
                page_url = page.get("next")

    print(f"\n{'=' * 70}")
    print(f"published {totals.created} · skipped {totals.skipped} · failed {len(totals.failed)}")
    for title, why in totals.failed[:12]:
        print(f"  - {title[:44]}: {why}")
    return 0 if totals.created else 1


if __name__ == "__main__":
    sys.exit(main())
