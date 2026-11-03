# Lessons learned

These notes cover building and running PTZ Cam. The setup is a Raspberry Pi 5 with a Hailo-10H AI HAT, driving a ClearOne UNITE 200 conference-room PTZ camera that watches the street through a window. The app was built on 30 October 2026 and reworked on 31 October.

**Times here are local time.** The Pi's clock runs on US Pacific time, 2 hours behind. The logs, the database and the build log in `ptz_camera_web.md` therefore show times 2 hours earlier.

## The ten that matter most

1. **Measure before you change anything.**
   - Night vision looked as if it should help detection. Measured, it cut counted cars from 17 to 2.
   - Auto brightness by day looked risky. Measured, it found 20 % more objects for the same vehicle count.
   - Neither result could have been guessed.
2. **Don't steer through a lagging picture.**
   - Auto-zoom used to watch the video and zoom until a person looked the right size. The picture arrives half a second late, and in half a second the lens doubles the picture, so it always overshot.
   - Measuring once and sending one absolute zoom command fixes that by design.
3. **An indoor camera is not an outdoor camera.** Its exposure range suits a meeting room. In bright daylight the picture went completely white for 2½ hours, and nothing on the network can change its exposure.
4. **Probe the device; don't trust its names or its claims.**
   - A `get_` request restores the image defaults.
   - An old-style endpoint is a factory reset.
   - The ONVIF imaging service lists values that are placeholder numbers.
5. **Check the raw source before blaming the processing.** "Is night vision making the picture white?" took one step to answer: decode the camera's untouched stream. It was the camera.
6. **You can't follow what's faster than the camera.** Passing cars cross the view in about 1.5 s. Chasing them achieved nothing and paused counting. Follow people first.
7. **Put things back.** After every experiment on the camera:
   - restore its settings exactly;
   - send it Home;
   - write down anything left changed, with the time and the old value.
8. **Caches lie, in three places:** the browser (an old JavaScript module), Python (stale compiled bytecode), and the app's own status (figures frozen while the camera is offline).
9. **Know whose clock you're reading.** The Pi's time zone isn't the user's. Quoting Pi times turned "midday" into "morning".
10. **Make hardware tools fail safe.** The zoom calibration hit a camera dropout halfway through. It stopped, wrote nothing, and the app came back. That's the behaviour to design for.

## 1. The camera

**What it actually supports** (probed on 30 and 31 October):

| Interface | Works | Refused or missing |
|---|---|---|
| RTSP `:554/1` | 1080p30 H.264 with AAC sound; two sessions at once; no password | `/2` offers only small sizes |
| VISCA, TCP 5678 | pan/tilt, zoom (variable and direct), presets including 200 and up, Home, *Focus Auto*, position inquiries | one-push autofocus, IR-cut and day/night, every exposure command and inquiry, the on-screen menu, power and version inquiries |
| HTTP `cgi-bin/param.cgi` | image sliders, reboot; moves via `ptzctrl.cgi` | exposure, gain, shutter; `hi3510` commands (404, apart from a factory reset) |
| ONVIF `:81` | device, media, PTZ, events | imaging: every request is "not implemented" |
| USB (UVC 1.00) | picture controls including anti-flicker; PTZ controls; uncompressed YUYV video up to 1080p30 | exposure, gain, compressed video, sound |

- **Endpoint names lie.**
  - `param.cgi?get_image_default_conf` is the image page's *Restore defaults* button. It was triggered by accident on 30 October while surveying settings; luckily they were already at defaults.
  - `cgi-bin/hi3510/param.cgi?cmd=sysreset` is a factory reset.
  - Rule: read the code behind the camera's own web page before calling any endpoint.
