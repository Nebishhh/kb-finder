import "server-only";

import { getPool } from "@/lib/db";
import {
  EMBEDDING_ERROR_CODES,
  EmbeddingError,
  embedQuery,
} from "@/lib/embeddings/openai";
import type { SearchFn } from "@/lib/search/handler";
import {
  canonicalJson,
  getSemanticConfig,
  type SemanticConfig,
} from "@/lib/search/semantic-config";
import type { SearchRow } from "@/lib/search/types";

// Ranking lives only in kb_search_semantic (supabase/migrations/*_add_semantic_
// embeddings.sql). The vector travels as a validated text literal, cast in the
// function argument so it is parsed once per call. The query is sent unnamed
// (no prepared statement), so a generic plan can never re-parse it per row.
export const SEMANTIC_SQL =
  "select rank, source_article_id, title, url, category, score " +
  "from kb_search_semantic($1::text::extensions.vector, $2::int, $3::text)";

// The stored row for the server's config sha256. kb_search_semantic (KBF02) only
// checks that it exists; checkConfigRow below checks what it holds.
export const CONFIG_ROW_SQL =
  "select config, model, dimensions, template_version " +
  "from embedding_configs where config_sha256 = $1";

// The message is fixed: nothing from the row or the file is ever put in it.
// errorSummary() logs it as "ConfigMismatchError KBF02_CONFIG_MISMATCH".
export class ConfigMismatchError extends Error {
  readonly code = "KBF02_CONFIG_MISMATCH";
  constructor() {
    super("embedding config mismatch");
    this.name = "ConfigMismatchError";
  }
}

// Exactly one row whose config jsonb, compared as canonicalJson() (the form the
// file is hashed in, so key order does not matter), and whose model, dimensions
// and template_version columns equal the file's values.
export function checkConfigRow(rows: readonly unknown[], config: SemanticConfig): void {
  if (rows.length !== 1) throw new ConfigMismatchError();
  const row = rows[0];
  if (typeof row !== "object" || row === null) throw new ConfigMismatchError();
  const r = row as Record<string, unknown>;
  let stored: string;
  try {
    stored = canonicalJson(r.config);
  } catch {
    throw new ConfigMismatchError();
  }
  if (
    stored !== config.canonical ||
    r.model !== config.model ||
    r.dimensions !== config.dimensions ||
    r.template_version !== config.templateVersion
  ) {
    throw new ConfigMismatchError();
  }
}

export class VectorError extends Error {
  readonly code = "VECTOR_INVALID";
  constructor() {
    super("vector invalid");
    this.name = "VectorError";
  }
}

// "[x1,...,xn]" for pgvector. String(x) is the shortest round-trip form, so a
// float32 value parses back to the same float32.
export function toVectorLiteral(values: readonly unknown[], dimensions: number): string {
  if (values.length !== dimensions) throw new VectorError();
  let nonZero = false;
  for (const x of values) {
    if (typeof x !== "number" || !Number.isFinite(x)) throw new VectorError();
    if (x !== 0) nonZero = true;
  }
  if (!nonZero) throw new VectorError();
  return "[" + values.map(String).join(",") + "]";
}

export type QueryFn = (
  text: string,
  params: unknown[],
) => Promise<{ rows: SearchRow[] }>;

// Rows of CONFIG_ROW_SQL for one config sha256.
export type ConfigRowsFn = (sha256: string) => Promise<unknown[]>;

export type SemanticDeps = {
  embed: (text: string, config: SemanticConfig) => Promise<number[]>;
  query: QueryFn;
  configRows: ConfigRowsFn;
  config: () => SemanticConfig;
};

const KNOWN_CODES: readonly string[] = EMBEDDING_ERROR_CODES;

export function makeSemanticSearch(deps: SemanticDeps): SearchFn {
  // Config shas whose stored row passed the check. Only success is cached, so a
  // failed check (or a failed read) is retried on the next request. The
  // production retriever is a module singleton, so this is per process.
  const verified = new Set<string>();
  return async (query, depth) => {
    const config = deps.config();
    // Before the query is embedded: a mismatch never spends an OpenAI call.
    if (!verified.has(config.sha256)) {
      checkConfigRow(await deps.configRows(config.sha256), config);
      verified.add(config.sha256);
    }
    let vector: number[];
    try {
      vector = await deps.embed(query, config);
    } catch (err) {
      // Always a fresh error with a fixed code: whatever the embedder threw
      // (message, code, cause) never reaches a log.
      const code =
        err instanceof EmbeddingError && KNOWN_CODES.includes(err.code)
          ? err.code
          : "OPENAI_UNEXPECTED";
      throw new EmbeddingError(code);
    }
    const literal = toVectorLiteral(vector, config.dimensions);
    const result = await deps.query(SEMANTIC_SQL, [literal, depth, config.sha256]);
    return result.rows;
  };
}

export const searchSemantic: SearchFn = makeSemanticSearch({
  embed: (text, config) => embedQuery(text, config),
  query: (text, params) => getPool().query<SearchRow>(text, params),
  configRows: async (sha256) => (await getPool().query(CONFIG_ROW_SQL, [sha256])).rows,
  config: getSemanticConfig,
});
