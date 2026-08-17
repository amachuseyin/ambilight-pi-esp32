# Wiring Notes

## LED Strip

Use a power supply sized for your strip. Large LED strips should not be powered
from the ESP32 or Raspberry Pi.

Basic wiring:

```text
Power supply +5V  -> LED strip +5V
Power supply GND  -> LED strip GND
ESP32 GND         -> LED strip GND
ESP32 GPIO 5      -> LED strip DIN
```

Recommended additions:

- Level shifter from ESP32 data pin to 5 V LED data.
- 300-500 ohm resistor in series with LED data.
- Large capacitor across LED 5 V and GND near the strip input.
- Power injection for long strips.

## Raspberry Pi Camera

Mount the camera so the full TV screen is visible. The calibration dashboard
handles perspective correction, so the camera does not need to be perfectly
centered, but it should be stable and not move after calibration.

## Network

The ESP32 connects to the Pi WebSocket server on port `8765`.

The dashboard is served by the Pi on port `8080`.

Reserve the Pi IP address in your router if possible, then use that address in
the ESP32 sketch as `SERVER_HOST`.