- **The HTTP replies are malformed.** The camera prints a debug line before the HTTP status line, so Python's `requests` and `urllib` raise `BadStatusLine` and curl fails. A raw socket speaking HTTP/1.0 works.
- **A VISCA "done" doesn't mean the camera has arrived.**
  - The camera acknowledges on socket 1 or 2, and reports completion about 1 ms later on the same socket. It does this even for a preset recall that takes seconds to drive.
  - Inquiries answer on socket 0, and a stop while idle gets no reply at all.
  - Replies must be matched by socket number, or a late reply is taken as the answer to the next command.
  - Position inquiries lag about a second while the camera moves, and are exact at rest.
- **ONVIF claims more than it does.** The imaging service is listed, but every request to it fails. The imaging values in the video-source description count 0, 1, 2 … 23 in order: placeholders from a sample implementation.
- **Brightness is an offset, not exposure.** Two steps add about 9 levels to darks and lights alike, and the noise stays the same. It can't rescue a dark picture or a white one.
- **Live and saved settings differ.**
  - The sliders on the camera's page apply at once, but last only until a restart unless *Save* is pressed.
  - On 31 October the saved brightness read 0, though it read 7 the day before and the user had set 9. The `ldc` entry had also gone.
  - The app never touches these settings. The page's Save button skips `ldc`, so it was most likely saved from that page.
  - Keep a note of the values you rely on, and check them after any camera restart.
- **The scene is behind glass.**
  - At night, room-light reflections and the window's wire mesh dominate.
  - By day, two window bars and the mesh hide people for a moment as they walk.
  - People on the far pavement are only a tenth of the frame tall at the Home view.

### Daylight white-out

- **What happened on 31 October.**
  - Mean brightness rose with the daylight instead of holding level: 95 at 08:00, 171 at 08:41, 208 at 09:16, 244 at 09:52.
  - Then the picture was completely white from 10:43 to 13:08, with no events at all for 2 h 25 min.
  - It came back hazy, washed out again in the afternoon, then dropped sharply at 17:28.
  - From 17:30 to 17:52 the picture darkened steadily with the fading light (mean 153 → 66). A working automatic exposure would have held it level.
