/**
 * Regenerate the service-specific half of `@knowledgeos/types` from the live
 * OpenAPI documents.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * THIS SCRIPT REQUIRES THE STACK TO BE RUNNING.
 *
 *     pnpm stack:up                 # Postgres, Redis, MinIO, Meilisearch
 *     # ...then start the services you want to regenerate from
 *     pnpm --filter @knowledgeos/types generate
 *
 * With nothing listening it exits non-zero and changes no files — a half-written
 * types package is worse than a stale one, so it writes only after every
 * requested service has answered.
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * What it does NOT do: touch `src/enums.ts`, `src/common.ts` or any hand-written
 * module. Those mirror `knowledgeos_core` directly and must stay readable and
 * reviewable even when every service is down. Generation only produces
 * `src/generated/<service>.ts`, which the hand-written modules may reference.
 *
 * Usage:
 *   tsx scripts/generate.ts                    # all services
 *   tsx scripts/generate.ts books search       # a subset
 *   GATEWAY_URL=http://localhost:8000 tsx scripts/generate.ts --via-gateway
 */

import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = join(HERE, '..', 'src', 'generated');

/** Local ports, matching infrastructure/docker/docker-compose.yml. */
const SERVICES: Record<string, string> = {
  auth: 'http://localhost:8001',
  books: 'http://localhost:8002',
  search: 'http://localhost:8003',
  payment: 'http://localhost:8004',
  ai: 'http://localhost:8005',
  notifications: 'http://localhost:8006',
  automation: 'http://localhost:8007',
  admin: 'http://localhost:8008',
};

const OPENAPI_PATH = '/openapi.json';
const TIMEOUT_MS = 5_000;

interface OpenApiDocument {
  openapi: string;
  info: { title: string; version: string };
  components?: { schemas?: Record<string, JsonSchema> };
}

interface JsonSchema {
  type?: string | string[];
  format?: string;
  enum?: (string | number)[];
  const?: string | number;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  additionalProperties?: boolean | JsonSchema;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  allOf?: JsonSchema[];
  $ref?: string;
  description?: string;
  nullable?: boolean;
  title?: string;
}

async function fetchSpec(name: string, baseUrl: string): Promise<OpenApiDocument> {
  const url = `${baseUrl}${OPENAPI_PATH}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) {
      throw new Error(`${name}: ${url} returned ${response.status}`);
    }
    return (await response.json()) as OpenApiDocument;
  } catch (cause) {
    throw new Error(
      `${name}: could not read ${url}. Is the stack up? ` +
        `Run \`pnpm stack:up\` and start the ${name} service.`,
      { cause },
    );
  } finally {
    clearTimeout(timer);
  }
}

/** `#/components/schemas/BookOut` -> `BookOut`. */
function refName(ref: string): string {
  const last = ref.split('/').pop();
  if (!last) throw new Error(`Unresolvable $ref: ${ref}`);
  return sanitiseIdentifier(last);
}

function sanitiseIdentifier(raw: string): string {
  const cleaned = raw.replace(/[^A-Za-z0-9_]/g, '_');
  return /^[0-9]/.test(cleaned) ? `_${cleaned}` : cleaned;
}

function quoteKey(key: string): string {
  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key) ? key : JSON.stringify(key);
}

function toType(schema: JsonSchema, indent: string): string {
  if (schema.$ref) return refName(schema.$ref);
  if (schema.const !== undefined) return JSON.stringify(schema.const);
  if (schema.enum) return schema.enum.map((v) => JSON.stringify(v)).join(' | ');

  const union = schema.anyOf ?? schema.oneOf;
  if (union) {
    const parts = union.map((s) => toType(s, indent));
    return Array.from(new Set(parts)).join(' | ');
  }
  if (schema.allOf) {
    return schema.allOf.map((s) => toType(s, indent)).join(' & ');
  }

  const type = Array.isArray(schema.type) ? schema.type[0] : schema.type;
  switch (type) {
    case 'string':
      return 'string';
    case 'integer':
    case 'number':
      return 'number';
    case 'boolean':
      return 'boolean';
    case 'null':
      return 'null';
    case 'array':
      return `${schema.items ? toType(schema.items, indent) : 'unknown'}[]`;
    case 'object':
      return objectType(schema, indent);
    default:
      return 'unknown';
  }
}

