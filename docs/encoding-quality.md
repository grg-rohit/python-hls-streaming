# Encoding Quality Reference

## Where to configure quality

All quality settings live in one file: `app/ffmpeg_service.py`

---

## 1. Bitrate ladder — `FULL_LADDER`

Located near the top of `ffmpeg_service.py`:

```python
FULL_LADDER: list[dict] = [
    {"name": "240p",  ..., "video_bitrate":   "600k", "audio_bitrate": "128k"},
    {"name": "360p",  ..., "video_bitrate":  "1200k", "audio_bitrate": "128k"},
    {"name": "480p",  ..., "video_bitrate":  "2500k", "audio_bitrate": "192k"},
    {"name": "720p",  ..., "video_bitrate":  "5000k", "audio_bitrate": "192k"},
    {"name": "1080p", ..., "video_bitrate":  "9000k", "audio_bitrate": "192k"},
    {"name": "1440p", ..., "video_bitrate": "16000k", "audio_bitrate": "192k"},
    {"name": "2160p", ..., "video_bitrate": "28000k", "audio_bitrate": "192k"},
]
```

These bitrates act as **ceilings**. The encoder can use less if the scene is
simple, but never more. This controls file size and bandwidth usage.

### Comparison with YouTube

| Tier | This project | YouTube (approx) |
|------|-------------|------------------|
| 360p | 1,200 kbps  | 500–1,000 kbps   |
| 720p | 5,000 kbps  | 2,500 kbps       |
| 1080p | 9,000 kbps | 4,500 kbps       |
| 1440p | 16,000 kbps | 9,000 kbps      |

This project intentionally uses higher bitrates than YouTube because there is no
re-encoding — the goal is to preserve source quality as faithfully as possible.

---

## 2. CPU encoder quality — `_cpu_flags()`

```python
"-preset", "slow",   # compression effort: ultrafast → slow → veryslow
"-crf",    "18",     # quality: 0=lossless, 18=near-lossless, 23=default, 51=worst
```

### CRF explained

CRF (Constant Rate Factor) is the primary quality knob for libx264.
It targets a perceptual quality level rather than a fixed bitrate:

| CRF | Quality | File size |
|-----|---------|-----------|
| 0   | Lossless | Very large |
| 18  | Near-lossless — virtually indistinguishable from source | Large |
| 23  | Default — good quality, moderate size | Moderate |
| 28  | Visibly degraded | Small |
| 51  | Worst possible | Tiny |

CRF and bitrate work together: CRF controls quality; the `-b:v` ceiling prevents
the encoder from going too high in easy scenes.

### Preset explained

Preset controls how hard the encoder works to compress. Slower = better
compression at the same quality level (same CRF):

```
ultrafast → superfast → veryfast → faster → fast → medium → slow → veryslow
  fastest,                                                            slowest
  worst compression                                        best compression
```

`slow` is a good balance for offline conversion where time isn't critical.

---

## 3. NVENC quality — `_gpu_flags()` (RTX GPU)

```python
"-preset", "p7",    # NVENC preset: p1=fastest quality … p7=best quality
"-tune",   "hq",    # high quality tuning (vs ll=low-latency, ull=ultra-low-latency)
"-rc",     "vbr",   # rate control: vbr=variable bitrate (best quality/size ratio)
"-cq",     "18",    # NVENC quality floor: equivalent to CRF 18
```

### NVENC vs libx264 quality gap

At the same CRF/CQ value, NVENC produces slightly lower quality than libx264
because NVENC is a fixed-function hardware block with less sophisticated
rate-distortion optimization. The gap is small at high bitrates (barely visible)
but more noticeable at low bitrates.

With `p7 + cq 18`, NVENC quality closely approaches `libx264 -preset fast + crf 18`,
while being 3–5× faster. The gap vs `libx264 -preset slow` is slightly larger.

### NVENC preset guide

| Preset | Speed (relative) | Quality |
|--------|-----------------|---------|
| p1 | ~8× faster than CPU | Lowest |
| p4 | ~5× faster | Balanced |
| p6 | ~4× faster | Good |
| p7 | ~3× faster | Best NVENC can do |

---

## 4. Segment duration — `SEGMENT_DURATION`

```python
SEGMENT_DURATION: int = 6  # seconds
```

Each `.ts` file holds 6 seconds of video. This affects:

- **Seek latency** — shorter segments mean the player can jump to a new position
  faster (fewer wasted bytes to skip). 2–4 seconds is common for low-latency.
- **Switching speed** — hls.js can only switch quality at segment boundaries.
  Shorter = faster switching.
- **File count** — a 1-hour video at 6s = 600 segments per quality tier. At
  2s it would be 1,800 files.
- **Keyframe alignment** — the project forces a keyframe every `SEGMENT_DURATION × 30`
  frames (assuming ≤30fps). Misaligned keyframes cause stalls on quality switches.

6 seconds is a reasonable default for VOD (video on demand) streaming.

---

## 5. Audio quality

All tiers use AAC at 44,100 Hz stereo. Bitrates:

| Tier | Audio bitrate | Note |
|------|--------------|------|
| 240p, 360p | 128 kbps | Good quality for most content |
| 480p and above | 192 kbps | High quality, transparent for most ears |

192 kbps AAC is generally considered transparent (indistinguishable from lossless
on normal speakers/headphones). Going higher provides no audible benefit.

---

## Trade-offs summary

| Setting | Higher value → | Lower value → |
|---------|---------------|---------------|
| `video_bitrate` | Better quality, larger files | Smaller files, more compression |
| CRF / `-cq` | More bits used, better quality | Fewer bits, worse quality |
| `preset` | Slower encode, better compression | Faster encode, larger file |
| `SEGMENT_DURATION` | Fewer files, slower seeking | More files, faster seeking |
