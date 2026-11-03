# PTZ Cam: local AI object-tracking camera

A Ring-style camera app for the PTZ camera at `192.168.1.87`. It runs entirely on this Raspberry Pi 5 and its Hailo-10H AI HAT: detection, tracking, recording and the web page. Nothing is sent to the cloud.

**Open it:** `http://192.168.1.88:8080` (or `http://rpi5-ai.local:8080`) from any phone or computer on your network.

- Why things are the way they are, and what went wrong on the way: [LESSONS_LEARNED.md](LESSONS_LEARNED.md).
- The design and build log: [ptz_camera_web.md](ptz_camera_web.md).

## What it does

- **Live**: a live view with every tracked object boxed and labelled, an **In view now** strip, and today's counts for people, vehicles, bicycles, motorcycles and animals.
  - PTZ pad: hold an arrow to move, zoom −/+, Home, and presets 1–9.
  - **Set current view as Home**: the camera returns to this view after following, and when the app starts.
  - **Auto-follow**: the camera pans, tilts and zooms to keep an object in frame, then returns Home when it has gone. **People come first**: a person takes over from any other target and is zoomed in on, and the event photo is replaced by the close-up. Cars that cross the view faster than the camera can turn are not chased.
  - Click any box to make the camera follow that object.
  - **Auto-focus after moves**: refocuses once the camera settles.
- **History**: a 24 h timeline and Ring-style event cards. Every event has a snapshot and a **30 s clip**, including 5 s from before the object appeared.
- **Stats**: totals and trends, an hourly or daily chart, a weekday × hour heatmap, and which direction things travel.
- **Night vision**: when it gets dark, the AI HAT brightens the picture (Zero-DCE). The live view and event photos show the brightened picture, and each night event also gets a **night-vision clip** next to the camera's original.
- **Auto brightness**: by day, a washed-out picture is stretched back to full contrast. A badge shows when the camera is blinded by the sun.
- **Settings**: categories and sensitivity, a detection zone you draw on the image, night vision, auto brightness, clip length, retention, auto-follow tuning, and camera addresses.

## How counting works

- Each object is tracked with a stable ID and counted **once**, when it has actually moved: at least half its own size, or a clear change in size as it approaches or leaves.
- Parked cars and other stationary things are never counted.
- Objects counted within the same 30 s window share one event. A cyclist, for example, is one event tagged Person + Bicycle.
- Deleting an event deletes its video and snapshot, but **keeps** it in the statistics.

**Auto-follow tradeoff.** While the camera moves, counting pauses, because camera motion would look like object motion. Counting resumes 2 s after the camera settles. For the most accurate stats, leave auto-follow off. For close-up clips of each visitor, turn it on.

**Steering by hand pauses auto-follow for 10 s.** The status line under the video says so ("paused while you steer"), so the camera never fights you. Saving a Home view ends the pause at once.

## Auto-follow

- **Target size** is the share of the frame height auto-zoom aims for. Use 50–60 % for close-ups of people that show faces and clothing.
- **Zoom in one step.** Steering the zoom by watching the picture always overshot, because the picture arrives about half a second late and the lens doubles the picture in about that time. Instead, once the target is centred and steady and the lens is at rest, the app measures the target's height, works out the zoom position from the lens curve, and sends one absolute zoom command. The camera stops exactly there; the app measures again and corrects if needed.
- **Lens curve.** It is measured once with `tools/calibrate_zoom.py` (see *Development*). **It has not been measured yet.** The first try, on 31 October, stopped safely when the camera dropped off the network. Until then an assumed 12× lens is used, which can overshoot if the real lens differs.
- **Lost target.** If another moving object is in view, the camera follows it. Otherwise a lost vehicle sends the camera Home at once, while a lost person is looked for until *Return home after*, at the Home zoom so the view is wide.
- To keep the camera on people only, untick the other categories under *Follow these categories*. The camera then stays at Home, counting, until someone walks by.
- The window's bars and mesh limit the close-ups: a person walking behind a bar disappears for a moment. The camera keeps turning at their walking speed and picks them up on the other side.

