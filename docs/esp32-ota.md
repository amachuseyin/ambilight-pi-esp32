# ESP32 OTA Updates

The ESP32 firmware enables Arduino OTA with this hostname:

```text
ambilight-esp32.local
```

## Arduino IDE

1. Upload the sketch once over USB.
2. Keep the ESP32 powered and on the same Wi-Fi as your computer.
3. In Arduino IDE, choose the OTA network port:

```text
ambilight-esp32.local
```

4. Upload normally.

If the OTA port does not appear:

- Confirm the ESP32 is powered.
- Confirm your computer and ESP32 are on the same Wi-Fi/VLAN.
- Power-cycle the ESP32 once.
- Make sure `ArduinoOTA.begin()` is still present in the sketch.

## Command Line

If you install Arduino CLI on another machine, compile/upload with your board
FQBN and network port. Example shape:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 esp32/ambilight_esp32
arduino-cli upload -p ambilight-esp32.local --fqbn esp32:esp32:esp32 esp32/ambilight_esp32
```

The exact FQBN depends on your ESP32 board package and board model.

## Reconnect Behavior

The firmware clears the strip if frames stop and then forces a WebSocket
reconnect. If Wi-Fi is disconnected, it retries Wi-Fi instead.
