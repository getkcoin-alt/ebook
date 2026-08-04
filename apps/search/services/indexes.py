"""Index definitions: attributes, ranking, typo tolerance, stop words, synonyms.

This module is the search quality of the platform. Everything else moves documents
around; this decides what "relevant" means.

Four settings do most of the work:

**Searchable attribute order is a ranking signal.** Meilisearch's ``attribute``
rule ranks a match in an earlier attribute above a match in a later one, so the
order below is a priority list, not a field inventory. ``title`` first, the full
``description`` last — otherwise a book that merely mentions "Python" in a
paragraph outranks *Learning Python*.

**Ranking rules are ordered and the order is the algorithm.** ``sort`` sits before
``exactness`` so an explicit user sort wins; the two custom rules at the end break
ties by popularity and rating, which is what makes the empty query return
something worth looking at.

**Typo tolerance is off where a typo is meaningful.** An ISBN with one digit
changed is a different book, not a near miss.

**Synonyms are bidirectional and hand-curated.** "ml" and "machine learning" have
to reach each other, and no amount of typo tolerance gets you there — the strings
share two characters.
"""

from __future__ import annotations

from typing import Any

#: Primary key for every index this service owns.
PRIMARY_KEY = "id"

# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------

#: Order matters: earlier attributes rank higher via the `attribute` rule.
BOOK_SEARCHABLE_ATTRIBUTES: list[str] = [
    "title",
    "subtitle",
    "author_names",
    "tags",
    "category_names",
    "publisher",
    "summary",
    "description",
    "isbn",
]

#: Every attribute a filter or facet may reference. A filter on anything absent
#: from this list is a 400 from the engine, which is why query.py builds filters
#: from a whitelist that is asserted against this list in the tests.
BOOK_FILTERABLE_ATTRIBUTES: list[str] = [
    "category_slugs",
    "category_ids",
    "author_slugs",
    "author_ids",
    "language",
    "formats",
    "tags",
    "publisher",
    "price_minor",
    "price_bucket",
    "rating_average",
    "is_free",
    "status",
    "published_at_ts",
]

BOOK_SORTABLE_ATTRIBUTES: list[str] = [
    "published_at_ts",
    "price_minor",
    "rating_average",
    "popularity",
    "created_at_ts",
    "title",
]

#: The default rules, plus two tie-breakers. Order is significant.
BOOK_RANKING_RULES: list[str] = [
    "words",  # more matched query terms first
    "typo",  # fewer typos first
    "proximity",  # matched terms closer together first
    "attribute",  # match in a higher-priority attribute first
    "sort",  # the user's explicit sort, ahead of exactness
    "exactness",  # exact term match over a prefix match
    "popularity:desc",  # tie-break: what people actually read
    "rating_average:desc",  # tie-break: what they thought of it
]

#: Words carrying no discriminating power. Removing them stops "the" from
#: dragging in every book on the shelf, and shortens the posting lists scanned.
STOP_WORDS: list[str] = [
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "how",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "this",
    "to",
    "was",
    "will",
    "with",
]


def _bidirectional(pairs: list[tuple[str, list[str]]]) -> dict[str, list[str]]:
    """Expand curated pairs into the both-ways map Meilisearch wants."""
    table: dict[str, set[str]] = {}
    for term, equivalents in pairs:
        for other in equivalents:
            table.setdefault(term, set()).add(other)
            table.setdefault(other, set()).add(term)
            # Equivalents of the same term are equivalent to each other, so
            # "ai" reaches "machine learning" through "artificial intelligence".
            for sibling in equivalents:
                if sibling != other:
                    table.setdefault(other, set()).add(sibling)
    return {term: sorted(values) for term, values in sorted(table.items())}


#: Hand-curated and **bidirectional**. Meilisearch treats each mapping as one-way,
#: so every pair is expanded in both directions — a one-way "ml" -> "machine
#: learning" leaves someone searching the full phrase unable to find a book
#: tagged only "ml".
SYNONYMS: dict[str, list[str]] = _bidirectional(
    [
        ("ml", ["machine learning"]),
        ("ai", ["artificial intelligence"]),
        ("ds", ["data science"]),
        ("js", ["javascript"]),
        ("ts", ["typescript"]),
        ("py", ["python"]),
        ("db", ["database"]),
        ("k8s", ["kubernetes"]),
        ("nlp", ["natural language processing"]),
        ("dl", ["deep learning"]),
        ("cs", ["computer science"]),
        ("ux", ["user experience"]),
        ("ui", ["user interface"]),
        ("devops", ["dev ops"]),
        ("ebook", ["e-book", "digital book"]),
        ("audiobook", ["audio book"]),
        ("sci-fi", ["science fiction"]),
        ("kids", ["children", "childrens"]),
    ]
)

