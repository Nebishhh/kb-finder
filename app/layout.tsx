import type { Metadata, Viewport } from "next";
import { Atkinson_Hyperlegible_Next } from "next/font/google";

import "./globals.css";

// next/font downloads the font at build time and serves it from this site:
// browsers never request anything from Google.
const sans = Atkinson_Hyperlegible_Next({
  weight: ["400", "600"],
  subsets: ["latin"],
  display: "swap",
  fallback: ["system-ui", "sans-serif"],
  // Next has no fallback metrics for this family; the default would log an
  // error on every compile.
  adjustFontFallback: false,
  variable: "--font-sans",
});

export const metadata: Metadata = {
  title: "KB Finder",
  description: "Find public Knowledge Base articles for an IT issue.",
};

// Emits <meta name="color-scheme" content="light dark">, so the browser uses
// the matching canvas and form controls before the stylesheet loads.
export const viewport: Viewport = {
  colorScheme: "light dark",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={sans.variable}>
      <body suppressHydrationWarning>
        <div className="shell">
          {children}
          <footer className="site-footer">
            <span>A personal project. Not an official USD tool.</span>
            <a className="footer-link" href="https://github.com/Nebishhh">
              Neba
            </a>
          </footer>
        </div>
      </body>
    </html>
  );
}
