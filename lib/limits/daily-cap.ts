import "server-only";

import { getPool } from "@/lib/db";
import {
  DAILY_SEARCH_CAP,
  GLOBAL_DAY_KIND,
  GLOBAL_KEY,
} from "@/lib/limits/config";
import type { LimitFn } from "@/lib/search/handler";

// One statement, so it runs unchanged through the transaction pooler and needs
// only kb_app's SELECT/INSERT/UPDATE/DELETE on kb_rate_counters.
//
// Why it is atomic: INSERT ... ON CONFLICT DO UPDATE locks the day's row. A
// concurrent request for the same day waits on that lock, then updates the
// newest committed version, so no increment is lost and every caller gets a
// distinct count back. The decision is made from that returned count, never from
// a separate read. The purge CTE only deletes windows that started more than one
// day before $1, which never includes the current day's row, so it cannot race
// the upsert.
//
// $1 is the request time, passed in (not now()) so tests can move the clock.
// The window is the UTC day containing $1.
export const DAILY_CAP_SQL =
  "with purge as (" +
  "delete from kb_rate_counters where window_start < $1::timestamptz - interval '1 day'" +
  "), bump as (" +
  "insert into kb_rate_counters (kind, key, window_start, count) " +
  "values ($2, $3, date_trunc('day', $1::timestamptz, 'UTC'), 1) " +
  "on conflict (kind, key, window_start) " +
  "do update set count = kb_rate_counters.count + 1 " +
  "returning count" +
  ") select count from bump";

export class LimitError extends Error {
  readonly code = "LIMIT_BAD_RESULT";
  constructor() {
    super("limit bad result");
    this.name = "LimitError";
  }
}

export type LimitQueryFn = (
  text: string,
  params: unknown[],
) => Promise<{ rows: { count: unknown }[] }>;

export type DailyCapDeps = {
  query: LimitQueryFn;
  now: () => Date;
  cap?: number;
};

export function makeDailyCap(deps: DailyCapDeps): LimitFn {
  const cap = deps.cap ?? DAILY_SEARCH_CAP;
  return async () => {
    const result = await deps.query(DAILY_CAP_SQL, [
      deps.now().toISOString(),
      GLOBAL_DAY_KIND,
      GLOBAL_KEY,
    ]);
    const count = result.rows.length === 1 ? result.rows[0].count : undefined;
    if (typeof count !== "number" || !Number.isInteger(count) || count < 1) {
      throw new LimitError();
    }
    return count <= cap ? "ok" : "daily_cap";
  };
}

export const dailyCap: LimitFn = makeDailyCap({
  query: (text, params) => getPool().query<{ count: unknown }>(text, params),
  now: () => new Date(),
});
