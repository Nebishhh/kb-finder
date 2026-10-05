import { format } from "node:util";

import { describe, expect, it, vi } from "vitest";

import { checkPassword, resolveGateConfig, type GateEnv } from "@/lib/gate/cookie";
import { handleGateLogin } from "@/lib/gate/login";
import { GATE_FAIL_CAP, GATE_FAIL_DAY_KIND, GLOBAL_KEY } from "@/lib/limits/config";
import { DAILY_CAP_SQL, LimitError } from "@/lib/limits/daily-cap";
import {
  GATE_RELEASE_SQL,
  GATE_RESERVE_SQL,
  makeGateFailCap,
  type GateFailCap,
} from "@/lib/limits/gate-fail-cap";

// Offline. The cap is a fake counter; the real SQL runs against a throwaway
// schema in search.db.test.ts. checkPassword is wrapped in a spy that calls the
// real function, so a test can prove the PIN was never compared.
// tests/web/setup.ts clears the gate variables, RATE_LIMIT and VERCEL before each
// test.

vi.mock("@/lib/gate/cookie", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/gate/cookie")>();
  return { ...actual, checkPassword: vi.fn(actual.checkPassword) };
});

const PIN = "8264";
const SECRET = "test-cookie-secret-0123456789abcdef"; // 35 bytes
const PIN_ENV: GateEnv = { ACCESS_PASSWORD: PIN, GATE_COOKIE_SECRET: SECRET };
const T = Date.UTC(2026, 9, 1, 12, 0, 0); // ms

