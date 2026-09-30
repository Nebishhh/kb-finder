import { handleSearch } from "@/lib/search/handler";
import { searchLexical } from "@/lib/search/lexical";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return handleSearch(request, {
    search: searchLexical,
    env: {
      DEFAULT_SEARCH_MODE: process.env.DEFAULT_SEARCH_MODE,
      EVAL_MODES_ENABLED: process.env.EVAL_MODES_ENABLED,
    },
  });
}
