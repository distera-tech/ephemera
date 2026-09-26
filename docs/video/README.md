# Motion-design video

`ephemera-motion.mp4` — 57 s, 1920×1080, 30 fps, H.264 + AAC, French captions, original synthesized soundtrack.

Everything is generated from source (no stock footage, no licensed audio):

* `ephemera-motion.html` — the animation; every frame is a pure function `render(t)`. Open it in a browser for a live preview.
* `record.mjs` — captures each frame with Chromium (Playwright) and pipes it into ffmpeg.
* `soundtrack.py` — synthesizes the music and sound effects with NumPy, synced to the same timeline.

Regenerate (needs Node + `playwright-core`, Chromium, Python with NumPy, an ffmpeg with libx264 — e.g. `pip install imageio-ffmpeg`):

```bash
node docs/video/record.mjs "$FFMPEG" silent.mp4 30
python docs/video/soundtrack.py sound.wav
"$FFMPEG" -i silent.mp4 -i sound.wav -c:v copy -c:a aac -b:a 192k -shortest docs/video/ephemera-motion.mp4
```

The lifecycle shown is the real one implemented by Ephemera; the GPU visuals are illustrative.
