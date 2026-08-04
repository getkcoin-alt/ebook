/** Catalogue domain: books, authors, categories, reviews. Owned by `apps/books`. */

import type { ISODateTime, MoneyAmount, Timestamped, UUID } from './common';
import type { BookFormat, BookStatus, Currency } from './enums';

export interface Author extends Timestamped {
  id: UUID;
  slug: string;
  name: string;
  bio: string | null;
  avatar_url: string | null;
  website_url: string | null;
  book_count: number;
  /** 0–5, one decimal. Null until the author has a rated book. */
  average_rating: number | null;
}

export interface Category extends Timestamped {
  id: UUID;
  slug: string;
  name: string;
  description: string | null;
  /** Null for a top-level category. Categories are one level deep by design. */
  parent_id: UUID | null;
  icon: string | null;
  book_count: number;
  /** Manual ordering for the nav; ties broken by name. */
  position: number;
}

/** One downloadable/readable artefact of a book. */
export interface BookAsset {
  format: BookFormat;
  /** Bytes. Rendered with `formatBytes`; never assume it fits a display unit. */
  size_bytes: number;
  /** Present only when the caller is entitled; the API omits it otherwise. */
  download_url?: string | null;
  page_count: number | null;
}

export interface BookCover {
  /** Original upload; use `next/image` and let it emit AVIF/WebP. */
  url: string;
  /** Tiny base64 JPEG for `placeholder="blur"`. */
  blur_data_url: string | null;
  width: number;
  height: number;
  /** Dominant colour, `#rrggbb` — used for the card's ambient glow. */
  accent_color: string | null;
}

/** The card-sized projection returned by list endpoints. */
export interface BookSummary {
  id: UUID;
  slug: string;
  title: string;
  subtitle: string | null;
  authors: Pick<Author, 'id' | 'slug' | 'name'>[];
  cover: BookCover | null;
  price: MoneyAmount;
  /** Pre-discount price when the book is on offer, otherwise null. */
  compare_at_price: MoneyAmount | null;
  status: BookStatus;
  average_rating: number | null;
  review_count: number;
  formats: BookFormat[];
  published_at: ISODateTime | null;
  /** True when the requesting user already owns it. Anonymous callers get false. */
  is_owned: boolean;
  is_wishlisted: boolean;
}

/** The full record returned by `/v1/books/{slug}`. */
export interface Book extends BookSummary, Timestamped {
  description: string;
  /**
   * A 2–3 sentence factual precis written for answer engines: no marketing, no
   * pronouns, front-loaded facts. Rendered verbatim into the page and into the
   * JSON-LD `description`, which is what an LLM actually quotes.
   */
  summary: string | null;
  categories: Pick<Category, 'id' | 'slug' | 'name'>[];
  tags: string[];
  language: string;
  isbn: string | null;
  publisher: string | null;
  page_count: number | null;
  word_count: number | null;
  /** Minutes, at 240 wpm. Server-computed so SSR and CSR agree. */
  reading_minutes: number | null;
  assets: BookAsset[];
  /** Free sample, readable without a purchase. */
  preview_available: boolean;
  seo_title: string | null;
  seo_description: string | null;
  /** Extracted question/answer pairs; drives the FAQPage JSON-LD. */
  faqs: BookFaq[];
  rating_breakdown: RatingBreakdown | null;
}

export interface BookFaq {
  question: string;
  answer: string;
}

/** Count of reviews per star value, 1-indexed by star. */
export interface RatingBreakdown {
  '1': number;
  '2': number;
  '3': number;
  '4': number;
  '5': number;
}

export interface Review extends Timestamped {
  id: UUID;
  book_id: UUID;
  user_id: UUID;
  user_name: string;
  user_avatar_url: string | null;
  /** Integer 1–5. */
  rating: number;
  title: string | null;
  body: string;
  /** True when the reviewer's purchase was verified against an order. */
  is_verified_purchase: boolean;
  helpful_count: number;
  /** Whether the *current* user has marked it helpful. */
  is_helpful_to_me: boolean;
}

export interface ReviewInput {
  rating: number;
  title?: string | null;
  body: string;
}

// ---- query parameters ---------------------------------------------------

export interface BookListFilters {
  category?: string;
  author?: string;
  tag?: string;
  format?: BookFormat;
  status?: BookStatus;
  currency?: Currency;
  /** Inclusive bounds, minor units. */
  min_price_minor?: number;
  max_price_minor?: number;
  min_rating?: number;
  /** Admin-only ILIKE fallback. Catalogue search goes through `/v1/search`. */
  q?: string;
}

export const BOOK_SORT_FIELDS = [
  'created_at',
  'published_at',
  'title',
  'price_minor',
  'average_rating',
  'review_count',
] as const;
export type BookSortField = (typeof BOOK_SORT_FIELDS)[number];
