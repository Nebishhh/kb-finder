import "server-only";

import { getPool } from "@/lib/db";
import {
  EMBEDDING_ERROR_CODES,
  EmbeddingError,
  embedQuery,
} from "@/lib/embeddings/openai";
import type { SearchFn } from "@/lib/search/handler";
import {
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

export type SemanticDeps = {
  embed: (text: string, config: SemanticConfig) => Promise<number[]>;
  query: QueryFn;
  config: () => SemanticConfig;
};

const KNOWN_CODES: readonly string[] = EMBEDDING_ERROR_CODES;

export function makeSemanticSearch(deps: SemanticDeps): SearchFn {
  return async (query, depth) => {
    const config = deps.config();
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
  config: getSemanticConfig,
});
