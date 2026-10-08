import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { format } from "node:util";

import { NextRequest } from "next/server";
// The bundled 16.3.8 docs call this unstable_doesProxyMatch; the installed package
// still exports it under its earlier name.
import { unstable_doesMiddlewareMatch } from "next/experimental/testing/server";
import { describe, expect, it, vi } from "vitest";

import { POST as gateRoute } from "@/app/api/gate/route";
import {
  checkGate,
  checkPassword,
  GATE_COOKIE,
  GATE_LIFETIME_S,
  gateCookieValues,
  gateSetCookie,
  resolveGateConfig,
  signGateCookie,
  verifyGateCookie,
  type GateConfig,
  type GateEnv,
} from "@/lib/gate/cookie";
import { GATE_MAX_BODY_BYTES, handleGateLogin } from "@/lib/gate/login";
import type { GateFailCap } from "@/lib/limits/gate-fail-cap";
import {
  handleSearch,
  type GateFn,
  type LimitFn,
  type SearchFn,
} from "@/lib/search/handler";
import nextConfig from "@/next.config";
import { config as proxyConfig, proxy } from "@/proxy";

// Offline. Every secret below is a test value. tests/web/setup.ts clears the gate
// variables before each test.

const ROOT = process.cwd();
const PASSWORD = "test-password-0123456789"; // 24 characters
const SECRET = "test-cookie-secret-0123456789abcdef"; // 35 bytes
const ON_ENV: GateEnv = { ACCESS_PASSWORD: PASSWORD, GATE_COOKIE_SECRET: SECRET };
const T = Date.UTC(2026, 9, 1, 12, 0, 0); // ms
const T_S = Math.floor(T / 1000);

type OnConfig = Extract<GateConfig, { mode: "on" }>;

// The failed-login cap never trips here; gate.cap.unit.test.ts covers it.
const OPEN_CAP: GateFailCap = { reserve: async () => 1, release: async () => {} };

function onConfig(env: GateEnv = ON_ENV): OnConfig {
  const config = resolveGateConfig(env);
  if (config.mode !== "on") throw new Error(`expected mode on, got ${config.mode}`);
  return config;
}

// highWaterMark 0: the stream is pulled only when something reads it.
function probeStream(chunks: Uint8Array[], probe: { pulls: number }) {
  let i = 0;
  return new ReadableStream<Uint8Array>(
    {
      pull(controller) {
        probe.pulls++;
        if (i < chunks.length) controller.enqueue(chunks[i++]);
        else controller.close();
      },
    },
    { highWaterMark: 0 },
  );
}

function untouchable(probe: { pulls: number }) {
  return new ReadableStream<Uint8Array>(
    {
      pull() {
        probe.pulls++;
        throw new Error("body was read");
      },
    },
    { highWaterMark: 0 },
  );
}

