/**
 * `@knowledgeos/types` — the TypeScript mirror of the platform's API contract.
 *
 * Source of truth: `packages/core-py/knowledgeos_core/schemas.py`,
 * `errors.py` and `pagination.py`, plus each service's own response models.
 * `scripts/generate.ts` can regenerate the service-specific half from the live
 * OpenAPI documents; the enums and envelopes below are hand-maintained because
 * they must be readable and stable even when the stack is down.
 *
 * Nothing in this package emits runtime code except the `const` enum objects.
 */

export * from './common';
export * from './enums';
export * from './catalogue';
export * from './identity';
export * from './commerce';
export * from './discovery';
