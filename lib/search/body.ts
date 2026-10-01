// Read a JSON request body with a byte cap, before anything parses it. Nothing
// here logs or keeps the body: the result is the parsed value or a reason.
//
// 32,768 bytes fits every schema-valid request. zod counts UTF-16 code units
// (max 5000), and JSON needs at most 6 bytes per unit ("\u0001"): 5000 * 6 =
// 30,000 bytes, plus the envelope ({"query":"","mode":"semantic"}) and slack.
// Raw UTF-8 is at most 3 bytes per unit (4 bytes for a 2-unit surrogate pair).
export const MAX_BODY_BYTES = 32_768;

export type BodyResult =
  | { ok: true; value: unknown }
  | { ok: false; reason: "too_large" | "invalid" };

const TOO_LARGE: BodyResult = { ok: false, reason: "too_large" };
const INVALID: BodyResult = { ok: false, reason: "invalid" };

const CONTENT_LENGTH = /^[0-9]+$/;

// The body's bytes, or null once more than maxBytes have arrived; the reader is
// then cancelled so the rest of the stream is never pulled.
async function readCapped(
  body: ReadableStream<Uint8Array> | null,
  maxBytes: number,
): Promise<Uint8Array | null> {
  if (body === null) return new Uint8Array(0);
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (!(value instanceof Uint8Array)) {
      await reader.cancel().catch(() => undefined);
      throw new TypeError("body chunk is not bytes");
    }
    total += value.byteLength;
    if (total > maxBytes) {
      await reader.cancel().catch(() => undefined);
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

export async function readJsonBody(
  request: Request,
  maxBytes: number = MAX_BODY_BYTES,
): Promise<BodyResult> {
  // Content-Length can be absent (chunked), so it only lets an oversized body be
  // refused without reading it; the stream is counted either way.
  const declared = request.headers.get("content-length");
  if (declared !== null) {
    if (!CONTENT_LENGTH.test(declared)) return INVALID;
    if (declared.length > 15 || Number(declared) > maxBytes) return TOO_LARGE;
  }

  let bytes: Uint8Array | null;
  try {
    bytes = await readCapped(request.body, maxBytes);
  } catch {
    return INVALID;
  }
  if (bytes === null) return TOO_LARGE;

  let text: string;
  try {
    // Like request.json(): UTF-8 with a leading BOM dropped, but invalid bytes
    // are an error instead of U+FFFD.
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return INVALID;
  }
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch {
    return INVALID;
  }
}
