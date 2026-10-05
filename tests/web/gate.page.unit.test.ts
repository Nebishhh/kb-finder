import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import GatePage, { dynamic } from "@/app/gate/page";
import GateForm from "@/app/gate/gate-form";
import { viewport } from "@/app/layout";

// Offline: no DOM. useRouter needs Next's app router context, so it is faked;
// the static render never submits. next/font only works inside a Next build.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("next/font/google", () => ({
  Atkinson_Hyperlegible_Next: () => ({ variable: "font-sans" }),
}));

const PASSWORD = "a-valid-passphrase-of-length";
const SECRET = "x".repeat(64);

function renderForm(): string {
  return renderToStaticMarkup(createElement(GateForm));
}

function renderPage(): string {
  return renderToStaticMarkup(GatePage());
}

describe("gate form", () => {
  it("posts the password field to /api/gate, never in a URL", () => {
    const html = renderForm();
    // React orders attributes its own way; HTML attribute names ignore case.
    const form = html.match(/<form[^>]*>/)?.[0] ?? "";
    const input = html.match(/<input[^>]*>/)?.[0] ?? "";
    expect(form).toContain('method="post"');
    expect(form).toContain('action="/api/gate"');
    expect(input).toContain('name="password"');
    expect(input).toContain('type="password"');
    expect(input).toMatch(/autocomplete="current-password"/i);
  });

  it("ties the Access code label to the input, with the Enter button and helper", () => {
    const html = renderForm();
    expect(html).toContain('<label class="field-label" for="access-code">Access code</label>');
    expect(html).toMatch(/<input id="access-code"/);
    expect(html).toMatch(/<button type="submit" class="button" disabled="">Enter<\/button>/);
    expect(html).toContain('<p class="helper">This demo is private. Ask me for the code.</p>');
  });

  it("shows no error and an empty status region on first render", () => {
    const html = renderForm();
    expect(html.match(/role="status"/g)).toHaveLength(1);
    expect(html).toContain('<div class="gate-status" role="status"></div>');
    expect(html).not.toContain("aria-invalid");
    expect(html).not.toContain("aria-describedby");
    expect(html).not.toContain("warn-mark");
    expect(html).not.toMatch(/wrong|too many|failed/i);
  });
});

describe("gate page", () => {
  // Each test stubs exactly the gate variables it needs, independent of order.
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("is rendered per request", () => {
    expect(dynamic).toBe("force-dynamic");
  });

  it("shows the brand-only header, the intro and the form", () => {
    vi.stubEnv("ACCESS_GATE", undefined);
    vi.stubEnv("ACCESS_PASSWORD", PASSWORD);
    vi.stubEnv("GATE_COOKIE_SECRET", SECRET);
    const html = renderPage();
    expect(html).toContain('<span class="brand">KB Finder</span>');
    expect(html).not.toContain("mode-label");
    expect(html).toContain("<h1>From a messy issue to the closest article.</h1>");
    expect(html).toContain(">Help articles are written in IT terms. You don&#x27;t have to be.<");
    expect(html).toContain(
      ">Searches 3,109 public articles from the Coyote OneStop Knowledge Base.<",
    );
    expect(html).toContain('<form class="gate-form"');
    expect(html).not.toContain("Configuration error");
  });

  it("renders a fixed configuration error and no form when the gate is not set up", () => {
    vi.stubEnv("ACCESS_GATE", undefined);
    vi.stubEnv("ACCESS_PASSWORD", undefined);
    vi.stubEnv("GATE_COOKIE_SECRET", undefined);
    const html = renderPage();
    expect(html).toContain(
      '<p class="config-error">Configuration error: the access gate is not set up.</p>',
    );
    expect(html).toContain("<h1>From a messy issue to the closest article.</h1>");
    expect(html).not.toContain("<form");
    expect(html).not.toContain("<input");
  });
});

describe("layout", () => {
  it("declares color-scheme light dark through the viewport export", () => {
    expect(viewport.colorScheme).toBe("light dark");
  });
});
