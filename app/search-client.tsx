"use client";

import { useState, type ReactNode } from "react";

import type { SearchResponse } from "@/lib/search/handler";

// The API returns up to 50 results; the UI shows the first 10 (SPEC.md).
const DISPLAY_COUNT = 10;

// Corpus size shown in the copy (SPEC.md).
const ARTICLE_COUNT = "3,109";

const MATCHING_TEXT = `Matching against ${ARTICLE_COUNT} articles.`;
const EMPTY_TEXT = "No matching articles.";
const EXPIRED_TEXT = "Your session has expired.";
const SIGN_IN_TEXT = "Sign in again";

// Every error message is fixed: no response text is ever shown.
export type SearchFailure = "expired" | "too_long" | "daily_limit" | "failed";

export type SearchOutcome =
  | { ok: true; data: SearchResponse }
  | { ok: false; failure: SearchFailure };

export type FetchFn = (input: string, init: RequestInit) => Promise<Response>;

const FAILURE_TEXT: Record<SearchFailure, string> = {
  expired: `${EXPIRED_TEXT} ${SIGN_IN_TEXT}.`,
  too_long: "The description is too long. Please shorten it.",
  daily_limit: "The daily search limit has been reached. Please try again tomorrow.",
  failed: "Search failed. Please try again.",
};

function resultsHeading(count: number): string {
  return count === 1 ? "Top match" : `Top ${count} matches`;
}

// What the hidden status line announces: the same text the page shows, and
// nothing before the first search.
export function statusText(loading: boolean, outcome: SearchOutcome | null): string {
  if (loading) return MATCHING_TEXT;
  if (outcome === null) return "";
  if (!outcome.ok) return FAILURE_TEXT[outcome.failure];
  const count = Math.min(outcome.data.results.length, DISPLAY_COUNT);
  return count === 0 ? EMPTY_TEXT : resultsHeading(count);
}

function failureFor(status: number): SearchFailure {
  if (status === 401) return "expired";
  if (status === 413) return "too_long";
  if (status === 429) return "daily_limit";
  return "failed";
}

// The query goes only into the POST body: never a URL, storage or a log.
export async function runSearch(
  fetchFn: FetchFn,
  query: string,
  mode: string,
): Promise<SearchOutcome> {
  try {
    const response = await fetchFn("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, mode }),
      cache: "no-store",
    });
    if (!response.ok) return { ok: false, failure: failureFor(response.status) };
    return { ok: true, data: (await response.json()) as SearchResponse };
  } catch {
    return { ok: false, failure: "failed" };
  }
}

export type SearchKey = { key: string; shiftKey: boolean; isComposing: boolean };

// Enter searches under the same condition as the button; Shift+Enter keeps its
// newline, and nothing happens while an IME composition is active.
export function shouldSearchOnKey(key: SearchKey, loading: boolean, query: string): boolean {
  if (key.isComposing || key.key === "Process") return false;
  if (key.key !== "Enter" || key.shiftKey) return false;
  return !loading && query.length > 0;
}

// lexical reads as keyword search; any other mode is shown under its own name.
function modeLabel(mode: string): string {
  if (mode === "lexical") return "Keyword search";
  return `${mode.charAt(0).toUpperCase()}${mode.slice(1)} search`;
}

// Fixed colors and shape come from globals.css; the text beside it carries
// the meaning, so the mark is hidden from assistive technology. The gate form
// uses the same mark.
export function WarnMark() {
  return (
    <svg className="warn-mark" aria-hidden="true" focusable="false" viewBox="0 0 18 16" width="18" height="16">
      <path className="warn-mark-shape" d="M9 1 17 15H1Z" />
      <path className="warn-mark-glyph" d="M9 6v4" />
      <circle className="warn-mark-dot" cx="9" cy="12.4" r="1" />
    </svg>
  );
}

function ArrowIcon() {
  return (
    <svg className="arrow" aria-hidden="true" focusable="false" viewBox="0 0 12 12" width="12" height="12">
      <path d="M2.5 9.5 9.5 2.5M4 2.5h5.5V8" />
    </svg>
  );
}

// An error: a short line ending in the warning mark, beside the fixed text.
function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="route notice">
      <div className="rail" aria-hidden="true">
        <span className="seg seg--short" />
        <WarnMark />
      </div>
      <p className="route-text">{children}</p>
    </div>
  );
}

