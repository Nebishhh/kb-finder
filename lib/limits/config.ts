// Limits for the hosted app. The app is private: the owner plus several
// coworkers sharing one password, so each limit is one global counter and there
// is no per-IP limit.

// Valid, allowed searches per UTC day, across all modes. Request 2001 of a day
// gets 429. This is a circuit breaker against a runaway loop, not a usage quota.
export const DAILY_SEARCH_CAP = 2000;

// The kb_rate_counters row the cap uses (CHECK: key 'all' exactly for the
// global kinds).
export const GLOBAL_DAY_KIND = "search_global_day";
export const GLOBAL_KEY = "all";

// Failed /api/gate logins per UTC day, across all clients. This, not the
// password's length, bounds brute force against a PIN: attempt 101 of a day
// gets 429 without the PIN being compared.
export const GATE_FAIL_CAP = 100;

// The kb_rate_counters row the failed-login cap uses (key GLOBAL_KEY).
export const GATE_FAIL_DAY_KIND = "gate_fail_global_day";

// How long the global rows (key GLOBAL_KEY) are kept as daily totals. Every
// other key is purged after one day. Both intervals are written out in
// DAILY_CAP_SQL (lib/limits/daily-cap.ts).
export const GLOBAL_RETENTION_DAYS = 90;
