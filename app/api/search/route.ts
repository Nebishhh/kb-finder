import { checkGate, gateEnvFrom } from "@/lib/gate/cookie";
import { dailyCap } from "@/lib/limits/daily-cap";
import { handleSearch } from "@/lib/search/handler";
import { searchLexical, searchLexicalV2 } from "@/lib/search/lexical";
import { searchSemantic, searchSemanticV2 } from "@/lib/search/semantic";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return handleSearch(request, {
    retrievers: {
      lexical: searchLexical,
      semantic: searchSemantic,
      "lexical-v2": searchLexicalV2,
      "semantic-v2": searchSemanticV2,
    },
    gate: (req) =>
      checkGate(req.headers.get("cookie"), gateEnvFrom(process.env), Date.now()),
    limit: dailyCap,
    env: {
      DEFAULT_SEARCH_MODE: process.env.DEFAULT_SEARCH_MODE,
      EVAL_MODES_ENABLED: process.env.EVAL_MODES_ENABLED,
      RATE_LIMIT: process.env.RATE_LIMIT,
      VERCEL: process.env.VERCEL,
    },
  });
}
