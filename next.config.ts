import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Stop next dev from writing a block into AGENTS.md / CLAUDE.md.
  agentRules: false,
  // Never forward browser console output to the dev terminal.
  logging: { browserToTerminal: false },
};

export default nextConfig;
