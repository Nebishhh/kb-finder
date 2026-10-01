import { dailyCap } from "@/lib/limits/daily-cap";
import { handleSearch } from "@/lib/search/handler";
import { searchLexical } from "@/lib/search/lexical";
import { searchSemantic } from "@/lib/search/semantic";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return handleSearch(request, {
    retrievers: { lexical: searchLexical, semantic: searchSemantic },
    limit: dailyCap,
    env: {
      DEFAULT_SEARCH_MODE: process.env.DEFAULT_SEARCH_MODE,
      EVAL_MODES_ENABLED: process.env.EVAL_MODES_ENABLED,
      RATE_LIMIT: process.env.RATE_LIMIT,
      VERCEL: process.env.VERCEL,
    },
  });
}
