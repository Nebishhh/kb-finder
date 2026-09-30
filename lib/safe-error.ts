// Reduce an error to "<class> <code>" for logging. Error messages are
// never used: Postgres errors can echo query input.
const SAFE_CODE = /^[A-Z0-9_]{1,40}$/;

export function errorSummary(err: unknown): string {
  const errorClass =
    err instanceof Error ? err.constructor.name || "Error" : typeof err;
  let code = "-";
  if (typeof err === "object" && err !== null && "code" in err) {
    const value = err.code;
    if (typeof value === "string" && SAFE_CODE.test(value)) code = value;
  }
  return `${errorClass} ${code}`;
}
