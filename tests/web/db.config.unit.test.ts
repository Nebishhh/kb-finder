import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { format } from "node:util";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  buildPoolConfig,
  DatabaseConfigError,
  getPool,
  type DatabaseConfigCode,
  type DbEnv,
} from "@/lib/db";
import { dailyCap } from "@/lib/limits/daily-cap";
import { errorSummary } from "@/lib/safe-error";
import { handleSearch } from "@/lib/search/handler";
import { searchLexical, searchLexicalV2 } from "@/lib/search/lexical";
import { searchSemantic } from "@/lib/search/semantic";

// Offline. pg's Pool is replaced by a recorder, so nothing connects. The
// certificate below is a throwaway self-signed test certificate whose key was
// discarded; it is not trusted for anything.

const poolConfigs = vi.hoisted(() => [] as unknown[]);
vi.mock("pg", () => ({
  Pool: class {
    constructor(config: unknown) {
      poolConfigs.push(config);
    }
    on() {
      return this;
    }
    query() {
      return Promise.reject(new Error("fake pool: no queries"));
    }
  },
}));

const ROOT = process.cwd();

const TEST_CA = [
  "-----BEGIN CERTIFICATE-----",
  "MIIBqDCCAU+gAwIBAgIUHQ0kWLilesz0F6njazjPCds04wkwCgYIKoZIzj0EAwIw",
  "KjEoMCYGA1UEAwwfa2ItZmluZGVyIHRlc3QgQ0EgKG5vdCB0cnVzdGVkKTAeFw0y",
  "NjEwMDIyMTI4NDRaFw0zNjA5MjkyMTI4NDRaMCoxKDAmBgNVBAMMH2tiLWZpbmRl",
  "ciB0ZXN0IENBIChub3QgdHJ1c3RlZCkwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNC",
  "AATLmXV68LdYtvvm9qmYPEPCxfFqADzYy/Dwxf22GqRyvChlsK8hanl0WTihLCS3",
  "c27XlOQnRiMmSaTzwuFQdFuRo1MwUTAdBgNVHQ4EFgQUCyN+7Vo+YaFNp5872Qhb",
  "+RmXtxAwHwYDVR0jBBgwFoAUCyN+7Vo+YaFNp5872Qhb+RmXtxAwDwYDVR0TAQH/",
  "BAUwAwEB/zAKBggqhkjOPQQDAgNHADBEAiAYkxsO+uZ0R3LjnUFmd1M22UqvtkGI",
  "doE9rGASgy8LLgIgJvBstM0QVxeuV7UauY2GCnOFOuyHtqv1OmlsD7vPLEw=",
  "-----END CERTIFICATE-----",
  "",
].join("\n");

// Sentinels: none of these may reach an error message or a log line.
const SECRET_PW = "sentinelPw7731";
const SECRET_HOST = "sentinel-host-7731.example.test";
const HOSTED_URL = `postgresql://kb_app.ref:${SECRET_PW}@${SECRET_HOST}:6543/postgres`;
const LOCAL_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres";
const HOSTED: DbEnv = { DATABASE_URL: HOSTED_URL, DATABASE_CA_PEM: TEST_CA, VERCEL: "1" };

function configError(env: DbEnv): DatabaseConfigError {
  try {
    buildPoolConfig(env);
  } catch (err) {
    expect(err).toBeInstanceOf(DatabaseConfigError);
    return err as DatabaseConfigError;
  }
  throw new Error("expected buildPoolConfig to throw");
}

function expectFailsClosed(env: DbEnv, code: DatabaseConfigCode): void {
  const err = configError(env);
  expect(err.code).toBe(code);
  expect(err.name).toBe("DatabaseConfigError");
  expect(err.message).toBe(`database config error: ${code}`);
  expect(errorSummary(err)).toBe(`DatabaseConfigError ${code}`);
}

