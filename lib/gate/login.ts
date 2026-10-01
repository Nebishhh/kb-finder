import "server-only";

import { z } from "zod";

import {
  checkPassword,
  gateSetCookie,
  resolveGateConfig,
  signGateCookie,
  type GateEnv,
} from "@/lib/gate/cookie";
import { errorSummary } from "@/lib/safe-error";
import { readJsonBody } from "@/lib/search/body";

// POST /api/gate: {"password": string} -> 204 with the gate cookie. The password
// and the body are never logged, echoed or stored; every response body is fixed.

// The only field is a short password, so 1 KiB is plenty.
export const GATE_MAX_BODY_BYTES = 1024;

const GateRequestSchema = z.object({ password: z.string().min(1).max(512) }).strict();

const INVALID_BODY = { error: "Invalid request" };
const TOO_LARGE_BODY = { error: "Request too large" };
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
): Promise<Response> {
  const config = resolveGateConfig(env);
  if (config.mode === "invalid") {
    console.error(`gate config error: ${config.code}`);
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

  try {
    if (!checkPassword(parsed.data.password, config)) {
      return json(UNAUTHORIZED_BODY, 401);
    }
    return new Response(null, {
      status: 204,
      headers: { ...NO_STORE, "Set-Cookie": gateSetCookie(signGateCookie(config, nowMs)) },
    });
  } catch (err) {
    console.error(`gate login failed: ${errorSummary(err)}`);
    return json(UNAVAILABLE_BODY, 500);
  }
}
