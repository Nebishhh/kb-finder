import type { Metadata } from "next";
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

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={sans.variable}>
      <body suppressHydrationWarning>
        <div className="shell">
          {children}
          <footer className="site-footer">A personal project. Not an official USD tool.</footer>
        </div>
      </body>
    </html>
  );
}
