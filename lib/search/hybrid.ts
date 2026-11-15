import "server-only";

import { HYBRID_RRF_K } from "@/lib/search/config";
import type { SearchFn } from "@/lib/search/handler";
import { searchLexical } from "@/lib/search/lexical";
import { searchSemantic } from "@/lib/search/semantic";
import type { SearchRow } from "@/lib/search/types";

// Hybrid experiment hybrid-rrf-v1 (DECISIONS.md, 2026-11-15, frozen).
//
// Reciprocal rank fusion of two ranked lists:
//   score = 1/(k + lexical rank) + 1/(k + semantic rank)
// An article missing from one list counts as rank depth + 1 there. Ties, found
// by exact integer arithmetic, go to the better semantic rank.
//
// This is the one place a result order is decided outside a SQL function, and
// only the fused order: each leg's own ranking still lives only in its SQL
// function and reaches this file as a finished list. The two legs are
// parameters, so the same code can fuse any other pair without a change here.

// The message is fixed: nothing from a leg's rows is ever put in it.
// errorSummary() logs it as "HybridLegError HYBRID_LEG_INVALID".
export class HybridLegError extends Error {
  readonly code = "HYBRID_LEG_INVALID";
  constructor() {
    super("hybrid leg invalid");
    this.name = "HybridLegError";
  }
}

// A leg must be what a ranking function returns: at most depth rows, ranks
// exactly 1..n in order, no article twice.
function checkLeg(rows: readonly SearchRow[], depth: number): void {
  if (rows.length > depth) throw new HybridLegError();
  const seen = new Set<string>();
  rows.forEach((row, index) => {
    if (row.rank !== index + 1) throw new HybridLegError();
    if (typeof row.source_article_id !== "string" || seen.has(row.source_article_id)) {
      throw new HybridLegError();
    }
    seen.add(row.source_article_id);
  });
}

type Fused = {
  row: SearchRow;
  lexicalRank: number;
  semanticRank: number;
  // score = num / den exactly, with a = k + lexical rank and b = k + semantic
  // rank: 1/a + 1/b = (a + b) / (a * b). Small integers, so exact.
  num: number;
  den: number;
};

function compareFused(x: Fused, y: Fused): number {
  // x.num / x.den against y.num / y.den by cross-multiplication. Higher first.
  const left = x.num * y.den;
  const right = y.num * x.den;
  if (left !== right) return left > right ? -1 : 1;
  if (x.semanticRank !== y.semanticRank) return x.semanticRank - y.semanticRank;
  // Not reachable for valid legs: an equal score and an equal semantic rank
  // mean an equal lexical rank, and ranks within a leg are unique. Kept as a
  // last key so the order is total. Code-unit order equals C collation for the
  // ASCII digits an article ID is made of.
  const a = x.row.source_article_id;
  const b = y.row.source_article_id;
  return a < b ? -1 : a > b ? 1 : 0;
}

export function fuseRrf(
  lexical: readonly SearchRow[],
  semantic: readonly SearchRow[],
  depth: number,
  k: number = HYBRID_RRF_K,
): SearchRow[] {
  if (!Number.isInteger(depth) || depth < 1 || !Number.isInteger(k) || k < 1) {
    throw new HybridLegError();
  }
  checkLeg(lexical, depth);
  checkLeg(semantic, depth);

  const missing = depth + 1;
  const lexicalRanks = new Map(lexical.map((row) => [row.source_article_id, row.rank]));
  const semanticIds = new Set(semantic.map((row) => row.source_article_id));

  const fused: Fused[] = [];
  const add = (row: SearchRow, lexicalRank: number, semanticRank: number) => {
    const a = k + lexicalRank;
    const b = k + semanticRank;
    fused.push({ row, lexicalRank, semanticRank, num: a + b, den: a * b });
  };
  // Title, URL and category come from the same articles row in both legs.
  for (const row of semantic) {
    add(row, lexicalRanks.get(row.source_article_id) ?? missing, row.rank);
  }
  for (const row of lexical) {
    if (!semanticIds.has(row.source_article_id)) add(row, row.rank, missing);
  }

  fused.sort(compareFused);
  return fused.slice(0, depth).map((f, index) => ({
    rank: index + 1,
    source_article_id: f.row.source_article_id,
    title: f.row.title,
    url: f.row.url,
    category: f.row.category,
    score: 1 / (k + f.lexicalRank) + 1 / (k + f.semanticRank),
  }));
}

export type HybridLegs = { lexical: SearchFn; semantic: SearchFn };

// Both legs start at once, so the lexical query runs while the semantic leg is
// still embedding the query. No fallback (DECISIONS.md, D1): if either leg
// fails the search fails, with that leg's own error, so the log line is the
// same fixed class and code the leg's own mode would log. allSettled, not all,
// so the other leg's rejection is never left unhandled.
export function makeHybridSearch(legs: HybridLegs): SearchFn {
  return async (query, depth) => {
    const [lexical, semantic] = await Promise.allSettled([
      legs.lexical(query, depth),
      legs.semantic(query, depth),
    ]);
    if (semantic.status === "rejected") throw semantic.reason;
    if (lexical.status === "rejected") throw lexical.reason;
    return fuseRrf(lexical.value, semantic.value, depth);
  };
}

// Eval-only (hybrid in lib/search/config.ts): the frozen lexical and semantic
// (article-v1) retrievers, unchanged.
export const searchHybrid: SearchFn = makeHybridSearch({
  lexical: searchLexical,
  semantic: searchSemantic,
});
