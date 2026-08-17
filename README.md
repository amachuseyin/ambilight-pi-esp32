# Pi + ESP32 Ambilight

Camera-based Ambilight for a TV. A Raspberry Pi captures the screen, computes
edge colors, and streams LED frames over WebSocket. An ESP32 receives those
frames and drives an SK6812 RGBW or compatible NeoPixel strip.

## What Is Included

- `pi/capture.py` - camera capture, perspective correction, color sampling, black-bar handling.
- `pi/server.py` - WebSocket relay and local calibration dashboard server.
- `pi/calibrate.html` - browser calibration and tuning UI.
- `pi/config.example.json` - example runtime configuration.
- `pi/run_ambilight.sh` - starts the Pi relay and capture process.
- `pi/install_service.sh` - installs a systemd boot service on the Pi.
- `esp32/ambilight_esp32/ambilight_esp32.ino` - ESP32 LED receiver firmware.

## Hardware

- Raspberry Pi with camera support.
- Raspberry Pi Camera Module or compatible camera supported by Picamera2.
- ESP32 board.
- Addressable LED strip, tested with 236 SK6812 RGBW LEDs.
- 5 V power supply sized for your LED count.
- Common ground between ESP32, LED strip power, and Pi-side electronics where relevant.
- Optional level shifter for the LED data line.

Typical LED wiring:

```text
ESP32 GPIO 5  -> LED DIN
5 V supply    -> LED 5V
Power ground  -> LED GND
ESP32 GND     -> LED GND
```

Change `LED_PIN` and `NUM_LEDS` in the ESP32 sketch if your wiring or strip size differs.

## Raspberry Pi Setup

Clone or copy this repo to the Pi:

```bash
cd /home/pi
git clone <your-repo-url> ambilight-pi-esp32
cd ambilight-pi-esp32/pi
```

Install system packages:

```bash
sudo apt update
sudo apt install -y python3-venv python3-picamera2 python3-opencv
```

Create a Python virtual environment:

```bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install -r requirements.txt
```

Create the runtime config:

```bash
cp config.example.json config.json
```

Start manually:

```bash
./run_ambilight.sh
```

Open the dashboard from another device on the same network:

```text
http://<pi-ip>:8080/calibrate.html
```

## Install Boot Startup

After manual startup works, install the systemd service:

```bash
cd /home/pi/ambilight-pi-esp32/pi
./install_service.sh
```

Useful commands:

```bash
sudo systemctl status ambilight-led.service
journalctl -u ambilight-led.service -f
sudo systemctl restart ambilight-led.service
sudo systemctl stop ambilight-led.service
```

## ESP32 Firmware

Open `esp32/ambilight_esp32/ambilight_esp32.ino` in Arduino IDE.

Install these Arduino libraries:

- Adafruit NeoPixel
- WebSockets by Markus Sattler
- ArduinoJson

Set these values in the sketch:

```cpp
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* SERVER_HOST   = "192.168.1.50";

#define LED_PIN   5
#define NUM_LEDS  236
```

`SERVER_HOST` must be the Raspberry Pi IP address or a hostname the ESP32 can resolve.
For reliability, reserve the Pi IP address in your router DHCP settings.

Upload once over USB. After that, Arduino OTA is enabled with hostname:

```text
ambilight-esp32.local
```

The firmware clears the LEDs if frames stop and forces a WebSocket reconnect if
the Pi restarts or the network drops.

## Calibration

1. Start the Pi runtime.
2. Open `http://<pi-ip>:8080/calibrate.html`.
3. Use the preview to place the four TV corners.
4. Tune LED order and offset until the physical strip lines up.
5. Tune channel order and RGB gains until colors look correct.
6. Save to `config.json` from the dashboard.
7. Restart the service to verify the saved config loads cleanly.

The current example uses:

```json
"led_order": "left-top-right-bottom",
"color_order": "bgr",
"red_gain": 1.0,
"green_gain": 0.92,
"blue_gain": 0.85
```

## Black Bars

For letterboxed movies or shows, enable:

```json
"blackbar_detect": true
```

The detector crops top/bottom black bars before sampling and smooths crop changes
to avoid flicker. If it reacts too much in dark scenes, disable it or raise/lower
`blackbar_threshold` in small steps.

## Latency Tuning

The example config is set up for low latency:

```json
"width": 320,
"height": 240,
"fps": 80.0,
"persp_width": 160,
"persp_height": 90,
"send_frame": false,
"binary_output": true
```

The Pi capture code also requests low camera buffering from Picamera2:

```text
buffer_count=2
queue=False
```

Recommended latency settings:

- Keep `send_frame` false during normal watching.
- Keep browser dashboard tabs closed unless calibrating.
- Use `binary_output: true` after the ESP32 firmware is uploaded.
- Use 320x240 capture for lowest latency, or 640x480 if you need higher sampling precision.
- Reserve the Pi IP address in the router so the ESP reconnects quickly.

If the ESP32 firmware does not include binary frame support yet, set
`binary_output` back to `false`.

## Troubleshooting

Check the Pi runtime:

```bash
curl http://127.0.0.1:8080/api/status
```

Healthy output should show:

```json
{
  "viewers": 1,
  "producers": 1,
  "last_frame_age_sec": 0.05
}
```

Meaning:

- `producers: 1` - the Pi camera capture is connected.
- `viewers: 1` - the ESP32 is connected.
- `last_frame_age_sec` near zero - frames are actively streaming.

If `viewers` is `0`, the ESP32 is not connected. Check ESP32 power, Wi-Fi,
`SERVER_HOST`, and whether the Pi IP changed.

If colors are swapped, try a different `color_order` in the dashboard. Common
values to test are `rgb`, `bgr`, and `grb`.

If top/bottom LEDs are dark during letterboxed content, enable black-bar detection.

If LEDs flicker in dark scenes, reduce black-bar detection sensitivity or disable it.

## Privacy

Do not commit real Wi-Fi credentials or local-only runtime files. This repo ignores:

- `pi/config.json`
- Python virtual environments
- logs
- local secret files

Keep `pi/config.example.json` generic enough to share.
