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
    // No mode label here: the configured value is never shown.
    return (
      <>
        <header className="site-header">
          <span className="brand">KB Finder</span>
        </header>
        <main className="page">
          <div className="intro">
            <h1>From a messy issue to the closest article.</h1>
            <p className="lede">Help articles are written in IT terms. You don&apos;t have to be.</p>
            <p className="helper">
              Searches 3,109 public articles from the Coyote OneStop Knowledge Base.
            </p>
          </div>
          <p className="config-error">
            Configuration error: the default search mode is not valid.
          </p>
        </main>
      </>
    );
  }
  return <SearchClient mode={mode} />;
}
