import "server-only";

import { getPool } from "@/lib/db";
import type { SearchRow } from "@/lib/search/types";

// Ranking lives only in kb_search_lexical (DECISIONS.md, frozen baseline).
const LEXICAL_SQL =
  "select rank, source_article_id, title, url, category, score " +
  "from kb_search_lexical($1::text, $2::int)";

export async function searchLexical(
  query: string,
  depth: number,
): Promise<SearchRow[]> {
  const result = await getPool().query<SearchRow>(LEXICAL_SQL, [query, depth]);
  return result.rows;
}
