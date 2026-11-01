// Real time clock: PCF8523 on the Adalogger FeatherWing.
//
// The PCF8523 only counts whole seconds. To timestamp words with millisecond
// resolution the firmware "anchors" the RTC: it notes the millis() value at
// which an RTC second begins and extrapolates from there with the nRF52's
// 32 kHz crystal. Times are UTC, as milliseconds since 1970-01-01.
#include "prec.h"
#include <RTClib.h>

static RTC_PCF8523 rtc;
static bool rtcPresent = false;
static uint32_t anchorSec = 0;     // unix seconds at anchorMillis
static uint32_t anchorMillis = 0;

// clock_begin(): start the RTC and anchor it. Returns false if no RTC answers.
bool clock_begin() {
  rtcPresent = rtc.begin();
  if (!rtcPresent) return false;
  rtc.start();
  clock_anchor();
  return true;
}

// clock_anchor(): wait (at most ~1.1 s) for the RTC seconds to change and
// remember the millis() value of that edge.
void clock_anchor() {
  if (!rtcPresent) return;
  uint32_t s0 = rtc.now().unixtime();
  uint32_t t0 = millis();
  uint32_t s = s0;
  while (s == s0 && millis() - t0 < 1100) {
    delay(1);
    s = rtc.now().unixtime();
  }
  anchorSec = s;
  anchorMillis = millis();
}

// clock_now_ms(): current UTC time in milliseconds since the unix epoch.
uint64_t clock_now_ms() {
  return (uint64_t)anchorSec * 1000ULL + (uint32_t)(millis() - anchorMillis);
}

// clock_set_ms(unixMs, receivedAtMillis): set the RTC to unixMs, where
// unixMs was valid at millis() == receivedAtMillis. The PCF8523 divider
// chain is stopped while writing and released exactly on the next whole
// second, so the RTC second edge is aligned to the given time.
void clock_set_ms(uint64_t unixMs, uint32_t receivedAtMillis) {
  if (!rtcPresent) return;
  rtc.stop();
  uint64_t nowMs = unixMs + (uint32_t)(millis() - receivedAtMillis);
  uint32_t sec = nowMs / 1000 + 1;
  uint32_t edge = millis() + (uint32_t)((uint64_t)sec * 1000 - nowMs);
  rtc.adjust(DateTime(sec));
  while ((int32_t)(millis() - edge) < 0) { }
  rtc.start();
  anchorSec = sec;
  anchorMillis = edge;
}

// clock_valid(): false if the RTC is missing or reports that it lost power.
bool clock_valid() {
  return rtcPresent && !rtc.lostPower();
}

// clock_format(unixMs, out, n, compact): ISO-like text. compact=true gives
// "YYYYMMDD-HHMMSS" (used for file names), otherwise "YYYY-MM-DDTHH:MM:SS.mmmZ".
void clock_format(uint64_t unixMs, char* out, size_t n, bool compact) {
  DateTime d((uint32_t)(unixMs / 1000));
  if (compact)
    snprintf(out, n, "%04d%02d%02d-%02d%02d%02d", d.year(), d.month(), d.day(), d.hour(), d.minute(), d.second());
  else
    snprintf(out, n, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", d.year(), d.month(), d.day(), d.hour(), d.minute(), d.second(), (int)(unixMs % 1000));
}
