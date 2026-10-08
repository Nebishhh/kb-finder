import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { Client } from "pg";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { SearchFn } from "@/lib/search/handler";
import type { ConfigRowsFn } from "@/lib/search/semantic";

// Live DB checks through the real route. Skipped unless
// KB_FINDER_TEST_DATABASE_URL is set. Only inputs that must return no
// results or a 400 are used: no ranking is inspected on the real corpus.
// tests/web/setup.ts clears OPENAI_API_KEY, EVAL_MODES_ENABLED,
// DEFAULT_SEARCH_MODE, RATE_LIMIT, VERCEL and the gate variables before every
// test and blocks real fetch.

const databaseUrl = process.env.KB_FINDER_TEST_DATABASE_URL;

function post(body: string): Request {
  return new Request("http://localhost/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });
}

describe.skipIf(!databaseUrl)("POST /api/search against the local DB", () => {
  let POST: (request: Request) => Promise<Response>;

  beforeAll(async () => {
    process.env.DATABASE_URL = databaseUrl;
    ({ POST } = await import("@/app/api/search/route"));
  });

  afterAll(async () => {
    const { getPool } = await import("@/lib/db");
    await getPool().end();
  });

  // The route tests never touch public.kb_rate_counters; the cap's SQL is
  // tested in a throwaway schema below.
  beforeEach(() => {
    process.env.RATE_LIMIT = "off";
    process.env.ACCESS_GATE = "off";
  });

  it.each(["   ", "the and of"])(
    "%j returns 200 with results []",
    async (query) => {
      const response = await POST(post(JSON.stringify({ query, mode: "lexical" })));
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({ mode: "lexical", results: [] });
    },
  );

  it.each([
    ["empty query", JSON.stringify({ query: "", mode: "lexical" })],
    ["extra field", JSON.stringify({ query: "wifi", mode: "lexical", limit: 100 })],
    ["semantic without the gate", JSON.stringify({ query: "wifi", mode: "semantic" })],
    ["invalid JSON", '{"query": "wifi", '],
  ])("%s returns 400", async (_name, body) => {
    const response = await POST(post(body));
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "Invalid request" });
  });

  it("semantic with the gate on and no key: 500, OPENAI_KEY_MISSING, no fetch", async () => {
    process.env.EVAL_MODES_ENABLED = "true"; // cleared again by setup.ts
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const response = await POST(post(JSON.stringify({ query: "wifi", mode: "semantic" })));
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: "Search failed" });
    expect(errors).toHaveBeenCalledWith("search failed: EmbeddingError OPENAI_KEY_MISSING");
    expect(fetchSpy).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  });
});

