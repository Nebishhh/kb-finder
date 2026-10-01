import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    // "@/..." imports, from tsconfig.json paths.
    tsconfigPaths: true,
    alias: {
      // server-only throws outside a react-server bundle. Next enforces it at
      // build time; tests use the package's own no-op entry.
      "server-only": fileURLToPath(
        new URL("./node_modules/server-only/empty.js", import.meta.url),
      ),
    },
  },
  test: {
    environment: "node",
    include: ["tests/web/**/*.test.ts"],
    // Blocks real fetch and clears OPENAI_API_KEY and the mode switches.
    setupFiles: ["tests/web/setup.ts"],
  },
});
