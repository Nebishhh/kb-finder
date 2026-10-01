import { gateEnvFrom, resolveGateConfig } from "@/lib/gate/cookie";

import GateForm from "./gate-form";

// Read the gate config per request, never at build time.
export const dynamic = "force-dynamic";

export default function GatePage() {
  const config = resolveGateConfig(gateEnvFrom(process.env));
  return (
    <main>
      <h1>KB Finder</h1>
      {config.mode === "invalid" ? (
        <p>Configuration error: the access gate is not set up.</p>
      ) : (
        <GateForm />
      )}
    </main>
  );
}
