import "server-only";

import { X509Certificate } from "node:crypto";

import { Pool, type PoolConfig } from "pg";

import { errorSummary } from "@/lib/safe-error";
import { STATEMENT_TIMEOUT_MS } from "@/lib/search/config";

// One pool per process. Cached on globalThis so dev hot reload reuses it
// instead of opening a new pool on every reload.
const globalForDb = globalThis as typeof globalThis & {
  kbFinderPool?: Pool;
};

export type DatabaseConfigCode =
  | "DB_URL_MISSING"
  | "DB_URL_INVALID"
  | "DB_URL_HAS_PARAMS"
  | "DB_CA_MISSING"
  | "DB_CA_INVALID";

// The message is fixed per code: nothing from DATABASE_URL or DATABASE_CA_PEM
// is ever put in it. errorSummary() logs it as "DatabaseConfigError <code>".
export class DatabaseConfigError extends Error {
  readonly code: DatabaseConfigCode;
  constructor(code: DatabaseConfigCode) {
    super(`database config error: ${code}`);
    this.name = "DatabaseConfigError";
    this.code = code;
  }
}

export type DbEnv = {
  DATABASE_URL?: string;
  DATABASE_CA_PEM?: string;
  VERCEL?: string;
};

const PEM_CERT = /-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+?-----END CERTIFICATE-----/g;

// One or more PEM certificates and nothing else, each one parseable.
function isPemCertificates(text: string): boolean {
  const blocks = text.match(PEM_CERT);
  if (blocks === null || text.replace(PEM_CERT, "").trim() !== "") return false;
  try {
    for (const block of blocks) new X509Certificate(block);
  } catch {
    return false;
  }
  return true;
}

// Hosted DATABASE_URL: an absolute postgres URL with a host and no query string.
// pg 8.23.1 merges the parsed URL over the Pool config
// (Object.assign({}, config, parse(connectionString))), so any sslmode, ssl,
// sslrootcert or uselibpqcompat parameter would replace the ssl object below,
// and parameters like statement_timeout would override ours.
function checkHostedUrl(connectionString: string): void {
  let url: URL;
  try {
    url = new URL(connectionString);
  } catch {
    throw new DatabaseConfigError("DB_URL_INVALID");
  }
  if (!["postgres:", "postgresql:"].includes(url.protocol) || url.hostname === "") {
    throw new DatabaseConfigError("DB_URL_INVALID");
  }
  if (url.search !== "") throw new DatabaseConfigError("DB_URL_HAS_PARAMS");
}

// Pure: builds the Pool config without connecting. Throws DatabaseConfigError.
//
// Local (VERCEL unset): no ssl option, as before; the pool talks to 127.0.0.1
// in plain text.
// Hosted (VERCEL set): TLS with full verification against DATABASE_CA_PEM, the
// Supabase database CA. rejectUnauthorized is true and checkServerIdentity is
// Node's default, so the certificate chain and the hostname are both checked
// (pg sets servername to the host when it is not an IP). A missing or invalid
// CA, or a URL with parameters, fails closed: no pool is ever created.
export function buildPoolConfig(env: DbEnv): PoolConfig {
  const connectionString = env.DATABASE_URL;
  if (!connectionString) throw new DatabaseConfigError("DB_URL_MISSING");

  const base: PoolConfig = {
    connectionString,
    max: 5,
    statement_timeout: STATEMENT_TIMEOUT_MS,
  };
  if (!env.VERCEL) return base;

  checkHostedUrl(connectionString);
  const ca = env.DATABASE_CA_PEM;
  if (!ca || ca.trim() === "") throw new DatabaseConfigError("DB_CA_MISSING");
  if (!isPemCertificates(ca)) throw new DatabaseConfigError("DB_CA_INVALID");
  return { ...base, ssl: { ca, rejectUnauthorized: true } };
}

// Created on first use, so next build does not need DATABASE_URL.
export function getPool(): Pool {
  if (globalForDb.kbFinderPool) return globalForDb.kbFinderPool;

  const pool = new Pool(
    buildPoolConfig({
      DATABASE_URL: process.env.DATABASE_URL,
      DATABASE_CA_PEM: process.env.DATABASE_CA_PEM,
      VERCEL: process.env.VERCEL,
    }),
  );
  // Errors on idle clients would otherwise crash the process.
  pool.on("error", (err) => {
    console.error(`db pool error: ${errorSummary(err)}`);
  });
  globalForDb.kbFinderPool = pool;
  return pool;
}
