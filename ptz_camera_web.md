# PTZ Camera Web: Local AI Object-Tracking Camera

A Ring-style web app for the PTZ camera at `192.168.1.87`. Detection, tracking, recording and the web UI all run on this Raspberry Pi 5 and its Hailo-10H AI HAT. Nothing goes to the cloud.

---

## 1. Goals

| # | Requirement (from the request) | How it is delivered |
|---|---|---|
| G1 | Object-tracking camera web page | Live view with tracked boxes, PTZ controls and optional PTZ auto-follow |
| G2 | Statistics on cars, people, animals, bicycles and motorcycles that pass by | Each unique moving object is tracked and counted once; stats page shows totals, hourly/daily charts, a heatmap and direction split |
| G3 | Timeline | Ring-style history: a 24 h timeline bar plus event cards, filterable by category and day |
| G4 | Still capture per event | Best-frame snapshot (with boxes) plus a zoomed thumbnail |
| G5 | 30 s video per event | 30 s MP4 clip including 5 s of pre-roll, cut from a rolling RAM buffer without re-encoding |
| G6 | Everything local | Inference on the Hailo-10H, storage on the SD card, UI served from the Pi, no CDN or web fonts, no internet needed at runtime |

## 2. Principles and constraints

- **Local only.** Models, inference, recordings, database and UI assets all stay on the Pi. The page loads nothing from the internet.
- **No re-encoding.** Clips are stream-copied (H.264 + AAC) from the camera, so they keep full 1080p quality at near-zero CPU.
- **Protect the SD card.** The rolling video buffer lives in RAM (`/dev/shm`). Only event clips and snapshots are written to disk.
- **Count passers-by, not parked cars.** An object only counts once it has actually moved. Stationary objects never trigger events.
- **Safe PTZ.** Every continuous move has a server-side watchdog: if the browser stops refreshing the command, the camera stops. Auto-follow is **off by default**. Counting pauses while the camera moves, because camera motion would otherwise look like object motion.
- **Survive failure.** The camera, decoder, recorder or Hailo can each drop out. Every worker reconnects with backoff, and the UI shows status.

## 3. Inventory (verified on 2026-10-30)

| Item | Finding |
|---|---|
| Board | Raspberry Pi 5 Model B, 4 cores, 16 GB RAM, 105 GB free on SD |
| OS | Ubuntu 26.04 LTS, kernel `7.0.0-1020-raspi`, timezone America/Los_Angeles (NTP synced) |
| AI accelerator | **Hailo-10H** (AI HAT+ 2), firmware 5.3.0, HailoRT 5.3.0, device node `/dev/h1x-0` |
| Driver fix applied | The DKMS module had not been built for kernel 1020 because headers were missing. Installed `linux-headers-7.0.0-1020-raspi` and `linux-headers-raspi` (so future kernels rebuild automatically), then loaded `hailo1x_pci` |
| Models | `models/yolov11m.hef` (**70 fps**) and `models/yolov8m.hef` (78 fps), from Hailo Model Zoo v5.3.0 for hailo10h. On-chip NMS, 80 COCO classes, input 640×640×3 UINT8 |
| Inference API | `VDevice.create_infer_model()`; synchronous `ConfiguredInferModel.run()` takes about 28 ms per frame. Output is a list of 80 per-class arrays of `[ymin, xmin, ymax, xmax, score]`, normalized to the letterboxed input (validated on `bus.jpg`: 4 people and 1 bus) |
| Python | Project venv `.venv` (uv CPython 3.11.15): hailort 5.3.0, numpy 2.4, opencv-python-headless 5.0, aiohttp 3.14 |
| Camera | "Unite 200" (V51U) PTZ. RTSP `rtsp://192.168.1.87:554/1` = 1080p30 H.264 Main + AAC 48 kHz stereo, GOP 30 (one keyframe per second). `/2` = 320×240. RTSP auth is off. VISCA over TCP `5678` (address 1); position inquiries verified. HTTP CGI `/cgi-bin/ptzctrl.cgi` is a fallback |
| Measured cost | Software decode of 1080p30 to 720p BGR at 15 fps: **83 % of one core**. Stream-copy segmenter: **2 %**. Two simultaneous RTSP sessions work |
| Clip assembly | Concatenating 2 s TS segments into an MP4 with `+faststart`, input-seeked: starts on a keyframe, decodes with zero errors |

## 4. Architecture

```
                         ┌──────────────────────── Raspberry Pi 5 ─────────────────────────┐
  Camera 192.168.1.87    │                                                                 │
  ┌──────────────┐ RTSP  │  ffmpeg #1 (decode)  ──raw BGR 1280x720@15──▶ FrameSource thread│
  │  H.264 1080p │──────▶│                                                   │             │
  │  + AAC       │ RTSP  │  ffmpeg #2 (copy) ──2 s .ts segments──▶ /dev/shm ring (120 s)    │
  │              │──────▶│                                                   │             │
  │  VISCA :5678 │◀──────│  PTZController (watchdog) ◀── AutoTracker ◀──┐    ▼             │
  └──────────────┘ TCP   │                                              │  Pipeline thread │
                         │   Hailo-10H ◀── HailoDetector (YOLOv11m) ◀───┤  (detect→track→ │
                         │                                              │   count→events)  │
                         │   ClipBuilder thread ◀── clip jobs ◀─────────┤                  │
                         │        │ (ffmpeg concat, -c copy)            ▼                  │
                         │        ▼                              EventManager              │
                         │   data/media/<day>/<id>/clip.mp4  ◀── snapshot.jpg/thumb.jpg    │
                         │   data/ptzcam.db (SQLite: events, tracks)                       │
                         │                                                                 │
                         │   aiohttp :8080 ── REST /api/*, MJPEG live, SSE /api/stream,    │
                         │                    /media/* (Range), /static/* (web UI)         │
                         └─────────────────────────────────────────────────────────────────┘
                                         ▲  LAN only (phone / PC browser)
```

One Python process (`python -m ptzcam`) with threads:

| Thread | Role |
|---|---|
| `FrameSource` | Owns ffmpeg #1. Reads exact-size BGR frames and keeps only the newest (drops when behind) |
| `Recorder` | Owns ffmpeg #2. Indexes and prunes RAM segments |
| `ClipBuilder` | Waits for a clip window to close, then builds the MP4 |
| `Pipeline` | Per frame: Hailo inference, tracker update, PTZ-motion gating, event logic, auto-follow, live-overlay state, SSE throttling |
| `PTZ watchdog` | Stops moves that are not refreshed |
| asyncio main | aiohttp server, SSE fan-out, status ticker, hourly retention job |

## 5. Detection categories

| Category | COCO classes | UI color |
|---|---|---|
| `person` (People) | person | `#4F9DFF` |
| `vehicle` (Vehicles) | car, bus, truck | `#FFB020` |
| `bicycle` (Bicycles) | bicycle | `#2ED47A` |
| `motorcycle` (Motorcycles) | motorcycle | `#FF5C7A` |
| `animal` (Animals) | bird, cat, dog, horse, sheep, cow, elephant, bear, zebra, giraffe | `#B57BFF` |

All other COCO classes are ignored. The fine-grained label (car, truck, dog…) is kept on every object.

## 6. Tracking and the "passed by" rule

**Tracker.** A dependency-free ByteTrack variant:
- Constant-velocity prediction, then greedy IoU matching within the same category: high-score detections first, then low-score detections (≥ 0.2) to rescue existing tracks.
- A centre-distance fallback for small, fast objects.
- Unmatched high-score detections spawn tentative tracks.
- A track is **confirmed** after 4 hits. It is dropped after 2.0 s unseen.

