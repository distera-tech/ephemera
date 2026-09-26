"""Synthesizes an original soundtrack synced to ephemera-motion.html (no samples, no licences).

Usage: python soundtrack.py out.wav
"""

import sys
import wave

import numpy as np

SR = 48_000
DUR = 57.0
N = int(SR * DUR)
out = np.zeros((N, 2))
rng = np.random.default_rng(3)


def env(n, a, r):
    e = np.ones(n)
    a, r = int(a * SR), int(r * SR)
    if a:
        e[:a] = np.linspace(0, 1, a)
    if r:
        e[-r:] *= np.linspace(1, 0, r) ** 2
    return e


def add(t0, sig, gain=1.0, pan=0.0):
    i = int(t0 * SR)
    j = min(N, i + len(sig))
    if j <= i:
        return
    s = sig[: j - i] * gain
    out[i:j, 0] += s * (1 - pan) / 2 * 2 ** 0.5
    out[i:j, 1] += s * (1 + pan) / 2 * 2 ** 0.5


def tone(freq, dur, a=0.005, r=0.3, harm=(1, 0.3, 0.1)):
    t = np.arange(int(dur * SR)) / SR
    s = sum(h * np.sin(2 * np.pi * freq * (k + 1) * t) for k, h in enumerate(harm))
    return s * env(len(t), a, r)


def click(f=2200):
    return tone(f, 0.06, 0.001, 0.05, (1, 0.2))


def chime(root):
    return sum(tone(root * m, 1.8, 0.004, 1.6, (1, 0.25, 0.05)) * g for m, g in ((1, 1), (1.25, 0.7), (1.5, 0.6), (2, 0.35)))


def sweep(f0, f1, dur, noise=0.0):
    t = np.arange(int(dur * SR)) / SR
    f = np.geomspace(f0, f1, len(t))
    s = np.sin(2 * np.pi * np.cumsum(f) / SR)
    if noise:
        n = rng.standard_normal(len(t))
        n = np.convolve(n, np.ones(40) / 40, "same")
        s = s * (1 - noise) + n * noise * 3
    return s * env(len(t), dur * 0.2, dur * 0.6)


def thud():
    t = np.arange(int(0.5 * SR)) / SR
    return np.sin(2 * np.pi * (60 + 80 * np.exp(-t * 30)) * t) * np.exp(-t * 7)


def glitch(dur=0.9):
    t = np.arange(int(dur * SR)) / SR
    sq = np.sign(np.sin(2 * np.pi * 110 * t)) * (np.sin(2 * np.pi * 13 * t) > 0)
    return (0.6 * sq + 0.4 * rng.standard_normal(len(t))) * env(len(t), 0.005, 0.3)


# --- ambient pad (A minor → resolves to C major for the outro)
t = np.arange(N) / SR
def pad_chord(freqs):
    return sum(np.sin(2 * np.pi * f * t + np.sin(2 * np.pi * 0.13 * t) * 0.8) for f in freqs)
minor = pad_chord([110, 130.81, 164.81, 220])
major = pad_chord([130.81, 164.81, 196, 261.63])
x = np.clip((t - 49) / 3, 0, 1)
pad = (minor * (1 - x) + major * x) * 0.035
pad *= np.clip(t / 2.5, 0, 1) * np.clip((DUR - t) / 2.5, 0, 1)
out[:, 0] += pad
out[:, 1] += pad

# --- S1: uneasy idle hum + heartbeat
hum = np.sin(2 * np.pi * 55 * t) * 0.08 * np.clip(t / 1, 0, 1) * np.clip((5.8 - t) / 0.8, 0, 1)
out[:, 0] += hum
out[:, 1] += hum
for b in np.arange(0.5, 5.5, 1.0):
    add(b, thud(), 0.35)
    add(b + 0.22, thud(), 0.2)

# --- S2: two options appear, two rejections
add(5.8, tone(440, 0.25, 0.005, 0.2), 0.12, -0.4)
add(6.6, tone(440, 0.25, 0.005, 0.2), 0.12, 0.4)
add(8.0, thud(), 0.6, -0.3)
add(8.5, thud(), 0.6, 0.3)

# --- S3: logo swell + reveal chord
add(10.4, sweep(80, 900, 1.0, 0.5), 0.12)
add(11.3, chime(261.63), 0.18)

# --- S4 lifecycle (u = t - 15.5)
for u in (3, 4, 7, 8.5, 11, 14, 15.5, 17.5):
    add(15.5 + u, click(), 0.25)
add(15.5 + 4.0, sweep(120, 700, 2.2), 0.10)          # GPU assembling
add(15.5 + 5.6, tone(196, 0.8, 0.01, 0.6), 0.14)      # GPU active
add(15.5 + 8.5, sweep(300, 1200, 2.3), 0.05)          # model loading
add(15.5 + 12.4, tone(880, 0.3, 0.003, 0.25), 0.15)   # result
add(15.5 + 14.0, sweep(4000, 900, 1.0, 0.8), 0.07)    # data wiped
add(15.5 + 15.5, sweep(900, 60, 1.9, 0.6), 0.16)      # GPU destroyed
add(15.5 + 19.0, chime(523.25), 0.22)                 # COMPUTE = 0

# --- S5 failure (v = t - 37)
for v in (0.2, 0.6, 1.2, 1.7, 2.3):
    add(37 + v, click(1800), 0.22)
add(37 + 0.4, sweep(120, 700, 1.2), 0.08)
add(37 + 2.9, glitch(), 0.12)
for i in range(3):
    add(37 + 4.5 + i * 0.75, tone(660 + i * 110, 0.25, 0.003, 0.2), 0.14)
add(37 + 5.1, sweep(900, 60, 1.5, 0.6), 0.14)
add(37 + 6.4, chime(523.25), 0.2)

# --- S6 pillars
for i in range(4):
    add(45.5 + i * 0.35, tone(1046.5, 0.15, 0.002, 0.12), 0.07)

# --- S7 outro: three hits + final chime
for i, f in enumerate((196, 220, 261.63)):
    add(51.3 + i * 0.45, thud(), 0.5)
    add(51.3 + i * 0.45, tone(f, 0.6, 0.005, 0.5), 0.12)
add(53.0, chime(523.25), 0.26)

out /= max(1e-9, np.abs(out).max()) / 0.85
pcm = (out * 32767).astype("<i2")
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
