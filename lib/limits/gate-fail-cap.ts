import "server-only";

import { getPool } from "@/lib/db";
import { GATE_FAIL_DAY_KIND, GLOBAL_KEY } from "@/lib/limits/config";
import { DAILY_CAP_SQL, LimitError } from "@/lib/limits/daily-cap";

// The failed-login counter behind /api/gate (lib/gate/login.ts): one global
// gate_fail_global_day row per UTC day. Each statement is a single statement, so
// it runs unchanged through the transaction pooler with kb_app's existing
// privileges on kb_rate_counters.
//
// Reserve is the search cap's statement with this kind: it purges windows that
// started more than one day before $1, then upserts today's row and returns the
// new count atomically (see DAILY_CAP_SQL).
export const GATE_RESERVE_SQL = DAILY_CAP_SQL;

// Release gives back one reservation on the same day's row: decrement, or delete
// the row when the count is 1 (the table's CHECK keeps count >= 1). One MERGE, so
// it is atomic: under READ COMMITTED, if another request changed the row first,
// MERGE re-evaluates the WHEN clauses on the newest version, so a concurrent
// reserve or release is never lost.
//
// $1 is the same request time the reservation used, so both touch the same window.
export const GATE_RELEASE_SQL =
  "merge into kb_rate_counters t " +
  "using (select $2::text as kind, $3::text as key, " +
  "date_trunc('day', $1::timestamptz, 'UTC') as window_start) s " +
  "on t.kind = s.kind and t.key = s.key and t.window_start = s.window_start " +
  "when matched and t.count > 1 then update set count = t.count - 1 " +
  "when matched then delete";

export type GateFailQueryFn = (
  text: string,
  params: unknown[],
) => Promise<{ rows: { count?: unknown }[]; rowCount: number | null }>;

// reserve returns the day's new failed-login count, this attempt included.
export type GateFailCap = {
  reserve: (nowMs: number) => Promise<number>;
  release: (nowMs: number) => Promise<void>;
};

export function makeGateFailCap(query: GateFailQueryFn): GateFailCap {
  const params = (nowMs: number) => [new Date(nowMs).toISOString(), GATE_FAIL_DAY_KIND, GLOBAL_KEY];
  return {
    async reserve(nowMs) {
      const result = await query(GATE_RESERVE_SQL, params(nowMs));
      const count = result.rows.length === 1 ? result.rows[0].count : undefined;
      if (typeof count !== "number" || !Number.isInteger(count) || count < 1) {
        throw new LimitError();
      }
      return count;
    },
    async release(nowMs) {
      const result = await query(GATE_RELEASE_SQL, params(nowMs));
      // The matching reservation guarantees the row exists.
      if (result.rowCount !== 1) throw new LimitError();
    },
  };
}

export const gateFailCap: GateFailCap = makeGateFailCap((text, params) =>
  getPool().query(text, params),
);
