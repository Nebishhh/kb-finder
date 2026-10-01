import { readFileSync } from "node:fs";
import { join } from "node:path";

import { Client } from "pg";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

// Live DB checks through the real route. Skipped unless
// KB_FINDER_TEST_DATABASE_URL is set. Only inputs that must return no
// results or a 400 are used: no ranking is inspected on the real corpus.
// tests/web/setup.ts clears OPENAI_API_KEY, EVAL_MODES_ENABLED and
// DEFAULT_SEARCH_MODE before every test and blocks real fetch.

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

  it("returns ranked rows with finite numeric scores, then leaves no trace", async () => {
    const { makeSemanticSearch, toVectorLiteral } = await import("@/lib/search/semantic");
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
        "insert into embedding_configs values ($1, '{}', $2, $3, 'article-v1')",
        [cfg.sha256, cfg.model, cfg.dimensions],
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
      const search = makeSemanticSearch({
        embed: async () => unit(0, cfg.dimensions),
        query: (text, params) => client.query(text, params),
        config: getSemanticConfig,
      });
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
