// Battery voltage, motion detection with the on-board LSM6DS3TR-C and sleep.
//
// The IMU runs its accelerometer in low power mode at 26 Hz with the
// "wake-up" (slope) interrupt routed to INT1 = D3, latched. While awake the
// firmware polls WAKE_UP_SRC to see motion; when going to sleep the nRF52840
// enters System OFF with D3 as wake source, so the next movement resets the
// board and the command channel comes back up.
#include "prec.h"
#include <Wire.h>

// LSM6DS3TR-C registers
#define REG_INT1_CTRL   0x0D
#define REG_CTRL1_XL    0x10
#define REG_CTRL2_G     0x11
#define REG_CTRL6_C     0x15
#define REG_WAKE_UP_SRC 0x1B
#define REG_TAP_CFG     0x58
#define REG_WAKE_UP_THS 0x5B
#define REG_WAKE_UP_DUR 0x5C
#define REG_MD1_CFG     0x5E

#define WAKE_THRESHOLD  2      // x FS/64 = 62 mg at +-2 g

static bool imuOk = false;

static void imu_write(uint8_t reg, uint8_t v) {
  Wire.beginTransmission(IMU_ADDR);
  Wire.write(reg);
  Wire.write(v);
  Wire.endTransmission();
}

static int imu_read(uint8_t reg) {
  Wire.beginTransmission(IMU_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false)) return -1;
  if (Wire.requestFrom((uint8_t)IMU_ADDR, (uint8_t)1) != 1) return -1;
  return Wire.read();
}

// Free an I2C bus left mid-transfer by a reset: clock SCL until the slave
// releases SDA, then generate a STOP condition.
static void i2c_recover() {
  pinMode(PIN_WIRE_SDA, INPUT_PULLUP);
  pinMode(PIN_WIRE_SCL, INPUT_PULLUP);
  delayMicroseconds(10);
  for (int i = 0; i < 9 && !digitalRead(PIN_WIRE_SDA); i++) {
    pinMode(PIN_WIRE_SCL, OUTPUT);
    digitalWrite(PIN_WIRE_SCL, LOW);
    delayMicroseconds(5);
    pinMode(PIN_WIRE_SCL, INPUT_PULLUP);
    delayMicroseconds(5);
  }
  pinMode(PIN_WIRE_SDA, OUTPUT);
  digitalWrite(PIN_WIRE_SDA, LOW);
  delayMicroseconds(5);
  pinMode(PIN_WIRE_SDA, INPUT_PULLUP);
  delayMicroseconds(5);
}

// power_begin(): set up the IMU wake-up interrupt, the battery ADC and the LED.
void power_begin() {
  pinMode(LED_RED, OUTPUT);
  pinMode(PIN_IMU_INT, INPUT);
  i2c_recover();
  Wire.begin();
  imuOk = imu_read(0x0F) == 0x6A;
  if (imuOk) {
    imu_write(REG_CTRL2_G, 0x00);       // gyro off
    imu_write(REG_CTRL1_XL, 0x20);      // 26 Hz, +-2 g
    imu_write(REG_CTRL6_C, 0x10);       // accelerometer high performance off = low power
    imu_write(REG_WAKE_UP_DUR, 0x00);
    imu_write(REG_WAKE_UP_THS, WAKE_THRESHOLD);
    imu_write(REG_TAP_CFG, 0x91);       // interrupts on, slope filter, latched
    imu_write(REG_INT1_CTRL, 0x00);
    imu_write(REG_MD1_CFG, 0x20);       // wake-up event -> INT1
    imu_read(REG_WAKE_UP_SRC);          // clear a pending event
  }
}

// power_motion(): true if the IMU saw movement since the last call.
bool power_motion() {
  if (!imuOk) return false;
  int s = imu_read(REG_WAKE_UP_SRC);    // reading clears the latch
  return s > 0 && (s & 0x08);
}

// power_battery_mv(): LiPo voltage in millivolts (A6 through a 1:2 divider).
uint16_t power_battery_mv() {
  analogReference(AR_INTERNAL_3_0);
  analogReadResolution(12);
  uint32_t raw = 0;
  for (int i = 0; i < 4; i++) raw += analogRead(PIN_VBAT);
  analogReference(AR_DEFAULT);
  analogReadResolution(10);
  return raw * 3000UL * 2 / 4096 / 4;
}

// power_led(on): red LED (shows logging activity).
void power_led(bool on) { digitalWrite(LED_RED, on ? HIGH : LOW); }

// power_sleep(): enter System OFF; the next movement resets the board.
// Does not return. Must not be called while logging.
void power_sleep() {
  Bluefruit.Advertising.stop();
  power_led(false);
  digitalWrite(LED_BLUE, LOW);
  if (imuOk) {
    imu_read(REG_WAKE_UP_SRC);
    uint32_t t0 = millis();
    while (digitalRead(PIN_IMU_INT) && millis() - t0 < 1000) imu_read(REG_WAKE_UP_SRC);
  }
  Serial.flush();
  systemOff(PIN_IMU_INT, HIGH);
}
