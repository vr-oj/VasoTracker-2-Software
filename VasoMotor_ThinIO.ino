// VasoMotor_ThinIO.ino
// Thin firmware: sample sensor, apply last setpoint, stream telemetry, echo ACK.

#include <Arduino.h>

namespace {

constexpr uint8_t kPressurePin = A0;
constexpr uint8_t kActuatorPin = 9;   // PWM pin or DAC driver
constexpr uint16_t kTelemetryHz = 25; // 25 Hz DATA lines
constexpr uint16_t kSampleHz = 100;   // Sensor sampling rate

volatile float g_set_mmHg = 0.0f;     // Last applied setpoint
unsigned long nextSampleMs = 0;
unsigned long nextDataMs = 0;

// Calibrate for the pressure transducer in use.
constexpr float kVref = 5.0f;
constexpr float kAdcCounts = 1023.0f;
constexpr float kSenseVmin = 0.5f;
constexpr float kSenseVmax = 4.5f;
constexpr float kPressureMax = 200.0f; // mmHg

float adcToMmHg(int counts) {
  float v = (static_cast<float>(counts) / kAdcCounts) * kVref;
  float vClamped = constrain(v, kSenseVmin, kSenseVmax);
  return (vClamped - kSenseVmin) * (kPressureMax / (kSenseVmax - kSenseVmin));
}

void applySetpoint(float p_mmHg) {
  float duty = constrain(p_mmHg / kPressureMax, 0.0f, 1.0f);
  int pwm = static_cast<int>(duty * 255.0f + 0.5f);
  analogWrite(kActuatorPin, pwm);
  g_set_mmHg = p_mmHg;
}

void sendAck(float p, unsigned long now) {
  Serial.print(F("ACK SET P="));
  Serial.print(p, 1);
  Serial.print(F(" T="));
  Serial.println(now);
}

} // namespace

void setup() {
  pinMode(kActuatorPin, OUTPUT);
  analogWrite(kActuatorPin, 0);
  Serial.begin(115200);
  while (!Serial) {
    ; // Wait for native USB boards
  }
  unsigned long start = millis();
  nextSampleMs = start;
  nextDataMs = start;
}

void loop() {
  static String line;
  // Basic non-blocking line parser
  while (Serial.available()) {
    char c = static_cast<char>(Serial.read());
    if (c == '\n' || c == '\r') {
      if (!line.isEmpty()) {
        if (line.startsWith("SET")) {
          int idx = line.indexOf('P');
          if (idx >= 0) {
            int eq = line.indexOf('=', idx);
            if (eq > 0) {
              float p = line.substring(eq + 1).toFloat();
              applySetpoint(p);
              sendAck(p, millis());
            }
          }
        } else if (line.startsWith("?SP")) {
          Serial.print(F("SP:"));
          Serial.println(g_set_mmHg, 1);
        } else if (line == "?") {
          Serial.println(F("OK"));
        }
        line = "";
      }
    } else if (line.length() < 120) {
      line += c;
    }
  }

  unsigned long now = millis();
  static float lastPressure = 0.0f;

  if (now >= nextSampleMs) {
    do {
      nextSampleMs += (1000UL / kSampleHz);
    } while (now >= nextSampleMs);
    int counts = analogRead(kPressurePin);
    lastPressure = adcToMmHg(counts);
  }

  if (now >= nextDataMs) {
    do {
      nextDataMs += (1000UL / kTelemetryHz);
    } while (now >= nextDataMs);
    Serial.print(F("DATA T="));
    Serial.print(now);
    Serial.print(F(" P="));
    Serial.print(lastPressure, 2);
    Serial.print(F(" P_SET="));
    Serial.println(g_set_mmHg, 2);
  }
}