function objectType(schema: JsonSchema, indent: string): string {
  const props = schema.properties;
  if (!props || Object.keys(props).length === 0) {
    const extra = schema.additionalProperties;
    if (extra && typeof extra === 'object') {
      return `Record<string, ${toType(extra, indent)}>`;
    }
    return 'Record<string, unknown>';
  }
  const inner = `${indent}  `;
  const required = new Set(schema.required ?? []);
  const lines = Object.entries(props).map(([key, value]) => {
    const doc = value.description ? `${inner}/** ${value.description.replace(/\*\//g, '*\\/')} */\n` : '';
    const optional = required.has(key) ? '' : '?';
    return `${doc}${inner}${quoteKey(key)}${optional}: ${toType(value, inner)};`;
  });
  return `{\n${lines.join('\n')}\n${indent}}`;
}

function renderModule(service: string, doc: OpenApiDocument): string {
  const schemas = doc.components?.schemas ?? {};
  const header = [
    '/* eslint-disable */',
    '/**',
    ` * GENERATED — do not edit.`,
    ` * Source: ${service} ${OPENAPI_PATH} (${doc.info.title} ${doc.info.version})`,
    ` * Regenerate with: pnpm --filter @knowledgeos/types generate`,
    ' */',
    '',
  ].join('\n');

  const body = Object.entries(schemas)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, schema]) => {
      const id = sanitiseIdentifier(name);
      const doc_ = schema.description ? `/** ${schema.description.replace(/\*\//g, '*\\/')} */\n` : '';
      const rendered = toType(schema, '');
      // An object literal becomes an interface; everything else an alias.
      return rendered.startsWith('{')
        ? `${doc_}export interface ${id} ${rendered}\n`
        : `${doc_}export type ${id} = ${rendered};\n`;
    })
    .join('\n');

  return `${header}\n${body}`;
}

async function main(): Promise<void> {
  const args = process.argv.slice(2).filter((a) => !a.startsWith('--'));
  const viaGateway = process.argv.includes('--via-gateway');
  const gateway = process.env.GATEWAY_URL ?? 'http://localhost:8000';

  const wanted = args.length > 0 ? args : Object.keys(SERVICES);
  const unknown = wanted.filter((name) => !(name in SERVICES));
  if (unknown.length > 0) {
    console.error(`Unknown service(s): ${unknown.join(', ')}`);
    console.error(`Known: ${Object.keys(SERVICES).join(', ')}`);
    process.exitCode = 2;
    return;
  }

  console.warn(`Fetching OpenAPI from ${wanted.length} service(s)...`);

  // Fetch everything first. Writing partial output on a half-up stack produces a
  // types package that compiles but lies, which is the worst possible outcome.
  const specs: [string, OpenApiDocument][] = [];
  const failures: string[] = [];
  await Promise.all(
    wanted.map(async (name) => {
      const base = viaGateway ? `${gateway}/${name}` : (SERVICES[name] as string);
      try {
        specs.push([name, await fetchSpec(name, base)]);
      } catch (error) {
        failures.push(error instanceof Error ? error.message : String(error));
      }
    }),
  );

  if (failures.length > 0) {
    console.error('\nNo files were written. Failures:\n');
    for (const message of failures) console.error(`  • ${message}`);
    console.error(
      '\nThe backend must be running for generation. See docs/guides/local-development.md.',
    );
    process.exitCode = 1;
    return;
  }

  await mkdir(OUT_DIR, { recursive: true });
  specs.sort(([a], [b]) => a.localeCompare(b));
  for (const [name, doc] of specs) {
    const target = join(OUT_DIR, `${name}.ts`);
    await writeFile(target, renderModule(name, doc), 'utf8');
    const count = Object.keys(doc.components?.schemas ?? {}).length;
    console.warn(`  ✓ ${name}: ${count} schema(s) -> src/generated/${name}.ts`);
  }

  const barrel = [
    '/* eslint-disable */',
    '/** GENERATED — do not edit. */',
    '',
    ...specs.map(([name]) => `export * as ${sanitiseIdentifier(name)} from './${name}';`),
    '',
  ].join('\n');
  await writeFile(join(OUT_DIR, 'index.ts'), barrel, 'utf8');

  console.warn('\nDone. Review the diff before committing — a contract change is a breaking change.');
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
