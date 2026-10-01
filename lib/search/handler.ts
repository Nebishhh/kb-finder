import { errorSummary } from "@/lib/safe-error";
import { readJsonBody } from "@/lib/search/body";
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
  // Exactly "off" skips the limiter, allowed only when VERCEL is unset (local
  // dev and evals). On Vercel it is a config error and every request gets 500.
  RATE_LIMIT?: string;
  VERCEL?: string;
};

// The daily cap (lib/limits/daily-cap.ts). Called once per valid, allowed
// search, before the retriever, so a failed retrieval still counts.
export type LimitResult = "ok" | "daily_cap";
export type LimitFn = () => Promise<LimitResult>;

// One retriever per mode. A request only ever reaches the retriever for the
// mode it asked for, after gating.
export type SearchDeps = {
  retrievers: Record<SearchMode, SearchFn>;
  limit: LimitFn;
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
const TOO_LARGE_BODY = { error: "Request too large" };
const TOO_MANY_BODY = { error: "Too many requests" };

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

// "on": the limiter runs. "skip": RATE_LIMIT is exactly "off" off Vercel.
// "invalid": RATE_LIMIT is "off" on Vercel, which is never allowed.
export function resolveRateLimit(env: SearchEnv): "on" | "skip" | "invalid" {
  if (env.RATE_LIMIT !== "off") return "on";
  return env.VERCEL ? "invalid" : "skip";
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
  const rateLimit = resolveRateLimit(deps.env);
  if (rateLimit === "invalid") {
    console.error("search config error: RATE_LIMIT=off on Vercel");
    return json(FAILED_BODY, 500);
  }

  // Byte-capped read (lib/search/body.ts); the body is never logged.
  const body = await readJsonBody(request);
  if (!body.ok) {
    return body.reason === "too_large"
      ? json(TOO_LARGE_BODY, 413)
      : json(INVALID_BODY, 400);
  }

  const parsed = SearchRequestSchema.safeParse(body.value);
  if (!parsed.success) return json(INVALID_BODY, 400);

  const { query, mode } = parsed.data;
  if (!isModeAllowed(mode, defaultMode, deps.env)) {
    return json(INVALID_BODY, 400);
  }

  // Only valid, allowed searches reach the cap. Fail closed: a limiter error is
  // a 500, never a search.
  if (rateLimit === "on") {
    let limited: LimitResult;
    try {
      limited = await deps.limit();
    } catch (err) {
      console.error(`search limit failed: ${errorSummary(err)}`);
      return json(FAILED_BODY, 500);
    }
    if (limited !== "ok") {
      console.error("search rejected: DAILY_CAP");
      return json(TOO_MANY_BODY, 429);
    }
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
