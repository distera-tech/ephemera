// Renders docs/video/ephemera-motion.html frame by frame and encodes an MP4.
// Usage: node record.mjs <ffmpeg> <out.mp4> [fps] [--stills t1,t2,...  outdir]
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const [ffmpeg, out, fpsArg, stillsFlag, stillsList, stillsDir] = process.argv.slice(2);
const fps = Number(fpsArg ?? 30);
const here = path.dirname(fileURLToPath(import.meta.url));
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH ?? "/opt/pw-browsers/chromium" });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
await page.goto(`file://${path.join(here, "ephemera-motion.html")}?record`);
const duration = await page.evaluate(() => window.DURATION);

if (stillsFlag === "--stills") {
  for (const t of stillsList.split(",").map(Number)) {
    await page.evaluate((x) => window.render(x), t);
    await page.screenshot({ path: path.join(stillsDir, `still-${String(t).padStart(5, "0")}.png`) });
  }
  await browser.close();
  process.exit(0);
}

const enc = spawn(ffmpeg, ["-y", "-f", "image2pipe", "-framerate", String(fps), "-c:v", "mjpeg", "-i", "-",
  "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
  { stdio: ["pipe", "inherit", "inherit"] });
const total = Math.round(duration * fps);
for (let i = 0; i < total; i++) {
  await page.evaluate((x) => window.render(x), i / fps);
  const buf = await page.screenshot({ type: "jpeg", quality: 95 });
  if (!enc.stdin.write(buf)) await new Promise((r) => enc.stdin.once("drain", r));
  if (i % 150 === 0) process.stderr.write(`frame ${i}/${total}\n`);
}
enc.stdin.end();
await new Promise((r) => enc.on("close", r));
await browser.close();