**Moving test.** Distances are measured in aspect-corrected coordinates:
- A track is *moving* once its centre has travelled at least `min_move` (default 0.5) × its box size from its anchor point, **and** at least 1.5 % of frame height.
- Or its box area has changed ≥ 2× over at least 1 s (it is approaching or leaving).
- Once a track is moving, it stays moving.

**A track is counted** (exactly once, as one passer-by) when all of these hold:
1. It is confirmed and moving.
2. Its category is enabled and its best score ≥ the category threshold.
3. If a detection zone polygon is set, the box's bottom-centre point (ground contact) has been inside it.
4. The PTZ is not moving and has settled for 2 s. When motion ends, every track's anchor is reset, so camera motion is never mistaken for object motion.

**Direction** is recorded per object: `ltr`, `rtl`, `approach`, `leave`, or `unknown`.

### 6.1 PTZ auto-follow and auto-zoom (optional, off by default)

**Two modes:**
- **Watch mode** (default): a fixed wide view. Every passer-by is counted. Best for stats.
- **Follow mode**: the camera follows one target and zooms in on it. Other objects are not counted while the camera moves, because counting pauses during motion and settle.

**Target selection.** The user can click a box in the live view (`track_id`); that pick is never swapped for another object. Otherwise, among confirmed, moving, countable tracks whose category is in `autotrack_categories`:
- **People come first.** A person outranks every other category, and takes over from an automatically picked non-person target as soon as one is seen moving.
- Objects crossing the picture faster than 0.25 frame sizes per second are skipped. From this window a passing car crosses the view in about 1.5 s, before the camera can turn. Measured at Home: people 0.05–0.17, passing cars mostly 0.4–0.86. The speed is the larger of the track's velocity and its average since it appeared, because the velocity estimate still lags at pick time.
- Ties go to counted tracks, then to category weight × area × score.

**Pan and tilt.**
- The error `(ex, ey)` is measured from the frame centre (−0.5 to 0.5) **at the predicted position**: centre + 0.6 s × the track's image velocity. The picture lags the camera by about half a second.
- A proportional term outside `deadzone` (capped by `max_pan_speed` / `max_tilt_speed`) is scaled by the magnification. That assumes about 12× at the full zoom range of 16384, growing exponentially with the position.
- An integral term learns the speed of a steadily walking target, so the camera keeps pace instead of trailing it. It starts once the target first comes within 0.2 of centre, so it cannot wind up during the first swing, and it is capped at half the maximum speed.
- Commands are only sent when they change, at no more than 5 Hz. An axis below the slowest VISCA step stays still.

**Auto-zoom** (`autozoom`): one measured step, not steering.
- When the lens is at rest (`PTZController.zoom_arrived`: the polled position reads back as the goal) and `LEAD_S` (0.6 s) has passed so the picture shows it, take the median box height over 3–5 frames.
- **Zoom in** if the target has stayed within 0.15 of centre for 0.5 s, drifts slower than 0.1 frame sizes per second, is clear of the edges, and `h < target_size / 1.2`. The goal is `pos_for(mag_at(zoom_pos) × target_size / h)`, capped at `max_zoom_pos`.
- **Back out** if `h > target_size × 1.25`, aiming for `target_size`.
- **About to leave** (touching an edge with error over 0.3, or predicted error over 0.45): go to half the magnification at once, even while the lens moves.
- Moves under 256 positions are skipped.
- Each move is one VISCA zoom direct (`PTZController.zoom_to`). The camera stops exactly at the goal, so the picture's lag cannot cause overshoot. `motion_until` covers the travel (`|Δ| / zoom_rate`), so counting pauses meanwhile.
- `mag_at` / `pos_for` interpolate the measured `ptz.zoom_curve` in log-magnification (`tools/calibrate_zoom.py`, `ptzcam/zoomcal.py`: ORB plus RANSAC similarity between 1024-position steps). Without a curve they assume 12× at 16384. The pan/tilt gain is `1 / mag_at(zoom_pos)`.
- Without VISCA positions (the HTTP fallback), auto-zoom is skipped.

**Losing the target.**
- **Coast** (people, bicycles, animals): for 1.2 s, keep turning at the learned speed. A person walking behind one of the window bars reappears on the other side.
- **Re-lock:** from 0.3 s missing (shorter gaps are detection flicker) until `return_home_seconds`, a matching track is taken to be the target if it is already known to move, or (people, bicycles, animals only) appeared around the loss at 0.4–2.5× the lost box height, and lies within 0.2 of the target's path: from its last position along its velocity, or towards the frame centre, where the camera was bringing it. A person, bicycle and motorcycle match each other, because a rider is detected as either.
  - A re-lock of the same category calls `Track.inherit`, which moves the count, event, DB row and votes to the new track. The person is not counted twice, and the zoomed-in frames replace the event photo.
  - Vehicles are only re-found among tracks already seen moving. While the camera pans, every parked car that comes into view is a new detection. On the first live run, a lost minivan was "re-found" as two other cars in a row, and the camera panned until it pointed into the room.
  - A re-locked target that never moves once the camera is still (4 s after settling) was probably something else, such as a parked car; the camera returns home.
- **Next object:** after the grace (1.2 s for people, bicycles and animals; 0.5 s otherwise), any followable object in view takes over (log `next: …`).
- Otherwise a **vehicle** sends the camera Home at once; it doesn't come back. A **person** is looked for at the Home zoom (one `zoom_to(home_zoom)`) until `return_home_seconds`, then Home.

**Safety.** Every auto command also goes through the watchdog. Turning auto-follow off stops all motion immediately.

## 7. Events, snapshots and clips (Ring model)

- **An event starts** when a track is counted and no event window is open. The window is `[t − preroll, t − preroll + clip_seconds]`, which defaults to `[t − 5 s, t + 25 s]`, so 30 s.
- **More objects** counted while the window is open join the same event. For example, a cyclist produces one event labelled Person + Bicycle. The first object counted after the window closes starts a new event.
- **Snapshot.** For every candidate track, the pipeline keeps the best frame, scored as score × √area, with a penalty if the box touches the frame edge.
  - On event creation it writes `snapshot.jpg` (1280×720 with boxes) and `thumb.jpg` (a 384×216 16:9 crop zoomed on the object).
  - If an attached object later produces a frame ≥ 20 % better, the snapshot is rewritten, at most once per 2 s, until the window closes.
- **Clip.** `ClipBuilder` waits until the window end plus one segment plus a margin. It then selects the RAM segments overlapping the window and runs `ffmpeg -f concat -i list -t <dur> -c copy -movflags +faststart clip.mp4` from the first segment's keyframe (no `-ss`: seeking into a segment and stream-copying dropped up to a GOP). A clip can therefore start up to one segment before the window; its lead is stored (`clip_lead`, `clip_wide_lead`) so the viewer keeps the same moment when switching versions.
  - The result is a browser-playable H.264/AAC MP4.
  - `clip_status` goes from `pending` to `ready` or `failed`, and the UI is told over SSE.
- **Mapping segments to wall-clock time.** Segment files are `seg_<n>.ts`, where `n` starts at the epoch second when ffmpeg starts.
  - Segment end time is its mtime. Its start is the previous segment's end when the numbers are consecutive, otherwise mtime − segment length.
  - Segments still needed by pending clips are never pruned.

## 8. Data

**Media:** `data/media/<YYYY-MM-DD>/<event_id>/{snapshot.jpg, thumb.jpg, clip.mp4}`

**SQLite** (`data/ptzcam.db`, WAL):

```sql
events(id PK, day TEXT, start_ts REAL, trigger_ts REAL, end_ts REAL, last_seen_ts REAL,
       top_category TEXT, categories TEXT /*JSON {cat:n}*/, labels TEXT /*JSON {label:n}*/,
       max_score REAL, snapshot TEXT, thumb TEXT, clip TEXT, clip_status TEXT)
tracks(id PK, event_id INTEGER NULL, track_id INTEGER, day TEXT, category TEXT, label TEXT,
       first_ts REAL, counted_ts REAL, last_ts REAL, max_score REAL, direction TEXT)
```