function Loading() {
  return (
    <div className="route loading">
      <div className="rail" aria-hidden="true">
        <span className="seg seg--to-node" />
        <span className="node node--hollow" />
        <span className="seg seg--dashed" />
      </div>
      <div className="route-text">
        <p className="node-label">Your issue</p>
      </div>
      <div className="rail" aria-hidden="true">
        <span className="seg seg--dashed" />
      </div>
      <div className="route-text">
        <p>{MATCHING_TEXT}</p>
        <p className="helper">This can take a few seconds.</p>
      </div>
      <div className="rail" aria-hidden="true">
        <span className="seg seg--dashed seg--lead" />
        <span className="node node--square" />
      </div>
      <div className="route-text">
        <p className="node-label">Knowledge Base</p>
        <p className="helper">Public Coyote OneStop articles.</p>
      </div>
    </div>
  );
}

export function SearchOutput({ outcome }: { outcome: SearchOutcome }) {
  if (!outcome.ok) {
    switch (outcome.failure) {
      case "expired":
        return (
          <Notice>
            {`${EXPIRED_TEXT} `}
            <a href="/gate">{SIGN_IN_TEXT}</a>.
          </Notice>
        );
      default:
        return <Notice>{FAILURE_TEXT[outcome.failure]}</Notice>;
    }
  }

  const shown = outcome.data.results.slice(0, DISPLAY_COUNT);
  if (shown.length === 0) {
    return (
      <div className="route empty">
        <div className="rail" aria-hidden="true">
          <span className="seg seg--short" />
          <span className="tick" />
        </div>
        <div className="route-text">
          <p>{EMPTY_TEXT}</p>
          <p className="helper">Try describing the problem in different words.</p>
        </div>
      </div>
    );
  }

  const heading = resultsHeading(shown.length);
  // The results mount fresh for every search, so the line draws once each time.
  return (
    <section aria-labelledby="results-heading">
      <div className="route results-head">
        <div className="rail" aria-hidden="true">
          <span className="seg seg--draw" />
        </div>
        <div className="route-text">
          <h2 id="results-heading">{heading}</h2>
          <p className="helper">
            Ranked by closeness. Results always appear, so check that the top few fit.
          </p>
        </div>
      </div>
      <ol className="result-list" role="list">
        {shown.map((result) => (
          <li key={result.sourceArticleId}>
            <a href={result.url} target="_blank" rel="noopener noreferrer">
              {result.title}
              {/* The arrow stays on the line of the title's last word. */}
              <span className="nowrap">
                &nbsp;
                <ArrowIcon />
                <span className="visually-hidden"> (opens in a new tab)</span>
              </span>
            </a>
            {result.category !== undefined && <p className="category">{result.category}</p>}
          </li>
        ))}
      </ol>
    </section>
  );
}

export default function SearchClient({ mode }: { mode: string }) {
  // The query lives only in component state: never in the URL, storage or logs.
  const [query, setQuery] = useState("");
  const [outcome, setOutcome] = useState<SearchOutcome | null>(null);
  const [loading, setLoading] = useState(false);

  async function search() {
    // The previous outcome is cleared while the new search runs.
    setOutcome(null);
    setLoading(true);
    setOutcome(await runSearch(fetch, query, mode));
    setLoading(false);
  }

  return (
    <>
      <header className="site-header">
        <span className="brand">KB Finder</span>
        <span className="mode-label">{modeLabel(mode)}</span>
      </header>
      <main className="page">
        <div className="intro">
          <h1>From a messy issue to the closest article.</h1>
          <p className="lede">Help articles use IT terms. You don&apos;t have to.</p>
          <p className="helper">
            {`Searches ${ARTICLE_COUNT} public articles from the Coyote OneStop Knowledge Base.`}
          </p>
        </div>

        <div className="field">
          <label className="field-label" htmlFor="issue">
            What&apos;s going wrong?
          </label>
          <textarea
            id="issue"
            className="input"
            rows={5}
            aria-describedby="issue-help"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              const key = {
                key: event.key,
                shiftKey: event.shiftKey,
                isComposing: event.nativeEvent.isComposing,
              };
              if (!shouldSearchOnKey(key, loading, query)) return;
              event.preventDefault();
              void search();
            }}
            enterKeyHint="search"
            autoComplete="off"
            spellCheck={false}
          />
          <p id="issue-help" className="helper">
            Leave out names, IDs, emails and phone numbers.
          </p>
        </div>
        <div className="actions">
          <button
            type="button"
            className="button"
            onClick={search}
            disabled={loading || query.length === 0}
          >
            Find articles
          </button>
        </div>

        <p className="visually-hidden" role="status">
          {statusText(loading, outcome)}
        </p>
        <div className="output">
          {loading ? <Loading /> : outcome !== null && <SearchOutput outcome={outcome} />}
        </div>
      </main>
    </>
  );
}
