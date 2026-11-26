# Conversion Pipeline

## What it does

Takes one video file of any name and format from `videos/` and produces a set of
HLS-compatible MPEG-TS segments at multiple bitrate levels, plus a master playlist
that a player like hls.js uses to switch between them adaptively.

---

## Entry points

```
convert_video.py          ← human-facing CLI
    └── calls app/ffmpeg_service.py::convert()   ← all logic lives here
```

---

## Step-by-step execution

```
convert()
  │
  ├── 1. _ffmpeg_binary()
  │       Calls shutil.which("ffmpeg") — finds FFmpeg on PATH.
  │       Raises FileNotFoundError with install instructions if missing.
  │
  ├── 2. find_source_video()
  │       Scans videos/ for the first file matching:
  │         .mp4 → .mkv → .mov → .avi → .webm  (in that priority order)
  │       Returns the Path. No renaming required.
  │
  ├── 3. probe_source(source)
  │       Runs ffprobe -print_format json -show_streams -select_streams v:0
  │       Reads width + height from the first video stream.
  │       Detects portrait rotation via tags.rotate (swaps w/h if rotated 90°/270°).
  │       Falls back to 1920×1080 if probing fails.
  │
  ├── 4. build_quality_ladder(src_w, src_h)
  │       Filters FULL_LADDER to only include tiers whose height ≤ source height.
  │       → No upscaling: a 720p source never produces a 1080p tier.
  │       → If source exceeds 2160p, adds a custom tier matching source resolution.
  │       → Always keeps at least one tier (240p).
  │
  ├── 5. GPU detection (if mode ≠ cpu)
  │       detect_gpu_encoder()
  │         Runs: ffmpeg -hide_banner -encoders
  │         Checks for h264_nvenc → h264_qsv → h264_amf in that priority.
  │         Returns the first match or None.
  │       _verify_nvenc_works()
  │         Does a 1-second test encode with the detected encoder.
  │         Falls back to CPU if the runtime test fails.
  │
  ├── 6. _prepare_output_dirs(qualities, clean)
  │       If clean=True: deletes contents of streams/ (not the dir itself —
  │         deleting the dir fails on Docker bind mounts due to EBUSY).
  │       Creates streams/<name>/ for each quality tier.
  │
  ├── 7. Encoding  (varies by mode)
  │
  │   mode = auto / gpu / cpu  →  sequential loop
  │   ┌──────────────────────────────────────────┐
  │   │  for quality in qualities:               │
  │   │      _encode_quality(...)                │
  │   └──────────────────────────────────────────┘
  │
  │   mode = hybrid  →  parallel ThreadPoolExecutor(max_workers=2)
  │   ┌──────────────────────────────────────────────────────────┐
  │   │  cpu_qualities = tiers where height ≤ 360               │
  │   │  gpu_qualities = tiers where height >  360               │
  │   │                                                          │
  │   │  Thread 1 (CPU): encodes 240p, 360p  with libx264        │
  │   │  Thread 2 (GPU): encodes 480p, 720p, 1080p… with NVENC  │
  │   │                           both run simultaneously        │
  │   └──────────────────────────────────────────────────────────┘
  │
  └── 8. _write_master_playlist(qualities)
          Writes streams/master.m3u8 with an EXT-X-STREAM-INF entry
          per quality tier, including BANDWIDTH and RESOLUTION attributes.
```

---

## FFmpeg command structure (per quality)

```
ffmpeg -y
  -i <source>
  -vf "scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2"

  [video flags — see Encoding modes below]

  -c:a aac  -b:a <audio_bitrate>  -ar 44100  -ac 2

  -g <fps*6>  -keyint_min <fps*6>  -sc_threshold 0   ← force keyframe alignment

  -f hls
  -hls_time 6
  -hls_list_size 0
  -hls_segment_type mpegts
  -hls_segment_filename streams/<name>/segment_%03d.ts
  streams/<name>/playlist.m3u8
```