- Statistics come from `tracks`, where each row is one counted passer-by.
- **Deleting an event removes its media but keeps the counts**: track rows get `event_id = NULL`.
- **Retention:**
  - Events and media older than `retention_days` (30) are deleted.
  - The oldest events are also deleted whenever media exceeds `max_storage_gb` (50).
  - Track rows (stats history) are kept for 365 days.

## 9. HTTP API contract

Base: `http://<pi>:8080`. JSON everywhere. Timestamps are unix seconds (float). Days are local `YYYY-MM-DD`. Normalized boxes are `[x1, y1, x2, y2]` in 0..1. Category keys are `person | vehicle | bicycle | motorcycle | animal`.

### 9.1 Pages and static files
- `GET /` serves `web/index.html`.
- `GET /static/<path>` serves `web/<path>`. Example: `/static/js/app.js` maps to `web/js/app.js`.
- `GET /media/<path>` serves `data/media/<path>` with Range support, so `<video>` can seek.

### 9.2 Event object

```json
{
  "id": 12,
  "start_ts": 1793405000.0, "trigger_ts": 1793405005.0, "end_ts": 1793405030.0,
  "last_seen_ts": 1793405021.3,
  "top_category": "person",
  "categories": {"person": 1, "bicycle": 1},
  "labels": {"person": 1, "bicycle": 1},
  "max_score": 0.91,
  "snapshot_url": "/media/2026-10-30/12/snapshot.jpg",
  "thumb_url": "/media/2026-10-30/12/thumb.jpg",
  "clip_url": "/media/2026-10-30/12/clip.mp4",
  "clip_status": "ready",
  "night": true,
  "clip_night_url": "/media/2026-10-30/12/clip_night.mp4",
  "clip_night_status": "ready",
  "duration": 30.0
}
```

- `clip_url` is `null` unless `clip_status == "ready"`.
- `night` says night vision was on when the event opened. The snapshot and thumbnail then show the night-vision picture. `clip_night_status` is `null` (no night clip), `pending`, `ready` or `failed`. `clip_night_url` is `null` unless it is `ready` (§18).
- `clip_status` is one of `pending | ready | failed`.
- `top_category` is the highest-priority category in the event. Priority: person > animal > motorcycle > bicycle > vehicle.

### 9.3 Endpoints

| Method and path | Request | Response |
|---|---|---|
| `GET /api/status` | none | Status object (below) |
| `GET /api/live.mjpg?overlay=1` | `overlay` 0/1 (default 1) | `multipart/x-mixed-replace; boundary=frame`. 960 px wide, about 12 fps |
| `GET /api/frame.jpg?overlay=0&width=1280` | none | Latest frame as a single JPEG |
| `GET /api/stream` | none | Server-Sent Events (9.4) |
| `GET /api/events?day=&category=&before=&limit=50` | All optional. `before` = start_ts cursor | `{"events":[Event…], "has_more":bool}`, newest first |
| `GET /api/events/{id}` | none | Event plus `"objects":[{"track_id","category","label","first_ts","counted_ts","last_ts","max_score","direction"}]` |
| `DELETE /api/events/{id}` | none | `{"ok":true}` |
| `GET /api/days?limit=60` | none | `{"days":[{"day":"2026-10-30","events":42,"counts":{cat:n}}]}`, newest first, days with activity only |
| `GET /api/stats?range=day\|week\|month&day=YYYY-MM-DD&category=` | `day` = last day of the range (default today); `category` optional, filters the heatmap and busiest hour | Stats object (below) |
| `POST /api/ptz/move` | `{"pan":-1..1,"tilt":-1..1,"speed":0..1}`. Positive pan = right, positive tilt = up | `{"ok":true}`. **Auto-stops after 1 s unless re-sent**; the client repeats every 400 ms while a button is held |
| `POST /api/ptz/stop` | none | `{"ok":true}`. Stops pan, tilt and zoom |
| `POST /api/ptz/zoom` | `{"dir":"in"\|"out"\|"stop","speed":0..1}` | `{"ok":true}`. Same 1 s watchdog |
| `POST /api/ptz/home` | `{"action":"go"\|"set"\|"reset"}` (default `go`). `set` stores the current view in camera preset 200 as Home; `reset` goes back to the camera's own Home | `{"ok":true}`, plus `"home_preset"` for set/reset; `502` if the camera refuses |
| `POST /api/ptz/preset` | `{"action":"call"\|"set","preset":1..254}` | `{"ok":true}` |
| `POST /api/ptz/autotrack` | `{"enabled":bool,"track_id":int\|null}` | `{"ok":true,"autotrack":bool,"target_id":int\|null}` |
| `GET /api/ptz/position` | none | `{"pan":int,"tilt":int,"zoom":int}` or `{"error":"…"}` |
| `GET /api/config` | none | Full config (section 11); camera password masked as `"********"` |
| `PUT /api/config` | Partial config, deep-merged | `{"ok":true,"config":{…},"restarted":bool}`; `400 {"error":"…"}` if invalid |
| `GET /api/models` | none | `{"models":["models/yolov11m.hef","models/yolov8m.hef"],"current":"models/yolov11m.hef","lowlight_models":["models/lowlight/zero_dce.hef","models/lowlight/zero_dce_pp.hef"],"lowlight_current":"models/lowlight/zero_dce.hef"}`. Each file is checked by its metadata (§18). |

Errors are returned as `{"error": "message"}` with a 4xx/5xx status.

**Status object**

```json
{
  "time": 1793405000.1, "uptime": 1234.5, "version": "1.0.0",
  "camera":   {"name": "Front", "online": true, "fps": 14.9, "width": 1280, "height": 720, "last_frame_age": 0.05},
  "detector": {"online": true, "model": "yolov11m", "device": "HAILO10H", "fps": 14.9, "infer_ms": 28.1},
  "recorder": {"online": true, "buffer_seconds": 118.0},
  "ptz":      {"enabled": true, "connected": true, "moving": false, "autotrack": false, "autozoom": true, "target_id": null, "zoom": 0,
               "home_preset": 0, "autofocus": true, "paused_for": 0.0},
  "lowlight": {"mode": "auto", "active": true, "luma": 14.2, "model": "zero_dce", "enhance_ms": 18.6,
               "detect": false, "save_clips": true},
  "system":   {"cpu_percent": 35.2, "cpu_temp": 58.1, "mem_percent": 12.3, "disk_free_gb": 100.2, "media_gb": 1.2, "load": [1.2, 1.0, 0.8]},
  "today":    {"person": 12, "vehicle": 40, "bicycle": 3, "motorcycle": 1, "animal": 2}
}
```

**Stats object**

```json
{
  "range": "day", "day": "2026-10-30", "start_ts": 0, "end_ts": 0, "bucket": "hour",
  "categories": ["person","vehicle","bicycle","motorcycle","animal"],
  "totals": {"person": 12, "vehicle": 40, "bicycle": 3, "motorcycle": 1, "animal": 2},
  "previous_totals": {"person": 10, "vehicle": 35, "bicycle": 1, "motorcycle": 0, "animal": 4},
  "series": [{"ts": 0, "label": "00:00", "counts": {"person": 0, "vehicle": 1, "bicycle": 0, "motorcycle": 0, "animal": 0}}],
  "directions": {"person": {"ltr": 5, "rtl": 6, "approach": 1, "leave": 0, "unknown": 0}},
  "heatmap": [[0, 0, "…24 values"], "…7 rows, Monday first"],
  "busiest": {"label": "17:00", "count": 22}
}
```

