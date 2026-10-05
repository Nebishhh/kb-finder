import { gateEnvFrom, resolveGateConfig } from "@/lib/gate/cookie";

import GateForm from "./gate-form";

// Read the gate config per request, never at build time.
export const dynamic = "force-dynamic";

// The same header (brand only, no mode label) and intro as the search page.
export default function GatePage() {
  const config = resolveGateConfig(gateEnvFrom(process.env));
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
        {config.mode === "invalid" ? (
          <p className="config-error">Configuration error: the access gate is not set up.</p>
        ) : (
          <GateForm />
        )}
      </main>
    </>
  );
}
