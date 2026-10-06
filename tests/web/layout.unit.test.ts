import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import RootLayout from "@/app/layout";

// next/font only works inside a Next build.
vi.mock("next/font/google", () => ({
  Atkinson_Hyperlegible_Next: () => ({ variable: "font-sans" }),
}));

function renderFooter(): string {
  const html = renderToStaticMarkup(
    RootLayout({ params: Promise.resolve({}), children: createElement("main") }),
  );
  return html.match(/<footer[^>]*>[\s\S]*<\/footer>/)?.[0] ?? "";
}

describe("site footer", () => {
  it("keeps the disclaimer text exactly", () => {
    expect(renderFooter()).toContain("<span>A personal project. Not an official USD tool.</span>");
  });

  it("links Neba to the GitHub profile in the same tab", () => {
    const footer = renderFooter();
    const link = footer.match(/<a[^>]*>([^<]*)<\/a>/);
    expect(link?.[1]).toBe("Neba");
    expect(link?.[0]).toContain('href="https://github.com/Nebishhh"');
    expect(link?.[0]).not.toContain("target=");
  });
});
