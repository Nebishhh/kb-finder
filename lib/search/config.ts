// Frozen Phase 4 lexical baseline (DECISIONS.md, 2026-09-30).
// Not settable by the request or by the environment.
export const SEARCH_DEPTH = 50;
export const STATEMENT_TIMEOUT_MS = 5000;

// Modes that exist. Which of them a request may use is decided by
// DEFAULT_SEARCH_MODE and EVAL_MODES_ENABLED (see handler.ts).
export const SEARCH_MODES = [
  "lexical",
  "semantic",
  "lexical-v2",
  "semantic-v2",
  "hybrid",
] as const;
export type SearchMode = (typeof SEARCH_MODES)[number];

// Modes DEFAULT_SEARCH_MODE may name. Experimental modes (lexical-v2,
// semantic-v2, hybrid) are never a default: naming one there is a config error,
// like an unknown mode.
export const DEFAULT_SEARCH_MODES = ["lexical", "semantic"] as const;

// Hybrid experiment hybrid-rrf-v1 (DECISIONS.md, 2026-11-15, frozen): the
// reciprocal rank fusion constant. Not settable by the request or by the
// environment.
export const HYBRID_RRF_K = 60;
