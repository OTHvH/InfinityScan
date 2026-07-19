import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "InfinityScan – Read Manga, Manhwa & Manhua",
  description: "A self-hosted manga/manhwa/manhua reader with neon-violet style.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">
        {children}
      </body>
    </html>
  );
}
