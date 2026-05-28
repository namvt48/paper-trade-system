import type { Metadata } from "next";
import "./globals.css";
import { UTCClock } from "@/components/utc-clock";

export const metadata: Metadata = {
  title: "Paper Trade System",
  description: "Centralized paper-trade dashboard for alpha strategies",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-slate-900 text-slate-100 min-h-screen">
        <nav className="border-b border-slate-700/60 bg-slate-900/95 backdrop-blur-sm px-6 py-3 flex items-center gap-6 sticky top-0 z-50">
          <a href="/" className="font-bold text-base tracking-tight text-white">
            <span className="text-indigo-400">▸</span> Paper Trade
          </a>
          <a href="/compare" className="text-slate-400 hover:text-slate-200 text-sm transition-colors">
            Compare
          </a>
          <div className="ml-auto">
            <UTCClock />
          </div>
        </nav>
        <main className="max-w-7xl mx-auto px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
