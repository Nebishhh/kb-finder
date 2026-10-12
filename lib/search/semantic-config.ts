import "server-only";

import { createHash } from "node:crypto";

import articleV1 from "@/config/embedding/article-v1.json";
import articleV2 from "@/config/embedding/article-v2.json";

// The query side of an embedding config. Everything comes from
// config/embedding/<version>.json, the same file scripts/embed_kb.py hashes;
// nothing here is a second copy. The sha256 is taken over the canonical
// serialization, which a unit test pins byte for byte to each file.
// article-v1 serves semantic; article-v2 (DECISIONS.md, 2026-10-07) serves
// the eval-only semantic-v2 mode.

const CONFIG_FILES = {
  "article-v1": articleV1,
  "article-v2": articleV2,
} as const;

export type TemplateVersion = keyof typeof CONFIG_FILES;

export type SemanticConfig = {
  model: string;
  dimensions: number;
  templateVersion: string;
  // canonicalJson() of the whole file: the exact string sha256 is taken over.
  canonical: string;
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

// expectedVersion, when given, must equal the file's template_version, so a
// file can never serve another version's mode.
export function loadSemanticConfig(
  raw: unknown,
  expectedVersion?: TemplateVersion,
): SemanticConfig {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new SemanticConfigError();
  }
  const cfg = raw as Record<string, unknown>;
  const { model, dimensions, template_version: templateVersion } = cfg;
  if (
    typeof model !== "string" ||
    model.trim() === "" ||
    typeof templateVersion !== "string" ||
    templateVersion.trim() === "" ||
    (expectedVersion !== undefined && templateVersion !== expectedVersion) ||
    typeof dimensions !== "number" ||
    !Number.isInteger(dimensions) ||
    dimensions <= 0 ||
    cfg.query_input !== QUERY_INPUT ||
    cfg.response_encoding !== RESPONSE_ENCODING
  ) {
    throw new SemanticConfigError();
  }
  const canonical = canonicalJson(cfg);
  const sha256 = createHash("sha256").update(canonical, "utf8").digest("hex");
  return { model, dimensions, templateVersion, canonical, sha256 };
}

const loaded = new Map<TemplateVersion, SemanticConfig>();

// Validated on first use of that version, never at import, so a bad config can
// only fail requests for its own mode, never lexical ones or the other version.
export function getSemanticConfig(
  version: TemplateVersion = "article-v1",
): SemanticConfig {
  let config = loaded.get(version);
  if (config === undefined) {
    config = loadSemanticConfig(CONFIG_FILES[version], version);
    loaded.set(version, config);
  }
  return config;
}
