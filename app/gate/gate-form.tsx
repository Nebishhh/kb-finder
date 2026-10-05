"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { WarnMark } from "../search-client";

type Status = "idle" | "loading" | "wrong" | "capped" | "error";

// Every error message is fixed: no response text is ever shown.
const ERROR_TEXT: Partial<Record<Status, string>> = {
  wrong: "Wrong access code.",
  capped: "Too many failed attempts today. Try again tomorrow.",
  error: "Sign-in failed. Please try again.",
};

// The password lives only in component state and the POST body: never in the
// URL, storage or logs. method="post" means that even without JavaScript the
// browser never puts it in a query string (/api/gate then rejects the form
// encoding with 400).
export default function GateForm() {
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const router = useRouter();

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setStatus("loading");
    try {
      const response = await fetch("/api/gate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
        cache: "no-store",
      });
      if (response.status === 204) {
        // The cookie is set; the navigation's request carries it through proxy.
        router.push("/");
        return;
      }
      if (response.status === 401) setStatus("wrong");
      else if (response.status === 429) setStatus("capped");
      else setStatus("error");
    } catch {
      setStatus("error");
    }
  }

  const error = ERROR_TEXT[status];

  return (
    <form className="gate-form" method="post" action="/api/gate" onSubmit={submit}>
      <div className="field">
        <label className="field-label" htmlFor="access-code">
          Access code
        </label>
        <input
          id="access-code"
          className="input"
          type="password"
          name="password"
          autoComplete="current-password"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? "access-code-error" : undefined}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </div>
      {/* One persistent status region, empty when there is no error. */}
      <div className="gate-status" role="status">
        {error && (
          <p id="access-code-error" className="field-error">
            <WarnMark />
            <span>{error}</span>
          </p>
        )}
      </div>
      <div className="actions">
        <button
          type="submit"
          className="button"
          disabled={status === "loading" || password.length === 0}
        >
          Enter
        </button>
        <p className="helper">This demo is private. Ask me for the code.</p>
      </div>
    </form>
  );
}