- `series` has 24 hourly buckets for `day`, 7 daily buckets for `week` and 30 for `month`. Daily labels look like `"Oct 30"`.
- `previous_totals` covers the same-length period immediately before.
- `heatmap` is 7 rows (Monday to Sunday) × 24 hours of counts over the range.

### 9.4 SSE stream (`GET /api/stream`)

| `event:` | `data:` (JSON) | When |
|---|---|---|
| `status` | Status object | Every 2 s |
| `tracks` | `{"ts":…, "tracks":[{"id":7,"category":"person","label":"person","score":0.83,"box":[x1,y1,x2,y2],"moving":true,"counted":true,"target":false}]}` | About 5 Hz |
| `counts` | `{"person":12,"vehicle":40,…}` (today) | When an object is counted |
| `event_new` | Event object | Event created |
| `event_update` | Event object | Snapshot improved, object joined, or clip ready/failed |
| `event_deleted` | `{"id":12}` | Event deleted |

## 10. Web UI

A single-page app in plain HTML, CSS and JavaScript (ES modules, no build step, no external resources). It is Ring-inspired, dark, mobile-first and responsive: a bottom tab bar on phones and a top bar on desktop. Category colors and inline-SVG icons are used consistently throughout.

1. **Live**
   - 16:9 live MJPEG with boxes. Controls: overlay toggle, fullscreen, LIVE badge with fps, and a camera online/offline state.
   - "Today" counter tiles for the five categories. They update live over SSE and pulse when they increase.
   - PTZ pad:
     - 8-way press-and-hold D-pad with a centre Home button. It sends the move, repeats every 400 ms, and stops on release or when the pointer leaves the button.
     - Zoom −/+, a speed slider, and presets 1–9 (tap to go, "Save" mode to set).
     - Auto-follow switch and auto-zoom switch. Auto-zoom is saved via `PUT /api/config {"ptz":{"autozoom":bool}}`.
     - With auto-follow on, clicking a box in the live view locks the camera onto that object.
     - A status line shows, for example, "Following person #12 · zoom 2.4×" or "Watching (wide)".
   - Recent activity: the six newest events.
2. **History (timeline)**
   - Day navigator (‹ date ›, Today) and filter chips (All plus the five categories, with counts).
   - A 24 h timeline bar with hour ticks, colored event markers and a "now" line. Clicking a marker opens the event.
   - Event cards grouped by hour: thumbnail, time, label chips, a clip-processing indicator and the duration. "Load more" paging.
   - **Event viewer** (modal):
     - The 30 s video, with the snapshot as poster.
     - A snapshot/video toggle and an object list with icons, confidence and direction.
     - Download MP4/JPG, Delete, and prev/next via buttons or ← → keys. Esc closes it.
3. **Stats**
   - Today / 7 days / 30 days.
   - KPI tiles per category: total plus trend versus the previous period.
   - Stacked bar chart with legend toggles and tooltips.
   - Weekday × hour heatmap, direction split, and the busiest hour.
4. **Settings**
   - Per-category enable and sensitivity, minimum movement, and model choice.
   - **Detection-zone editor**: draw a polygon over a live frame.
   - Clip length and pre-roll, retention, and storage cap.
   - Auto-follow tuning.
   - Camera URLs (advanced).
   - System panel: model, device, inference fps/ms, CPU temperature, disk.

## 11. Configuration (`config.json`, edited from Settings)

```json
{
  "camera":  {"name": "Front", "rtsp_url": "rtsp://192.168.1.87:554/1", "http_url": "http://192.168.1.87",
              "username": "admin", "password": "admin", "visca_host": "192.168.1.87", "visca_port": 5678, "visca_address": 1},
  "detect":  {"model": "models/yolov11m.hef", "fps": 15, "width": 1280, "height": 720, "min_move": 0.5, "min_hits": 4,
              "zone": [],
              "categories": {"person": {"enabled": true, "min_score": 0.5}, "vehicle": {"enabled": true, "min_score": 0.5},
                             "bicycle": {"enabled": true, "min_score": 0.45}, "motorcycle": {"enabled": true, "min_score": 0.45},
                             "animal": {"enabled": true, "min_score": 0.45}}},
  "events":  {"clip_seconds": 30, "preroll_seconds": 5},
  "storage": {"data_dir": "data", "segment_dir": "/dev/shm/ptzcam", "segment_seconds": 2, "buffer_seconds": 120,
              "retention_days": 30, "max_storage_gb": 50},
  "ptz":     {"enabled": true, "autotrack": false,
              "autotrack_categories": ["person", "vehicle", "bicycle", "motorcycle", "animal"],
              "deadzone": 0.12, "max_pan_speed": 12, "max_tilt_speed": 10, "return_home_seconds": 15, "home_preset": 0,
              "settle_seconds": 2.0, "autozoom": true, "target_size": 0.35, "max_zoom_pos": 8192, "zoom_speed": 3,
              "autofocus": true},
  "lowlight": {"mode": "auto", "model": "models/lowlight/zero_dce.hef", "detect": false, "save_clips": true,
               "night_luma": 45, "day_luma": 70},
  "web":     {"host": "0.0.0.0", "port": 8080, "live_width": 960, "live_fps": 12, "auth_user": "", "auth_password": "",
              "allowed_hosts": []}
}
```

`lowlight` (§18):
- `mode` is `auto`, `on` or `off`.
- `model` is empty or a night-vision HEF in `models/`.
- `detect` feeds the brightened picture to the detector. It is off because it measured worse.
- `night_luma < day_luma` is the hysteresis.

Changing `camera.rtsp_url`, `detect.model`, `detect.fps`, `detect.width`, `detect.height` or `lowlight.model` restarts the pipeline in-process. `home_preset: 0` means the VISCA Home position.

## 12. File layout

```
ptz_cam/
  ptz_camera_web.md     this plan
  README.md             run / use / troubleshoot
  config.json           runtime config (created on first run)
  requirements.txt      run.sh   ptzcam.service
  models/               yolov11m.hef, yolov8m.hef
  ptzcam/               backend package
    __main__.py config.py camera.py detector.py tracker.py events.py
    recorder.py ptz.py storage.py pipeline.py web.py sysinfo.py
  web/                  frontend
    index.html  css/app.css  js/*.js  img/favicon.svg
  tests/                stdlib unittest: tracker, events, storage, recorder index
  tools/                mock_server.py (UI dev), ui_smoke.py (headless screenshots)
  data/                 runtime: ptzcam.db, media/
```

## 13. Deployment

- `systemd` unit `ptzcam.service`:
  - Runs `User=pi`, `WorkingDirectory=/home/pi/ptz_cam`, `ExecStart=.venv/bin/python -m ptzcam`.
  - `Restart=always`, `HAILORT_LOGGER_PATH=NONE`, enabled at boot.
- The project tree is owned by `pi:pi`. `/dev/h1x-0` is mode 0666 via Hailo's udev rule.
- The RAM buffer is `/run/ptzcam`, created by systemd (`RuntimeDirectory`, preserved across service restarts). It is not `/dev/shm`, because logind's RemoveIPC wipes a user's `/dev/shm` files when that user logs out.
- UI: `http://<pi-ip>:8080`, LAN only. Optional HTTP Basic auth via `web.auth_user` / `web.auth_password`.

## 14. Verification plan

1. **Unit tests** (stdlib `unittest`, no hardware):
   - Tracker: one moving object gives one count; a stationary object gives none; two crossing objects stay two IDs.
   - Event grouping and window logic.
   - Storage stats and deletion keeping counts.
   - Segment time mapping.
2. **Offline end-to-end:**
   - `python -m ptzcam --source <sample.mp4> --no-ptz` loops a sample video as if it were the camera.
   - Assert that events are created, snapshots and thumbs are written, clips become `ready` and play (ffprobe), and stats count the right categories.