function post(body: string): Request {
  return new Request("http://localhost/api/gate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });
}

// An in-memory counter with the cap's contract, recording calls in order.
function fakeCap(start = 0) {
  const state = { count: start, calls: [] as string[] };
  const cap: GateFailCap = {
    reserve: vi.fn(async () => {
      state.calls.push("reserve");
      return ++state.count;
    }),
    release: vi.fn(async () => {
      state.calls.push("release");
      state.count--;
    }),
  };
  return { state, cap };
}

async function login(pin: string, cap: GateFailCap, env: GateEnv = PIN_ENV) {
  const response = await handleGateLogin(post(JSON.stringify({ password: pin })), env, T, cap);
  return {
    status: response.status,
    cache: response.headers.get("Cache-Control"),
    setCookie: response.headers.get("Set-Cookie"),
    text: await response.text(),
  };
}

describe("ACCESS_PASSWORD validation", () => {
  it("exactly 4 ASCII digits is a valid PIN", () => {
    expect(resolveGateConfig(PIN_ENV).mode).toBe("on");
    expect(resolveGateConfig({ ...PIN_ENV, ACCESS_PASSWORD: "1234" }).mode).toBe("on");
    expect(resolveGateConfig({ ...PIN_ENV, ACCESS_PASSWORD: "0000" }).mode).toBe("on");
  });

  it.each([
    "123",
    "12345",
    "12a4",
    " 1234",
    "1234 ",
    "1234\n",
    "١٢٣٤", // Arabic-Indic digits are not ASCII
    "１２３４", // fullwidth digits
    "a".repeat(19),
    "1".repeat(19),
    "",
    undefined,
  ])("%j fails closed", (password) => {
    expect(resolveGateConfig({ ...PIN_ENV, ACCESS_PASSWORD: password })).toEqual({
      mode: "invalid",
      code: "GATE_PASSWORD_INVALID",
    });
  });

  it("a passphrase of 20 or more characters is still valid", () => {
    for (const password of ["a".repeat(20), "correct horse battery staple"]) {
      expect(resolveGateConfig({ ...PIN_ENV, ACCESS_PASSWORD: password }).mode).toBe("on");
    }
  });
});

describe("the failed-login cap in /api/gate", () => {
  it("a wrong PIN is counted and stays counted", async () => {
    const { state, cap } = fakeCap();
    const result = await login("0000", cap);
    expect(result).toEqual({
      status: 401,
      cache: "no-store",
      setCookie: null,
      text: JSON.stringify({ error: "Unauthorized" }),
    });
    expect(state.calls).toEqual(["reserve"]);
    expect(state.count).toBe(1);
  });

  it("the right PIN reserves, compares, then releases: net zero", async () => {
    const { state, cap } = fakeCap(3);
    vi.mocked(checkPassword).mockClear();
    const result = await login(PIN, cap);
    expect(result.status).toBe(204);
    expect(result.setCookie).toMatch(/^kbf_gate=v1\.\d+\.[A-Za-z0-9_-]{43}; /);
    expect(state.calls).toEqual(["reserve", "release"]);
    expect(state.count).toBe(3);
    expect(checkPassword).toHaveBeenCalledTimes(1);
    expect(vi.mocked(cap.reserve).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(checkPassword).mock.invocationCallOrder[0],
    );
    expect(vi.mocked(checkPassword).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(cap.release).mock.invocationCallOrder[0],
    );
    expect(cap.reserve).toHaveBeenCalledWith(T);
    expect(cap.release).toHaveBeenCalledWith(T);
  });

  it("the 10th failure of a day is still compared; reaching 11 is 429 without a compare", async () => {
    expect(GATE_FAIL_CAP).toBe(10);
    const { state, cap } = fakeCap();
    for (let i = 1; i <= 10; i++) expect((await login("0000", cap)).status, `attempt ${i}`).toBe(401);
    expect(state.count).toBe(10);

    vi.mocked(checkPassword).mockClear();
    // Even the right PIN: the count reaches 11, so it is never compared.
    const result = await login(PIN, cap);
    expect(result).toEqual({
      status: 429,
      cache: "no-store",
      setCookie: null,
      text: JSON.stringify({ error: "Too many attempts" }),
    });
    expect(checkPassword).not.toHaveBeenCalled();
    expect(cap.release).not.toHaveBeenCalled();
    expect(state.count).toBe(11);
    expect((await login("0000", cap)).status).toBe(429);
    expect(checkPassword).not.toHaveBeenCalled();
  });

  it("the right PIN at count 10 still gets in", async () => {
    const { state, cap } = fakeCap(9);
    expect((await login(PIN, cap)).status).toBe(204);
    expect(state.count).toBe(9);
  });

  it("a reserve error is 500, fails closed, and the PIN is never compared", async () => {
    vi.mocked(checkPassword).mockClear();
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const cap: GateFailCap = {
      reserve: vi.fn(async () => {
        throw Object.assign(new Error(`relation ${PIN}`), { code: "42P01" });
      }),
      release: vi.fn(async () => {}),
    };
    const result = await login(PIN, cap);
    expect(result).toEqual({
      status: 500,
      cache: "no-store",
      setCookie: null,
      text: JSON.stringify({ error: "Gate unavailable" }),
    });
    expect(checkPassword).not.toHaveBeenCalled();
    expect(cap.release).not.toHaveBeenCalled();
    expect(errors.mock.calls).toEqual([["gate limit failed: Error 42P01"]]);
    errors.mockRestore();
  });

  it("a release error is logged; the right PIN still gets in", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const cap: GateFailCap = {
      reserve: async () => 1,
      release: async () => {
        throw new LimitError();
      },
    };
    const result = await login(PIN, cap);
    expect(result.status).toBe(204);
    expect(result.setCookie).not.toBeNull();
    expect(errors.mock.calls).toEqual([["gate release failed: LimitError LIMIT_BAD_RESULT"]]);
    errors.mockRestore();
  });

  it("RATE_LIMIT=off with VERCEL unset skips the cap", async () => {
    const { cap } = fakeCap(50);
    const env = { ...PIN_ENV, RATE_LIMIT: "off" };
    expect((await login("0000", cap, env)).status).toBe(401);
    expect((await login(PIN, cap, env)).status).toBe(204);
    expect(cap.reserve).not.toHaveBeenCalled();
    expect(cap.release).not.toHaveBeenCalled();
  });

  it("RATE_LIMIT=off with VERCEL set is 500 before the body is read", async () => {
    const { cap } = fakeCap();
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    let pulls = 0;
    const body = new ReadableStream<Uint8Array>(
      {
        pull() {
          pulls++;
          throw new Error("body was read");
        },
      },
      { highWaterMark: 0 },
    );
    const response = await handleGateLogin(
      new Request("http://localhost/api/gate", { method: "POST", body, duplex: "half" } as RequestInit),
      { ...PIN_ENV, RATE_LIMIT: "off", VERCEL: "1" },
      T,
      cap,
    );
    expect(response.status).toBe(500);
    expect(await response.text()).toBe(JSON.stringify({ error: "Gate unavailable" }));
    expect(pulls).toBe(0);
    expect(cap.reserve).not.toHaveBeenCalled();
    expect(errors.mock.calls).toEqual([["gate config error: RATE_LIMIT=off on Vercel"]]);
    errors.mockRestore();
  });

  it("any other RATE_LIMIT value keeps the cap on, also on Vercel", async () => {
    for (const RATE_LIMIT of ["OFF", "0", "false", ""]) {
      const { cap } = fakeCap();
      expect((await login(PIN, cap, { ...PIN_ENV, RATE_LIMIT, VERCEL: "1" })).status).toBe(204);
      expect(cap.reserve).toHaveBeenCalledTimes(1);
    }
  });

  it.each([
    ["invalid JSON", '{"password": "8'],
    ["extra field", JSON.stringify({ password: PIN, x: 1 })],
    ["oversized", JSON.stringify({ password: "1".repeat(2000) })],
  ])("%s is answered before the cap", async (_name, body) => {
    const { cap } = fakeCap();
    const response = await handleGateLogin(post(body), PIN_ENV, T, cap);
    expect([400, 413]).toContain(response.status);
    expect(cap.reserve).not.toHaveBeenCalled();
  });

  it("gate off: 204 without the cap", async () => {
    const { cap } = fakeCap();
    expect((await login("0000", cap, { ACCESS_GATE: "off" })).status).toBe(204);
    expect(cap.reserve).not.toHaveBeenCalled();
  });
});

