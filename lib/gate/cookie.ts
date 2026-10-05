import "server-only";

import { createHash, createHmac, timingSafeEqual } from "node:crypto";

import type { GateResult } from "@/lib/search/handler";

// Access gate for the hosted app: one shared password or 4-digit PIN (the owner
// plus 2-3 trusted testers), exchanged at /api/gate for a signed cookie.
// Brute force against a PIN is bounded by the global failed-login cap
// (lib/gate/login.ts), not by the password's length. This is the only
// module that reads ACCESS_PASSWORD or GATE_COOKIE_SECRET; nothing here logs.
//
// Cookie: kbf_gate=v1.<exp>.<mac>
//   exp  Unix seconds, issued + 7 days
//   mac  base64url(HMAC-SHA256(GATE_COOKIE_SECRET,
//                              "kbf_gate|v1|" + exp + "|" + sha256hex(ACCESS_PASSWORD)))
// The password's hash is in the MAC input, so rotating either the secret or the
// password revokes every cookie already issued.

export const GATE_COOKIE = "kbf_gate";
export const GATE_LIFETIME_S = 7 * 24 * 60 * 60;
export const MIN_PASSWORD_LENGTH = 20;
// A PIN: exactly four ASCII digits, nothing around them.
const PIN_SHAPE = /^[0-9]{4}$/;
export const MIN_SECRET_BYTES = 32;

export type GateEnv = {
  ACCESS_GATE?: string;
  ACCESS_PASSWORD?: string;
  GATE_COOKIE_SECRET?: string;
  // Exactly "off" skips the failed-login cap, only when VERCEL is unset.
  RATE_LIMIT?: string;
  VERCEL?: string;
};

export type GateConfigError =
  | "GATE_OFF_ON_VERCEL"
  | "GATE_PASSWORD_INVALID"
  | "GATE_SECRET_INVALID";

export type GateConfig =
  | { mode: "off" }
  | { mode: "on"; password: string; secret: string }
  | { mode: "invalid"; code: GateConfigError };

// The five names, picked out of a process.env-like object at request time.
export function gateEnvFrom(env: Record<string, string | undefined>): GateEnv {
  return {
    ACCESS_GATE: env.ACCESS_GATE,
    ACCESS_PASSWORD: env.ACCESS_PASSWORD,
    GATE_COOKIE_SECRET: env.GATE_COOKIE_SECRET,
    RATE_LIMIT: env.RATE_LIMIT,
    VERCEL: env.VERCEL,
  };
}

// ACCESS_GATE exactly "off" disables the gate, only when VERCEL is unset (local
// dev and evals). Anything else needs a valid password and a long enough
// secret; anything else fails closed. A valid password is exactly 4 ASCII digits
// (a PIN) or at least 20 characters (a passphrase), so either can be used without
// a code change.
export function resolveGateConfig(env: GateEnv): GateConfig {
  if (env.ACCESS_GATE === "off") {
    return env.VERCEL ? { mode: "invalid", code: "GATE_OFF_ON_VERCEL" } : { mode: "off" };
  }
  const password = env.ACCESS_PASSWORD;
  const secret = env.GATE_COOKIE_SECRET;
  if (!isValidPassword(password)) {
    return { mode: "invalid", code: "GATE_PASSWORD_INVALID" };
  }
  if (!secret || Buffer.byteLength(secret, "utf8") < MIN_SECRET_BYTES) {
    return { mode: "invalid", code: "GATE_SECRET_INVALID" };
  }
  return { mode: "on", password, secret };
}

export function isValidPassword(password: string | undefined): password is string {
  if (!password) return false;
  return PIN_SHAPE.test(password) || password.length >= MIN_PASSWORD_LENGTH;
}

type OnConfig = Extract<GateConfig, { mode: "on" }>;

function sha256(text: string): Buffer {
  return createHash("sha256").update(text, "utf8").digest();
}

function mac(exp: string, config: OnConfig): string {
  const passwordHash = sha256(config.password).toString("hex");
  return createHmac("sha256", config.secret)
    .update(`${GATE_COOKIE}|v1|${exp}|${passwordHash}`, "utf8")
    .digest("base64url");
}

export function signGateCookie(config: OnConfig, nowMs: number): string {
  const exp = String(Math.floor(nowMs / 1000) + GATE_LIFETIME_S);
  return `v1.${exp}.${mac(exp, config)}`;
}

// v1, a positive integer exp, and an unpadded base64url SHA-256 MAC (43 chars).
const COOKIE_SHAPE = /^v1\.([1-9][0-9]{0,11})\.([A-Za-z0-9_-]{43})$/;

export function verifyGateCookie(
  value: string | undefined,
  config: OnConfig,
  nowMs: number,
): boolean {
  if (value === undefined) return false;
  const match = COOKIE_SHAPE.exec(value);
  if (match === null) return false;
  const [, exp, given] = match;
  const now = Math.floor(nowMs / 1000);
  const expSeconds = Number(exp);
  if (expSeconds <= now || expSeconds > now + GATE_LIFETIME_S) return false;
  const expected = Buffer.from(mac(exp, config), "utf8");
  const actual = Buffer.from(given, "utf8");
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

// Both sides hashed first, so the comparison is constant-time and equal-length
// whatever the input's length.
export function checkPassword(input: string, config: OnConfig): boolean {
  return timingSafeEqual(sha256(input), sha256(config.password));
}

export function gateSetCookie(value: string): string {
  return (
    `${GATE_COOKIE}=${value}; Max-Age=${GATE_LIFETIME_S}; Path=/; ` +
    "HttpOnly; Secure; SameSite=Lax"
  );
}

// Every kbf_gate value in a Cookie header (there can be more than one).
export function gateCookieValues(cookieHeader: string | null): string[] {
  if (!cookieHeader) return [];
  const values: string[] = [];
  for (const part of cookieHeader.split(";")) {
    const eq = part.indexOf("=");
    if (eq === -1) continue;
    if (part.slice(0, eq).trim() === GATE_COOKIE) values.push(part.slice(eq + 1).trim());
  }
  return values;
}

// The single check used by proxy.ts (optimistic, page requests) and by
// /api/search (authoritative).
export function checkGate(
  cookieHeader: string | null,
  env: GateEnv,
  nowMs: number,
): GateResult {
  const config = resolveGateConfig(env);
  if (config.mode === "off") return "open";
  if (config.mode === "invalid") return "misconfigured";
  return gateCookieValues(cookieHeader).some((v) => verifyGateCookie(v, config, nowMs))
    ? "open"
    : "denied";
}
