# Configuration Reference

Runtime settings live in `pi/config.json`. Start from:

```bash
cp pi/config.example.json pi/config.json
```

Important fields:

- `server`: WebSocket server URL for the capture producer. Use `ws://localhost:8765` when server and capture run on the same Pi.
- `width`, `height`, `fps`: camera capture settings.
- `corners`: calibrated TV corner points in camera image coordinates.
- `persp_width`, `persp_height`: corrected internal sampling size.
- `send_frame`: set `false` for normal low-latency operation.
- `color_order`: channel order sent to the ESP32.
- `red_gain`, `green_gain`, `blue_gain`: color tuning.
- `black_level`: dark cutoff to keep LEDs off for near-black content.
- `led_order`: physical perimeter order.
- `led_offset`: rotates the final LED list to match the physical start point.
- `sample_depth`: how deep each edge sample reaches into the corrected image.
- `sample_inset`: moves edge samples inward.
- `blackbar_detect`: crops letterbox bars before sampling.
- `blackbar_threshold`: brightness threshold for active picture detection.
- `blackbar_margin`: padding around detected active picture.
- `smoothing_enabled`: enables anti-flicker LED color smoothing.
- `smoothing_attack`: smoothing factor for larger changes, from 0 to 1.
- `smoothing_decay`: smoothing factor for small noisy changes, from 0 to 1.
- `smoothing_threshold`: color delta threshold between decay and attack.

After changing config manually:

```bash
sudo systemctl restart ambilight-led.service
```
