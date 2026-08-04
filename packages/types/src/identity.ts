/** Identity domain: users, sessions, tokens, principals. Owned by `apps/auth`. */

import type { ISODateTime, Timestamped, UUID } from './common';
import type { Currency, GrantedPermission, NotificationChannel, UserRole } from './enums';

export interface User extends Timestamped {
  id: UUID;
  email: string;
  /** Null until the user sets one; fall back to the email local part. */
  name: string | null;
  avatar_url: string | null;
  roles: UserRole[];
  is_email_verified: boolean;
  is_two_factor_enabled: boolean;
  locale: string;
  /** IANA zone, e.g. `Asia/Kolkata`. */
  timezone: string;
  preferred_currency: Currency;
  last_login_at: ISODateTime | null;
}

export interface UserPreferences {
  theme: 'light' | 'dark' | 'system';
  reader: ReaderPreferences;
  marketing_emails: boolean;
  notification_channels: NotificationChannel[];
}

export interface ReaderPreferences {
  font_family: 'serif' | 'sans' | 'mono' | 'dyslexic';
  /** px. Clamped 14–28 by the UI. */
  font_size: number;
  /** Unitless multiplier, 1.2–2.2. */
  line_height: number;
  /** Characters per line target; drives the measure. */
  max_width: 'narrow' | 'comfortable' | 'wide';
  theme: 'light' | 'sepia' | 'dark';
  mode: 'paginated' | 'scroll';
  justify: boolean;
}

/**
 * The decoded access token, matching `knowledgeos_core.security.Principal`.
 *
 * Read from the in-memory token, never from storage. It is a *claim* about the
 * user made by the auth service; it decides what the UI renders, never what the
 * API allows.
 */
export interface Principal {
  user_id: UUID;
  email: string | null;
  roles: UserRole[];
  permissions: GrantedPermission[];
  session_id: string | null;
  token_id: string | null;
  is_service: boolean;
}

/**
 * Login/refresh response.
 *
 * There is deliberately no `refresh_token` field: the refresh token is set by the
 * server as an httpOnly, Secure, SameSite cookie so JavaScript — and therefore any
 * XSS payload — cannot read it. The access token is short-lived and lives only in
 * memory.
 */
export interface AuthTokens {
  access_token: string;
  token_type: 'bearer';
  /** Seconds until the access token expires. */
  expires_in: number;
}

export interface AuthSession extends AuthTokens {
  user: User;
}

export interface LoginInput {
  email: string;
  password: string;
  /** Present only on the second leg of a 2FA login. */
  totp_code?: string;
}

export interface RegisterInput {
  email: string;
  password: string;
  name?: string;
}

export interface Session {
  id: UUID;
  /** Coarse device description derived server-side from the UA. Never the raw UA. */
  device: string;
  /** Truncated to /24 (v4) or /48 (v6) before storage. */
  ip_prefix: string | null;
  location: string | null;
  created_at: ISODateTime;
  last_seen_at: ISODateTime;
  is_current: boolean;
}
