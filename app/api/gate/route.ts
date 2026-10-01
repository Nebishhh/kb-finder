import { gateEnvFrom } from "@/lib/gate/cookie";
import { handleGateLogin } from "@/lib/gate/login";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return handleGateLogin(request, gateEnvFrom(process.env), Date.now());
}