describe("buildPoolConfig, hosted (VERCEL set)", () => {
  it("verifies TLS against DATABASE_CA_PEM, with the frozen pool settings", () => {
    const config = buildPoolConfig(HOSTED);
    expect(config).toEqual({
      connectionString: HOSTED_URL,
      max: 5,
      statement_timeout: 5000,
      ssl: { ca: TEST_CA, rejectUnauthorized: true },
    });
    // Node's default hostname check stays in place.
    expect(config.ssl).not.toHaveProperty("checkServerIdentity");
  });

  it("accepts a bundle of several certificates and CRLF line endings", () => {
    const bundle = `${TEST_CA}${TEST_CA}`.replace(/\n/g, "\r\n");
    expect(buildPoolConfig({ ...HOSTED, DATABASE_CA_PEM: bundle }).ssl).toEqual({
      ca: bundle,
      rejectUnauthorized: true,
    });
  });

  it.each([undefined, "", "   \n"])("fails closed without the CA (%j)", (ca) => {
    expectFailsClosed({ ...HOSTED, DATABASE_CA_PEM: ca }, "DB_CA_MISSING");
  });

  const body = TEST_CA.split("\n").slice(1, -2).join("\n");
  it.each([
    ["plain text", "not a certificate"],
    ["base64 without PEM armor", body],
    ["a private key block", "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49\n-----END PRIVATE KEY-----\n"],
    ["a certificate with a corrupt body", TEST_CA.replace("MIIBqDCC", "AAAAAAAA")],
    ["a certificate with trailing junk", `${TEST_CA}junk`],
    ["escaped newlines instead of real ones", TEST_CA.replace(/\n/g, "\\n")],
  ])("fails closed on a non-PEM CA: %s", (_label, ca) => {
    expectFailsClosed({ ...HOSTED, DATABASE_CA_PEM: ca }, "DB_CA_INVALID");
  });

  // pg 8.23.1 merges URL parameters over the Pool config, so any of these would
  // replace or weaken the ssl object (or override statement_timeout).
  it.each([
    "sslmode=require",
    "sslmode=verify-full",
    "sslmode=disable",
    "sslmode=no-verify",
    "uselibpqcompat=true&sslmode=require",
    "uselibpqcompat=true",
    "ssl=true",
    "ssl=0",
    "sslrootcert=/tmp/ca.pem",
    "statement_timeout=0",
  ])("fails closed on a URL with parameters (?%s)", (query) => {
    expectFailsClosed({ ...HOSTED, DATABASE_URL: `${HOSTED_URL}?${query}` }, "DB_URL_HAS_PARAMS");
  });

  it.each([
    "not a url",
    "/var/run/postgresql mydb",
    "socket://user@/var/run/postgresql?db=x",
    `https://${SECRET_HOST}/postgres`,
    "postgresql:///postgres",
  ])("fails closed on a URL that is not an absolute postgres URL with a host (%s)", (url) => {
    expectFailsClosed({ ...HOSTED, DATABASE_URL: url }, "DB_URL_INVALID");
  });

  it("fails closed without DATABASE_URL", () => {
    expectFailsClosed({ ...HOSTED, DATABASE_URL: undefined }, "DB_URL_MISSING");
  });

  it("checks the URL before the CA", () => {
    expectFailsClosed(
      { DATABASE_URL: `${HOSTED_URL}?sslmode=disable`, VERCEL: "1" },
      "DB_URL_HAS_PARAMS",
    );
  });
});

describe("buildPoolConfig, local (VERCEL unset)", () => {
  it("has no ssl option and the same pool settings", () => {
    const config = buildPoolConfig({ DATABASE_URL: LOCAL_URL });
    expect(config).toEqual({ connectionString: LOCAL_URL, max: 5, statement_timeout: 5000 });
    expect(config).not.toHaveProperty("ssl");
  });

  it("ignores DATABASE_CA_PEM and an empty VERCEL, as before", () => {
    for (const env of [
      { DATABASE_URL: LOCAL_URL, DATABASE_CA_PEM: TEST_CA },
      { DATABASE_URL: LOCAL_URL, DATABASE_CA_PEM: "junk", VERCEL: "" },
    ]) {
      expect(buildPoolConfig(env)).toEqual({ connectionString: LOCAL_URL, max: 5, statement_timeout: 5000 });
    }
  });

  it("fails without DATABASE_URL", () => {
    expectFailsClosed({}, "DB_URL_MISSING");
  });
});

describe("no secret reaches an error or a log", () => {
  const SECRET_CA = TEST_CA.replace("-----END", "sentinelCa7731\n-----END");
  const cases: DbEnv[] = [
    { ...HOSTED, DATABASE_CA_PEM: undefined },
    { ...HOSTED, DATABASE_CA_PEM: SECRET_CA },
    { ...HOSTED, DATABASE_CA_PEM: "sentinelCa7731" },
    { ...HOSTED, DATABASE_URL: `${HOSTED_URL}?sslmode=require&sslrootcert=sentinelCa7731` },
    { ...HOSTED, DATABASE_URL: `postgres:// ${SECRET_PW} @@${SECRET_HOST}:x` },
  ];
  const SENTINELS = [SECRET_PW, SECRET_HOST, "sentinelCa7731", "BEGIN CERTIFICATE"];

  it.each(cases)("case %#", (env) => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((m) =>
      vi.spyOn(console, m).mockImplementation(() => {}),
    );
    const err = configError(env);
    const text = [err.message, String(err), err.stack ?? "", JSON.stringify(err), errorSummary(err)].join("\n");
    const logged = spies.flatMap((s) => s.mock.calls.map((args) => format(...args))).join("\n");
    for (const sentinel of SENTINELS) {
      expect(text).not.toContain(sentinel);
      expect(logged).not.toContain(sentinel);
    }
  });
});

