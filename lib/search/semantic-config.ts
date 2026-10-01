import "server-only";

import { createHash } from "node:crypto";

import articleV1 from "@/config/embedding/article-v1.json";

// The query side of the active embedding config. Everything comes from
// config/embedding/article-v1.json, the same file scripts/embed_kb.py hashes;
// nothing here is a second copy. The sha256 is taken over the canonical
// serialization, which a unit test pins byte for byte to the file.

export type SemanticConfig = {
  model: string;
  dimensions: number;
  sha256: string;
};

export class SemanticConfigError extends Error {
  readonly code = "SEMANTIC_CONFIG_INVALID";
  constructor() {
    super("semantic config invalid");
    this.name = "SemanticConfigError";
  }
}

// What the query side implements. The file must name exactly these.
const QUERY_INPUT = "raw-query-string-v1";
const RESPONSE_ENCODING = "base64-float32le";

// Same bytes as Python's json.dumps(sort_keys=True, separators=(",", ":"),
// ensure_ascii=True) for JSON values: sorted keys, no spaces, non-ASCII as \uXXXX.
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new SemanticConfigError();
    return JSON.stringify(value);
  }
  if (typeof value === "string") {
    return JSON.stringify(value).replace(
      /[\u007f-\uffff]/g,
      (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"),
    );
  }
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const keys = Object.keys(record).sort();
    return (
      "{" +
      keys.map((k) => canonicalJson(k) + ":" + canonicalJson(record[k])).join(",") +
      "}"
    );
  }
  throw new SemanticConfigError();
}

export function loadSemanticConfig(raw: unknown): SemanticConfig {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new SemanticConfigError();
  }
  const cfg = raw as Record<string, unknown>;
  const { model, dimensions } = cfg;
  if (
    typeof model !== "string" ||
    model.trim() === "" ||
    typeof dimensions !== "number" ||
    !Number.isInteger(dimensions) ||
    dimensions <= 0 ||
    cfg.query_input !== QUERY_INPUT ||
    cfg.response_encoding !== RESPONSE_ENCODING
  ) {
    throw new SemanticConfigError();
  }
  const sha256 = createHash("sha256").update(canonicalJson(cfg), "utf8").digest("hex");
  return { model, dimensions, sha256 };
}

let active: SemanticConfig | undefined;

// Validated on first semantic use, never at import, so a bad config can only
// fail semantic requests, never lexical ones.
export function getSemanticConfig(): SemanticConfig {
  active ??= loadSemanticConfig(articleV1);
  return active;
}