#: Typo tolerance, tightened where a near-miss is a different thing entirely.
TYPO_TOLERANCE: dict[str, Any] = {
    "enabled": True,
    "minWordSizeForTypos": {
        # Below 5 characters one typo can turn any word into any other; below 9,
        # two typos can. These floors are what stop "cat" matching "cap".
        "oneTypo": 5,
        "twoTypos": 9,
    },
    "disableOnWords": ["epub", "mobi", "pdf", "isbn", "api", "sql"],
    #: An ISBN is a checksummed identifier. A one-digit "typo" is another book.
    "disableOnAttributes": ["isbn"],
}

#: Returned by search. Kept explicit so adding a field to the document does not
#: silently start shipping it to browsers — some fields are indexing-only.
BOOK_DISPLAYED_ATTRIBUTES: list[str] = [
    "id",
    "slug",
    "title",
    "subtitle",
    "description",
    "summary",
    "authors",
    "categories",
    "tags",
    "formats",
    "language",
    "price_minor",
    "currency",
    "compare_at_price_minor",
    "is_free",
    "rating_average",
    "rating_count",
    "review_count",
    "cover_url",
    "publisher",
    "published_at",
    "published_at_ts",
    "popularity",
]

BOOK_SETTINGS: dict[str, Any] = {
    "searchableAttributes": BOOK_SEARCHABLE_ATTRIBUTES,
    "filterableAttributes": BOOK_FILTERABLE_ATTRIBUTES,
    "sortableAttributes": BOOK_SORTABLE_ATTRIBUTES,
    "displayedAttributes": BOOK_DISPLAYED_ATTRIBUTES,
    "rankingRules": BOOK_RANKING_RULES,
    "stopWords": STOP_WORDS,
    "synonyms": SYNONYMS,
    "typoTolerance": TYPO_TOLERANCE,
    "faceting": {"maxValuesPerFacet": 100},
    "pagination": {
        # Deep pagination is a denial-of-service vector against the engine; the
        # API exposes cursors and never needs to reach past this.
        "maxTotalHits": 5000
    },
}

# ---------------------------------------------------------------------------
# Authors and categories
# ---------------------------------------------------------------------------

AUTHOR_SETTINGS: dict[str, Any] = {
    "searchableAttributes": ["name", "bio"],
    "filterableAttributes": ["is_featured", "book_count"],
    "sortableAttributes": ["book_count", "rating_average", "name"],
    "displayedAttributes": [
        "id",
        "slug",
        "name",
        "bio",
        "avatar_url",
        "book_count",
        "rating_average",
    ],
    "rankingRules": ["words", "typo", "proximity", "attribute", "sort", "exactness"],
    "stopWords": STOP_WORDS,
    "typoTolerance": TYPO_TOLERANCE,
}

CATEGORY_SETTINGS: dict[str, Any] = {
    "searchableAttributes": ["name", "description"],
    "filterableAttributes": ["parent_slug", "is_active"],
    "sortableAttributes": ["position", "book_count", "name"],
    "displayedAttributes": [
        "id",
        "slug",
        "name",
        "description",
        "parent_slug",
        "icon",
        "book_count",
        "position",
    ],
    "rankingRules": ["words", "typo", "proximity", "attribute", "sort", "exactness"],
    "stopWords": STOP_WORDS,
}


def settings_for(index_name: str, *, books: str, authors: str, categories: str) -> dict[str, Any]:
    """Configuration for one index, by name."""
    if index_name == books:
        return dict(BOOK_SETTINGS)
    if index_name == authors:
        return dict(AUTHOR_SETTINGS)
    if index_name == categories:
        return dict(CATEGORY_SETTINGS)
    raise ValueError(f"Unknown index: {index_name}")


def with_embedder(
    config: dict[str, Any], *, name: str, dimensions: int, source: str = "userProvided"
) -> dict[str, Any]:
    """Attach a vector embedder to an index configuration.

    Only called when ``SEMANTIC_ENABLED`` is true. ``userProvided`` means we
    compute vectors ourselves and ship them in the document's ``_vectors`` field,
    rather than handing Meilisearch an API key and letting it call a provider —
    keeping every outbound LLM call inside the ai service, where the budget is
    enforced. See the README's "Semantic search" section.
    """
    merged = dict(config)
    merged["embedders"] = {name: {"source": source, "dimensions": dimensions}}
    return merged
