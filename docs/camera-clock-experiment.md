# Camera-clock experiment

Can Protect place boxes from the camera's own clock instead of a fixed `AIPORT_BOX_DELAY_MS`?

## What we know

- Protect's RTSP relay passes on a native UniFi camera's RTCP sender reports. Their clock is
  the camera's uptime: 339676 s on the doorbell and 843376 s on Backyard, steady to about 2 ms.
  Driveway, which isn't a UniFi camera, gets a relay clock (413 s, 0.2 s of jitter).
- A native camera's `EventSmartDetect` carries `clockMonotonic`/`clockStream` in ms on that
  clock (seen: `clockStream 69938862`, `clockMonotonic 69964728`).
- No wall-clock capture time anywhere else: no RTP header extensions, no SEI, and only a
  whole-second `Date:` header in the RTSP replies.
- ffmpeg's frame timestamps start at zero for each session, even with `-copyts`.

## Phase 1: does Protect use these clocks at all?

`AIPORT_CAMERA_CLOCK=<deviceID>[,...]` starts `camera_clock.Reader` for those cameras: its own
RTSP session to the relay, reading sender reports and keeping
`offset = local wall ms - camera ms`. `clockMonotonic` and `clockStream` become
`frame stamp - offset` (off by the frame's decode time, fine for this test).
`AIPORT_CLOCK_WALL_SKEW_MS` (`"<ms>[,<deviceID>=<ms>]"`) shifts `clockWall`.

- **Variant A:** camera clock on, no skew. Walk past the doorbell; check playback.
- **Variant B:** camera clock on, `AIPORT_CLOCK_WALL_SKEW_MS=8CEDE172C456=5000`. Walk again.
  If the boxes are still about right, Protect places them by the camera clock: go to Phase 2.
  If they are 5 s late, Protect uses `clockWall`: stop, the fixed delay stays.

## Phase 2 (only if Phase 1 passes)

pi-port does its own RTSP pull and feeds ffmpeg through a pipe, so each frame's RTP time is
known (UniFi streams have no B-frames: output frame n is input frame n). Each frame's camera
time is its RTP time mapped through the latest sender report; send that as
`clockMonotonic`/`clockStream`, derive `clockWall` from it, and set the box delay to 0 on those
cameras. To handle:
- RTP timestamps wrapping every 13.25 h
- a camera reboot resetting its uptime
- the relay restarting a session
- no report yet (fall back to today's clocks)

Tests replay a clip recorded with its sender reports.
