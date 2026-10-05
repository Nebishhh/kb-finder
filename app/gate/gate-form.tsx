"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

type Status = "idle" | "loading" | "wrong" | "capped" | "error";

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

  return (
    <form method="post" action="/api/gate" onSubmit={submit}>
      <label>
        Password{" "}
        <input
          type="password"
          name="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </label>{" "}
      <button type="submit" disabled={status === "loading" || password.length === 0}>
        Enter
      </button>
      {status === "wrong" && <p>Wrong password.</p>}
      {status === "capped" && <p>Too many failed attempts today. Try again tomorrow.</p>}
      {status === "error" && <p>Sign-in failed. Please try again.</p>}
    </form>
  );
}
