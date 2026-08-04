# `@knowledgeos/config`

Shared build-time configuration: ESLint flat configs, the Tailwind design-token
preset, and TypeScript bases. Consumed by every TypeScript package and by
`apps/frontend`.

## ESLint

Flat config only (ESLint 9). Three layers, each building on the previous:

```js
// eslint.config.mjs
import next from '@knowledgeos/config/eslint/next';   // apps/frontend
import react from '@knowledgeos/config/eslint/react'; // packages/ui
import base from '@knowledgeos/config/eslint/base';   // everything else
export default next;
```

Two deliberate omissions:

- **Not type-aware.** `projectService` needs a resolved program per file; on a
  monorepo this size that triples lint time to re-derive what `tsc --noEmit`
  already proves in `pnpm typecheck`. The rules kept are the ones a type checker
  cannot express.
- **No `eslint-config-next`.** It is still an eslintrc bundle behind a `FlatCompat`
  shim and pulls in a second copy of the TypeScript parser. Most of what it checks,
  the Next compiler already errors on.

What the Next layer *does* add is the rule that matters most here: inside a `.tsx`
file, only `NEXT_PUBLIC_*` (and `NODE_ENV`) may be read from `process.env`. Any
other read is inlined into the browser bundle at build time and shipped to every
visitor — that is how a secret leaks.

## Tailwind preset

```ts
// tailwind.config.ts
import preset from '@knowledgeos/config/tailwind/preset';
export default { presets: [preset], content: [...] };
```

Colours are HSL **channel triplets** in CSS variables (`--primary: 231 74% 56%`),
not finished colours. That is what makes `bg-primary/60` work — Tailwind composes
`hsl(var(--primary) / 0.6)` — and it is what lets the entire palette swap by
toggling one attribute on `<html>`, with no second stylesheet and no re-render.

Beyond the shadcn/ui token set it adds:

- a `reader.*` scale — the sepia reading surface is not a tint of the app chrome,
  it has its own contrast budget;
- layered shadows (`subtle` / `raised` / `overlay`) — one blur reads as a sticker,
  two read as an object on a surface;
- a single easing curve, `ease-swift`, so all motion in the product decelerates
  identically;
- a `reduce-motion:` variant, because `prefers-reduced-motion` is a requirement
  rather than something each component remembers.

## TypeScript

| Base | For |
|---|---|
| `tsconfig/base.json` | extends the repo root `tsconfig.base.json`, adds `noEmit` |
| `tsconfig/library.json` | internal packages (`react-jsx`, DOM libs) |
| `tsconfig/nextjs.json` | `apps/frontend` (`jsx: preserve`, the `next` TS plugin) |

The root base sets `strict`, `noUncheckedIndexedAccess`, `verbatimModuleSyntax` and
`isolatedModules`; nothing here relaxes them.
