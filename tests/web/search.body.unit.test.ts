import { describe, expect, it, vi } from "vitest";

import { MAX_BODY_BYTES, readJsonBody } from "@/lib/search/body";
import { SEARCH_DEPTH } from "@/lib/search/config";
import { handleSearch, type SearchFn } from "@/lib/search/handler";
import { SearchRequestSchema } from "@/lib/search/schema";

// Offline. Bodies are streams built here, so each test controls exactly what is
// pulled, whether Content-Length is sent, and whether the reader is cancelled.

const INVALID = { error: "Invalid request" };
const TOO_LARGE = { error: "Request too large" };
const encoder = new TextEncoder();

type Probe = { pulls: number; cancelled: boolean };

// highWaterMark 0: a stream pulls only when something reads it, never ahead.
const NO_PREFETCH = { highWaterMark: 0 };

// A stream that yields the given chunks, recording pulls and cancellation.
function chunkStream(chunks: Uint8Array[], probe: Probe): ReadableStream<Uint8Array> {
  let i = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      probe.pulls++;
      if (i < chunks.length) controller.enqueue(chunks[i++]);
      else controller.close();
    },
    cancel() {
      probe.cancelled = true;
    },
  }, NO_PREFETCH);
}

// A stream that fails the test if anything reads it.
function untouchableStream(probe: Probe): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    pull() {
      probe.pulls++;
      throw new Error("body was read");
    },
  }, NO_PREFETCH);
}

function streamRequest(body: ReadableStream<Uint8Array>, contentLength?: string): Request {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (contentLength !== undefined) headers.set("Content-Length", contentLength);
  return new Request("http://localhost/api/search", {
    method: "POST",
    headers,
    body,
    duplex: "half",
  } as RequestInit);
}

function bytesRequest(bytes: Uint8Array, contentLength?: string): Request {
  return streamRequest(chunkStream([bytes], { pulls: 0, cancelled: false }), contentLength);
}

// A lexical request body padded with trailing JSON whitespace to exactly n bytes.
function paddedBody(n: number): Uint8Array {
  const json = JSON.stringify({ query: "wifi", mode: "lexical" });
  return encoder.encode(json + " ".repeat(n - encoder.encode(json).byteLength));
}

function fakeSearch() {
  return vi.fn<SearchFn>(async () => []);
}

const semanticNeverCalled = vi.fn<SearchFn>(async () => {
  throw new Error("semantic retriever called");
});

async function run(request: Request, search: SearchFn) {
  const response = await handleSearch(request, {
    retrievers: { lexical: search, semantic: semanticNeverCalled },
    limit: async () => "ok" as const,
    env: {},
  });
  return {
    status: response.status,
    cache: response.headers.get("Cache-Control"),
    body: await response.json(),
  };
}

