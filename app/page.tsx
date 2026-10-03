import { resolveDefaultMode } from "@/lib/search/handler";

import SearchClient from "./search-client";

// Read the default mode per request, never at build time.
export const dynamic = "force-dynamic";

export default function Home() {
  // Same resolution as /api/search, so the UI always sends an allowed mode.
  const mode = resolveDefaultMode({
    DEFAULT_SEARCH_MODE: process.env.DEFAULT_SEARCH_MODE,
  });
  if (mode === null) {
    return (
      <main>
        <h1>KB Finder</h1>
        <p>Configuration error: the default search mode is not valid.</p>
      </main>
    );
  }
  return <SearchClient mode={mode} />;
}
