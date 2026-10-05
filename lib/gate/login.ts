import "server-only";

import { z } from "zod";

import {
  checkPassword,
  gateSetCookie,
  resolveGateConfig,
  signGateCookie,
  type GateEnv,
} from "@/lib/gate/cookie";
import { GATE_FAIL_CAP } from "@/lib/limits/config";
import type { GateFailCap } from "@/lib/limits/gate-fail-cap";
import { errorSummary } from "@/lib/safe-error";
import { readJsonBody } from "@/lib/search/body";
import { resolveRateLimit } from "@/lib/search/handler";

// POST /api/gate: {"password": string} -> 204 with the gate cookie. The password
// and the body are never logged, echoed or stored; every response body is fixed.

// The only field is a short password, so 1 KiB is plenty.
export const GATE_MAX_BODY_BYTES = 1024;

const GateRequestSchema = z.object({ password: z.string().min(1).max(512) }).strict();

const INVALID_BODY = { error: "Invalid request" };
const TOO_LARGE_BODY = { error: "Request too large" };
const TOO_MANY_BODY = { error: "Too many attempts" };
const UNAUTHORIZED_BODY = { error: "Unauthorized" };
const UNAVAILABLE_BODY = { error: "Gate unavailable" };

const NO_STORE = { "Cache-Control": "no-store" };

function json(body: unknown, status: number): Response {
  return Response.json(body, { status, headers: NO_STORE });
}

export async function handleGateLogin(
  request: Request,
  env: GateEnv,
  nowMs: number,
  failCap: GateFailCap,
): Promise<Response> {
  const config = resolveGateConfig(env);
  if (config.mode === "invalid") {
    console.error(`gate config error: ${config.code}`);
    return json(UNAVAILABLE_BODY, 500);
  }
  // Same rule as the search cap: RATE_LIMIT=off only off Vercel.
  const rateLimit = resolveRateLimit(env);
  if (rateLimit === "invalid") {
    console.error("gate config error: RATE_LIMIT=off on Vercel");
    return json(UNAVAILABLE_BODY, 500);
  }

  const body = await readJsonBody(request, GATE_MAX_BODY_BYTES);
  if (!body.ok) {
    return body.reason === "too_large" ? json(TOO_LARGE_BODY, 413) : json(INVALID_BODY, 400);
  }
  const parsed = GateRequestSchema.safeParse(body.value);
  if (!parsed.success) return json(INVALID_BODY, 400);

  // Gate off (local dev only): nothing to sign.
  if (config.mode === "off") return new Response(null, { status: 204, headers: NO_STORE });

  // Failed-login cap. Every attempt first reserves a failure, atomically, and
  // only then is the password compared; a right password gives the reservation
  // back. Reserving before comparing is what makes the cap hold: with
  // check-then-count (read the count, compare, count only on failure), N
  // concurrent guesses could all read a count under the cap before any of them
  // was counted, and all be compared. Here each attempt gets its own count from
  // the atomic upsert, so at most GATE_FAIL_CAP attempts a day are ever
  // compared, however many arrive at once. Fails closed: a reserve error is 500
  // and nothing is compared.
  if (rateLimit === "on") {
    let count: number;
    try {
      count = await failCap.reserve(nowMs);
    } catch (err) {
      console.error(`gate limit failed: ${errorSummary(err)}`);
      return json(UNAVAILABLE_BODY, 500);
    }
    if (count > GATE_FAIL_CAP) return json(TOO_MANY_BODY, 429);
  }

  let cookie: string;
  try {
    // Wrong: 401, and the reservation stays counted.
    if (!checkPassword(parsed.data.password, config)) {
      return json(UNAUTHORIZED_BODY, 401);
    }
    cookie = signGateCookie(config, nowMs);
  } catch (err) {
    console.error(`gate login failed: ${errorSummary(err)}`);
    return json(UNAVAILABLE_BODY, 500);
  }

  // Right: give the reservation back. A release error is logged and the login
  // still succeeds; the attempt then stays counted, which errs toward the cap.
  if (rateLimit === "on") {
    try {
      await failCap.release(nowMs);
    } catch (err) {
      console.error(`gate release failed: ${errorSummary(err)}`);
    }
  }
  return new Response(null, {
    status: 204,
    headers: { ...NO_STORE, "Set-Cookie": gateSetCookie(cookie) },
  });
}