3. **Live end-to-end** against the camera: fps ≈ 15, recorder buffer filling, events created from real street activity, clip decodes cleanly.
4. **UI:**
   - Headless Chromium screenshots of every tab at phone and desktop widths.
   - Zero console errors, and the event viewer plays a clip.
5. **PTZ:** a small move then Home, run once. The watchdog stop is checked by sending a move without a stop.
6. **Independent multi-agent review** (backend correctness, frontend, robustness/security). Findings verified, then fixed.
7. **Service:** enable and start, survive a reboot of the service, and stay healthy for several minutes (no memory growth, stable CPU and temperature).

## 15. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Parked cars counted all day | Moving test plus re-anchoring; stationary objects never count |
| Glass reflections and window screen in the view | Detection-zone polygon, per-category thresholds, min hits |
| Cyclist double-counted as person + bicycle | Both are counted (both are true), but they share one event |
| PTZ motion creates fake movement | Counting paused during motion and settle; anchors reset |
| Browser closes mid-move | 1 s server watchdog auto-stop |
| SD-card wear from continuous recording | Ring buffer in RAM; only event clips are written |
| Kernel update breaks the Hailo driver again | `linux-headers-raspi` meta installed, so DKMS rebuilds on each new kernel |
| Camera or network drop | ffmpeg workers reconnect with backoff; UI shows offline; clips without footage are marked `failed` |
| Night scenes | Lower thresholds per category in Settings; IR depends on the camera |

## 16. Work breakdown

1. Environment: driver fix, venv, models, pipeline measurements. **Done.**
2. Backend package `ptzcam/` plus unit tests (lead engineer).
3. Frontend `web/` plus mock server and headless smoke test (parallel agent, built against section 9).
4. Integration on real hardware: offline replay, then the live camera.
5. Multi-agent review, verification and fixes.
6. systemd service, ownership, README, final soak test.

## 17. Build status (2026-10-30)

Built, deployed as `ptzcam.service` on port 8080, and verified on the real hardware.

**Added during the build, at the user's request:**
- *Set current view as Home*. Home is camera preset 200. The camera returns there after auto-follow and when the app starts.
- Autofocus after every move: after settling, at most once per 3 s. The camera rejects one-push AF (`18 01`), so the app sends *Focus Auto* (`38 02`).
- Every tracked object is boxed and labelled on the live view, with still objects drawn quieter. There is an *In view now* strip.
- The status line shows when manual steering has paused auto-follow (10 s). Saving Home resumes it.

**Camera capabilities found:**
- No IR-cut or day/night control: the VISCA `04 01` and `04 51` inquiries are rejected, so there is no night vision.
- Zoom range is 0..16384.
- High preset numbers (200, 201) are accepted.

**Multi-agent review.** 4 lenses (concurrency, counting, PTZ safety, API/security). Every finding was adversarially verified, and every confirmed one is fixed:
- **PTZ safety:** a STOP is sent when PTZ is disabled mid-move. Stops go before recalls. Re-lock only targets moving objects. Home state is known at startup. Zoom-out works over the HTTP fallback. A rejected recall clears `at_home`.
- **Counting:** box-size and position changes must persist before they count as movement, which rejects occlusion glitches on parked cars. Snapshots come from inside the event window. Directions are not taken from camera motion. Per-category thresholds take effect below 0.35. Event labels stay consistent.
- **Robustness:** the recorder re-creates its buffer dir and the service uses `/run/ptzcam`. Each pipeline loop has its own stop event. Track ids are unique per process. The live view shows "offline" instead of a frozen frame. Failed restarts roll the settings back. The config is written before memory changes.
- **Security:**
  - A Host allow-list (IP literals, bare and local names, plus `web.allowed_hosts`) blocks DNS rebinding.
  - The Origin and `Sec-Fetch-Site` check blocks CSRF.
  - Request bodies must be JSON.
  - UI assets are served from versioned, immutable URLs. Stale cached modules caused the first "Set Home does nothing" report.

**Verification:**
- 21 unit tests.
- Offline evaluation on a dense pedestrian video: stable ids, parked cars never counted.
- 25 API integration checks.
- PTZ hardware tests: move, watchdog stop, zoom, Home, preset 200/201, and the auto-follow pan → zoom → return-home loop.
- 68 headless-browser UI checks (phone, desktop, 360 px) against the live service, with zero console errors.
- Soak tests:
  - 30 min live: memory flat at about 160 MB, 15 fps, 0 errors, 8 real night-time cars captured with 30 s clips.
  - 10 min under systemd.

## 18. Night vision and second review (2026-10-30, night)

**Goal.** Make the camera useful in the dark. The user asked whether Zero-DCE could help, then asked for the Zero-DCE picture to feed the live stream and to be saved as the photo and video.

**What the night scene looks like** (measured at 21:00):
- Mean brightness is 12–20 of 255.
- The camera looks through window glass with a fly screen in front, so room-light reflections show in the glass.
- Temporal noise is about 1 level, so the image is clean but very dark.
- Focus was checked on clean clip frames and is consistent all evening. An earlier "blur" reading was an artefact of the boxes drawn on snapshots.

**Camera-side options, all dead ends:**
- `param.cgi?post_image_value&bright&N` (0–14; the user had it at 9) is a black-level **offset**. Each two steps add about 9 levels to both dark and bright pixels, and noise stays the same, so it adds no exposure.
- The web UI has no exposure, gain or shutter settings.
- The IR-cut and day/night VISCA commands are rejected (§17).
- The camera's HTTP replies put a debug line before the status line, so strict HTTP clients reject them.
- Brightness was restored to the user's 9.

**Zero-DCE on the Hailo.**
- The Hailo Model Zoo v5.3.0 ships `zero_dce.hef` and `zero_dce_pp.hef` for hailo10h. Both take RGB uint8 at 600×400 and return the enhanced RGB image. All eight curve iterations run on-chip, and the output scale is 1/255.
- They live in `models/lowlight/`. `hef_kind()` reads HEF metadata to tell detectors (`HAILO_NMS_BY_CLASS`) from enhancers (same-shape image out). Config validation and `/api/models` use it, so an enhancer can never be picked as the detector. That was review finding 1, caused by the download.
- Speed on the HAT, shared with YOLO through the scheduler:

  | Model | Per frame | YOLO latency |
  |---|---|---|
  | Zero-DCE | ~19 ms | 27.5 → ~31 ms |
  | Zero-DCE++ | ~27 ms (slower) | |

  Zero-DCE is the default.

**Should the detector see the enhanced picture? No (measured).** `tools/eval_lowlight.py` runs the production detector, tracker and counting rules on four inputs per frame.

On the 28 night event clips (8 s each, 15 fps; these clips are biased toward cars the raw detector already found):

| Input | Detections ≥0.35 | Counted |
|---|---|---|
| raw | 253 | **17** cars |
| classical (gamma + CLAHE) | 195 | 9 |
| Zero-DCE | 80 | 2 |
| Zero-DCE++ | 125 | 2 |

On 20 min of continuous night footage (3 fps, unbiased), detections ≥0.35 were raw 19, classical 16, Zero-DCE 1 and Zero-DCE++ 17. All 13 of Zero-DCE++'s "new" detections were false: "person" on a lamp reflection and "animal" on a white post.

Cause: enhancement amplifies the fly-screen mesh, the noise and the street-light cast into texture, and the headlight cues get lost. So `lowlight.detect` defaults to false, and Zero-DCE drives only what people see.

