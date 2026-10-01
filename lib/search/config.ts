// Frozen Phase 4 lexical baseline (DECISIONS.md, 2026-09-30).
// Not settable by the request or by the environment.
export const SEARCH_DEPTH = 50;
export const STATEMENT_TIMEOUT_MS = 5000;

// Modes that exist. Which of them a request may use is decided by
// DEFAULT_SEARCH_MODE and EVAL_MODES_ENABLED (see handler.ts).
export const SEARCH_MODES = ["lexical", "semantic"] as const;
export type SearchMode = (typeof SEARCH_MODES)[number];
