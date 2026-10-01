import { NextResponse, type NextRequest } from "next/server";

import { checkGate, gateEnvFrom } from "@/lib/gate/cookie";

// Optimistic gate for page requests only: no valid kbf_gate cookie, or a gate
// that is not configured, redirects to /gate (which shows the config error).
// /api/search re-checks the cookie itself and is the real boundary.
export function proxy(request: NextRequest): NextResponse {
  const result = checkGate(
    request.headers.get("cookie"),
    gateEnvFrom(process.env),
    Date.now(),
  );
  if (result === "open") return NextResponse.next();
  return NextResponse.redirect(new URL("/gate", request.url));
}

// Must stay a constant (Next reads it at build time). /api/search is excluded so
// proxy never buffers a search body; /api/gate and /gate so login can happen;
// _next/static, _next/image and favicon.ico so assets load. Each exclusion is a
// whole path segment, so e.g. /gateway is still gated.
export const config = {
  matcher: [
    "/((?!(?:api/search|api/gate|gate|_next/static|_next/image|favicon\\.ico)(?:/|$)).*)",
  ],
};
