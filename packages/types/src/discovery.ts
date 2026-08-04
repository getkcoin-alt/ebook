/** Search, recommendations and analytics. Owned by `apps/search` and `apps/admin`. */

import type { BookSummary } from './catalogue';
import type { ISODateTime, UUID } from './common';
import type { JobStatus } from './enums';

export interface SearchHit {
  book: BookSummary;
  /** Meilisearch relevance score, 0–1. Only meaningful within one result set. */
  score: number;
  /** `<mark>`-wrapped fragments. Already escaped server-side. */
  highlights: {
    title?: string;
    description?: string;
  };
}

export interface SearchFacetValue {
  value: string;
  label: string;
  count: number;
}

export interface SearchFacets {
  categories: SearchFacetValue[];
  authors: SearchFacetValue[];
  formats: SearchFacetValue[];
  tags: SearchFacetValue[];
  price_ranges: SearchFacetValue[];
}

export interface SearchResponse {
  query: string;
  hits: SearchHit[];
  facets: SearchFacets;
  /** Meilisearch's estimate — do not present it as exact. */
  estimated_total: number;
  /** Server-side round trip in ms; surfaced in the UI because it is fast. */
  took_ms: number;
  /** Populated when the query looks like a typo. */
  did_you_mean: string | null;
}

export interface SearchParams {
  q: string;
  limit?: number;
  offset?: number;
  categories?: string[];
  authors?: string[];
  formats?: string[];
  tags?: string[];
  min_price_minor?: number;
  max_price_minor?: number;
  sort?: 'relevance' | 'newest' | 'price_asc' | 'price_desc' | 'rating';
}

export interface SearchSuggestion {
  /** What to display and to put in the input on Tab. */
  text: string;
  kind: 'book' | 'author' | 'category' | 'query';
  /** Where selecting it navigates. */
  href: string;
  subtitle: string | null;
  image_url: string | null;
}

export interface RecommendationSet {
  /** Why these were chosen — shown as the section heading. */
  reason: 'similar' | 'also_bought' | 'trending' | 'for_you' | 'new_in_category';
  title: string;
  books: BookSummary[];
}

// ---- automation / research ---------------------------------------------

export interface AutomationJob {
  id: UUID;
  status: JobStatus;
  /** Pipeline stage names in order; `current_stage` indexes into this. */
  stages: AutomationStage[];
  current_stage: string | null;
  book_id: UUID | null;
  source_filename: string;
  error: string | null;
  created_at: ISODateTime;
  completed_at: ISODateTime | null;
}

export interface AutomationStage {
  name: string;
  status: JobStatus;
  started_at: ISODateTime | null;
  finished_at: ISODateTime | null;
  duration_ms: number | null;
  detail: string | null;
}

// ---- admin analytics ----------------------------------------------------

export interface MetricPoint {
  /** Bucket start. Granularity is whatever the query asked for. */
  t: ISODateTime;
  value: number;
}

export interface MetricSeries {
  key: string;
  label: string;
  points: MetricPoint[];
  /** Sum or latest depending on `aggregation`; precomputed for the tile. */
  total: number;
  /** Fractional change vs the previous equal-length window. Null on first period. */
  change: number | null;
  unit: 'count' | 'currency_minor' | 'percent' | 'ms';
}

export interface AdminOverview {
  revenue: MetricSeries;
  orders: MetricSeries;
  new_users: MetricSeries;
  books_published: MetricSeries;
  top_books: { book: BookSummary; units: number; revenue_minor: number }[];
  generated_at: ISODateTime;
}