describe("makeGateFailCap", () => {
  const ISO = new Date(T).toISOString();

  it("reserve runs the search cap's statement with the gate kind and returns the count", async () => {
    expect(GATE_RESERVE_SQL).toBe(DAILY_CAP_SQL);
    const query = vi.fn(async () => ({ rows: [{ count: 4 }], rowCount: 1 }));
    expect(await makeGateFailCap(query).reserve(T)).toBe(4);
    expect(query).toHaveBeenCalledWith(GATE_RESERVE_SQL, [ISO, GATE_FAIL_DAY_KIND, GLOBAL_KEY]);
    expect(GATE_FAIL_DAY_KIND).toBe("gate_fail_global_day");
  });

  it.each([[[]], [[{ count: 0 }]], [[{ count: "4" }]], [[{ count: 1.5 }]], [[{ count: 1 }, { count: 2 }]]])(
    "reserve rejects a bad result %j",
    async (rows) => {
      const query = vi.fn(async () => ({ rows, rowCount: rows.length }));
      await expect(makeGateFailCap(query).reserve(T)).rejects.toBeInstanceOf(LimitError);
    },
  );

  it("release runs one MERGE on the same window and needs exactly one row", async () => {
    const query = vi.fn(async () => ({ rows: [], rowCount: 1 }));
    await makeGateFailCap(query).release(T);
    expect(query).toHaveBeenCalledWith(GATE_RELEASE_SQL, [ISO, GATE_FAIL_DAY_KIND, GLOBAL_KEY]);
    const none = vi.fn(async () => ({ rows: [], rowCount: 0 }));
    await expect(makeGateFailCap(none).release(T)).rejects.toBeInstanceOf(LimitError);
  });
});

describe("the PIN never leaks", () => {
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

  const throwing = (): Promise<never> => Promise.reject(new Error(`boom ${PIN}`));
  const cases: [string, string, GateEnv, GateFailCap][] = [
    ["right PIN", JSON.stringify({ password: PIN }), PIN_ENV, fakeCap().cap],
    ["wrong PIN", JSON.stringify({ password: "8265" }), PIN_ENV, fakeCap().cap],
    ["capped, right PIN", JSON.stringify({ password: PIN }), PIN_ENV, fakeCap(GATE_FAIL_CAP).cap],
    ["reserve error", JSON.stringify({ password: PIN }), PIN_ENV, { reserve: throwing, release: throwing }],
    ["release error", JSON.stringify({ password: PIN }), PIN_ENV, { reserve: async () => 1, release: throwing }],
    ["RATE_LIMIT off", JSON.stringify({ password: PIN }), { ...PIN_ENV, RATE_LIMIT: "off" }, fakeCap().cap],
    ["RATE_LIMIT off on Vercel", JSON.stringify({ password: PIN }),
      { ...PIN_ENV, RATE_LIMIT: "off", VERCEL: "1" }, fakeCap().cap],
    ["invalid JSON", `{"password": "${PIN}`, PIN_ENV, fakeCap().cap],
    ["extra field", JSON.stringify({ password: PIN, note: PIN }), PIN_ENV, fakeCap().cap],
    ["oversized", JSON.stringify({ password: PIN.repeat(300) }), PIN_ENV, fakeCap().cap],
    // A 5-digit value is not a valid PIN, so the gate is misconfigured.
    ["misconfigured", JSON.stringify({ password: PIN }), { ...PIN_ENV, ACCESS_PASSWORD: `${PIN}9` },
      fakeCap().cap],
  ];

  it.each(cases)("%s", async (_name, body, env, cap) => {
    const captured = captureOutput();
    const response = await handleGateLogin(post(body), env, T, cap);
    const text = await response.text();
    const headers = [...response.headers.entries()].map(([k, v]) => `${k}: ${v}`).join("\n");
    vi.restoreAllMocks();
    expect(text).not.toContain(PIN);
    expect(headers).not.toContain(PIN);
    for (const line of captured) expect(line).not.toContain(PIN);
  });
});
