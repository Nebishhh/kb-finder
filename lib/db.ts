import "server-only";

import { Pool } from "pg";

import { errorSummary } from "@/lib/safe-error";
import { STATEMENT_TIMEOUT_MS } from "@/lib/search/config";

// One pool per process. Cached on globalThis so dev hot reload reuses it
// instead of opening a new pool on every reload.
const globalForDb = globalThis as typeof globalThis & {
  kbFinderPool?: Pool;
};

export class DatabaseConfigError extends Error {
  constructor() {
    super("DATABASE_URL is not set");
    this.name = "DatabaseConfigError";
  }
}

// Created on first use, so next build does not need DATABASE_URL.
export function getPool(): Pool {
  if (globalForDb.kbFinderPool) return globalForDb.kbFinderPool;

  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) throw new DatabaseConfigError();

  const pool = new Pool({
    connectionString,
    max: 5,
    statement_timeout: STATEMENT_TIMEOUT_MS,
  });
  // Errors on idle clients would otherwise crash the process.
  pool.on("error", (err) => {
    console.error(`db pool error: ${errorSummary(err)}`);
  });
  globalForDb.kbFinderPool = pool;
  return pool;
}
