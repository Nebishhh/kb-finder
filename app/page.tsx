"use client";

import { useState } from "react";

import type { SearchResponse, SearchResult } from "@/lib/search/handler";

// The API returns up to 50 results; the UI shows the first 10 (SPEC.md).
const DISPLAY_COUNT = 10;

type Status = "idle" | "loading" | "error";

export default function Home() {
  // The query lives only in component state: never in the URL, storage or logs.
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [status, setStatus] = useState<Status>("idle");

  async function search() {
    setStatus("loading");
    try {
      const response = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, mode: "lexical" }),
        cache: "no-store",
      });
      if (!response.ok) throw new Error("search request failed");
      const data = (await response.json()) as SearchResponse;
      setResults(data.results);
      setStatus("idle");
    } catch {
      setResults(null);
      setStatus("error");
    }
  }

  const shown = results === null ? [] : results.slice(0, DISPLAY_COUNT);

  return (
    <main>
      <h1>KB Finder</h1>
      <textarea
        aria-label="Describe the IT issue"
        rows={6}
        cols={80}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        autoComplete="off"
        spellCheck={false}
      />
      <div>
        <button
          type="button"
          onClick={search}
          disabled={status === "loading" || query.length === 0}
        >
          Search
        </button>
      </div>

      {status === "error" && <p>Search failed. Please try again.</p>}

      {results !== null && (
        <>
          <p>
            Showing {shown.length} of {results.length} results.
          </p>
          <ol>
            {shown.map((result) => (
              <li key={result.sourceArticleId}>
                <a href={result.url} target="_blank" rel="noopener noreferrer">
                  {result.title}
                </a>
                {result.category !== undefined && <> ({result.category})</>}
                {" "}score {result.score}
              </li>
            ))}
          </ol>
        </>
      )}
    </main>
  );
}
