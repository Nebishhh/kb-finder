// Spend and DB-load cap for the hosted app (Phase 7). The app is private: the
// owner plus at most 2-3 trusted testers sharing one password, so there is one
// global counter and no per-IP limit.

// Valid, allowed searches per UTC day, across both modes. Request 201 of a day
// gets 429.
export const DAILY_SEARCH_CAP = 200;

// The kb_rate_counters row the cap uses (CHECK: key 'all' exactly for this kind).
export const GLOBAL_DAY_KIND = "search_global_day";
export const GLOBAL_KEY = "all";
