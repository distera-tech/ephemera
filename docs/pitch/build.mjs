// Renders docs/pitch/ephemera-pitch.html to a 16:9 PDF (one slide per page).
// Usage: node build.mjs [out.pdf]   (needs playwright-core + Chromium)
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const here = path.dirname(fileURLToPath(import.meta.url));
const out = process.argv[2] ?? path.join(here, "ephemera-pitch.pdf");
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH ?? "/opt/pw-browsers/chromium" });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
await page.goto(`file://${path.join(here, "ephemera-pitch.html")}`, { waitUntil: "networkidle" });
await page.pdf({ path: out, width: "1920px", height: "1080px", printBackground: true, preferCSSPageSize: true });
await browser.close();
console.log(out);
