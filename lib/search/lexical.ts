import "server-only";

import { getPool } from "@/lib/db";
import type { SearchRow } from "@/lib/search/types";

// Ranking lives only in kb_search_lexical (DECISIONS.md, frozen baseline).
export const LEXICAL_SQL =
  "select rank, source_article_id, title, url, category, score " +
  "from kb_search_lexical($1::text, $2::int)";

// article-v2 experiment (DECISIONS.md, 2026-10-07): the same ranking over
// search_vector_v2, which adds staff-only text. Ranking lives only in
// kb_search_lexical_v2 (supabase/migrations/*_add_article_v2.sql).
export const LEXICAL_V2_SQL =
  "select rank, source_article_id, title, url, category, score " +
  "from kb_search_lexical_v2($1::text, $2::int)";

export async function searchLexical(
  query: string,
  depth: number,
): Promise<SearchRow[]> {
  const result = await getPool().query<SearchRow>(LEXICAL_SQL, [query, depth]);
  return result.rows;
}

export async function searchLexicalV2(
  query: string,
  depth: number,
): Promise<SearchRow[]> {
  const result = await getPool().query<SearchRow>(LEXICAL_V2_SQL, [query, depth]);
  return result.rows;
}