**Pipeline at night** (`lowlight.LightMeter` / `NightView`, `detector.HailoEnhancer`, `nightclip.NightClipper`):
1. **Night or day.** A strided mean of each frame is smoothed with τ = 8 s, with hysteresis: night below 45, day above 70. Modes are auto, on and off.
2. **Enhance.** Zero-DCE runs on the frame squeezed to 600×400, on the Hailo.
3. **NightView** (about 11 ms of CPU), at the model's resolution:
   - A partial grey-world white balance (strength 0.85) and a 0.5–99.7 % contrast stretch (at most 3×), in one LUT that adapts at 5 % per frame.
   - 40 % of the colour kept.
   - A 3×3 box blur against amplified speckle and compression blocks. It costs 1 ms; a bilateral filter cost 10 ms and looked no better.
   - A motion-adaptive temporal denoise:
     - Motion is a quarter-resolution difference above 12, upscaled and dilated 9×9.
     - Still pixels keep 85 % history.
     - Measured on real Zero-DCE sequences, it cuts frame-to-frame noise in still areas by about half (6.1 → 3.9).
   - Output is **712×400** (16:9). Upscaling adds no detail, and the detail layer that was tried only added grain.
   - The model's input is an exact 2× `INTER_AREA` halving (1.1 ms) followed by a linear resize. A direct non-integer `INTER_AREA` to 600×400 cost 9 ms.
   - The app runs OpenCV single-threaded (`cv2.setNumThreads(1)`). On these small images the default 4-thread pool burned up to twice the CPU for the same wall time, for example NightView at 20 ms of CPU against 11 ms.
4. **Uses of the night view:** the live MJPEG (upscaled by the stream), event snapshots and thumbnails, and night clips.
5. **Night clips.** A worker JPEG-encodes every night frame into a RAM ring holding the pre-roll plus pending event windows. When a window closes, its frames go through x264 in one burst (veryfast, CRF 28, 2 threads, `nice 10`, constant frame rate on the wall clock) into `clip_night.mp4` next to the camera's stream-copied `clip.mp4`. With the denoised view, that is about 1.6 MB per 30 s for about 9 s of CPU. The first live night clip, made before the denoise was tuned and at ultrafast CRF 26, was 14.5 MB. Measured x264 cost for 30 s at 720p15: ultrafast 13 s of CPU, veryfast 26 s. Continuous encoding would cost about 0.44 of a core all night, which this fanless Pi cannot spare.
6. **Data.** Events gain `night`, `clip_night` and `clip_night_status`, migrated in place on old databases. Pending night clips become `failed` at startup, because the ring lived in RAM. The API adds `night`, `clip_night_url` and `clip_night_status`.
7. **UI.**
   - A 🌙 *Night vision* badge on the live video.
   - A moon on night event cards.
   - The viewer plays the night clip by default; a *Night vision* toggle (remembered per browser) switches to the original, and the download follows.
   - A Settings card with mode, model, *Detect on the brightened picture*, *Save night-vision clips*, and a live line (on/off, scene brightness, ms per frame).

**Thermal and CPU.** The Pi has no fan. A 14-minute A/B test of the first version:

| Night vision | CPU | Temperature | Throttle or soft-limit flag |
|---|---|---|---|
| off | 28.6 % | 79.5 °C, still falling | 7 of 21 samples |
| on | 38.5 % | 82.5 °C | 20 of 21 samples |

A CPU-time profile then found that OpenCV's default 4-thread pool nearly doubled the CPU spent on these small images. After `cv2.setNumThreads(1)` app-wide, the 2× `INTER_AREA` input path and quarter-resolution motion detection, a clean 9-minute run **with night vision on** measured about 29 % CPU and 78–81 °C (mostly about 79.5), with no throttle flags, at 15.0–15.6 fps. That is what the app used before night vision existed. The on-demand design (enhance only while someone watches or an event is open) was therefore not needed. An active cooler is still recommended.

**Second code review** (`/code-review high`, whole project). 10 findings, all fixed:
1. Enhancer HEFs offered as detector models. Fixed by `models/lowlight/` and metadata checks.
2. With PTZ disabled, `at_home` stayed false, so nothing was counted when a zone was drawn. The view is now assumed to be Home when the app never moves the camera.
3. `in_zone` was set from image positions while the camera was away or moving. Now it is only set while counting is allowed.
4. Auto-follow picked objects that could never become events (disabled category, below `min_score`, outside the zone), which stopped counting for nothing.
5. Re-enabling PTZ did not restore auto-follow. Fixed.
6. `user_locked` was never read. A target the user picked is now never swapped for another object; it is given up only at return-home.
7. VISCA reply matching. Probed: this camera ACKs on socket 1 or 2 and completes about 1 ms later on the same socket, even for preset recalls; inquiries answer on socket 0; a stop while idle gets no reply. A completion or error must now carry the socket of its own ACK, and inquiries accept only socket-0 data replies, so stale replies are skipped. Silence still means sent.
8. The clip callback crashed for a deleted event. Fixed in `_media_done`.
9. `LiveView.jpeg` ignored `imencode`'s ok flag. Fixed.
10. The isotropic geometry was duplicated in the tracker. It is now one `iso()` helper.

**Verification.**
- 39 unit tests, up from 21:
  - `test_night`: meter hysteresis; the NightView cast, grain and no-ghosting; a real ffmpeg night clip with pre-roll; too little footage; DB migration; night events; deleted-event cleanup.
  - `test_autotrack`: only countable objects are followed; a user's pick is never swapped.
  - `test_visca`: stale replies are skipped, own-socket errors raise, silence counts as sent.
  - `test_config`: lowlight rules and model kinds.
  - One more `test_events` case: the zone is ignored while the camera is away.
- A smoke run of the new backend on a scratch port with a copy of the production DB.
- Headless browser against the live service: phone 36/36, desktop 37/37, 360 px 5/5 checks, with 0 console or page errors. It covers the night badge, the Settings card and status line, the viewer playing the night clip and toggling to the original, and the toggle fitting the viewer panel (it first overflowed, and was fixed).
- Live:
  - Night vision active, 15.2 fps, Zero-DCE about 18–22 ms per frame.
  - Real night events 31–33 each got `clip_night.mp4`: 29.9 s, 443–450 frames, 2.0–2.8 MB once tuned.
  - App memory steady at about 200 MB.

## 19. Zooming in on people (2026-10-31)

The user found the camera rarely zoomed in on people, and asked for it to zoom in whenever a person is detected, so footage can help the police.

**Diagnosis** from the logs, a 17-minute recording of the live track stream plus PTZ status, and event clips:
- **Futile car chases.** Since 2026-10-29: 242 car follows, 18 person follows. Passing cars cross the Home view in about 1.5 s (0.4–0.86 frame widths per second), so the camera only starts turning as they leave. Each chase also froze counting for about 9 s (follow, 3 s lost, home travel, 2 s settle), so people walking by meanwhile could be missed.
- **People lost as soon as the camera moved.** Each follow ended 1–5 s after it started:
  - People on the far sidewalk sit at the top of the Home view, so the camera tilts a long way. The tracker loses them to motion blur and the window bars, then re-detects them under a new id.
  - That new track cannot count as moving while the camera moves, and re-lock only accepted moving tracks, so the camera gave up. The same person was then often counted again.
  - Riders flip between person and bicycle detections.
- **Zoom too fast and too eager.** Zoom-in started within ±0.2 of centre while the pan still lagged a walking person. Speed 7 roughly doubles the picture in half a second, about the picture's lag, so one zoom pushed a person out of frame. In the baseline, 3 of 4 person follows got no zoom at all.

**Changes** (`ptzcam/ptz.py`, `Track.inherit`): see §6.1.

**Simulation** (`tools/sim_autotrack.py`): 54 combinations of pan speed per step (0.5–2 °/s), lens speed, lag (0.3–0.6 s) and walking speed (1.5–6 °/s).

| | Old | New |
|---|---|---|
| Person kept in frame | 84 %; lost in 22 of 54 cases | 100 %; lost in 0 |
| Average distance from centre | 0.29 | 0.10 |
| Median zoomed height (target 0.61) | 0.34 | 0.52 |
| Zoom reversals, median / worst | 9 / 36 | 0 / 7 |

