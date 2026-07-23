# Browser Player

## Overview

The player is a single HTML page with no frontend framework, no build step, and
no bundler. It loads hls.js from a CDN and controls it with plain JavaScript.

---

## File responsibilities

| File | Responsibility |
|------|---------------|
| `public/index.html` | Page structure, DOM skeleton, script/style links |
| `public/player.js`  | All player logic — hls.js init, quality switching, status updates |
| `public/styles.css` | Dark theme, responsive layout, badge and table styles |

---

## Startup sequence

```
Browser loads index.html
  │
  ▼
player.js: init()
  │
  ├── showSpinner()
  ├── setStreamStatus("checking…")
  │
  ├── fetchStreamInfo()          GET /api/stream-info  (retries up to 5×)
  │     returns: { qualities: ["240p","360p","720p","1080p"], streams_ready: true }
  │
  ├── prePopulateDropdown(qualities)
  │     → builds <option> elements from quality names immediately
  │     → user can see 240p / 360p / 720p / 1080p in the dropdown
  │        before the video even starts buffering
  │
  ├── renderQualityBadges(qualities)
  │     → renders clickable pill badges below the player
  │
  ├── renderQualityInfo(qualities)
  │     → renders resolution + bitrate table with Select buttons
  │
  └── (if streams_ready)
        Hls.isSupported()?
          yes → initHls()         uses hls.js
          no  → canPlayType()?
                  yes → initNative()    Safari / iOS fallback
                  no  → showError("unsupported")
```

---

## hls.js initialisation

```javascript
const hls = new Hls({
  enableWorker:            true,   // decode in a Web Worker for smoother UI
  lowLatencyMode:          false,  // VOD, not live
  startLevel:              -1,     // -1 = auto ABR from the start
  fragLoadingMaxRetry:     4,
  manifestLoadingMaxRetry: 3,
});

hls.loadSource("/streams/master.m3u8");
hls.attachMedia(video);
```

### Events handled

| Event | Action |
|-------|--------|
| `MANIFEST_PARSED` | `remapDropdownToHlsLevels()` — syncs option values to actual hls.js level indexes; calls `video.play()` |
| `LEVEL_SWITCHED`  | `updatePlayingStatus()` — updates Mode/Playing display, highlights active badge and table row |
| `FRAG_BUFFERED`   | `hideSpinner()` |
| `ERROR` (fatal, NETWORK) | `hls.startLoad()` — tries to recover |
| `ERROR` (fatal, MEDIA)   | `hls.recoverMediaError()` — tries to recover |
| `ERROR` (fatal, other)   | `hls.destroy()` + `showError()` — non-recoverable |

---

## Quality switching

Three UI surfaces all call the same underlying logic:

```
Dropdown change
Badge click          →  selectQualityByName(name)  or  qualitySelect change handler
Table Select button       │
                          ├── sets hls.currentLevel = <index>
                          ├── sets hls.loadLevel    = <index>   (forces immediate switch)
                          ├── sets hls.nextLevel    = <index>
                          ├── updates dropdown value
                          ├── highlights active badge
                          └── highlights active table row
```

**Auto mode** sets `hls.currentLevel = -1`. hls.js then monitors bandwidth and
switches levels automatically. The "Playing" display still updates in real time
via `LEVEL_SWITCHED` events.

### Why `remapDropdownToHlsLevels()` is needed

The dropdown is pre-populated from `/api/stream-info` using quality names as
option values (`"720p"`, `"1080p"`, etc.). But hls.js assigns its own integer
indexes to levels, and the order may differ from the API list. After
`MANIFEST_PARSED`, `remapDropdownToHlsLevels()` builds a `name → index` map from
`hls.levels` and updates each option's `.value` to the correct integer, ensuring
`hls.currentLevel = parseInt(option.value)` always points to the right stream.

---

## API contract

The player depends on one backend endpoint:

### `GET /api/stream-info`

```json
{
  "title":         "Local Streaming Test",
  "manifest_url":  "/streams/master.m3u8",
  "qualities":     ["240p", "360p", "480p", "720p", "1080p"],
  "streams_ready": true
}
```

- `qualities` — list of quality names discovered from `streams/` by scanning for
  directories that contain a `playlist.m3u8`. Sorted ascending by height.
- `streams_ready` — `true` only when `streams/master.m3u8` exists and at least
  one quality playlist exists. The player shows an error overlay if this is `false`.

---

## Error handling

| Condition | Behaviour |
|-----------|-----------|
| `/api/stream-info` unreachable | Retries 5× with 1.5s delay, then shows error overlay |
| `streams_ready: false` | Shows error overlay with conversion instructions |
| hls.js NETWORK error (fatal) | Calls `hls.startLoad()` to retry segment loading |
| hls.js MEDIA error (fatal) | Calls `hls.recoverMediaError()` |
| hls.js other fatal error | Destroys hls instance, shows error overlay |
| Native video element error | Shows error overlay with error message |
| Browser has no HLS support | Shows "HLS is not supported in this browser" |

---

## Safari / iOS native HLS fallback

Safari supports HLS natively via `<video src="…m3u8">`. When `Hls.isSupported()`
returns false but `video.canPlayType("application/vnd.apple.mpegurl")` is truthy,
the player sets `video.src` directly to the master playlist. The quality dropdown
is disabled in this mode because hls.js level APIs are unavailable.

---

## Responsive layout

| Breakpoint | Behaviour |
|-----------|-----------|
| > 600px | Controls bar horizontal, status group right-aligned |
| ≤ 600px | Controls bar stacks vertically, status group full width, resolution/bitrate columns hidden in quality table |

The video wrapper uses `aspect-ratio: 16 / 9` so it scales correctly on any
screen width without JavaScript.
