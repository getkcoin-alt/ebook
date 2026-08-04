/**
 * Envelope types every endpoint on the platform shares.
 *
 * Mirrors `knowledgeos_core.pagination` (Page / CursorPage) and
 * `knowledgeos_core.errors` (the single error shape).
 */

import type { Currency, SortOrder } from './enums';

/** UUID as it arrives over the wire. Aliased for readability, not safety. */
export type UUID = string;

/** ISO-8601 timestamp string, e.g. `2026-08-04T09:12:33.481Z`. */
export type ISODateTime = string;

export interface Timestamped {
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

/**
 * Money, always in **minor units** — paise, cents.
 *
 * A float cannot represent 0.1, so a float price column loses money at scale and
 * produces invoices that do not reconcile. Nothing in this codebase turns an
 * amount into a Number for arithmetic; formatting for display is the only
 * legitimate conversion and it lives in `@knowledgeos/utils/formatMoney`.
 */
export interface MoneyAmount {
  amount_minor: number;
  currency: Currency;
}

// ---- errors -------------------------------------------------------------

/**
 * The error body every service returns:
 * `{"error": {"code", "message", "details", "request_id"}}`
 *
 * `code` is stable and machine-readable — branch on it. `message` is human-facing
 * and safe to surface directly.
 */
export interface ApiErrorDetail {
  code: string;
  message: string;
  details?: Record<string, unknown> | null;
  request_id?: string | null;
}

export interface ApiErrorResponse {
  error: ApiErrorDetail;
}

/** The stable `code` values produced by `knowledgeos_core.errors`. */
export const ApiErrorCode = {
  BAD_REQUEST: 'bad_request',
  UNAUTHORIZED: 'unauthorized',
  TOKEN_EXPIRED: 'token_expired',
  PAYMENT_REQUIRED: 'payment_required',
  FORBIDDEN: 'forbidden',
  RESOURCE_NOT_FOUND: 'resource_not_found',
  METHOD_NOT_ALLOWED: 'method_not_allowed',
  CONFLICT: 'conflict',
  PAYLOAD_TOO_LARGE: 'payload_too_large',
  UNSUPPORTED_MEDIA_TYPE: 'unsupported_media_type',
  VALIDATION_ERROR: 'validation_error',
  RATE_LIMITED: 'rate_limited',
  INTERNAL_ERROR: 'internal_error',
  UPSTREAM_ERROR: 'upstream_error',
  UPSTREAM_TIMEOUT: 'upstream_timeout',
  SERVICE_UNAVAILABLE: 'service_unavailable',
  /** Client-side only: the request never reached a server. */
  NETWORK_ERROR: 'network_error',
} as const;
export type ApiErrorCode = (typeof ApiErrorCode)[keyof typeof ApiErrorCode];

/** Field-level validation failures, as FastAPI reports them under `details.fields`. */
export interface ValidationFieldError {
  loc: (string | number)[];
  msg: string;
  type: string;
}

// ---- pagination ---------------------------------------------------------

export interface PageMeta {
  page: number;
  limit: number;
  total: number;
  pages: number;
  has_next: boolean;
  has_prev: boolean;
}

/** Offset-paginated envelope. For admin tables — "page 4 of 27". Costs a COUNT(*). */
export interface Page<T> {
  items: T[];
  meta: PageMeta;
}

/**
 * Keyset-paginated envelope. For infinite scroll and public catalogue feeds.
 *
 * Stays O(limit) however deep the reader scrolls, and never skips or duplicates a
 * row when a book is published mid-scroll. `OFFSET 40000` on a million-row
 * catalogue is what takes a database down, so the catalogue views use this.
 */
export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface PageParams {
  page?: number;
  limit?: number;
}

export interface CursorParams {
  cursor?: string | null;
  limit?: number;
}

export interface SortParams {
  sort_by?: string;
  sort_order?: SortOrder;
}

export const DEFAULT_PAGE_SIZE = 20;
export const MAX_PAGE_SIZE = 100;

// ---- small shared responses --------------------------------------------

export interface MessageResponse {
  message: string;
  success: boolean;
}

export interface IdResponse {
  id: UUID;
}

export interface ListResponse<T> {
  items: T[];
  total: number;
}

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  environment: string;
  uptime_seconds: number;
}