**Live fixes after the first deployment:**
- A cyclist's person detection flickered for a frame, and re-lock jumped to the bike and back. Re-lock now waits until the target has been missing for 0.3 s, and a switch to something right where the target was keeps the learned speed.
- A lost minivan was "re-found" as two other cars coming into view while the camera panned, and the camera turned until it pointed into the room. Now only people, bicycles and animals can be re-found as new detections, at a similar size, and only they coast.
- Cars entering from the frame edge were picked while their velocity estimate still lagged. Speed is now the larger of the velocity and the average since the object appeared, and the limit is 0.25 (measured at Home: people 0.05–0.17).

## 20. Washed-out mornings: auto brightness (2026-10-31)

**Symptom.** At 09:08 the live picture was white: mean 253 of 255, 99–100 % of pixels clipped. Counting stopped at 08:43.

**Timeline** from event photos. Mean luma climbed steadily with the daylight instead of holding level:

| Time | 06:00 | 06:41 | 07:16 | 07:52 | 08:43 |
|---|---|---|---|---|---|
| Mean luma | 95 | 171 | 208 | 244 | 250 |

**What was tried:**
- **VISCA exposure inquiries** (AE mode, shutter, iris, gain, bright, exposure compensation, backlight, WDR): all refused with syntax error `90 60 02 FF`.
- **Web settings:** the `param.cgi` image settings are bright, saturation, contrast, sharpness and hue only. There is no hi3510 CGI (404).
- **Brightness:** `bright` 9 → 0 moved the white picture from 253 to 249, flat, with no detail.
- **Contrast:** `contrast` 0 gave a uniform 4, and 14 a uniform 252, with zero variance. The camera was sending a flat image.
- **Camera restart** (`param.cgi?post_reboot`, the web page's Reboot button, approved by the user): back in 6 s, still white. Settings were restored to bright 9 and contrast 8.

The camera's exposure cannot cope with direct sun on its window, and nothing reachable controls it. The fix for full white-out is physical shading.

**`lowlight.AutoLevels`** corrects washed-out frames by day:
- It applies when the darkest parts are at least 40 bright.
- It stretches the 0.5–99.5 % luma percentiles to 12–243, at most ×4, with one LUT for all channels.
- It uses NightView's adaptation rate.
- A picture squeezed into fewer than 12 levels is reported as **blinded** and left alone.
- It costs about 4 ms per 1280×720 frame.

Status gains `brightness {auto, detect, gain, white, blinded}`. The live view shows a badge (×gain, or "Too bright: camera blinded"), and Settings has an Auto brightness card.

**Detector input**, measured with `tools/eval_lowlight.py --variants raw,levels` on 18 event clips from 07:00–08:15 (7,622 frames, mean luma 214):

| | Detections ≥ 0.5 | Counted |
|---|---|---|
| Raw | 1,938 | 85 vehicles, 10 people |
| Levels | 2,318 (+20 %) | 85 vehicles, 9 people, 1 dog |

- The dog is the morning walker's dog, which the live log also counted.
- Almost all "lost" detections were one repeated false "vehicle" on a road marking.
- So unlike Zero-DCE at night, `brightness.detect` defaults to on.

**Zoom overshoot, follow-up (same day).** The user reported that auto-zoom always overshot, and asked whether YOLO could help. It can: by measuring rather than steering (see §6.1).

Simulation (`tools/sim_autotrack.py`): 162 combinations, adding lens speed (4,000–16,000 positions/s) and lenses that don't match the assumed model (8×, 12× and 20×), with a position readout lagging 0.6 s.

| | Worst overshoot | Cases over 1.2× | Time to size, median |
|---|---|---|---|
| 07:48 continuous version | 1.23× | 2 | 9.9 s, 37 never |
| One step, assumed 12× model | 1.45× | 46 | 3.3 s |
| One step, measured curve | 1.00× | 0 | 3.3 s, all at size when the camera keeps up |

The lens must therefore be measured. At pan speed k = 1 the measured version lands on the target size (median 0.61) with 0–2 zoom reversals. The `zoom_speed` setting is gone, because zoom direct runs at the camera's own speed.

## 21. Daylight white-out, continued (2026-10-31, afternoon)

**Clock.**
- The Pi runs `America/Los_Angeles`. The user is 2 hours ahead (UTC−5 that day).
- Times in §17–§21 are Pi time. For the user, the white-out of §20 was 10:43–13:08: midday, not morning.
- The web UI formats event times in the browser. The day folders (`storage.local_day`) and hourly stats use the Pi's zone.

**Every remaining exposure route was probed** at the user's request. All are dead ends:
- **VISCA exposure set commands**, all refused with `90 60 02 FF` and no change to the picture:
  - AE full auto, shutter priority, manual and bright (`01 04 39 00/0A/03/0D`);
  - exposure compensation (`3E 02/03`);
  - WD (`3D 02`);
  - the OSD menu (`01 06 06 02`, `06 10`).
- **ONVIF** (`:81/onvif/device_service`, found by WS-Discovery; scopes `hardware/Hi35xx` and `type/sample`):
  - GetServices lists an imaging service, but GetImagingSettings, GetOptions and GetStatus return `ActionNotSupported`.
  - The Imaging block in Media GetVideoSources holds placeholder values 0…23.
- **USB (UVC 1.00)**, connected by the user:
  - Controls: brightness, contrast, saturation, hue, gamma, sharpness, backlight compensation, **power_line_frequency** (it was 50 Hz; its default is off) and pan/tilt/zoom/focus. There are no exposure controls.
  - Video: YUYV only, 640×360@60 to 1920×1080@30, with no audio. The app stays on RTSP and VISCA over the LAN.

**Anti-flicker hypothesis.**
- HiSilicon's normal anti-flicker mode holds exposure to multiples of the flicker period, which can over-expose bright scenes.
- The evidence: from 15:28 to 15:52 the picture's mean fell from about 160 to 66 as the daylight faded. Its exposure was pinned at its floor.
- Toggling 50 Hz, off and 60 Hz at 15:58 made no difference, because the exposure wasn't pinned in that light.
- `power_line_frequency` was set to **off at 16:00** for a full-day test. `tools/uvc_controls.py` reads and sets it.

**Reliability.**
- The camera dropped off the network at 11:06, 11:09 and about 11:17, for 10–75 s each. The Pi's link stayed up.
- The 11:17 dropout interrupted the first lens calibration after one step (×1.10). It wrote nothing.
- The camera's saved brightness was found at 0 (7 the day before), with `ldc` missing from `get_image_conf`. That is consistent with a Save on the camera's own image page. The live value was set back to 9.

Lessons from the whole build are collected in `LESSONS_LEARNED.md`.

## 22. Two cameras: the eye steers the PTZ (2026-11-02)

**Goal (the user's).** The fixed USB wide camera (Avaya HC020) beside the PTZ camera is the "eye": it helps the PTZ, the two cameras are synced, and it becomes the main camera while the PTZ is washed out.

**Measured first:**
- **Avaya over UVC:**
  - H.264 and MJPEG up to 3840×2160@30; YUYV up to 1080p.
  - 1080p has the same field of view as 4K (scale 1.000).
  - H.264 1080p15 has a fixed 30-frame GOP (keyframe every 2.0 s), and its 2 s segments decode on their own.
  - Decoding 1080p15 to 1280×720 costs about 0.3 core; 4K decode is impossible.
  - Digital zoom reaches at most 1.77×, centred, and reacts in about 0.4 s.
  - Auto exposure only: manual exposure is refused with EINVAL.
- **The Unite accepts absolute pan/tilt** (`81 01 06 02`, 4+4 nibbles). Home is pan 0, tilt −431, at the lower tilt limit of −432. 300 units take 1.7 s at speed 0x10.
- **Hailo:**
  - Separate bindings per thread: 36.6 fps from one thread, 70.4 from two, 52.5 together with Zero-DCE.
  - `hailortcli run2`: yolov11m alone 70 fps; with zero_dce, 52 + 48 fps.

**Design:**
- **Per camera:** each camera is a `CameraUnit` (`pipeline.py`) running in its own thread, with its own frames, ring, tracker and picture state.
- **Shared:** the detector (one lane per thread; it is only closed when no thread is inside it), the events, PTZ control, `AutoTracker` and `MainSelector` (`failover.py`).
- **Eye capture** (`usbcam.py`): one ffmpeg reads the Avaya and writes two outputs, the ring at `/run/ptzcam/eye` and the frames. The recorder has a passive mode.
- **Sync** (`sync.py`, `syncal.py`, `tools/calibrate_sync.py`, `data/sync.json`):
  - A daylight sweep of absolute pan/tilt positions, matched with SIFT, CLAHE and a RANSAC homography.
  - The fit is a polynomial for eye → pan/tilt and back, plus the PTZ's footprint in the eye.
  - Pan and tilt are clamped to the calibrated range.
- **Eye mode in `AutoTracker`** (`update_eye`):
  - Targets come from eye tracks: people first, inside the zone that counts (the eye's own, or the PTZ's Home view and zone mapped into the eye's picture: `AutoTracker.eye_zone`, set by the pipeline), and a slew limit taken from the map's Jacobian.
  - A lost person, bicycle or animal (locked by the user or not) is found again as a new eye track of the same kind that appears near where it was heading; it inherits the count and event.
  - Idle away from Home (after manual control, a mode switch or a restart), it goes Home after `return_home_seconds`, as PTZ mode does.
  - The lead is the eye's lag (0.15 s) plus an arrival time of 0.6 s. Aims are re-sent every 0.4 s at a speed that arrives in 0.6 s (smooth pursuit); a new target is swung to at full speed.
  - Zoom is one step from the eye's median box height, capped so the drift between aims stays within 0.3 of the field, and capped beyond the calibrated range (below the lowest tilt, above the highest, past the pan range) so the target still fits. How far off the camera is, and so whether to swing, is measured to the clamped goal: a target past a limit is not re-aimed every frame.
  - Refinement (`update` in eye mode) compares each PTZ detection with the pose the late picture shows (a pose history). It runs only while the eye saw the target within 0.3 s, extrapolated to the PTZ picture's moment. The correction eases towards the measured map error, with a gain of 0.4 and at most half the Home field. The matched PTZ track joins the eye's event, so the close-up becomes the photo.
  - A click on the PTZ view is placed in the eye's picture through the pose that picture shows (the history, less the learned correction), or the current pose once the camera has been still for the picture's lag; the current target is kept until then.
  - It falls back to PTZ-only following when there is no map, the eye is offline or blinded, or VISCA is down.
- **Counting:** the eye counts when it is enabled, online and not blinded, and has a zone: its own, or the PTZ zone or Home footprint mapped through the sync. The mapping needs the PTZ's Home pose, read at Home or recorded by the calibration (`meta.ptz_home`). While the PTZ is washed out or offline the eye counts its whole view. On a handover, the new counter's tracks that match the old counter's counted ones are marked counted; everything else still counts. Each camera's counted tracks are remembered for 30 s, and "recent" is judged from when the old counter last worked: its last picture if it went offline, or its last detection (at most 10 s before the flag) if it whited out, since detections fade before the flags trip. Matching is by position through the sync while the PTZ is still and the memory fresh, else by number per category among tracks the new counter could count (moving, enabled, in its zone; longest seen first). `tracks.camera` records which camera counted.
- **Start-up:** the main camera is not re-decided until both cameras have sent a picture (at most 15 s), so a camera still connecting is not an outage.
- **Photos:** while one camera counts and the other is main, the main camera's tracks are matched to the event's tracks by position through the sync (`_link_to_counter`) and join the event, never counted again. The main camera's picture then replaces the other camera's photo at once and is never replaced by it. A photo taken from an unusable (washed-out) picture gives way to any usable one. Without the sync, when the PTZ counts and the wide camera is main, the wide camera's own picture of the moment is the photo (its view holds the PTZ's); the PTZ's view need not show what the wide camera counted, so then the counter's own picture stays.
- **Eye-follow while the PTZ counts** (eye.count off, or no eye zone yet): the eye's tracks are linked to the PTZ's counted ones through the sync, and only those are followed. The eye sees people half a second before the PTZ does; swinging away first left them uncounted.
- **Failover:**
  - The PTZ blinded for 3 s, or ≥ 50 % white for 20 s, or offline, hands over to the eye if the eye is less white.
  - It is clear again after 120 s below half the threshold.
  - A switch is held for at least 30 s unless a camera goes offline.
  - Main decides the live view (`?camera=`), the event photos, the default clip, and Zero-DCE. While the eye is main, PTZ frames make photos only while the PTZ picture is clear (`MainSelector.ptz_clear`).
  - `reason` is recomputed every second ("PTZ camera", "PTZ picture washed out", "PTZ picture not clear yet", "PTZ picture clearing up", "… offline", "set to …"); `history` keeps why each switch happened.
- **Clips:** `clip.mp4` stays the PTZ's. `clip_wide.mp4` comes from the eye ring. With `eye.clips` "auto" it is kept when, during the event, the PTZ moved, was away from Home or offline, or the eye was main (`events.clip_wide_keep`, set once a second from whichever camera delivers frames), when the event's main camera was the eye, or when the PTZ clip failed. It is dropped only once the PTZ clip is ready, by whichever clip finishes last, so the decision survives a restart.
- **Status for the web UI:** `counting_note` says why the eye does not count (off, offline, blinded, no sync or zone, Home not read yet); `sync.scheduled` is tools/sync_when_clear.py's progress (`data/sync_pending.json`: waiting with minutes clear so far, syncing, or failed with the reason, kept a day); the SSE `main` event carries the switch time (`since`), and the page keeps a switch until a status built after it arrives. Settings shows all three and offers the unattended sync command.
- **Eye capture:** the frame pipe uses the camera's own clock (`-enc_time_base -1`). The Avaya's frame times jitter (52, 68 and 80 ms apart), and on the default 1/15 s encoder clock two could share a tick: the muxer then dropped the second, up to a third of the frames at times, each with a "non monotonically increasing dts" error.
- **Other:** eye track ids start at 2⁴⁰ (`ptz.EYE_ID_BASE`; the PTZ tracker uses a few thousand ids a day). The eye's anti-flicker applies at once, and it and the eye's crop (`meta.eye_eptz`) are set on every connect and checked every 30 s. Camera names in the status come from the settings.

**Review (2026-11-02 evening).** An adversarial review of the backend found 32 problems; independent verification confirmed all 32 (2 high). Each fix has a test that fails on the code before it. A second review of those fixes found 19 more (regressions and incomplete fixes: a stop racing an absolute-move send, re-finding the wrong person, a click compared at different moments, handovers missing what the old counter counted, photos from a view that need not show the object); each reproduced, was fixed and has a test. See LESSONS_LEARNED.md §7.

**Simulation** (`tools/sim_autotrack.py --eye --calibrated`): 108 cases, with a physically late PTZ picture.

| Mode | In frame | Worst overshoot | Zoom reversals (max) | Time to size, median |
|---|---|---|---|---|
| Eye mode | 100 % | 1.00× | 1 | 2.7 s |

With a 1° map error, centring error was 0.01–0.02 of the frame with refinement, against 0.06–0.11 without.

Lessons: LESSONS_LEARNED.md §7.
