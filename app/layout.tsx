import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "KB Finder",
  description: "Find public Knowledge Base articles for an IT issue.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