## Bright daylight (white-out)

This is a conference-room camera (ClearOne UNITE 200). In bright daylight its automatic exposure can't turn down far enough, and the picture fades to white. On 31 October it was completely white from 10:43 to 13:08 and washed out for much of the afternoon. While it is white, nothing can be detected or counted.

**Nothing on the network can change its exposure.** All of these were tried:
- The VISCA exposure commands (automatic, manual, shutter priority, bright mode, exposure compensation, backlight and WDR): every one is refused.
- The camera's web page: it only offers brightness, contrast, colour and sharpness, and brightness just lifts the dark levels.
- ONVIF: it lists an imaging service, but every exposure request answers "not implemented".
- The on-screen menu command: refused.
- Lowering brightness or contrast only turns the white into flat grey or black. Restarting the camera doesn't help.

**Auto brightness.** The app stretches what is left of a washed-out picture back to full contrast, for the live view, photos and detection.
- On 18 hazy clips this found 20 % more objects with the same vehicle count, and a repeated false "vehicle" on a road marking disappeared.
- The camera's own clips are recorded straight from it, so they stay as the camera saw them.
- Settings → *Auto brightness* turns it off, or keeps detection on the original picture.

**When the picture is completely white, nothing is left to recover.** The live view then shows *Too bright: camera blinded*. Shade the camera from direct sun.

**Anti-flicker test (USB cable).** The one exposure-related setting this camera offers is anti-flicker, and only over its USB port.
- It was set to 50 Hz. Anti-flicker stops mains lighting from flickering on video by keeping the shutter at 1/100 s or slower. That is fine indoors, but in sunshine it can let in far too much light.
- To test whether it causes the white-out, it was switched **off** on 31 October at 18:00. If bright days still turn white, it isn't the cause. If night pictures start to flicker under street lights, set it back to 50 Hz.
- On the morning of 1 November the camera restarted (its USB connection dropped and came back), and the setting went back to 50 Hz, so that day was not a test.
- Keep the camera's USB cable plugged into the Pi. The network cable is still needed for everything else (see *Camera notes*).
- Check or change it as `pi`, from this folder. A camera restart puts it back to 50 Hz. `--camera unite` makes sure it is this camera, not another USB camera.
  ```bash
  .venv/bin/python tools/uvc_controls.py --camera unite get power_line_frequency     # 0 = off, 1 = 50 Hz, 2 = 60 Hz
  .venv/bin/python tools/uvc_controls.py --camera unite set power_line_frequency=0   # off (1 = back to 50 Hz)
  ```

## Two cameras (a wide camera beside the PTZ)

A fixed USB wide camera (here an Avaya HC020, mounted beside the PTZ camera at the same window) can work with the PTZ camera. The app calls it the *eye*:

- **It counts.** Its view never moves, so counting never pauses while the PTZ follows someone. It counts in your PTZ zone (or the PTZ's whole Home view) mapped into its picture, so the counted area stays the same.
- **It steers the PTZ.** Once the two cameras are synced, the wide camera picks who to follow (people first). It points the PTZ straight at them with exact pan/tilt commands and zooms in one step from the size it measures. The PTZ's own picture then fine-tunes the aim. The PTZ no longer chases through its own picture, which lags half a second.
- **It takes over when the PTZ is washed out.** When the sun blinds the PTZ (or it goes offline), the live view, the event photos and the default clips come from the wide camera. They switch back once the PTZ picture has been clear for 2 minutes. The live view says which camera is showing and why.
- **Clips.** Every event keeps the PTZ's clip. With *Wide clips: auto*, the wide camera's clip is kept only when the PTZ followed someone during the event or the wide camera was the main camera. Otherwise it adds nothing and costs storage.

**Setting it up**

1. Connect the wide camera's USB cable to one of the Pi's blue USB 3 ports, and fit the Pi 5 Active Cooler first. A second video stream adds about a third of a CPU core, and the fanless Pi already runs near 80 °C.
2. Check the Pi sees it: `.venv/bin/python tools/uvc_controls.py --camera avaya`.
3. Settings → *Wide camera* → turn it on. The app restarts with both cameras.
4. Sync the cameras once, **in daylight** (both must see the street clearly; at night their pictures cannot be matched). This takes about 3 minutes while the PTZ moves over a grid. It also measures the PTZ's zoom curve if that was never done.
   ```bash
   sudo systemctl stop ptzcam
   .venv/bin/python tools/calibrate_sync.py
   sudo systemctl start ptzcam
   ```
   Re-run it whenever either camera is moved or bumped. Until it has run, the wide camera still takes over when the PTZ is washed out, and the PTZ follows from its own picture as before.

**Settings**

| Setting | What it does |
|---|---|
| *Count on the wide camera* (`eye.count`) | Counting comes from the wide camera when it can (needs the sync, or a zone drawn on the wide picture) |
| *Wide camera steers the PTZ* (`eye.follow`) | The wide camera picks targets and aims the PTZ (needs the sync) |
| *Wide clips* (`eye.clips`) | `auto`, `always` or `never` |
| *Anti-flicker* (`eye.anti_flicker`) | Put back on every reconnect. Off helps in sunshine (see *Bright daylight*) |
| *Failover* (`failover.mode`) | `auto` (the PTZ unless it is washed out or offline), or always the PTZ / the wide camera |
| *Washed out at* / *after* / *back after* | How white (50 %), for how long (20 s) before switching, and how long clear (120 s) before switching back |

**Limits**

- The wide camera's zoom is digital only (at most 1.8×). Detail on distant people still comes from the PTZ.
- Only one program can use a USB camera's video at a time. The app holds it while the wide camera is on.
- When the wide camera counts, the *direction* (left to right and so on) comes from its view.

## Night vision

This camera has no infrared mode, so night vision is done in software, on the AI HAT:

- **When:** automatically when the scene gets dark (scene brightness below 45 of 255; off again above 70, so passing headlights don't flip it). You can also set it to *Always* or *Never* in Settings → Night vision.
- **How:** [Zero-DCE](https://github.com/Li-Chongyi/Zero-DCE) learns a brightening curve for every pixel, so dark areas come up without blowing out lights. Hailo's precompiled model runs on the HAT in about 19 ms per frame. The app then removes the street-light colour cast, stretches the contrast, keeps 40 % of the colour, and averages away grain in still areas without smearing anything that moves. The picture is 712×400: that is all the detail the model produces, and at night there is no more to recover.
- **What you get:** the live view and event photos show the night picture, and the **🌙 Night vision** badge appears on the video. Each night event gets a second clip, `clip_night.mp4` (712×400, 15 fps). The viewer plays it by default; the **Night vision** button switches to the camera's original clip.
- **Detection still uses the original picture.** Tested on night footage, detecting on the brightened picture made things worse: 2 cars counted instead of 17 on the same 28 clips, plus false "person" and "animal" detections on a lamp reflection and a white post. Brightening amplifies the window's fly-screen mesh and the sensor noise into texture that confuses the detector. *Detect on the brightened picture* in Settings lets you try it anyway.
- **Zero-DCE vs Zero-DCE++:** both are in `models/lowlight/`. On this HAT, Zero-DCE++ is slower (27 ms per frame against 19 ms) and looks no better, so Zero-DCE is the default.

What would help night footage more than any software:

- **Light the street side.** A normal white floodlight, or a motion-activated one, gives the camera real signal. Brightening cannot recover detail that was never captured.
- **Fly screen and reflections.** The camera looks through a window with a fly screen. At night the screen and the room-light reflections in the glass dominate the picture. Remove the screen in front of the camera if you can, keep room lights off, or put a dark cloth or hood around the camera.
- **The camera's Brightness setting does not help.** Measured: it only lifts black to grey and adds no exposure.

## Camera notes

- **Connections.** The app needs the camera's **network cable**: video and sound come over RTSP, and camera control over VISCA (TCP port 5678), with the camera's HTTP API as a fallback.
  - The PTZ camera's **USB cable** is only for its anti-flicker setting. Set `camera.usb_device` to `Unite 200` and `camera.anti_flicker` in `config.json`, and the app puts it back whenever the camera restarts.
  - For this camera, USB can't replace the network. Its USB video is uncompressed and has no sound, and the Pi 5 has no hardware video encoder to record it. The Home preset and zoom positions also only come over the network. (The wide camera is different: it sends H.264 over USB, which the app records as it comes.)
- **No infrared mode.** The camera rejects the IR-cut and day/night commands. Night vision is done in software (above). An IR illuminator will not help; white light will.
- **Autofocus.** The camera does not support one-push autofocus. After each move the app sends *Focus Auto*, which also brings the lens back from manual focus.
- Home is stored in camera preset **200**. Presets 1–9 are yours.
- **Camera settings and restarts.** The sliders on the camera's own web page apply at once, but last only until the camera restarts unless you press *Save* there. After a restart it uses its saved values. On 31 October its saved brightness was 0.
- **Careful with the camera's web API.**
  - Some requests named `get_…` change settings: `get_image_default_conf` restores the image defaults.
  - `cgi-bin/hi3510/param.cgi?cmd=sysreset` is a factory reset.
  - Read the code behind the camera's own page before calling anything.
  - Its replies start with a debug line before the HTTP status line, which strict HTTP clients reject.
- **ONVIF** is on port 81 (`/onvif/device_service`, no password). It offers streaming and PTZ, but its imaging service doesn't work.

## Installing

These steps are for a fresh Raspberry Pi 5 with the Hailo-10H AI HAT+ 2; this Pi is already set up. They assume the user `pi` and the folder `/home/pi/ptz_cam`. Edit `ptzcam.service` if yours differ.

1. **Hailo software (HailoRT 5.3.0).**
   - From the Hailo Developer Zone (free account), download:
     - `hailort-pcie-driver_5.3.0_all.deb`
     - `hailort_5.3.0_arm64.deb`
     - the Python wheel `hailort-5.3.0-cp311-cp311-linux_aarch64.whl`
   - Then install them:
   ```bash
   sudo apt install linux-headers-raspi ffmpeg ./hailort-pcie-driver_5.3.0_all.deb ./hailort_5.3.0_arm64.deb
   sudo reboot
   ls /dev/h1x-0          # present = the AI HAT is ready
   ```
2. **Python 3.11.** HailoRT's wheel needs that version. Use [uv](https://docs.astral.sh/uv/) to set it up:
   ```bash
   uv venv --python 3.11 .venv
   uv pip install --python .venv/bin/python /path/to/hailort-5.3.0-cp311-cp311-linux_aarch64.whl -r requirements.txt
   ```
3. **Models.** Download them from the Hailo Model Zoo (v5.3.0, Hailo-10H):
   ```bash
   B=https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/v5.3.0/hailo10h
   mkdir -p models/lowlight
   curl -fLo models/yolov11m.hef $B/yolov11m.hef            # detector (yolov8m.hef also works)
   curl -fLo models/lowlight/zero_dce.hef $B/zero_dce.hef   # night vision (zero_dce_pp.hef also works)
   ```
4. **First run.** `./run.sh` creates `config.json` with the defaults.
   - Set the camera's RTSP URL and VISCA host in Settings → Camera.
   - Set its login and `http_url` in `config.json`.
5. **Start at boot:**
   ```bash
   sudo cp ptzcam.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now ptzcam
   ```

## Running it

It runs as a systemd service and starts at boot:

```bash
sudo systemctl status ptzcam      # is it running?
sudo systemctl restart ptzcam     # restart
journalctl -u ptzcam -f           # live log
```

To run it by hand (stop the service first):

```bash
./run.sh                                     # same as the service
./run.sh --source /usr/local/hailo/resources/videos/example.mp4 --no-ptz --port 8081   # test video, no camera
```

Tools that use the AI HAT or control the camera need the service stopped first. Only one program can use the AI HAT at a time, and the service owns the camera's control connection.

## Files

| Path | What |
|---|---|
| `config.json` | Settings (also edited from the Settings page). Created on first run; not in the repository, as it holds passwords |
| `data/ptzcam.db` | Events and counts (SQLite). `data/` is not in the repository |
| `data/media/<day>/<event>/` | `snapshot.jpg`, `thumb.jpg`, `clip.mp4`, `clip_night.mp4` for night events, `clip_wide.mp4` from the wide camera |
| `data/sync.json` | The two cameras' sync, measured by `tools/calibrate_sync.py` |
| `/run/ptzcam/` | Rolling 2-minute video buffer, in RAM to protect the SD card (`eye/` holds the wide camera's; manual runs use `/dev/shm/ptzcam/`) |
| `models/` | YOLO models for the Hailo-10H (`yolov11m` default, `yolov8m`). Downloaded (see *Installing*); not in the repository |
| `models/lowlight/` | Night-vision models for the Hailo-10H (`zero_dce` default, `zero_dce_pp`), from the Hailo Model Zoo |
| `tools/` | Calibration, evaluation and test tools (see *Development*) |
| `ptz_camera_web.md` | Design and build log |
| `LESSONS_LEARNED.md` | What was learned building and running it |

Retention: events older than 30 days are deleted automatically, and so are the oldest events once media exceeds the storage limit (50 GB by default). Counts are kept for a year. All of this can be changed in Settings.

## Development

```bash
.venv/bin/python -m unittest discover -s tests -t .                                    # unit tests (no hardware)
.venv/bin/python tools/sim_autotrack.py [--calibrated] [--eye]                         # auto-follow against a simulated camera (--eye: wide camera aims)
.venv/bin/python tools/usbcam_smoke.py                                                 # the wide camera's capture, on the real camera (app's eye off)
.venv/bin/python tools/eval_tracking.py VIDEO --out /tmp/eval                           # tracking/counting on a video
.venv/bin/python tools/eval_lowlight.py CLIP... --out /tmp/eval --start 3 --duration 8  # detector input: raw vs night vision or auto brightness
.venv/bin/python tools/mock_server.py --port 8090                                       # UI with fake data
python tools/ui_smoke.py --base http://127.0.0.1:8090 --out /tmp/shots                  # headless browser checks (needs Playwright)
.venv/bin/python tools/uvc_controls.py [--camera NAME]                                 # USB cameras' picture controls
```

**Measuring the zoom lens.** Do this once, with a clear daytime picture: not white, and not dark. It takes about 2 minutes with the app stopped:

```bash
sudo systemctl stop ptzcam
.venv/bin/python tools/calibrate_zoom.py      # --dry-run measures without saving
sudo systemctl start ptzcam
```

The camera zooms from wide to full zoom at its Home view in 16 steps. It writes `ptz.zoom_curve` and `ptz.zoom_rate` into `config.json`, and writes nothing if any step fails.

Notes:
- Stop the service before `eval_lowlight.py`, `eval_tracking.py` and `calibrate_zoom.py`.
- For experiments, run a second copy on a scratch config, data folder and port, so the real history and settings stay clean:
  ```bash
  ./run.sh --config /tmp/x/config.json --data-dir /tmp/x/data --segment-dir /tmp/x/seg --port 8081
  ```
- After editing Python files, delete `ptzcam/__pycache__` before restarting. Python decides whether its compiled copy is stale from the file's size and its modification time in whole seconds, so an edit and an undo within the same second can leave the old code running.
- HailoRT appends to `hailort.log` in the current folder unless `HAILORT_LOGGER_PATH=NONE` is set, as `run.sh` and the service do.

## Security

- The page is meant for your home network only. Don't forward port 8080 on your router.
- Requests must use the Pi's IP address or a local name (`rpi5-ai`, `rpi5-ai.local`, `*.lan`). Anything else gets *421 Unknown host name*. This blocks other websites from reaching the camera through your browser. To allow another name, add it to `"web": {"allowed_hosts": [...]}` in `config.json`.
- Only the app's own page can move the camera or change settings, so other sites cannot send those requests.
- To require a password, set `web.auth_user` and `web.auth_password` in `config.json`, then restart.
- The camera itself still uses its factory admin login, and its RTSP stream and ONVIF have no password. Changing those on the camera is recommended. If you do, update the RTSP URL in Settings → Camera, and `camera.username` and `camera.password` in `config.json`.

## Troubleshooting

- **Picture completely white by day.** See *Bright daylight* above.
- **Camera drops off the network.**
  - On 31 October it dropped out three times around 13:00, each time for up to a minute or two, after hours in the sun. The Pi's own network stayed up.
  - Check the camera's power, and that it isn't overheating behind the glass.
  - The app reconnects by itself. Until the camera's VISCA connection is back (retried every 30 s), it steers over HTTP and auto-zoom waits.
- **Camera offline.**
  - Check that `rtsp://192.168.1.87:554/1` plays (in VLC, or with `ffprobe`). The app reconnects by itself.
  - The camera gets its address from DHCP, so it can change. Reserve it in your router. If it changes, update the RTSP URL and VISCA host in Settings → Camera, and `camera.http_url` in `config.json`.
- **Day folders or hourly charts look 2 hours off.**
  - The Pi's clock is set to US Pacific time. Event times in the app follow your browser and are right, but recordings are filed under the Pi's date and the hourly charts use its hours.
  - Set the Pi to your time zone and restart:
  ```bash
  timedatectl list-timezones                  # find yours, e.g. Region/City
  sudo timedatectl set-timezone Region/City
  sudo systemctl restart ptzcam
  ```
- **Zoom calibration fails.** It needs a clear daytime picture with detail, and a camera that stays connected for about two minutes. It saves nothing when it fails, so just run it again later.
- **Camera sync fails or the PTZ points beside the target.**
  - The sync needs both cameras to see the street clearly, in daylight, through the same window.
  - Re-run `tools/calibrate_sync.py` if either camera moved. The log line "sync map ignored" means `data/sync.json` was made for another wide camera.
  - The PTZ's own picture corrects small errors as it follows, but not a moved camera.
- **Wide camera offline.** Check `tools/uvc_controls.py --camera avaya` sees it, and that nothing else is using its video. The app retries by itself, and the PTZ carries on alone meanwhile.
- **Detector offline after a kernel update.**
  - The Hailo driver is built by DKMS. Check that it exists for the running kernel:
    ```bash
    dkms status
    ls /dev/h1x-0
    ```
  - The `linux-headers-raspi` package is installed so new kernels get the driver automatically. If it's missing:
    ```bash
    sudo apt install linux-headers-$(uname -r)
    sudo modprobe hailo1x_pci
    ```
- **False detections at night.** The camera looks through a window, so room lights reflect in the glass. Turn room lights off, or draw a detection zone that covers only the street.
- **Night picture grainy or tinted.** That is the noise and colour that were in the dark original, made visible. More light on the scene is the real fix. The original clip is always kept next to the night-vision one.
- **Running hot.**
  - Without a fan, the Pi 5 with the AI HAT runs at about 80 °C, right at the point where it starts slowing itself down.
  - Night vision is tuned to add almost no CPU: about 29 % with it on, the same as before it existed.
  - Even so, fit the official Raspberry Pi 5 Active Cooler; it fits under the AI HAT+ 2.
- **PTZ not responding.** The app uses VISCA on TCP port 5678 and falls back to the camera's HTTP API, retrying VISCA every 30 s.
- **A code change doesn't take effect.** Delete `ptzcam/__pycache__` and restart the service (see *Development*).
