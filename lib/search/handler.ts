import { errorSummary } from "@/lib/safe-error";
import {
  SEARCH_DEPTH,
  SEARCH_MODES,
  type SearchMode,
} from "@/lib/search/config";
import { SearchRequestSchema } from "@/lib/search/schema";
import type { SearchRow } from "@/lib/search/types";

// Query text is never logged, stored or echoed. Logs carry a fixed string
// plus errorSummary() only; error bodies are fixed.

export type SearchFn = (query: string, depth: number) => Promise<SearchRow[]>;

export type SearchEnv = {
  DEFAULT_SEARCH_MODE?: string;
  EVAL_MODES_ENABLED?: string;
};

// One retriever per mode. A request only ever reaches the retriever for the
// mode it asked for, after gating.
export type SearchDeps = {
  retrievers: Record<SearchMode, SearchFn>;
  env: SearchEnv;
};

export type SearchResult = {
  rank: number;
  sourceArticleId: string;
  title: string;
  url: string;
  category?: string;
  score: number;
};

export type SearchResponse = { mode: string; results: SearchResult[] };

const INVALID_BODY = { error: "Invalid request" };
const FAILED_BODY = { error: "Search failed" };

function json(body: unknown, status: number): Response {
  return Response.json(body, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

function isKnownMode(
  mode: string,
  knownModes: readonly string[],
): mode is SearchMode {
  return knownModes.includes(mode);
}

// null when DEFAULT_SEARCH_MODE names a mode that does not exist.
export function resolveDefaultMode(
  env: SearchEnv,
  knownModes: readonly string[] = SEARCH_MODES,
): string | null {
  const mode = env.DEFAULT_SEARCH_MODE || "lexical";
  return isKnownMode(mode, knownModes) ? mode : null;
}

// SPEC mode gating: the default mode is always allowed;
// EVAL_MODES_ENABLED=true unlocks the other known modes.
export function isModeAllowed(
  mode: string,
  defaultMode: string,
  env: SearchEnv,
  knownModes: readonly string[] = SEARCH_MODES,
): boolean {
  if (!isKnownMode(mode, knownModes)) return false;
  if (mode === defaultMode) return true;
  return env.EVAL_MODES_ENABLED === "true";
}

// One row in, one result out, in the order the database returned.
export function toResult(row: SearchRow): SearchResult {
  return {
    rank: row.rank,
    sourceArticleId: row.source_article_id,
    title: row.title,
    url: row.url,
    ...(row.category === null ? {} : { category: row.category }),
    score: row.score,
  };
}

export async function handleSearch(
  request: Request,
  deps: SearchDeps,
): Promise<Response> {
  const defaultMode = resolveDefaultMode(deps.env);
  if (defaultMode === null) {
    console.error("search config error: invalid DEFAULT_SEARCH_MODE");
    return json(FAILED_BODY, 500);
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return json(INVALID_BODY, 400);
  }

  const parsed = SearchRequestSchema.safeParse(body);
  if (!parsed.success) return json(INVALID_BODY, 400);

  const { query, mode } = parsed.data;
  if (!isModeAllowed(mode, defaultMode, deps.env)) {
    return json(INVALID_BODY, 400);
  }

  try {
    const rows = await deps.retrievers[mode](query, SEARCH_DEPTH);
    const response: SearchResponse = { mode, results: rows.map(toResult) };
    return json(response, 200);
  } catch (err) {
    console.error(`search failed: ${errorSummary(err)}`);
    return json(FAILED_BODY, 500);
  }
}