// The real SEMANTIC_SQL against both real migrations, in a throwaway schema on
// a dedicated client, inside one transaction that is always rolled back. The
// query vector is fixed; no OpenAI call is made.
describe.skipIf(!databaseUrl)("kb_search_semantic through SEMANTIC_SQL", () => {
  const MIGRATIONS = [
    "20260930042716_create_articles.sql",
    "20261001011735_add_semantic_embeddings.sql",
  ].map((name) => readFileSync(join(process.cwd(), "supabase", "migrations", name), "utf8"));

  function unit(index: number, dims: number): number[] {
    const v = new Array<number>(dims).fill(0);
    v[index] = 1;
    return v;
  }

  // The embedding_configs row holds the real file config, as embed_kb.py load
  // writes it, so the server-side check (checkConfigRow) passes. tamper runs
  // against that row before the search.
  async function withSemanticSchema(
    body: (search: SearchFn, configRows: ConfigRowsFn, embed: () => void) => Promise<void>,
    tamper?: string,
  ) {
    const { CONFIG_ROW_SQL, makeSemanticSearch, toVectorLiteral } = await import(
      "@/lib/search/semantic"
    );
    const { getSemanticConfig } = await import("@/lib/search/semantic-config");
    const cfg = getSemanticConfig();
    const schema = `kbtest_${crypto.randomUUID().replaceAll("-", "")}`;
    const client = new Client({ connectionString: databaseUrl });
    await client.connect();
    try {
      await client.query("begin");
      await client.query(`create schema ${schema}`);
      await client.query(`set local search_path to ${schema}`);
      for (const sql of MIGRATIONS) await client.query(sql);
      await client.query(
        "insert into articles (source_article_id, title, url, full_text, sections, parser_version) " +
          "select id, 'Heading ' || id, " +
          "'https://td.usd.edu/TDClient/33/Portal/KB/Article/' || id || '/x', 'Body.', '[]', '2' " +
          "from unnest(array['9', '10', '100']) as id",
      );
      await client.query(
        "insert into embedding_configs values ($1, $2::jsonb, $3, $4, $5)",
        [cfg.sha256, cfg.canonical, cfg.model, cfg.dimensions, cfg.templateVersion],
      );
      // 10 matches the query exactly; 9 and 100 are orthogonal and tie at 0.
      const vectors: [string, number][] = [["9", 1], ["10", 0], ["100", 2]];
      for (const [id, index] of vectors) {
        await client.query(
          "insert into article_embeddings (config_sha256, source_article_id, source_text_sha256, " +
            "input_kind, input_sha256, input_tokens, truncated, embedding) " +
            "select $1, a.source_article_id, kb_article_v1_sha256(a.title, a.full_text), " +
            "'text', repeat('c', 64), 5, false, $3::text::extensions.vector " +
            "from articles a where a.source_article_id = $2",
          [cfg.sha256, id, toVectorLiteral(unit(index, cfg.dimensions), cfg.dimensions)],
        );
      }
      if (tamper !== undefined) await client.query(tamper, [cfg.sha256]);
      const embed = vi.fn(async () => unit(0, cfg.dimensions));
      const configRows: ConfigRowsFn = async (sha256) =>
        (await client.query(CONFIG_ROW_SQL, [sha256])).rows;
      const search = makeSemanticSearch({
        embed,
        query: (text, params) => client.query(text, params),
        configRows,
        config: getSemanticConfig,
      });
      await body(search, configRows, () => expect(embed).not.toHaveBeenCalled());
    } finally {
      await client.query("rollback");
      const left = await client.query(
        "select count(*)::int as n from pg_namespace where nspname = $1",
        [schema],
      );
      await client.end();
      expect(left.rows[0].n).toBe(0);
    }
  }

  it("returns ranked rows with finite numeric scores, then leaves no trace", async () => {
    await withSemanticSchema(async (search) => {
      const rows = await search("wifi", 50);
      expect(rows.map((r) => [r.rank, r.source_article_id])).toEqual([
        [1, "10"],
        [2, "100"],
        [3, "9"],
      ]);
      for (const r of rows) {
        expect(typeof r.score).toBe("number");
        expect(Number.isFinite(r.score)).toBe(true);
      }
      expect(rows[0].score).toBe(1);
      expect(rows[1].score).toBe(0);
      expect(rows[0].title).toBe("Heading 10");
    });
  });

  it("the stored row equals the file: jsonb round-trips to the file's canonical form", async () => {
    const { checkConfigRow } = await import("@/lib/search/semantic");
    const { getSemanticConfig } = await import("@/lib/search/semantic-config");
    await withSemanticSchema(async (_search, configRows) => {
      const rows = await configRows(getSemanticConfig().sha256);
      expect(rows).toHaveLength(1);
      expect(() => checkConfigRow(rows, getSemanticConfig())).not.toThrow();
    });
  });

  it.each([
    ["config jsonb {}", "update embedding_configs set config = '{}' where config_sha256 = $1"],
    ["one config key changed",
      "update embedding_configs set config = jsonb_set(config, '{template}', '\"x\"') " +
        "where config_sha256 = $1"],
    ["model column", "update embedding_configs set model = 'text-embedding-3-large' " +
      "where config_sha256 = $1"],
    ["template_version column",
      "update embedding_configs set template_version = 'article-v2' where config_sha256 = $1"],
  ])("tampered %s: KBF02_CONFIG_MISMATCH before any embed", async (_name, tamper) => {
    await withSemanticSchema(async (search, _configRows, embedNotCalled) => {
      await expect(search("wifi", 50)).rejects.toMatchObject({
        name: "ConfigMismatchError",
        code: "KBF02_CONFIG_MISMATCH",
      });
      embedNotCalled();
    }, tamper);
  });
});

