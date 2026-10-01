import "server-only";

import type { SemanticConfig } from "@/lib/search/semantic-config";

// One query embedding per call. Query text goes to OpenAI and nowhere else: it
// is never logged, cached, stored or put in a URL or an error. Every failure is
// an EmbeddingError whose message and code are a fixed string; provider status
// text and response bodies are never read into an error.

export const EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings";
export const EMBED_TIMEOUT_MS = 10_000;
const NORM_TOLERANCE = 1e-3;

export const EMBEDDING_ERROR_CODES = [
  "OPENAI_KEY_MISSING",
  "OPENAI_HTTP_429",
  "OPENAI_HTTP_4XX",
  "OPENAI_HTTP_5XX",
  "OPENAI_HTTP_OTHER",
  "OPENAI_TIMEOUT",
  "OPENAI_NETWORK",
  "OPENAI_BAD_RESPONSE",
  "OPENAI_UNEXPECTED",
] as const;
export type EmbeddingErrorCode = (typeof EMBEDDING_ERROR_CODES)[number];

export class EmbeddingError extends Error {
  constructor(readonly code: EmbeddingErrorCode) {
    super(code);
    this.name = "EmbeddingError";
  }
}

export type EmbedEnv = { OPENAI_API_KEY?: string };
export type FetchFn = (input: string, init: RequestInit) => Promise<Response>;

// base64 of `dimensions` little-endian float32 values, as the official SDKs
// request by default. Read explicitly little-endian, never via a platform-endian
// Float32Array view. Requires the exact byte length, all finite, unit norm.
export function decodeEmbedding(b64: unknown, dimensions: number): number[] {
  if (typeof b64 !== "string" || !/^[A-Za-z0-9+/]*={0,2}$/.test(b64)) {
    throw new EmbeddingError("OPENAI_BAD_RESPONSE");
  }
  const bytes = Buffer.from(b64, "base64");
  if (bytes.length !== dimensions * 4 || bytes.toString("base64") !== b64) {
    throw new EmbeddingError("OPENAI_BAD_RESPONSE");
  }
  const values: number[] = new Array(dimensions);
  let sumSquares = 0;
  for (let i = 0; i < dimensions; i++) {
    const x = bytes.readFloatLE(i * 4);
    if (!Number.isFinite(x)) throw new EmbeddingError("OPENAI_BAD_RESPONSE");
    values[i] = x;
    sumSquares += x * x;
  }
  if (Math.abs(Math.sqrt(sumSquares) - 1) > NORM_TOLERANCE) {
    throw new EmbeddingError("OPENAI_BAD_RESPONSE");
  }
  return values;
}

function statusError(status: number): EmbeddingError {
  if (status === 429) return new EmbeddingError("OPENAI_HTTP_429");
  if (status >= 400 && status < 500) return new EmbeddingError("OPENAI_HTTP_4XX");
  if (status >= 500) return new EmbeddingError("OPENAI_HTTP_5XX");
  return new EmbeddingError("OPENAI_HTTP_OTHER");
}

// The key is read here, at call time, from the server environment only.
export async function embedQuery(
  text: string,
  config: SemanticConfig,
  deps: { env?: EmbedEnv; fetchFn?: FetchFn } = {},
): Promise<number[]> {
  const env = deps.env ?? process.env;
  const key = env.OPENAI_API_KEY;
  if (!key) throw new EmbeddingError("OPENAI_KEY_MISSING");
  const fetchFn: FetchFn = deps.fetchFn ?? ((input, init) => fetch(input, init));

  let response: Response;
  try {
    response = await fetchFn(EMBEDDINGS_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${key}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: config.model,
        input: text,
        dimensions: config.dimensions,
        encoding_format: "base64",
      }),
      signal: AbortSignal.timeout(EMBED_TIMEOUT_MS),
      cache: "no-store",
      redirect: "error",
    });
  } catch (err) {
    const name = err instanceof Error ? err.name : "";
    throw new EmbeddingError(
      name === "TimeoutError" || name === "AbortError" ? "OPENAI_TIMEOUT" : "OPENAI_NETWORK",
    );
  }

  if (response.status !== 200) {
    await response.body?.cancel().catch(() => undefined);
    throw statusError(response.status);
  }

  let parsed: unknown;
  try {
    parsed = await response.json();
  } catch {
    throw new EmbeddingError("OPENAI_BAD_RESPONSE");
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw new EmbeddingError("OPENAI_BAD_RESPONSE");
  }
  const body = parsed as { model?: unknown; data?: unknown };
  if (body.model !== config.model || !Array.isArray(body.data) || body.data.length !== 1) {
    throw new EmbeddingError("OPENAI_BAD_RESPONSE");
  }
  const item = body.data[0] as { index?: unknown; embedding?: unknown } | null;
  if (typeof item !== "object" || item === null || item.index !== 0) {
    throw new EmbeddingError("OPENAI_BAD_RESPONSE");
  }
  return decodeEmbedding(item.embedding, config.dimensions);
}
