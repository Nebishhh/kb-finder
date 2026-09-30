import { afterAll, beforeAll, describe, expect, it } from "vitest";

// Live DB checks through the real route. Skipped unless
// KB_FINDER_TEST_DATABASE_URL is set. Only inputs that must return no
// results or a 400 are used: no ranking is inspected on the real corpus.

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
    ["unknown mode", JSON.stringify({ query: "wifi", mode: "semantic" })],
    ["invalid JSON", '{"query": "wifi", '],
  ])("%s returns 400", async (_name, body) => {
    const response = await POST(post(body));
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "Invalid request" });
  });
});