- **What it means.** For most of the day the exposure was stuck at its shortest. At night it is stuck at its longest (mean brightness 12–20). It only regulates within a narrow band in between.
- **What doesn't help:**
  - brightness and contrast, which only turn a flat white into flat grey or black;
  - a camera restart (tried, with the user's approval);
  - every network control in the table above.
- **The lead: anti-flicker.**
  - It's the only exposure-related control, and it's only on USB. It was set to 50 Hz, although its default is off.
  - In HiSilicon's standard anti-flicker mode, exposure is held to whole multiples of the mains flicker period (1/100 s at 50 Hz), which can over-expose bright scenes. That matches the white-out.
  - It was switched off at 18:00 on 31 October. Toggling it at dusk changed nothing, as expected, because the exposure wasn't stuck in that light. The verdict comes from the next bright day.
- **How the diagnosis went.** "Is Zero-DCE making it white?" was answered by decoding the camera's untouched stream. It was 99.6 % pure white (mean 253 of 255), and night vision had been off since 07:50.

### Reliability

- **The camera dropped off the network three times:** at 13:06, 13:09 and 13:17, each time for up to a minute or two, after about four hours in the sun.
  - The Pi's own link never dropped.
  - The cause is unknown: heat, power, or someone using the camera's page.
- **The app recovered by itself each time.** The stream reconnects with back-off. VISCA is retried every 30 s, with HTTP control meanwhile.
- **The camera's address comes from DHCP.** Reserve it in the router.

## 2. Raspberry Pi 5 and the Hailo-10H

- **The DKMS driver needs kernel headers.** The Hailo driver (`hailo1x_pci`, `/dev/h1x-0`) is built by DKMS, and it broke after a kernel update because the new kernel's headers were missing. The `linux-headers-raspi` package keeps them coming.
- **HailoRT pins the Python version.** Hailo's wheel is for Python 3.11 and the system Python is 3.14, so the app runs in a uv-managed 3.11 venv.
- **On the Hailo-10H, use `hailortcli run2`, not `run`.** In Python, keep a reference to `get_physical_devices()` before calling `.control.identify()`.
- **One process owns the AI HAT.** Evaluation tools need the service stopped.
- **`/dev/shm` is not safe for the video buffer.** logind's `RemoveIPC` wipes the user's `/dev/shm` at logout. The service uses a systemd `RuntimeDirectory` (`/run/ptzcam`) instead. That also survives service restarts, so pending clips can still be cut.
- **OpenCV's thread pool costs more than it saves on small images.** It nearly doubled the CPU for the same wall time. Setting `cv2.setNumThreads(1)` app-wide brought the app with night vision on from 38.5 % to about 29 % CPU, the same as before night vision existed.
- **Resize in integer steps.** A non-integer `INTER_AREA` resize cost 9 ms. An exact 2× `INTER_AREA` followed by a linear resize costs about 1 ms.
- **Heat.** Without a fan, the Pi 5 with the AI HAT sits around 80 °C, at the edge of throttling. Fit the Active Cooler.
- **The Pi 5 has no hardware H.264 encoder.** This shaped the whole recording design:
  - the camera's own H.264 is copied into clips without re-encoding;
  - night clips are encoded in short bursts, about 9 s of CPU per 30 s clip;
  - continuous encoding, at about 0.44 of a core, was ruled out;
  - for the same reason, recording the camera's raw USB video isn't practical.
- **Memory.** The service sets `MALLOC_MMAP_THRESHOLD_` and `MALLOC_ARENA_MAX`, so large per-frame buffers go straight back to the OS instead of glibc's heap creeping. The app holds at about 160–200 MB.
- **HailoRT writes `hailort.log`** into the current folder unless `HAILORT_LOGGER_PATH=NONE` is set. That includes test runs.

## 3. Detection and counting

- **Count once, after real movement.**
  - An object counts when it has moved at least half its own size, or clearly grown or shrunk.
  - Size and position changes must persist, so a parked car that is briefly hidden doesn't count. Parked cars are never counted.
- **Camera motion looks like object motion.** Counting therefore pauses while the camera moves, and for 2 s after. That's the price of auto-follow, and one reason not to chase cars.
- **Enhancing for people is not enhancing for the detector.**
  - On 28 night clips, counted cars were: raw 17, classical enhancement 9, Zero-DCE 2, Zero-DCE++ 2.
  - Zero-DCE++ also added false "person" and "animal" detections on a lamp reflection and a white post.
  - Brightening turns the mesh, the noise and the street-light cast into texture. So night vision only drives what people see.
- **By day the opposite held.**
  - Auto brightness on 18 washed-out clips: 20 % more detections, the same 85 vehicles, and one false vehicle on a road marking gone.
  - So `brightness.detect` is on and `lowlight.detect` is off. Test each case; don't generalise.
- **The fancier model isn't always better.** Zero-DCE++ is slower on the HAT (27 ms per frame against 19 ms) and looked no better.
- **Night pictures have a hard limit.** 712×400 is all the detail Zero-DCE gives back, and upscaling or adding a detail layer only added grain. Light on the street is the real fix.

## 4. Auto-follow and zoom

- **Start from the data.**
  - Before the rework there were 242 car follows against 18 person follows.
  - Cars cross the Home view in about 1.5 s, so the camera only started turning as they left, and each chase paused counting for about 9 s.
  - So now people come first, and anything faster than 0.25 frame widths per second isn't chased. People move at 0.05–0.17.
- **Speed estimates lag at the frame edge.** A car entering the view had a low velocity estimate for its first frames, so it got picked. Use the larger of the current velocity and the average since the object appeared.
- **Moving the camera loses people.**
  - Motion blur and the window bars break the track. The detector finds the person again under a new ID, and that new track can't prove it's moving while the camera moves.
  - Fixes: re-lock onto a nearby track of the same kind once the target has been missing for 0.3 s.
  - Move the count and the event to the new track (`Track.inherit`), so nobody is counted twice.
  - Keep turning at the learned speed through short gaps.
- **Re-finding must be strict.** A lost minivan was "re-found" as two parked cars coming into view, and the camera turned until it looked into the room. Now only slow things (people, bicycles, animals) can be re-found as new detections, and only at a similar size.
- **Riders flicker** between person and bicycle. Treat them as one kind when re-locking, and ignore one-frame flickers.
- **The overshoot was structural.**
  - At speed 7 the lens doubles the picture in about 0.5 s, about as long as the picture's lag, so steering the zoom from the video overshoots.
  - Tuning only trades overshoot for slowness. The tuned version took 9.9 s (median) to reach size, and in 37 of 162 simulated cases it never got there.
  - The fix:
    1. Wait until the lens is at rest.
    2. Take the median box height.
    3. Work out the zoom from the lens curve.
    4. Send one VISCA zoom-direct command. The camera stops exactly there.
    5. Wait for it to arrive, plus 0.6 s, before measuring again.
- **A model of the lens isn't enough.** In simulation, assuming a 12× lens gave a worst overshoot of 1.45× when the real lens was 20×. With the measured curve the worst was 1.00×. So the lens must be measured (`tools/calibrate_zoom.py`), and that needs a clear picture.
- **Simulate before deploying.** `tools/sim_autotrack.py` closes the loop with lag, lens speed and a mismatched lens.
  - Its 54 cases showed the old controller losing the person in 22 of them.
  - Its 162 cases chose the zoom design.
- **Calibration status: not done yet.**
  - The picture was white or dim most of 31 October.
  - The one attempt, at 13:17, was cut off by a camera dropout after its first step (×1.10), and stopped safely.

## 5. The web app

- **Version the asset URLs.** A browser kept an old cached `ptz.js`, so a new button did nothing (the first "Set Home does nothing" report). Assets are now served from `/v/<build-hash>/` with immutable caching.
- **A home-network app still needs web security.**
  - A Host allow-list blocks DNS rebinding.
  - An `Origin` and `Sec-Fetch-Site` check blocks requests from other sites.
  - Request bodies must be JSON.
- **Everything stays local.** No CDNs or web fonts, the models run on the HAT, and the recordings stay on the Pi.
- **Times.**
  - The page formats event times in the browser, which is right for whoever is looking.
  - Day folders and hourly charts are computed on the Pi, in its time zone, so the Pi's zone must match the user's.

## 6. Working practice

- **Test on a scratch copy:** another config, data folder and port, so the real history and settings stay clean.
- **Gate a deploy on the test result** (`… | grep -q OK && systemctl restart …`). Once, a command chain ending in `tail` restarted the service despite a failing test.
- **Delete `__pycache__` before restarting.** An edit and an undo within the same second left Python running the edited bytecode, so tests failed against code that was already correct.
- **Restore settings exactly.**
  - After each image test, the camera went back to brightness 9 and contrast 8.
  - Every test that changed the camera (exposure probes, anti-flicker toggles) sent the original values back in a `finally` block.
- **Ask before anything hard to undo.** The camera restart was approved first. Factory-reset endpoints are never called.
- **Background waits need fresh data.**
  - The app's brightness and light figures freeze at their last values while the camera is offline, so a watcher that only reads them can be fooled. The first watcher didn't allow for that; the later one also checks the frame age.
  - Background waits also end with the session. A wait until dusk or the next morning has to be re-armed, or run outside the session.
- **Note what you leave changed** on the device, such as the anti-flicker test: when, and the old value.
- **Say which clock.** Quote the user's local time, or say "Pi time".

## 7. Two cameras (2 November)

The user mounted a fixed USB camera (an Avaya HC020) beside the PTZ camera. It counts, steers the PTZ, and takes over when the PTZ is washed out.

- **Check the two cameras see the same thing before comparing them.**
  - The first comparison was meaningless: matching image features between the two pictures found nothing in common (0 consistent matches).
  - The wide camera was pointing into the lit room.
- **Let the camera do the heavy video work.**
  - Decoding the Avaya's 4K on the Pi ran at 0.7–0.8× real time on 2–3 cores, and the Pi went from 74 °C to 83 °C. That rules it out.
  - The camera's own 1080p shows exactly the same field of view as its 4K: the match scale was 1.000. Its digital zoom (at most 1.77×) is a lossless crop.
  - At 15 fps H.264 it keeps a fixed 30-frame GOP, so there is a keyframe every 2 s, matching the 2 s segments. Decoding costs about 0.3 of a core.
- **Measure the hardware's limits before designing around them.**
  - Absolute pan/tilt, `81 01 06 02`, was assumed missing until it was tried. It works, and it is what makes eye-driven aiming possible.
  - The Hailo looked too slow for two cameras plus night vision: 36.6 fps with one thread.
  - With a separate set of bindings per thread, two threads reach 70.4 fps, and 52.5 alongside Zero-DCE. The limit was the API's latency per call, not the chip.
- **One V4L2 device, one reader.**
  - Only one process can stream a USB camera. So one ffmpeg reads it and writes two outputs: the stream-copied recording ring and the decoded frames for detection.
  - The recorder gained a passive mode, cutting clips from a ring another process writes.
  - Two clips cut into one event folder at the same moment used to share a temporary listing file. Each output now has its own.
- **Simulate before deploying, and check the simulator too.** The eye-driven controller looked right in unit tests. The closed-loop simulation found three problems:
  1. **The camera trailed the walker.** Pursuit speed was chosen to reach each aim in 0.6 s, but the aim led by only about 0.4 s. The lead must be the eye's lag plus the same arrival time.
  2. **The PTZ-picture correction wound up without limit.** It added the previous correction on top of an error that had been measured from the camera's real pose, so it counted the correction twice. Any small constant error, such as a latency one frame off, then ratcheted up.
  3. **The simulator itself was wrong.** Its "late" PTZ picture combined the current scene with an old pose, so it reported trailing that wasn't there. A real picture is late as a whole.

  After fixing all three, over 108 cases the walker stayed in frame 100 % of the time. With the lens measured there was no zoom overshoot; with the assumed lens model, 24 cases zoomed up to 1.45× too far, the same limit as PTZ mode, so measure the lens. A deliberate 1° map error was cut from 0.06–0.11 of the frame to 0.01–0.02 by the correction from the PTZ's picture.
- **Work out the storage before adding a second clip per event.**
  - The 3 GB cap already held only about 5 h of events.
  - A wide clip for every event would have halved that, so by default a wide clip is kept only when the PTZ's clip doesn't show the whole scene.
- **An independent critic earned its keep.** It caught three problems:
  - **Crash risk:** a `sys.exit` in a helper that the capture thread would have called, which would have stopped that thread for good when the camera was missing.
  - **Unsafe aim:** pan goals were not clamped to the calibrated range.
  - **Storage:** the arithmetic in the previous bullet.
- **Reconnects reset USB settings.** A camera restart put anti-flicker back to 50 Hz and spoiled a day's test. The app now re-applies its USB settings on reconnect, and the wide camera's crop too (the sync map only fits that crop).
- **An adversarial review of the finished backend found 32 problems, and an independent check confirmed every one.** Each fix has a test that fails on the old code. The lessons behind them:
  - **A decision about a camera must not depend on that camera's frames.** Whether to keep an event's wide clip was decided only on PTZ frames. So while the PTZ was offline, which is exactly when the wide clip is the only video, every wide clip was deleted. Shared decisions now run once a second from whichever camera is delivering frames.
  - **What must survive a restart goes in the database.** That decision lived in memory, so clips recovered after a crash were all deleted. It is now saved with the event and also follows from what is stored (main camera, PTZ clip failed).
  - **A "stop" must be its own request.** A stop that landed while an absolute move was being sent was overwritten by the send's own bookkeeping, and the camera drove on to the cancelled goal. Turning PTZ control off never stopped a swing at all. The stop is now a flag that only the worker clears.
  - **Never replay a stale command.** A goal kept for de-duplication was sent again when VISCA came back, so the camera swung to where someone had been minutes earlier. A sent goal is now cleared, and one that cannot be sent is dropped.
  - **A handover must not assume the old camera saw everything.** Marking everyone the new counter saw as counted lost every person a washed-out PTZ had missed. The two cameras' tracks are now matched by position, or, without the sync map, by number per kind of object.
  - **"Where it was" is not "where it is".** A lost eye target's last position kept being matched in the PTZ picture, so passers-by were linked to its event. A click on the late PTZ picture was mapped through the camera's current pose instead of the pose that picture shows.
  - **State text is not state.** A photo check compared the failover's reason string, which went stale after a brief outage. It now asks the selector whether the PTZ picture is clear, and the reason is recomputed every second.
- **Review the fixes too.** A second adversarial review, of the fixes alone, found 19 more problems, several of them regressions the first round introduced:
  - Clearing a sent goal (so it could not replay) also stopped a swing from resuming after a one-off read error.
  - Re-finding a lost target by distance alone gave one person's count to another, walking the other way, and once locked onto someone standing still.
  - A click on the PTZ view was mapped at the late picture's moment but compared with eye positions half a second newer: at walking pace that is the gap to the next person.
  - A handover looked for "recent" counts as of now, but a failure is only noticed seconds later (5 s for an outage), and a whiting-out picture stops detecting before it is flagged. Recency now runs from when the old camera last worked.
  - Two reviewers disagreed about photos without the sync map. The resolution came from geometry: the wide view contains the PTZ's view, not the other way round.
- **Watch the real logs after deploying.** A start-up race made the wide camera main for two minutes (the PTZ stream sends its first picture a second later), and the eye's ffmpeg was quietly dropping frames whose jittery timestamps shared a 1/15 s tick. Neither showed in any test.

## 8. Open items

- **Sync the cameras** with `tools/calibrate_sync.py`, in daylight, with both looking at the street. `tools/sync_when_clear.py` can wait for a clear moment and run it unattended. Then check live follows driven by the wide camera, and the counts against the PTZ's history.
- **Fit the Active Cooler** before running both cameras: the Pi read 82.6 °C with one.
- **Storage cap.** Consider raising it from 3 GB, since the card has about 99 GB free. Then evidence is kept for more than a few hours.

- **Anti-flicker verdict.**
  - It hasn't been tested yet. The camera restarted on the morning of 1 November, and that put anti-flicker back to 50 Hz. A camera restart undoes it, so the app should re-apply it whenever the camera reconnects.
  - Once it is applied, compare a bright day's clips (about 10:00–14:00) with 31 October's, and check the night clips for flicker.
  - If it works, have the app switch it automatically: off by day, 50 Hz at night. If it doesn't, set it back to 50 Hz.
- **Measure the lens** with `tools/calibrate_zoom.py` on a clear picture. Then check live person follows:
  - overshoot at most 1.2×;
  - vehicles back Home within about a second of being lost;
  - "next:" switches to another object when one is in view.
- **Camera brightness.** Decide whether the saved value should be 0 or 9, and save it on the camera's page.
- **Time zone.** Set the Pi to the user's time zone.
- **Dropouts.** Find out whether heat or power is behind them. Shading would help with both heat and glare.
- **Cooling.** Fit the Pi 5 Active Cooler.
- **Passwords.** The camera still uses its factory login, and its RTSP stream and ONVIF have no password.