The `-g` / `-keyint_min` / `-sc_threshold 0` combination forces a keyframe at the
start of every 6-second segment boundary across all quality levels. This alignment
is critical — without it, hls.js cannot switch between levels mid-stream without
causing a visible glitch or stall.

The `scale + pad` filter preserves the source aspect ratio at any input shape
(16:9, 4:3, portrait, ultrawide) and pads the remainder with black.

---

## Encoding modes

### CPU (`libx264`)
```
-c:v     libx264
-preset  slow        ← high quality (ultrafast→superfast→veryfast→faster→fast→medium→slow→veryslow)
-crf     18          ← constant rate factor: 0=lossless, 18=near-lossless, 23=default, 51=worst
-b:v     <bitrate>   ← bitrate cap (CRF drives quality; bitrate acts as ceiling)
-maxrate <bitrate>
-bufsize <2x bitrate>
-profile:v high
-level   4.0 / 4.2   ← 4.2 for resolutions above 720p
```

### NVENC / RTX GPU (`h264_nvenc`)
```
-c:v     h264_nvenc
-preset  p7          ← p1=fastest quality … p7=best quality
-tune    hq          ← high quality tuning mode
-rc      vbr         ← variable bitrate (best quality/size for VBR)
-cq      18          ← NVENC quality floor: equivalent to CRF 18
-b:v     <bitrate>
-maxrate <bitrate>
-bufsize <2x bitrate>
-profile:v high
```

### Intel QuickSync (`h264_qsv`)
```
-c:v          h264_qsv
-preset       slow
-global_quality 18   ← QSV equivalent of CRF
-b:v / -maxrate / -bufsize
```

### AMD AMF (`h264_amf`)
```
-c:v    h264_amf
-quality quality     ← quality > balanced > speed
-rc     vbr_latency
-qmin   18
-b:v / -maxrate / -bufsize
```

---

## Quality ladder

| Tier  | Resolution | Video bitrate | Audio bitrate |
|-------|-----------|---------------|---------------|
| 240p  | 426×240   | 600 kbps      | 128 kbps      |
| 360p  | 640×360   | 1,200 kbps    | 128 kbps      |
| 480p  | 854×480   | 2,500 kbps    | 192 kbps      |
| 720p  | 1280×720  | 5,000 kbps    | 192 kbps      |
| 1080p | 1920×1080 | 9,000 kbps    | 192 kbps      |
| 1440p | 2560×1440 | 16,000 kbps   | 192 kbps      |
| 2160p | 3840×2160 | 28,000 kbps   | 192 kbps      |

Tiers above the source resolution are skipped automatically.

---

## Resume logic

When `--resume` is passed, `_quality_complete(quality)` checks each tier before
encoding:

```python
def _quality_complete(quality):
    playlist = STREAMS_DIR / quality["name"] / "playlist.m3u8"
    if not playlist.exists():
        return False
    return len(list((STREAMS_DIR / quality["name"]).glob("segment_*.ts"))) > 0
```

A tier is considered complete if it has a `playlist.m3u8` **and** at least one
`.ts` segment. If either is missing, it re-encodes.

---

## Output file layout

```
streams/
├── master.m3u8           ← references all quality playlists
├── 240p/
│   ├── playlist.m3u8     ← lists all segments for this quality
│   ├── segment_000.ts
│   ├── segment_001.ts
│   └── ...
├── 360p/
│   └── ...
├── 480p/ 720p/ 1080p/    ← same structure
```

### master.m3u8 format
```
#EXTM3U
#EXT-X-VERSION:3

#EXT-X-STREAM-INF:BANDWIDTH=9192000,RESOLUTION=1920x1080,NAME="1080p"
1080p/playlist.m3u8

#EXT-X-STREAM-INF:BANDWIDTH=5192000,RESOLUTION=1280x720,NAME="720p"
720p/playlist.m3u8
...
```

Paths are relative to `streams/` so they resolve correctly whether served from
localhost or an ngrok tunnel.