// DAILY_CAP_SQL as kb_app, against all four migrations in a throwaway schema on a
// dedicated client, inside one transaction that is always rolled back. The
// request time is a parameter, so the clock is moved without waiting.
describe.skipIf(!databaseUrl)("the daily cap through DAILY_CAP_SQL as kb_app", () => {
  const MIGRATIONS = [
    "20260930042716_create_articles.sql",
    "20261001011735_add_semantic_embeddings.sql",
    "20261001182428_lock_down_data_api.sql",
    "20261001190406_add_app_role.sql",
  ].map((name) => readFileSync(join(process.cwd(), "supabase", "migrations", name), "utf8"));

  async function inThrowawaySchema(body: (client: Client) => Promise<void>) {
    const schema = `kbtest_${crypto.randomUUID().replaceAll("-", "")}`;
    const client = new Client({ connectionString: databaseUrl });
    await client.connect();
    try {
      await client.query("begin");
      await client.query(`create schema ${schema}`);
      await client.query(`set local search_path to ${schema}`);
      await client.query("set local timezone to 'UTC'"); // stable window_start::text
      for (const sql of MIGRATIONS) await client.query(sql);
      // postgres holds ADMIN on kb_app but not SET; this grant is rolled back.
      await client.query("grant kb_app to current_user with inherit false, set true");
      await client.query("set local role kb_app");
      const who = await client.query("select current_user as u");
      expect(who.rows[0].u).toBe("kb_app");
      await body(client);
    } finally {
      await client.query("rollback");
      const left = await client.query(
        "select count(*)::int as n from pg_namespace where nspname = $1",
        [schema],
      );
      await client.end();
      expect(left.rows[0].n).toBe(0);
    }
  }

  async function capAt(client: Client, iso: string) {
    const { makeDailyCap } = await import("@/lib/limits/daily-cap");
    return makeDailyCap({
      query: (text, params) => client.query(text, params),
      now: () => new Date(iso),
    });
  }

  async function rows(client: Client) {
    const result = await client.query(
      "select kind, key, window_start::text as w, count from kb_rate_counters " +
        "order by window_start, kind",
    );
    return result.rows;
  }

  it("counts increment on the day's single global row", async () => {
    await inThrowawaySchema(async (client) => {
      const cap = await capAt(client, "2026-10-01T09:00:00Z");
      for (let i = 0; i < 3; i++) expect(await cap()).toBe("ok");
      expect(await rows(client)).toEqual([
        { kind: "search_global_day", key: "all", w: "2026-10-01 00:00:00+00", count: 3 },
      ]);
    });
  });

  it("allows exactly 200 a day; request 201 trips the cap; the next UTC day starts over", async () => {
    await inThrowawaySchema(async (client) => {
      const { DAILY_SEARCH_CAP } = await import("@/lib/limits/config");
      expect(DAILY_SEARCH_CAP).toBe(200);
      const day1 = await capAt(client, "2026-10-01T23:59:59Z");
      for (let i = 1; i <= 200; i++) expect(await day1(), `request ${i}`).toBe("ok");
      expect(await day1()).toBe("daily_cap");
      expect(await day1()).toBe("daily_cap");

      const day2 = await capAt(client, "2026-10-02T00:00:00Z");
      expect(await day2()).toBe("ok");
      const after = await rows(client);
      expect(after).toEqual([
        { kind: "search_global_day", key: "all", w: "2026-10-01 00:00:00+00", count: 202 },
        { kind: "search_global_day", key: "all", w: "2026-10-02 00:00:00+00", count: 1 },
      ]);
    });
  });

  it("deletes windows that started more than one day before the request", async () => {
    await inThrowawaySchema(async (client) => {
      const key = "b".repeat(64);
      await client.query(
        "insert into kb_rate_counters values " +
          "('search_ip_10m', $1, '2026-10-02T13:59:59Z', 4), " + // 1 day + 1 s before: purged
          "('search_ip_10m', $1, '2026-10-02T14:00:00Z', 5), " + // exactly 1 day: kept
          "('search_ip_day', $1, '2026-10-02T00:00:00Z', 6), " + // 38 h before: purged
          "('search_global_day', 'all', '2026-10-01T00:00:00Z', 9)", // purged
        [key],
      );
      const cap = await capAt(client, "2026-10-03T14:00:00Z");
      expect(await cap()).toBe("ok");
      expect(await rows(client)).toEqual([
        { kind: "search_ip_10m", key, w: "2026-10-02 14:00:00+00", count: 5 },
        { kind: "search_global_day", key: "all", w: "2026-10-03 00:00:00+00", count: 1 },
      ]);
    });
  });
});

