import { z } from "zod";

import { SEARCH_MODES } from "@/lib/search/config";

// SPEC.md, Search API. Extra fields (e.g. limit) are rejected. A known mode
// still has to pass the server-side gate in handler.ts.
export const SearchRequestSchema = z
  .object({
    query: z.string().min(1).max(5000),
    mode: z.enum(SEARCH_MODES),
  })
  .strict();

export type SearchRequest = z.infer<typeof SearchRequestSchema>;
