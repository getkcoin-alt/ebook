/**
 * Domain enums — a 1:1 mirror of `packages/core-py/knowledgeos_core/schemas.py`.
 *
 * These are `const` objects plus a derived union type rather than TS `enum`s, for
 * three reasons:
 *
 * 1. `verbatimModuleSyntax` + `isolatedModules` make TS enums awkward to re-export.
 * 2. A TS enum is a *nominal* type: `BookStatus.PUBLISHED` is not assignable from
 *    the string `"published"` that actually arrives over the wire.
 * 3. The union erases completely, so nothing ships to the browser.
 *
 * The Python side is `StrEnum`, so the wire value is always the plain string.
 */

export const UserRole = {
  USER: 'user',
  AUTHOR: 'author',
  MODERATOR: 'moderator',
  ADMIN: 'admin',
  SUPERADMIN: 'superadmin',
} as const;
export type UserRole = (typeof UserRole)[keyof typeof UserRole];

/** Least → most privileged. Used for "at least this role" comparisons. */
export const ROLE_RANK: Readonly<Record<UserRole, number>> = {
  user: 0,
  author: 1,
  moderator: 2,
  admin: 3,
  superadmin: 4,
};

export const Permission = {
  BOOKS_READ: 'books:read',
  BOOKS_WRITE: 'books:write',
  BOOKS_DELETE: 'books:delete',
  BOOKS_PUBLISH: 'books:publish',
  USERS_READ: 'users:read',
  USERS_WRITE: 'users:write',
  USERS_DELETE: 'users:delete',
  ORDERS_READ: 'orders:read',
  ORDERS_REFUND: 'orders:refund',
  REVIEWS_MODERATE: 'reviews:moderate',
  AUTOMATION_RUN: 'automation:run',
  AUTOMATION_READ: 'automation:read',
  ANALYTICS_READ: 'analytics:read',
  SETTINGS_WRITE: 'settings:write',
  AI_USE: 'ai:use',
} as const;
export type Permission = (typeof Permission)[keyof typeof Permission];

/**
 * A permission as it can appear *in a token*: either a concrete permission, a
 * `resource:*` wildcard, or the global `*`. The auth service embeds the resolved
 * set in the access token, and `Principal.has_permission` honours both wildcards.
 */
export type GrantedPermission = Permission | `${string}:*` | '*';

/**
 * Default grants per role, mirroring `ROLE_PERMISSIONS` in schemas.py.
 *
 * The frontend uses this only to render optimistic UI (e.g. showing an admin nav
 * item before the token has been decoded). Authorisation is always the server's
 * answer — this table is a hint, never a decision.
 */
export const ROLE_PERMISSIONS: Readonly<Record<UserRole, readonly Permission[]>> = {
  user: [Permission.BOOKS_READ, Permission.AI_USE],
  author: [
    Permission.BOOKS_READ,
    Permission.BOOKS_WRITE,
    Permission.AI_USE,
    Permission.AUTOMATION_RUN,
    Permission.AUTOMATION_READ,
  ],
  moderator: [
    Permission.BOOKS_READ,
    Permission.BOOKS_WRITE,
    Permission.REVIEWS_MODERATE,
    Permission.USERS_READ,
    Permission.AI_USE,
  ],
  admin: [
    Permission.BOOKS_READ,
    Permission.BOOKS_WRITE,
    Permission.BOOKS_DELETE,
    Permission.BOOKS_PUBLISH,
    Permission.USERS_READ,
    Permission.USERS_WRITE,
    Permission.ORDERS_READ,
    Permission.ORDERS_REFUND,
    Permission.REVIEWS_MODERATE,
    Permission.AUTOMATION_RUN,
    Permission.AUTOMATION_READ,
    Permission.ANALYTICS_READ,
    Permission.AI_USE,
  ],
  // superadmin bypasses the check entirely — see Principal.has_permission.
  superadmin: Object.values(Permission),
};

export const BookStatus = {
  DRAFT: 'draft',
  PROCESSING: 'processing',
  PENDING_REVIEW: 'pending_review',
  PUBLISHED: 'published',
  UNPUBLISHED: 'unpublished',
  REJECTED: 'rejected',
  ARCHIVED: 'archived',
} as const;
export type BookStatus = (typeof BookStatus)[keyof typeof BookStatus];

export const BookFormat = {
  PDF: 'pdf',
  EPUB: 'epub',
  MOBI: 'mobi',
  AUDIOBOOK: 'audiobook',
} as const;
export type BookFormat = (typeof BookFormat)[keyof typeof BookFormat];

export const OrderStatus = {
  PENDING: 'pending',
  AWAITING_PAYMENT: 'awaiting_payment',
  PAID: 'paid',
  FAILED: 'failed',
  CANCELLED: 'cancelled',
  REFUNDED: 'refunded',
  PARTIALLY_REFUNDED: 'partially_refunded',
} as const;
export type OrderStatus = (typeof OrderStatus)[keyof typeof OrderStatus];

/** Statuses after which the buyer owns the book. */
export const FULFILLED_ORDER_STATUSES: readonly OrderStatus[] = [
  OrderStatus.PAID,
  OrderStatus.PARTIALLY_REFUNDED,
];

export const PaymentProvider = {
  RAZORPAY: 'razorpay',
  STRIPE: 'stripe',
  MANUAL: 'manual',
} as const;
export type PaymentProvider = (typeof PaymentProvider)[keyof typeof PaymentProvider];

export const Currency = {
  INR: 'INR',
  USD: 'USD',
  EUR: 'EUR',
  GBP: 'GBP',
} as const;
export type Currency = (typeof Currency)[keyof typeof Currency];

export const JobStatus = {
  QUEUED: 'queued',
  RUNNING: 'running',
  SUCCEEDED: 'succeeded',
  FAILED: 'failed',
  RETRYING: 'retrying',
  DEAD_LETTERED: 'dead_lettered',
  CANCELLED: 'cancelled',
} as const;
export type JobStatus = (typeof JobStatus)[keyof typeof JobStatus];

export const NotificationChannel = {
  EMAIL: 'email',
  SMS: 'sms',
  WHATSAPP: 'whatsapp',
  PUSH: 'push',
  IN_APP: 'in_app',
} as const;
export type NotificationChannel = (typeof NotificationChannel)[keyof typeof NotificationChannel];

export const SortOrder = { ASC: 'asc', DESC: 'desc' } as const;
export type SortOrder = (typeof SortOrder)[keyof typeof SortOrder];