// The failed-login cap as kb_app, against every migration in order (so the gate
// kind migration is included), in a throwaway schema on a dedicated client,
// inside one transaction that is always rolled back. The request time is a
// parameter, so the clock is moved without waiting.
describe.skipIf(!databaseUrl)("the failed-login cap and the gate kind as kb_app", () => {
  const MIGRATION_DIR = join(process.cwd(), "supabase", "migrations");
  const NAMES = readdirSync(MIGRATION_DIR).filter((n) => n.endsWith(".sql")).sort();
  const GATE_KIND = NAMES.filter((n) => n.endsWith("_add_gate_fail_kind.sql"));
  const ARTICLE_V2 = NAMES.filter((n) => n.endsWith("_add_article_v2.sql"));
  const MIGRATIONS = NAMES.map((name) => readFileSync(join(MIGRATION_DIR, name), "utf8"));
  // The gate kind migration, rerun by name: it is idempotent; article-v2 is not.
  const GATE_KIND_SQL = readFileSync(join(MIGRATION_DIR, GATE_KIND[0]), "utf8");
  const PIN = "8264";
  const GATE_ENV = {
    ACCESS_PASSWORD: PIN,
    GATE_COOKIE_SECRET: "test-cookie-secret-0123456789abcdef",
  };

  async function inThrowawaySchema(body: (client: Client) => Promise<void>, rerunGateKind = false) {
    const schema = `kbtest_${crypto.randomUUID().replaceAll("-", "")}`;
    const client = new Client({ connectionString: databaseUrl });
    await client.connect();
    try {
      await client.query("begin");
      await client.query(`create schema ${schema}`);
      await client.query(`set local search_path to ${schema}`);
      await client.query("set local timezone to 'UTC'"); // stable window_start::text
      for (const sql of MIGRATIONS) await client.query(sql);
      if (rerunGateKind) await client.query(GATE_KIND_SQL);
      // postgres holds ADMIN on kb_app but not SET; this grant is rolled back.
      await client.query("grant kb_app to current_user with inherit false, set true");
      await client.query("set local role kb_app");
      const who = await client.query("select current_user as u");
      expect(who.rows[0].u).toBe("kb_app");
      await body(client);
    } finally {
      await client.query("rollback");
      const left = await client.query(
        "select count(*)::int as n from pg_namespace where nspname = $1",
        [schema],
      );
      await client.end();
      expect(left.rows[0].n).toBe(0);
    }
  }

  async function rows(client: Client) {
    const result = await client.query(
      "select kind, key, window_start::text as w, count from kb_rate_counters " +
        "order by window_start, kind",
    );
    return result.rows;
  }

  async function failCap(client: Client) {
    const { makeGateFailCap } = await import("@/lib/limits/gate-fail-cap");
    return makeGateFailCap((text, params) => client.query(text, params));
  }

  // "ok", or the SQLSTATE; a rejected insert rolls back only its savepoint.
  async function insertCode(client: Client, kind: string, key: string, count = 1) {
    await client.query("savepoint s");
    try {
      await client.query(
        "insert into kb_rate_counters values ($1, $2, '2026-10-01T00:00Z', $3)",
        [kind, key, count],
      );
      await client.query("release savepoint s");
      return "ok";
    } catch (err) {
      await client.query("rollback to savepoint s");
      return (err as { code?: string }).code;
    }
  }

  it("the gate kind migration is the latest before article-v2, changes only the two CHECKs, and is idempotent", async () => {
    expect(GATE_KIND).toHaveLength(1);
    expect(ARTICLE_V2).toHaveLength(1);
    expect(NAMES.at(-1)).toBe(ARTICLE_V2[0]);
    expect(NAMES.at(-2)).toBe(GATE_KIND[0]);
    await inThrowawaySchema(async (client) => {
      const defs = await client.query(
        "select conname, pg_get_constraintdef(oid) as def from pg_constraint " +
          "where conrelid = 'kb_rate_counters'::regclass order by conname",
      );
      expect(defs.rows).toEqual([
        {
          conname: "kb_rate_counters_check",
          def:
            "CHECK (((key = 'all'::text) = (kind = ANY (ARRAY['search_global_day'::text, " +
            "'gate_fail_global_day'::text]))))",
        },
        { conname: "kb_rate_counters_count_check", def: "CHECK ((count >= 1))" },
        {
          conname: "kb_rate_counters_key_check",
          def: "CHECK (((key ~ '^[0-9a-f]{64}$'::text) OR (key = 'all'::text)))",
        },
        {
          conname: "kb_rate_counters_kind_check",
          def:
            "CHECK ((kind = ANY (ARRAY['search_ip_10m'::text, 'search_ip_day'::text, " +
            "'search_global_day'::text, 'gate_ip_10m'::text, 'gate_fail_global_day'::text])))",
        },
        { conname: "kb_rate_counters_pkey", def: "PRIMARY KEY (kind, key, window_start)" },
      ]);
    }, true);
  });

  it("accepts the new kind with 'all' only; existing kinds and rules are unchanged", async () => {
    await inThrowawaySchema(async (client) => {
      const hex = "c".repeat(64);
      expect(await insertCode(client, "gate_fail_global_day", "all")).toBe("ok");
      expect(await insertCode(client, "gate_fail_global_day", hex)).toBe("23514");
      expect(await insertCode(client, "search_global_day", "all")).toBe("ok");
      for (const kind of ["search_ip_10m", "search_ip_day", "gate_ip_10m"]) {
        expect(await insertCode(client, kind, hex), kind).toBe("ok");
        expect(await insertCode(client, kind, "all"), kind).toBe("23514");
      }
      expect(await insertCode(client, "search_global_day", hex)).toBe("23514");
      expect(await insertCode(client, "search_ip_10m", "C".repeat(64))).toBe("23514");
      expect(await insertCode(client, "search_ip_10m", "c".repeat(63))).toBe("23514");
      expect(await insertCode(client, "raw_ip", hex)).toBe("23514");
      expect(await insertCode(client, "gate_fail_10m", "all")).toBe("23514");
      expect(await insertCode(client, "search_ip_10m", "d".repeat(64), 0)).toBe("23514");
      expect(await insertCode(client, "gate_fail_global_day", "all")).toBe("23505"); // pkey
    });
  });

  it("reserve and release count right, on a row separate from the search cap", async () => {
    await inThrowawaySchema(async (client) => {
      const { makeDailyCap } = await import("@/lib/limits/daily-cap");
      const at = Date.parse("2026-10-01T09:00:00Z");
      const search = makeDailyCap({
        query: (text, params) => client.query(text, params),
        now: () => new Date(at),
      });
      const cap = await failCap(client);
      expect(await search()).toBe("ok");
      expect(await cap.reserve(at)).toBe(1);
      expect(await cap.reserve(at)).toBe(2);
      expect(await cap.reserve(at)).toBe(3);
      expect(await search()).toBe("ok");
      await cap.release(at);
      expect(await rows(client)).toEqual([
        { kind: "gate_fail_global_day", key: "all", w: "2026-10-01 00:00:00+00", count: 2 },
        { kind: "search_global_day", key: "all", w: "2026-10-01 00:00:00+00", count: 2 },
      ]);
      await cap.release(at);
      await cap.release(at); // count 1: the row is deleted, never set to 0
      expect(await rows(client)).toEqual([
        { kind: "search_global_day", key: "all", w: "2026-10-01 00:00:00+00", count: 2 },
      ]);
      // With no reservation left, a release finds no row and throws.
      await expect(cap.release(at)).rejects.toMatchObject({ code: "LIMIT_BAD_RESULT" });
      expect(await cap.reserve(at)).toBe(1);
    });
  });

  it("the 11th reservation of a day trips, even for the right PIN; the next UTC day starts over", async () => {
    await inThrowawaySchema(async (client) => {
      const { handleGateLogin } = await import("@/lib/gate/login");
      const { GATE_FAIL_CAP } = await import("@/lib/limits/config");
      expect(GATE_FAIL_CAP).toBe(10);
      const cap = await failCap(client);
      const login = async (pin: string, iso: string) => {
        const response = await handleGateLogin(
          new Request("http://localhost/api/gate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password: pin }),
          }),
          GATE_ENV,
          Date.parse(iso),
          cap,
        );
        return response.status;
      };

      const day1 = "2026-10-01T23:59:59Z";
      expect(await login(PIN, day1)).toBe(204); // reserved, then released: net zero
      for (let i = 1; i <= 10; i++) expect(await login("0000", day1), `attempt ${i}`).toBe(401);
      expect(await rows(client)).toEqual([
        { kind: "gate_fail_global_day", key: "all", w: "2026-10-01 00:00:00+00", count: 10 },
      ]);
      expect(await login(PIN, day1)).toBe(429);
      expect(await login("0000", day1)).toBe(429);

      const day2 = "2026-10-02T00:00:00Z";
      expect(await login(PIN, day2)).toBe(204);
      expect(await login("0000", day2)).toBe(401);
      expect(await rows(client)).toEqual([
        { kind: "gate_fail_global_day", key: "all", w: "2026-10-01 00:00:00+00", count: 12 },
        { kind: "gate_fail_global_day", key: "all", w: "2026-10-02 00:00:00+00", count: 1 },
      ]);
    });
  });

  it("reserve purges windows that started more than one day before the request", async () => {
    await inThrowawaySchema(async (client) => {
      await client.query(
        "insert into kb_rate_counters values " +
          "('gate_fail_global_day', 'all', '2026-10-01T00:00:00Z', 9), " + // 2 days before: purged
          "('search_global_day', 'all', '2026-10-02T00:00:00Z', 7)", // exactly 1 day: kept
      );
      const cap = await failCap(client);
      expect(await cap.reserve(Date.parse("2026-10-03T00:00:00Z"))).toBe(1);
      expect(await rows(client)).toEqual([
        { kind: "search_global_day", key: "all", w: "2026-10-02 00:00:00+00", count: 7 },
        { kind: "gate_fail_global_day", key: "all", w: "2026-10-03 00:00:00+00", count: 1 },
      ]);
    });
  });
});

