import { gateEnvFrom } from "@/lib/gate/cookie";
import { handleGateLogin } from "@/lib/gate/login";
import { gateFailCap } from "@/lib/limits/gate-fail-cap";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return handleGateLogin(request, gateEnvFrom(process.env), Date.now(), gateFailCap);
}
