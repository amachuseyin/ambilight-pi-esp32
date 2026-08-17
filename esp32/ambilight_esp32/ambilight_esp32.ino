/*
  Ambilight ESP32 LED driver.

  Connects to your WiFi and to the server.py relay as a "viewer" client,
  receives the live zone-color JSON stream, and drives the SK6812 RGBW
  strip accordingly.

  Required libraries (install via Arduino IDE Library Manager):
    - Adafruit NeoPixel
    - WebSockets (by Markus Sattler)
    - ArduinoJson
*/

#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <Adafruit_NeoPixel.h>
#include <ArduinoOTA.h>

// ---------------------------------------------------------------------
// CONFIG - edit these before uploading
// ---------------------------------------------------------------------
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

const char* SERVER_HOST = "192.168.1.50"; // Pi ambilight server IP or hostname
const uint16_t SERVER_PORT = 8765;
const uint16_t UDP_PORT = 8766;

#define LED_PIN   5     
#define NUM_LEDS  236

// Safety / tuning
#define MAX_BRIGHTNESS        200
#define FRAME_TIMEOUT_MS      2000
#define RECONNECT_AFTER_MS    5000
#define STATUS_EVERY_MS       1000
#define TELEMETRY_EVERY_MS    5000
#define ENABLE_RGBW_EXTRACT   0
#define ENABLE_GAMMA          1
#define GAMMA_VALUE           2.2f
// ---------------------------------------------------------------------

Adafruit_NeoPixel strip(NUM_LEDS, LED_PIN, NEO_GRBW + NEO_KHZ800);
WebSocketsClient webSocket;
WiFiUDP udp;

unsigned long lastFrameTime = 0;
unsigned long lastStatusTime = 0;
unsigned long lastTelemetryTime = 0;
unsigned long lastReconnectAttempt = 0;
unsigned long frameCount = 0;
unsigned long droppedFrames = 0;
unsigned long reconnectCount = 0;
uint32_t lastSequence = 0;
bool ledsClearedByTimeout = false;
bool smoothingEnabled = true;
bool smoothingReady = false;
float smoothingAttack = 0.75f;
float smoothingDecay = 0.28f;
float smoothingThreshold = 18.0f;

// Allocate memory once globally to prevent RAM fragmentation crashes
// 24000 is large enough to comfortably hold 300 zones of JSON data
DynamicJsonDocument doc(24000);

uint8_t gammaLut[256];
float smoothedColors[NUM_LEDS][3];
uint8_t udpBuffer[2048];

void webSocketEvent(WStype_t type, uint8_t* payload, size_t length);

void buildGammaLut() {
  for (int i = 0; i < 256; i++) {
#if ENABLE_GAMMA
    float normalized = i / 255.0f;
    gammaLut[i] = (uint8_t)roundf(powf(normalized, GAMMA_VALUE) * 255.0f);
#else
    gammaLut[i] = (uint8_t)i;
#endif
  }
}

void setCorrectedPixel(int index, uint8_t r, uint8_t g, uint8_t b) {
#if ENABLE_GAMMA
  r = gammaLut[r];
  g = gammaLut[g];
  b = gammaLut[b];
#endif

#if ENABLE_RGBW_EXTRACT
  uint8_t w = min(r, min(g, b));
  r -= w;
  g -= w;
  b -= w;
  strip.setPixelColor(index, strip.Color(r, g, b, w));
#else
  strip.setPixelColor(index, strip.Color(r, g, b, 0));
#endif
}

void setSmoothedPixel(int index, uint8_t r, uint8_t g, uint8_t b) {
  if (!smoothingEnabled) {
    setCorrectedPixel(index, r, g, b);
    return;
  }

  float next[3] = {(float)r, (float)g, (float)b};
  if (!smoothingReady) {
    smoothedColors[index][0] = next[0];
    smoothedColors[index][1] = next[1];
    smoothedColors[index][2] = next[2];
    setCorrectedPixel(index, r, g, b);
    return;
  }

  float delta = max(
    fabsf(next[0] - smoothedColors[index][0]),
    max(fabsf(next[1] - smoothedColors[index][1]), fabsf(next[2] - smoothedColors[index][2]))
  );
  float alpha = delta >= smoothingThreshold ? smoothingAttack : smoothingDecay;
  smoothedColors[index][0] = (smoothedColors[index][0] * (1.0f - alpha)) + (next[0] * alpha);
  smoothedColors[index][1] = (smoothedColors[index][1] * (1.0f - alpha)) + (next[1] * alpha);
  smoothedColors[index][2] = (smoothedColors[index][2] * (1.0f - alpha)) + (next[2] * alpha);

  setCorrectedPixel(
    index,
    (uint8_t)roundf(smoothedColors[index][0]),
    (uint8_t)roundf(smoothedColors[index][1]),
    (uint8_t)roundf(smoothedColors[index][2])
  );
}