function post(path: string, body: BodyInit | ReadableStream<Uint8Array>, headers: HeadersInit = {}) {
  return new Request(`http://localhost${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body,
    duplex: "half",
  } as RequestInit);
}

describe("gate config", () => {
  it("ACCESS_GATE exactly off disables the gate, only when VERCEL is unset", () => {
    expect(resolveGateConfig({ ACCESS_GATE: "off" })).toEqual({ mode: "off" });
    expect(resolveGateConfig({ ACCESS_GATE: "off", VERCEL: "" })).toEqual({ mode: "off" });
    expect(resolveGateConfig({ ACCESS_GATE: "off", VERCEL: "1" })).toEqual({
      mode: "invalid",
      code: "GATE_OFF_ON_VERCEL",
    });
    // Anything but exactly "off" means the gate is on and needs its secrets.
    expect(resolveGateConfig({ ACCESS_GATE: "OFF" })).toEqual({
      mode: "invalid",
      code: "GATE_PASSWORD_INVALID",
    });
    expect(resolveGateConfig({ ...ON_ENV, ACCESS_GATE: "on", VERCEL: "1" }).mode).toBe("on");
  });

  it("a missing or short password fails closed", () => {
    for (const password of [undefined, "", "a".repeat(19)]) {
      expect(resolveGateConfig({ ...ON_ENV, ACCESS_PASSWORD: password })).toEqual({
        mode: "invalid",
        code: "GATE_PASSWORD_INVALID",
      });
    }
    expect(resolveGateConfig({ ...ON_ENV, ACCESS_PASSWORD: "a".repeat(20) }).mode).toBe("on");
  });

  it("a missing or short secret fails closed (bytes, not characters)", () => {
    for (const secret of [undefined, "", "s".repeat(31), "€".repeat(10)]) {
      expect(resolveGateConfig({ ...ON_ENV, GATE_COOKIE_SECRET: secret })).toEqual({
        mode: "invalid",
        code: "GATE_SECRET_INVALID",
      });
    }
    expect(resolveGateConfig({ ...ON_ENV, GATE_COOKIE_SECRET: "s".repeat(32) }).mode).toBe("on");
    // 11 euro signs are 33 bytes.
    expect(resolveGateConfig({ ...ON_ENV, GATE_COOKIE_SECRET: "€".repeat(11) }).mode).toBe(
      "on",
    );
  });

  it("checkGate: off is open, invalid is misconfigured, on needs a cookie", () => {
    expect(checkGate(null, { ACCESS_GATE: "off" }, T)).toBe("open");
    expect(checkGate(null, { ACCESS_GATE: "off", VERCEL: "1" }, T)).toBe("misconfigured");
    expect(checkGate(null, {}, T)).toBe("misconfigured");
    expect(checkGate(null, ON_ENV, T)).toBe("denied");
  });
});

describe("gate cookie", () => {
  const config = onConfig();
  const valid = signGateCookie(config, T);

  it("has the v1.<exp>.<mac> shape with a 7-day exp", () => {
    expect(GATE_LIFETIME_S).toBe(604_800);
    const [v, exp, mac] = valid.split(".");
    expect(v).toBe("v1");
    expect(Number(exp)).toBe(T_S + GATE_LIFETIME_S);
    expect(mac).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });

  it("is valid until it expires", () => {
    expect(verifyGateCookie(valid, config, T)).toBe(true);
    expect(verifyGateCookie(valid, config, T + (GATE_LIFETIME_S - 1) * 1000)).toBe(true);
  });

  it("expired", () => {
    expect(verifyGateCookie(valid, config, T + GATE_LIFETIME_S * 1000)).toBe(false);
    expect(verifyGateCookie(valid, config, T + 30 * GATE_LIFETIME_S * 1000)).toBe(false);
  });

  it("more than one lifetime in the future, even with a correct MAC", () => {
    const future = signGateCookie(config, T + 2000);
    expect(verifyGateCookie(future, config, T)).toBe(false);
  });

  it("tampered exp", () => {
    const [v, exp, mac] = valid.split(".");
    expect(verifyGateCookie(`${v}.${Number(exp) - 1}.${mac}`, config, T)).toBe(false);
  });

  it("tampered mac", () => {
    const flipped = valid.slice(0, -1) + (valid.endsWith("A") ? "B" : "A");
    expect(verifyGateCookie(flipped, config, T)).toBe(false);
  });

  it("wrong secret", () => {
    const other = onConfig({ ...ON_ENV, GATE_COOKIE_SECRET: SECRET + "x" });
    expect(verifyGateCookie(valid, other, T)).toBe(false);
  });

  it("rotating the password alone revokes it", () => {
    const rotated = onConfig({ ...ON_ENV, ACCESS_PASSWORD: PASSWORD + "x" });
    expect(verifyGateCookie(valid, rotated, T)).toBe(false);
  });

  it.each([
    "",
    "v1",
    "v1.",
    "v1..",
    valid.replace(/^v1/, "v2"),
    valid.replace(/^v1/, "V1"),
    `${valid}.x`,
    `${valid}=`,
    ` ${valid}`,
    valid.replace(/\.(\d+)\./, ".0$1."), // leading zero
    valid.replace(/\.(\d+)\./, ".-$1."),
    valid.replace(/\.(\d+)\./, ".$1.5."),
    `v1.${T_S + 100}.${"A".repeat(42)}`,
    `v1.${T_S + 100}.${"A".repeat(44)}`,
    `v1.${T_S + 100}.${"+".repeat(43)}`,
  ])("malformed %j", (value) => {
    expect(verifyGateCookie(value, config, T)).toBe(false);
  });

  it("missing", () => {
    expect(verifyGateCookie(undefined, config, T)).toBe(false);
    expect(checkGate(null, ON_ENV, T)).toBe("denied");
    expect(checkGate("", ON_ENV, T)).toBe("denied");
    expect(checkGate("other=1", ON_ENV, T)).toBe("denied");
  });

  it("is found among other cookies", () => {
    expect(gateCookieValues(`a=1; ${GATE_COOKIE}=${valid}; b=2`)).toEqual([valid]);
    expect(checkGate(`a=1; ${GATE_COOKIE}=${valid}; b=2`, ON_ENV, T)).toBe("open");
    expect(checkGate(`${GATE_COOKIE}=junk; ${GATE_COOKIE}=${valid}`, ON_ENV, T)).toBe("open");
    expect(checkGate(`x${GATE_COOKIE}=${valid}`, ON_ENV, T)).toBe("denied");
  });

  it("Set-Cookie carries exactly the planned flags", () => {
    expect(gateSetCookie(valid)).toBe(
      `kbf_gate=${valid}; Max-Age=604800; Path=/; HttpOnly; Secure; SameSite=Lax`,
    );
  });

  it("password check", () => {
    expect(checkPassword(PASSWORD, config)).toBe(true);
    expect(checkPassword(PASSWORD + " ", config)).toBe(false);
    expect(checkPassword(PASSWORD.slice(0, -1), config)).toBe(false);
    expect(checkPassword("", config)).toBe(false);
    expect(checkPassword(PASSWORD.repeat(10), config)).toBe(false);
  });
});

describe("handleSearch with the gate", () => {
  const BODY = new TextEncoder().encode(JSON.stringify({ query: "wifi", mode: "lexical" }));

  function setup(env: GateEnv) {
    const limit = vi.fn<LimitFn>(async () => "ok");
    const lexical = vi.fn<SearchFn>(async () => []);
    const semantic = vi.fn<SearchFn>(async () => []);
    const lexicalV2 = vi.fn<SearchFn>(async () => []);
    const gate: GateFn = (request) => checkGate(request.headers.get("cookie"), env, T);
    const run = async (request: Request) => {
      const response = await handleSearch(request, {
        retrievers: { lexical, semantic, "lexical-v2": lexicalV2 },
        gate,
        limit,
        env: {},
      });
      return {
        status: response.status,
        cache: response.headers.get("Cache-Control"),
        body: await response.json(),
      };
    };
    return { limit, lexical, run };
  }

  it("no cookie: 401, body untouched, cap and retriever never called", async () => {
    const h = setup(ON_ENV);
    const probe = { pulls: 0 };
    const result = await h.run(post("/api/search", untouchable(probe)));
    expect(result).toEqual({ status: 401, cache: "no-store", body: { error: "Unauthorized" } });
    expect(probe.pulls).toBe(0);
    expect(h.limit).not.toHaveBeenCalled();
    expect(h.lexical).not.toHaveBeenCalled();
  });

  it("an invalid cookie is also 401 before the body", async () => {
    const h = setup(ON_ENV);
    const probe = { pulls: 0 };
    const result = await h.run(
      post("/api/search", untouchable(probe), { Cookie: `${GATE_COOKIE}=v1.1.x` }),
    );
    expect(result.status).toBe(401);
    expect(probe.pulls).toBe(0);
  });

  it("a valid cookie reaches the body read, then the cap and the retriever", async () => {
    const h = setup(ON_ENV);
    const probe = { pulls: 0 };
    const cookie = `${GATE_COOKIE}=${signGateCookie(onConfig(), T)}`;
    const result = await h.run(post("/api/search", probeStream([BODY], probe), { Cookie: cookie }));
    expect(result.status).toBe(200);
    expect(probe.pulls).toBeGreaterThan(0);
    expect(h.limit).toHaveBeenCalledTimes(1);
    expect(h.lexical).toHaveBeenCalledTimes(1);
  });

  it("a misconfigured gate is 500 for every request, body untouched", async () => {
    const h = setup({ ACCESS_GATE: "off", VERCEL: "1" });
    const probe = { pulls: 0 };
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const result = await h.run(post("/api/search", untouchable(probe)));
    expect(result).toEqual({ status: 500, cache: "no-store", body: { error: "Search failed" } });
    expect(probe.pulls).toBe(0);
    expect(errors.mock.calls).toEqual([["search config error: access gate"]]);
    errors.mockRestore();
  });

  it("gate off (local only): searches run without a cookie", async () => {
    const h = setup({ ACCESS_GATE: "off" });
    const result = await h.run(post("/api/search", BODY));
    expect(result.status).toBe(200);
  });
});

describe("POST /api/gate", () => {
  async function login(body: BodyInit, env: GateEnv = ON_ENV, headers: HeadersInit = {}) {
    const response = await handleGateLogin(post("/api/gate", body, headers), env, T, OPEN_CAP);
    return {
      status: response.status,
      cache: response.headers.get("Cache-Control"),
      setCookie: response.headers.get("Set-Cookie"),
      text: await response.text(),
    };
  }

  it("right password: 204 with the cookie and exact flags", async () => {
    const result = await login(JSON.stringify({ password: PASSWORD }));
    expect(result.status).toBe(204);
    expect(result.text).toBe("");
    expect(result.cache).toBe("no-store");
    const cookie = signGateCookie(onConfig(), T);
    expect(result.setCookie).toBe(
      `kbf_gate=${cookie}; Max-Age=604800; Path=/; HttpOnly; Secure; SameSite=Lax`,
    );
    expect(checkGate(`kbf_gate=${cookie}`, ON_ENV, T)).toBe("open");
  });

  it("wrong password: 401 with a fixed body and no cookie", async () => {
    const result = await login(JSON.stringify({ password: PASSWORD + "x" }));
    expect(result).toEqual({
      status: 401,
      cache: "no-store",
      setCookie: null,
      text: JSON.stringify({ error: "Unauthorized" }),
    });
  });

  it("oversized: 413", async () => {
    expect(GATE_MAX_BODY_BYTES).toBe(1024);
    const result = await login(JSON.stringify({ password: "p".repeat(2000) }));
    expect(result.status).toBe(413);
    expect(result.text).toBe(JSON.stringify({ error: "Request too large" }));
    expect(result.setCookie).toBeNull();
  });

  it.each([
    ["invalid JSON", '{"password": "x'],
    ["form encoding", `password=${PASSWORD}`],
    ["missing password", "{}"],
    ["empty password", JSON.stringify({ password: "" })],
    ["password not a string", JSON.stringify({ password: 42 })],
    ["extra field", JSON.stringify({ password: PASSWORD, remember: true })],
  ])("%s: 400", async (_name, body) => {
    const result = await login(body);
    expect(result.status).toBe(400);
    expect(result.text).toBe(JSON.stringify({ error: "Invalid request" }));
    expect(result.setCookie).toBeNull();
  });

  it.each([
    ["password missing", { GATE_COOKIE_SECRET: SECRET }],
    ["secret missing", { ACCESS_PASSWORD: PASSWORD }],
    ["password short", { ...ON_ENV, ACCESS_PASSWORD: "a".repeat(19) }],
    ["secret short", { ...ON_ENV, GATE_COOKIE_SECRET: "s".repeat(31) }],
    ["off on Vercel", { ACCESS_GATE: "off", VERCEL: "1" }],
  ])("config %s: 500 before the body is read", async (_name, env) => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const probe = { pulls: 0 };
    const response = await handleGateLogin(
      post("/api/gate", untouchable(probe)),
      env,
      T,
      OPEN_CAP,
    );
    expect(response.status).toBe(500);
    expect(await response.text()).toBe(JSON.stringify({ error: "Gate unavailable" }));
    expect(probe.pulls).toBe(0);
    errors.mockRestore();
  });

  it("gate off: 204 and no cookie", async () => {
    const result = await login(JSON.stringify({ password: "anything" }), { ACCESS_GATE: "off" });
    expect(result.status).toBe(204);
    expect(result.setCookie).toBeNull();
  });

  it("the route reads the environment at request time", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const missing = await gateRoute(post("/api/gate", JSON.stringify({ password: PASSWORD })));
    expect(missing.status).toBe(500);
    errors.mockRestore();

    process.env.ACCESS_PASSWORD = PASSWORD; // cleared again by setup.ts
    process.env.GATE_COOKIE_SECRET = SECRET;
    process.env.RATE_LIMIT = "off"; // no database here; VERCEL is unset
    const ok = await gateRoute(post("/api/gate", JSON.stringify({ password: PASSWORD })));
    expect(ok.status).toBe(204);
    expect(ok.headers.get("Set-Cookie")).toMatch(/^kbf_gate=v1\.\d+\.[A-Za-z0-9_-]{43}; /);
  });
});

describe("proxy", () => {
  it("has a constant matcher", () => {
    expect(proxyConfig).toEqual({
      matcher: [
        "/((?!(?:api/search|api/gate|gate|_next/static|_next/image|favicon\\.ico|icon\\.svg)(?:/|$)).*)",
      ],
    });
  });

  // /icon.svg/x is excluded like /gate/x and /favicon.ico/x: each exclusion
  // covers its whole segment, and no route serves such a path (404).
  it.each([
    "/api/search",
    "/api/gate",
    "/gate",
    "/_next/static/chunks/app.js",
    "/_next/image?url=%2Fx.png&w=64&q=75",
    "/favicon.ico",
    "/icon.svg",
    "/icon.svg?3f2a9c1e7b4d5a60",
    "/icon.svg/x",
  ])("does not run on %s", (url) => {
    expect(unstable_doesMiddlewareMatch({ config: proxyConfig, nextConfig, url })).toBe(false);
  });

  it.each([
    "/",
    "/about",
    "/gateway",
    "/gate-x",
    "/api/searchx",
    "/api/other",
    "/favicon.icon",
    "/icon.svgx",
    "/icons.svg",
    "/x/icon.svg",
  ])(
    "runs on %s",
    (url) => {
      expect(unstable_doesMiddlewareMatch({ config: proxyConfig, nextConfig, url })).toBe(true);
    },
  );

  function setEnv(env: GateEnv) {
    for (const [k, v] of Object.entries(env)) if (v !== undefined) process.env[k] = v;
  }

  it("a page request without a cookie redirects to /gate", () => {
    setEnv(ON_ENV);
    const response = proxy(new NextRequest("http://localhost/"));
    expect(response.status).toBe(307);
    expect(response.headers.get("Location")).toBe("http://localhost/gate");
  });

  it("a page request with a valid cookie goes through", () => {
    setEnv(ON_ENV);
    const cookie = signGateCookie(onConfig(), Date.now());
    const response = proxy(
      new NextRequest("http://localhost/", { headers: { Cookie: `kbf_gate=${cookie}` } }),
    );
    expect(response.headers.get("x-middleware-next")).toBe("1");
    expect(response.headers.get("Location")).toBeNull();
  });

  it("a misconfigured gate redirects to /gate, which shows the error", () => {
    setEnv({ ACCESS_GATE: "off", VERCEL: "1" });
    const response = proxy(new NextRequest("http://localhost/"));
    expect(response.headers.get("Location")).toBe("http://localhost/gate");
  });

  it("gate off: pages go through", () => {
    setEnv({ ACCESS_GATE: "off" });
    const response = proxy(new NextRequest("http://localhost/"));
    expect(response.headers.get("x-middleware-next")).toBe("1");
  });
});

describe("the password and query never leak", () => {
  const SENTINEL = "kbf-sentinel-7f3a9c";
  const SECRET_PASSWORD = `${SENTINEL}-correct-password`;
  const SENTINEL_ENV: GateEnv = { ACCESS_PASSWORD: SECRET_PASSWORD, GATE_COOKIE_SECRET: SECRET };

  function captureOutput(): string[] {
    const captured: string[] = [];
    for (const method of ["log", "info", "warn", "error", "debug", "trace"] as const) {
      vi.spyOn(console, method).mockImplementation((...args: unknown[]) => {
        captured.push(format(...args));
      });
    }
    const write = ((chunk: string | Uint8Array) => {
      captured.push(typeof chunk === "string" ? chunk : Buffer.from(chunk).toString());
      return true;
    }) as typeof process.stdout.write;
    vi.spyOn(process.stdout, "write").mockImplementation(write);
    vi.spyOn(process.stderr, "write").mockImplementation(write);
    return captured;
  }

  async function expectNoLeak(response: Response, captured: string[]) {
    const text = await response.text();
    const headers = [...response.headers.entries()].map(([k, v]) => `${k}: ${v}`).join("\n");
    vi.restoreAllMocks();
    expect(text).not.toContain(SENTINEL);
    expect(headers).not.toContain(SENTINEL);
    for (const line of captured) expect(line).not.toContain(SENTINEL);
  }

  it.each([
    ["right password", JSON.stringify({ password: SECRET_PASSWORD }), SENTINEL_ENV],
    ["wrong password", JSON.stringify({ password: `${SENTINEL}-wrong-password` }), SENTINEL_ENV],
    ["oversized", JSON.stringify({ password: SENTINEL.repeat(100) }), SENTINEL_ENV],
    ["invalid JSON", `{"password": "${SENTINEL}`, SENTINEL_ENV],
    ["extra field", JSON.stringify({ password: SECRET_PASSWORD, note: SENTINEL }), SENTINEL_ENV],
    // The sentinel alone is 19 characters: too short, so the gate is misconfigured.
    ["misconfigured", JSON.stringify({ password: SENTINEL }), { ...SENTINEL_ENV, ACCESS_PASSWORD: SENTINEL }],
  ])("/api/gate: %s", async (_name, body, env) => {
    const captured = captureOutput();
    const response = await handleGateLogin(post("/api/gate", body), env, T, OPEN_CAP);
    await expectNoLeak(response, captured);
  });

  it("/api/search without a cookie (401)", async () => {
    const captured = captureOutput();
    const response = await handleSearch(
      post("/api/search", JSON.stringify({ query: `my laptop ${SENTINEL}`, mode: "lexical" })),
      {
        retrievers: {
          lexical: async () => [],
          semantic: async () => [],
          "lexical-v2": async () => [],
        },
        gate: (request) => checkGate(request.headers.get("cookie"), SENTINEL_ENV, T),
        limit: async () => "ok",
        env: {},
      },
    );
    expect(response.status).toBe(401);
    await expectNoLeak(response, captured);
  });
});