describe("getPool and /api/search on a hosted misconfiguration", () => {
  const saved = { ...process.env };
  type PoolGlobal = typeof globalThis & { kbFinderPool?: unknown };

  beforeEach(() => {
    poolConfigs.length = 0;
    delete (globalThis as PoolGlobal).kbFinderPool;
  });
  afterEach(() => {
    process.env = { ...saved };
    delete (globalThis as PoolGlobal).kbFinderPool;
    vi.restoreAllMocks();
  });

  function setEnv(env: DbEnv): void {
    for (const name of ["DATABASE_URL", "DATABASE_CA_PEM", "VERCEL"] as const) {
      if (env[name] === undefined) delete process.env[name];
      else process.env[name] = env[name];
    }
  }

  it("never creates a pool when the CA is missing", () => {
    setEnv({ ...HOSTED, DATABASE_CA_PEM: undefined });
    expect(() => getPool()).toThrow(DatabaseConfigError);
    expect(poolConfigs).toEqual([]);
    expect((globalThis as PoolGlobal).kbFinderPool).toBeUndefined();
  });

  it("creates the pool from buildPoolConfig when the config is valid", () => {
    setEnv(HOSTED);
    getPool();
    expect(poolConfigs).toEqual([buildPoolConfig(HOSTED)]);
  });

  it.each([
    [{ ...HOSTED, DATABASE_CA_PEM: undefined }, "DB_CA_MISSING"],
    [{ ...HOSTED, DATABASE_CA_PEM: "sentinelCa7731" }, "DB_CA_INVALID"],
    [{ ...HOSTED, DATABASE_URL: `${HOSTED_URL}?sslmode=require` }, "DB_URL_HAS_PARAMS"],
  ] as const)("search returns the fixed 500 and logs only the class and code (%#)", async (env, code) => {
    setEnv(env);
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const response = await handleSearch(
      new Request("http://localhost/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: "wifi password reset", mode: "lexical" }),
      }),
      {
        retrievers: {
          lexical: searchLexical,
          semantic: searchSemantic,
          "lexical-v2": searchLexicalV2,
        },
        gate: () => "open",
        limit: dailyCap,
        env: { VERCEL: "1" },
      },
    );
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: "Search failed" });
    expect(errors.mock.calls.map((args) => format(...args))).toEqual([
      `search limit failed: DatabaseConfigError ${code}`,
    ]);
    expect(poolConfigs).toEqual([]);
  });

  it("the retriever fails the same way", async () => {
    setEnv({ ...HOSTED, DATABASE_CA_PEM: undefined });
    await expect(searchLexical("q", 50)).rejects.toBeInstanceOf(DatabaseConfigError);
    expect(poolConfigs).toEqual([]);
  });
});

describe("DATABASE_CA_PEM readers", () => {
  function sourceFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) return sourceFiles(full);
      return /\.(ts|tsx|js|mjs|cjs)$/.test(name) ? [full] : [];
    });
  }
  const files = [
    ...sourceFiles(join(ROOT, "app")),
    ...sourceFiles(join(ROOT, "lib")),
    join(ROOT, "proxy.ts"),
    join(ROOT, "next.config.ts"),
  ];

  it("is read only in lib/db.ts, which is server-only", () => {
    const readers = files.filter((f) => readFileSync(f, "utf8").includes("DATABASE_CA_PEM"));
    expect(readers).toEqual([join(ROOT, "lib", "db.ts")]);
    expect(readFileSync(join(ROOT, "lib", "db.ts"), "utf8").startsWith('import "server-only";')).toBe(true);
  });

  it("has no NEXT_PUBLIC_ form anywhere in app, lib, proxy.ts, next.config.ts or .env.example", () => {
    for (const file of [...files, join(ROOT, ".env.example")]) {
      expect(readFileSync(file, "utf8"), file).not.toMatch(/NEXT_PUBLIC_[A-Za-z0-9_]/);
    }
  });

  it(".env.example names DATABASE_CA_PEM without a value", () => {
    const lines = readFileSync(join(ROOT, ".env.example"), "utf8").split(/\r?\n/);
    expect(lines.some((l) => l.includes("DATABASE_CA_PEM"))).toBe(true);
    expect(lines.some((l) => /^\s*DATABASE_CA_PEM\s*=/.test(l))).toBe(false);
  });
});
