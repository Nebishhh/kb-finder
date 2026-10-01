import { beforeEach } from "vitest";

// Every web test runs offline. A real fetch is impossible: any call that does
// not go through a test's own fake rejects. The key and the mode switches are
// cleared so nothing from the developer's shell can change behavior.

const CLEARED = [
  "OPENAI_API_KEY",
  "EVAL_MODES_ENABLED",
  "DEFAULT_SEARCH_MODE",
  "RATE_LIMIT",
  "VERCEL",
] as const;

export const blockedFetch = (): Promise<Response> =>
  Promise.reject(new Error("real fetch is blocked in tests"));

function lockDown(): void {
  for (const name of CLEARED) delete process.env[name];
  globalThis.fetch = blockedFetch as typeof fetch;
}

lockDown();
beforeEach(lockDown);
