import "server-only";

import { getPool } from "@/lib/db";

// Ranking lives only in kb_search_lexical (DECISIONS.md, frozen baseline).
const LEXICAL_SQL =
  "select rank, source_article_id, title, url, category, score " +
  "from kb_search_lexical($1::text, $2::int)";

export type LexicalRow = {
  rank: number;
  source_article_id: string;
  title: string;
  url: string;
  category: string | null;
  score: number;
};

export async function searchLexical(
  query: string,
  depth: number,
): Promise<LexicalRow[]> {
  const result = await getPool().query<LexicalRow>(LEXICAL_SQL, [query, depth]);
  return result.rows;
}