describe("the byte cap", () => {
  it("is 32,768 bytes", () => {
    expect(MAX_BODY_BYTES).toBe(32_768);
  });

  it("Content-Length over the cap: 413 and the body is never touched", async () => {
    const probe: Probe = { pulls: 0, cancelled: false };
    const search = fakeSearch();
    const result = await run(
      streamRequest(untouchableStream(probe), String(MAX_BODY_BYTES + 1)),
      search,
    );
    expect(result).toEqual({ status: 413, cache: "no-store", body: TOO_LARGE });
    expect(probe.pulls).toBe(0);
    expect(search).not.toHaveBeenCalled();
  });

  it("a huge Content-Length is 413, not a number overflow", async () => {
    const probe: Probe = { pulls: 0, cancelled: false };
    const result = await readJsonBody(
      streamRequest(untouchableStream(probe), "9".repeat(40)),
    );
    expect(result).toEqual({ ok: false, reason: "too_large" });
    expect(probe.pulls).toBe(0);
  });

  it("no Content-Length and an oversized stream: 413 and the reader is cancelled", async () => {
    const probe: Probe = { pulls: 0, cancelled: false };
    const chunk = encoder.encode("x".repeat(8192));
    const chunks = Array.from({ length: 20 }, () => chunk); // 163,840 bytes on offer
    const search = fakeSearch();
    const result = await run(streamRequest(chunkStream(chunks, probe)), search);
    expect(result).toEqual({ status: 413, cache: "no-store", body: TOO_LARGE });
    expect(probe.cancelled).toBe(true);
    expect(probe.pulls).toBeLessThan(chunks.length); // stopped early, not drained
    expect(search).not.toHaveBeenCalled();
  });

  it("a Content-Length under the cap does not stop the stream from being counted", async () => {
    const probe: Probe = { pulls: 0, cancelled: false };
    const chunk = encoder.encode("x".repeat(8192));
    const result = await readJsonBody(
      streamRequest(chunkStream([chunk, chunk, chunk, chunk, chunk], probe), "10"),
    );
    expect(result).toEqual({ ok: false, reason: "too_large" });
    expect(probe.cancelled).toBe(true);
  });

  it.each([
    ["streamed", undefined],
    ["with Content-Length", String(MAX_BODY_BYTES)],
  ])("exactly 32,768 bytes is accepted (%s)", async (_name, contentLength) => {
    const body = paddedBody(MAX_BODY_BYTES);
    expect(body.byteLength).toBe(MAX_BODY_BYTES);
    const search = fakeSearch();
    const result = await run(bytesRequest(body, contentLength), search);
    expect(result.status).toBe(200);
    expect(search).toHaveBeenCalledWith("wifi", SEARCH_DEPTH);
  });

  it("32,769 bytes is 413", async () => {
    const result = await run(bytesRequest(paddedBody(MAX_BODY_BYTES + 1)), fakeSearch());
    expect(result).toEqual({ status: 413, cache: "no-store", body: TOO_LARGE });
  });

  it('5,000 "\\u0001" characters as escaped JSON (30,000+ bytes) are accepted', async () => {
    const query = "\u0001".repeat(5000);
    const text = JSON.stringify({ query, mode: "lexical" });
    const bytes = encoder.encode(text);
    expect(text).toContain("\\u0001");
    expect(bytes.byteLength).toBeGreaterThan(30_000);
    expect(bytes.byteLength).toBeLessThanOrEqual(MAX_BODY_BYTES);

    const read = await readJsonBody(bytesRequest(bytes));
    expect(read.ok).toBe(true);
    if (!read.ok) return;
    expect(SearchRequestSchema.safeParse(read.value).success).toBe(true);

    const search = fakeSearch();
    const result = await run(bytesRequest(bytes), search);
    expect(result.status).toBe(200);
    expect(search).toHaveBeenCalledWith(query, SEARCH_DEPTH);
  });

  it("5,000 three-byte characters sent raw (15,000+ bytes) are accepted", async () => {
    const query = "€".repeat(5000); // €, 3 bytes in UTF-8
    const search = fakeSearch();
    const result = await run(
      bytesRequest(encoder.encode(JSON.stringify({ query, mode: "lexical" }))),
      search,
    );
    expect(result.status).toBe(200);
    expect(search).toHaveBeenCalledWith(query, SEARCH_DEPTH);
  });
});

describe("invalid bodies are 400 with the existing fixed body", () => {
  it("invalid UTF-8", async () => {
    const bytes = new Uint8Array([
      ...encoder.encode('{"query":"wi'),
      0xc3, 0x28, // invalid 2-byte sequence
      ...encoder.encode('fi","mode":"lexical"}'),
    ]);
    const search = fakeSearch();
    const result = await run(bytesRequest(bytes), search);
    expect(result).toEqual({ status: 400, cache: "no-store", body: INVALID });
    expect(search).not.toHaveBeenCalled();
  });

  it.each(["abc", "-1", "1.5", "1e3", "12, 12", "0x10"])(
    "malformed Content-Length %j, with the body never touched",
    async (contentLength) => {
      const probe: Probe = { pulls: 0, cancelled: false };
      const search = fakeSearch();
      const result = await run(streamRequest(untouchableStream(probe), contentLength), search);
      expect(result).toEqual({ status: 400, cache: "no-store", body: INVALID });
      expect(probe.pulls).toBe(0);
      expect(search).not.toHaveBeenCalled();
    },
  );

  it("invalid JSON", async () => {
    const result = await run(bytesRequest(encoder.encode('{"query": "wifi", ')), fakeSearch());
    expect(result).toEqual({ status: 400, cache: "no-store", body: INVALID });
  });

  it("an empty body", async () => {
    const result = await run(bytesRequest(new Uint8Array(0)), fakeSearch());
    expect(result).toEqual({ status: 400, cache: "no-store", body: INVALID });
  });

  it("a stream that errors", async () => {
    const failing = new ReadableStream<Uint8Array>({
      pull() {
        throw new Error("network reset");
      },
    });
    const result = await run(streamRequest(failing), fakeSearch());
    expect(result).toEqual({ status: 400, cache: "no-store", body: INVALID });
  });
});

describe("decoding matches request.json()", () => {
  it("drops a leading UTF-8 BOM", async () => {
    const bytes = new Uint8Array([0xef, 0xbb, 0xbf, ...paddedBody(40)]);
    const read = await readJsonBody(bytesRequest(bytes));
    expect(read).toEqual({ ok: true, value: { query: "wifi", mode: "lexical" } });
  });
});
