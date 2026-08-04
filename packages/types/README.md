# `@knowledgeos/types`

The TypeScript mirror of the platform's API contract. No runtime dependencies, no
side effects — importing it costs nothing in the browser bundle beyond the handful
of `const` enum objects.

## What is in here

| Module | Mirrors |
|---|---|
| `common.ts` | `knowledgeos_core/pagination.py` (`Page`, `CursorPage`, `PageMeta`) and `errors.py` (`ApiErrorResponse`) |
| `enums.ts` | `knowledgeos_core/schemas.py` — `UserRole`, `Permission`, `BookStatus`, `OrderStatus`, `Currency`, … |
| `catalogue.ts` | `apps/books` — `Book`, `BookSummary`, `Author`, `Category`, `Review` |
| `identity.ts` | `apps/auth` — `User`, `Principal`, `AuthTokens`, `Session` |
| `commerce.ts` | `apps/payment` + reading state — `Order`, `Entitlement`, `ReadingProgress`, `Bookmark` |
| `discovery.ts` | `apps/search`, `apps/admin` — `SearchResponse`, `MetricSeries`, `AutomationJob` |

## Why enums are `const` objects, not `enum`

```ts
export const BookStatus = { PUBLISHED: 'published', ... } as const;
export type BookStatus = (typeof BookStatus)[keyof typeof BookStatus];
```

A TypeScript `enum` is *nominal*: `BookStatus.PUBLISHED` is not assignable from the
string `"published"` that actually arrives in the JSON body, so every API response
would need a cast. The Python side is a `StrEnum`, so the wire value is the plain
string and the union type above matches it exactly. It also plays correctly with
`isolatedModules` and `verbatimModuleSyntax`, which the repo's tsconfig sets.

## Money

Every amount is `MoneyAmount { amount_minor: number; currency: Currency }` — paise
or cents, never a float. `0.1` is not representable in IEEE-754 binary floating
point; a float price column loses money at scale and produces invoices that do not
reconcile. Convert to a display string with `formatMoney` from
`@knowledgeos/utils`, and never for arithmetic.

## Regenerating from OpenAPI

```bash
pnpm stack:up                                # infra
# start the services you want to regenerate from
pnpm --filter @knowledgeos/types generate    # all services
pnpm --filter @knowledgeos/types generate books search
GATEWAY_URL=http://localhost:8000 pnpm --filter @knowledgeos/types generate --via-gateway
```

**The script requires the backend to be running.** It reads `/openapi.json` from
each service, and if any of them does not answer it writes nothing and exits
non-zero — a half-regenerated types package compiles but lies, which is worse than
a stale one.

Output goes to `src/generated/<service>.ts`. The hand-written modules above are
never touched: they must stay readable and reviewable when the stack is down, and
they carry the domain commentary that an OpenAPI document cannot express.