// The real LEXICAL_SQL and LEXICAL_V2_SQL as kb_app, against every migration in
// order, in a throwaway schema on a dedicated client, inside one transaction that
// is always rolled back. Rows are synthetic placeholders.
describe.skipIf(!databaseUrl)("LEXICAL_V2_SQL beside LEXICAL_SQL as kb_app", () => {
  const MIGRATION_DIR = join(process.cwd(), "supabase", "migrations");
  const MIGRATIONS = readdirSync(MIGRATION_DIR)
    .filter((n) => n.endsWith(".sql"))
    .sort()
    .map((name) => readFileSync(join(MIGRATION_DIR, name), "utf8"));

  it("lexical-v2 finds a staff-only term that v1 does not; other rows match v1", async () => {
    const { LEXICAL_SQL, LEXICAL_V2_SQL } = await import("@/lib/search/lexical");
    const schema = `kbtest_${crypto.randomUUID().replaceAll("-", "")}`;
    const client = new Client({ connectionString: databaseUrl });
    await client.connect();
    try {
      await client.query("begin");
      await client.query(`create schema ${schema}`);
      await client.query(`set local search_path to ${schema}`);
      for (const sql of MIGRATIONS) await client.query(sql);
      // 1 has the term only in its staff-only text; 2 has it in the body.
      await client.query(
        "insert into articles (source_article_id, title, url, full_text, sections, " +
          "staff_only, parser_version) values " +
          "('1', 'Printer setup', 'https://td.usd.edu/TDClient/33/Portal/KB/Article/1/x', " +
          "'Add a campus printer.', '[]', " +
          "'{\"text\": \"Escalate to zebrafinch queue.\", \"links\": []}', '2'), " +
          "('2', 'Queue help', 'https://td.usd.edu/TDClient/33/Portal/KB/Article/2/x', " +
          "'The zebrafinch queue handles printers.', '[]', null, '2')",
      );
      await client.query("grant kb_app to current_user with inherit false, set true");
      await client.query("set local role kb_app");

      const ids = async (sql: string, q: string) =>
        (await client.query(sql, [q, 50])).rows.map((r) => r.source_article_id);
      expect(await ids(LEXICAL_SQL, "zebrafinch")).toEqual(["2"]);
      expect(await ids(LEXICAL_V2_SQL, "zebrafinch")).toEqual(["2", "1"]);
      expect(await ids(LEXICAL_SQL, "escalate")).toEqual([]);
      expect(await ids(LEXICAL_V2_SQL, "escalate")).toEqual(["1"]);
      // No staff text involved: same rows, ranks and scores.
      const v1 = (await client.query(LEXICAL_SQL, ["campus printer", 50])).rows;
      const v2 = (await client.query(LEXICAL_V2_SQL, ["campus printer", 50])).rows;
      expect(v1.length).toBeGreaterThan(0);
      expect(v2).toEqual(v1);
    } finally {
      await client.query("rollback");
      const left = await client.query(
        "select count(*)::int as n from pg_namespace where nspname = $1",
        [schema],
      );
      await client.end();
      expect(left.rows[0].n).toBe(0);
    }
  });
});
