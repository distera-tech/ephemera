import type { NextConfig } from "next";

// The browser only ever talks to this Next.js origin. /api/* is proxied server-side
// to the Ephemera control-plane API. Nothing in the browser can reach a GPU or the
// model server: those are only reachable from the worker, over Brev SSH.
const apiUrl = process.env.API_INTERNAL_URL ?? "http://localhost:8000";
// The rewrite proxy buffers request bodies (default 10 MB). Keep this above the API's
// MAX_DOCUMENT_SIZE_MB so the API — not the proxy — enforces the limit with a clean 413.
const proxyBodyLimitMb = Number(process.env.WEB_PROXY_MAX_BODY_MB ?? 32);

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  experimental: { proxyClientMaxBodySize: `${proxyBodyLimitMb}mb` },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};

export default nextConfig;
