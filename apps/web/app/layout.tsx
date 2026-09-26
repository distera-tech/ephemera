import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Shell } from "@/components/Shell";

import "./globals.css";

export const metadata: Metadata = {
  title: "Ephemera — ephemeral AI inference",
  description: "Provision. Infer. Destroy. Ephemeral GPU infrastructure for sensitive AI workloads.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
