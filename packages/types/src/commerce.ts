/** Commerce domain: orders, invoices, entitlements, coupons. Owned by `apps/payment`. */

import type { BookSummary } from './catalogue';
import type { ISODateTime, MoneyAmount, Timestamped, UUID } from './common';
import type { Currency, OrderStatus, PaymentProvider } from './enums';

export interface OrderItem {
  id: UUID;
  book_id: UUID;
  /** Denormalised at purchase time — the catalogue may change or unpublish later. */
  title: string;
  slug: string;
  cover_url: string | null;
  /** Unit price as charged, minor units. Never re-read from the catalogue. */
  unit_price: MoneyAmount;
  quantity: number;
  subtotal: MoneyAmount;
}

export interface Order extends Timestamped {
  id: UUID;
  /** Human-quotable reference, e.g. `KOS-2026-0004821`. */
  reference: string;
  user_id: UUID;
  status: OrderStatus;
  items: OrderItem[];
  currency: Currency;
  subtotal: MoneyAmount;
  discount: MoneyAmount;
  /** India GST on digital goods, computed server-side. */
  tax: MoneyAmount;
  total: MoneyAmount;
  refunded_amount: MoneyAmount;
  coupon_code: string | null;
  provider: PaymentProvider | null;
  /** Provider's id. Shown to support, never used for authorisation. */
  provider_payment_id: string | null;
  paid_at: ISODateTime | null;
  invoice_url: string | null;
  failure_reason: string | null;
}

export interface CartLine {
  book: BookSummary;
  quantity: number;
}

export interface CheckoutInput {
  book_ids: UUID[];
  coupon_code?: string;
  currency: Currency;
  provider: PaymentProvider;
  /** Where the provider returns the buyer. Must be same-origin. */
  return_url: string;
}

/**
 * What the API hands back to start a payment.
 *
 * `provider_payload` is opaque on purpose — it is whatever Razorpay or Stripe's
 * browser SDK needs, and the shape is the provider's, not ours. It never contains
 * a secret key; only a publishable/order handle.
 */
export interface CheckoutSession {
  order_id: UUID;
  reference: string;
  provider: PaymentProvider;
  amount: MoneyAmount;
  provider_payload: Record<string, unknown>;
  expires_at: ISODateTime;
}

export interface CouponPreview {
  code: string;
  is_valid: boolean;
  /** Present only when valid. */
  discount?: MoneyAmount;
  message: string;
}

/** Proof the user may read a book. Granted by `entitlement.granted`. */
export interface Entitlement {
  book_id: UUID;
  order_id: UUID | null;
  granted_at: ISODateTime;
  /** Null for a perpetual purchase; set for subscription access. */
  expires_at: ISODateTime | null;
  source: 'purchase' | 'subscription' | 'gift' | 'free';
}

/** A book in the user's library: the book plus how far through it they are. */
export interface LibraryEntry {
  book: BookSummary;
  entitlement: Entitlement;
  progress: ReadingProgress | null;
  last_read_at: ISODateTime | null;
}

export interface WishlistEntry {
  book: BookSummary;
  added_at: ISODateTime;
}

// ---- reading state (owned by `apps/books`, but always read alongside the library)

export interface ReadingProgress {
  book_id: UUID;
  /** 0–1. The single source of truth for the progress bar. */
  percentage: number;
  /** Format-specific position: page index for PDF, CFI for EPUB. */
  location: string | null;
  /** Chapter/section id from the TOC, when resolvable. */
  chapter_id: string | null;
  updated_at: ISODateTime;
}

export interface Bookmark {
  id: UUID;
  book_id: UUID;
  location: string;
  chapter_id: string | null;
  /** The quoted text, for showing the bookmark without loading the book. */
  excerpt: string | null;
  note: string | null;
  /** Hex colour of the highlight, null for a plain bookmark. */
  color: string | null;
  created_at: ISODateTime;
}

export interface TocEntry {
  id: string;
  label: string;
  /** 1-based nesting depth. */
  level: number;
  location: string;
  children: TocEntry[];
}

/** One rendered unit of book content, streamed on demand. */
export interface ReaderChapter {
  id: string;
  label: string;
  /** Sanitised HTML. Sanitising happens server-side; the client does not re-parse. */
  html: string;
  word_count: number;
  /** 0-based index into the flattened TOC. */
  index: number;
}

export interface ReaderManifest {
  book_id: UUID;
  slug: string;
  title: string;
  authors: string[];
  toc: TocEntry[];
  /** Total chapters; drives the progress denominator. */
  chapter_count: number;
  word_count: number;
  /** False when the caller only has the free sample. */
  is_full_access: boolean;
}