describe("secret readers", () => {
  function sourceFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) return sourceFiles(full);
      return /\.(ts|tsx|js|mjs|cjs)$/.test(name) ? [full] : [];
    });
  }
  const files = [
    ...sourceFiles(join(ROOT, "app")),
    ...sourceFiles(join(ROOT, "lib")),
    join(ROOT, "proxy.ts"),
    join(ROOT, "next.config.ts"),
  ];

  it("ACCESS_PASSWORD and GATE_COOKIE_SECRET are read only in lib/gate/cookie.ts, which is server-only", () => {
    const readers = files.filter((f) => /ACCESS_PASSWORD|GATE_COOKIE_SECRET/.test(readFileSync(f, "utf8")));
    expect(readers).toEqual([join(ROOT, "lib", "gate", "cookie.ts")]);
    for (const f of ["lib/gate/cookie.ts", "lib/gate/login.ts"]) {
      expect(readFileSync(join(ROOT, f), "utf8").startsWith('import "server-only";'), f).toBe(true);
    }
  });

  it("no NEXT_PUBLIC_ name in app, lib, proxy.ts or next.config.ts", () => {
    for (const file of files) {
      expect(readFileSync(file, "utf8"), file).not.toMatch(/NEXT_PUBLIC_[A-Za-z0-9_]/);
    }
  });

  it(".env.example names the gate variables without values", () => {
    const lines = readFileSync(join(ROOT, ".env.example"), "utf8").split(/\r?\n/);
    for (const name of ["ACCESS_GATE", "ACCESS_PASSWORD", "GATE_COOKIE_SECRET"]) {
      expect(lines.some((l) => l.includes(name)), name).toBe(true);
      expect(lines.some((l) => new RegExp(`^\\s*${name}\\s*=`).test(l)), name).toBe(false);
    }
  });

  it("the gate form never puts the password in a URL", () => {
    const form = readFileSync(join(ROOT, "app", "gate", "gate-form.tsx"), "utf8");
    expect(form).toMatch(/method="post"/);
    expect(form).not.toMatch(/searchParams|URLSearchParams|\?password/);
  });
});