void clearStrip() {
  strip.clear();
  strip.show();
  smoothingReady = false;
}

void connectWebSocket() {
  webSocket.begin(SERVER_HOST, SERVER_PORT, "/");
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(3000);
}

void forceReconnectWebSocket() {
  Serial.println("[WS] Forcing reconnect after frame timeout");
  webSocket.disconnect();
  delay(100);
  connectWebSocket();
  reconnectCount++;
  lastReconnectAttempt = millis();
}

void reconnectWiFi() {
  Serial.println("[WiFi] Reconnecting");
  WiFi.disconnect();
  delay(100);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  lastReconnectAttempt = millis();
}

void markFrameShown() {
  frameCount++;
  lastFrameTime = millis();
  ledsClearedByTimeout = false;
  lastReconnectAttempt = 0;
  smoothingReady = true;
}

bool acceptSequence(uint32_t sequence) {
  if (sequence == 0) return true;
  if (lastSequence != 0) {
    if (sequence <= lastSequence) return false;
    if (sequence > lastSequence + 1) droppedFrames += sequence - lastSequence - 1;
  }
  lastSequence = sequence;
  return true;
}

void showPackedColors(uint8_t* payload, size_t offset, uint16_t count) {
  uint16_t limit = min((uint16_t)NUM_LEDS, count);
  for (uint16_t i = 0; i < limit; i++) {
    size_t p = offset + ((size_t)i * 3);
    setSmoothedPixel(i, payload[p], payload[p + 1], payload[p + 2]);
  }
  for (uint16_t i = limit; i < NUM_LEDS; i++) {
    strip.setPixelColor(i, 0);
  }
  strip.show();
  markFrameShown();
}

void handleBinaryFrame(uint8_t* payload, size_t length) {
  if (length < 6) return;
  if (payload[0] != 'A' || payload[1] != 'M' || payload[2] != 'B') return;

  if (payload[3] == '2') {
    if (length < 10) return;
    uint32_t sequence = (uint32_t)payload[4] |
                        ((uint32_t)payload[5] << 8) |
                        ((uint32_t)payload[6] << 16) |
                        ((uint32_t)payload[7] << 24);
    if (!acceptSequence(sequence)) return;
    uint16_t count = (uint16_t)payload[8] | ((uint16_t)payload[9] << 8);
    size_t expectedLength = 10 + ((size_t)count * 3);
    if (length < expectedLength) return;
    showPackedColors(payload, 10, count);
    return;
  }

  if (payload[3] != '1') return;

  uint16_t count = (uint16_t)payload[4] | ((uint16_t)payload[5] << 8);
  size_t expectedLength = 6 + ((size_t)count * 3);
  if (length < expectedLength) return;
  showPackedColors(payload, 6, count);
}

void handleUdpPackets() {
  int packetSize = udp.parsePacket();
  while (packetSize > 0) {
    if (packetSize <= (int)sizeof(udpBuffer)) {
      int len = udp.read(udpBuffer, sizeof(udpBuffer));
      if (len > 0) handleBinaryFrame(udpBuffer, (size_t)len);
    } else {
      while (udp.available()) udp.read();
      droppedFrames++;
    }
    packetSize = udp.parsePacket();
  }
}

void sendTelemetry() {
  if (!webSocket.isConnected()) return;
  unsigned long now = millis();
  float fps = frameCount * 1000.0f / max(1UL, now - lastStatusTime);
  char message[256];
  snprintf(
    message,
    sizeof(message),
    "{\"type\":\"telemetry\",\"fps\":%.1f,\"rssi\":%d,\"heap\":%u,\"dropped\":%lu,\"reconnects\":%lu,\"last_frame_age_ms\":%lu,\"seq\":%lu}",
    fps,
    WiFi.RSSI(),
    ESP.getFreeHeap(),
    droppedFrames,
    reconnectCount,
    now - lastFrameTime,
    (unsigned long)lastSequence
  );
  webSocket.sendTXT(message);
}

void applyConfig(JsonDocument& config) {
  if (config["smoothing_enabled"].is<bool>()) {
    smoothingEnabled = config["smoothing_enabled"];
  }
  if (config["smoothing_attack"].is<float>()) {
    smoothingAttack = constrain((float)config["smoothing_attack"], 0.0f, 1.0f);
  }
  if (config["smoothing_decay"].is<float>()) {
    smoothingDecay = constrain((float)config["smoothing_decay"], 0.0f, 1.0f);
  }
  if (config["smoothing_threshold"].is<float>()) {
    smoothingThreshold = max(0.0f, (float)config["smoothing_threshold"]);
  }
}

void webSocketEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      Serial.println("[WS] Disconnected");
      break;

    case WStype_CONNECTED:
      Serial.println("[WS] Connected, announcing as viewer");
      webSocket.sendTXT("{\"role\":\"viewer\"}");
      break;

    case WStype_BIN:
      handleBinaryFrame(payload, length);
      break;

    case WStype_TEXT: {
      // Clear the global document before reusing it
      doc.clear();
      DeserializationError err = deserializeJson(doc, payload, length);
      if (err) {
        Serial.print("[WS] JSON parse failed: ");
        Serial.println(err.c_str());
        return;
      }

      const char* msgType = doc["type"];
      if (msgType == nullptr) return;

      if (strcmp(msgType, "config") == 0) {
        applyConfig(doc);
        int total = (int)doc["top"] + (int)doc["bottom"] +
                    (int)doc["left"] + (int)doc["right"];
        Serial.printf("[Config] top=%d bottom=%d left=%d right=%d (total=%d)\n",
                      (int)doc["top"], (int)doc["bottom"],
                      (int)doc["left"], (int)doc["right"], total);
      }
      else if (strcmp(msgType, "frame") == 0) {
        JsonArray colors = doc["colors"];
        int i = 0;
        for (JsonArray c : colors) {
          if (i >= NUM_LEDS) break;
          setSmoothedPixel(i, c[0] | 0, c[1] | 0, c[2] | 0);
          i++;
        }
        for (; i < NUM_LEDS; i++) {
          strip.setPixelColor(i, 0);
        }
        
        strip.show();
        markFrameShown();
      }
      break;
    }
    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);

  buildGammaLut();

  strip.begin();
  strip.setBrightness(MAX_BRIGHTNESS); 
  clearStrip();

  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true);
  delay(200);
  WiFi.setSleep(false); // disable power-save mode to prevent disconnects

  Serial.printf("Connecting to WiFi '%s'", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    delay(300);
    Serial.printf(" [status=%d]", WiFi.status());
    attempts++;
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi FAILED. Retrying in 5 seconds...");
    delay(5000);
    ESP.restart();  
  }

  Serial.print("WiFi connected, IP: ");
  Serial.println(WiFi.localIP());

  ArduinoOTA.setHostname("ambilight-esp32");
  ArduinoOTA
    .onStart([]() {
      Serial.println("[OTA] Start");
      clearStrip();
    })
    .onEnd([]() {
      Serial.println("\n[OTA] End");
    })
    .onProgress([](unsigned int progress, unsigned int total) {
      Serial.printf("[OTA] Progress: %u%%\r", (progress * 100) / total);
    })
    .onError([](ota_error_t error) {
      Serial.printf("[OTA] Error[%u]\n", error);
    });
  ArduinoOTA.begin();
  Serial.println("OTA ready: ambilight-esp32.local");

  udp.begin(UDP_PORT);
  Serial.printf("UDP LED receiver listening on port %u\n", UDP_PORT);

  connectWebSocket();
  
  // Heartbeat REMOVED to prevent conflicts with Python server

  lastFrameTime = millis();
  lastStatusTime = millis();
}

void loop() {
  ArduinoOTA.handle();
  webSocket.loop();
  handleUdpPackets();

  unsigned long now = millis();
  if (!ledsClearedByTimeout && now - lastFrameTime > FRAME_TIMEOUT_MS) {
    clearStrip();
    ledsClearedByTimeout = true;
    Serial.println("[Safety] No frames; LEDs cleared");
  }

  if (now - lastFrameTime > RECONNECT_AFTER_MS &&
      (lastReconnectAttempt == 0 || now - lastReconnectAttempt > RECONNECT_AFTER_MS)) {
    if (WiFi.status() != WL_CONNECTED) {
      reconnectWiFi();
    } else {
      forceReconnectWebSocket();
    }
  }

  if (now - lastStatusTime >= STATUS_EVERY_MS) {
    Serial.printf("[FPS] %.1f\n", frameCount * 1000.0 / (now - lastStatusTime));
    frameCount = 0;
    lastStatusTime = now;
  }

  if (now - lastTelemetryTime >= TELEMETRY_EVERY_MS) {
    sendTelemetry();
    lastTelemetryTime = now;
  }

  delay(1); 
}
