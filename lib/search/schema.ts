import { z } from "zod";

// SPEC.md, Search API. Extra fields (e.g. limit) are rejected.
export const SearchRequestSchema = z
  .object({
    query: z.string().min(1).max(5000),
    mode: z.literal("lexical"),
  })
  .strict();

export type SearchRequest = z.infer<typeof SearchRequestSchema>;
