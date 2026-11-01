// Cadence (steps per minute) from the Polar accelerometer.
//
// Two ways, as on the watch (bangle/prec/polar.js):
//  * counting: every ACC sample of the up-down axis (settings.cadAxis; x on
//    an H10 worn normally, where it averages -1 g) goes into cad_feed(). The
//    axis mean is followed by a slow low-pass (about -1000 mG); one step is
//    the signal rising through the mean after having been at least HYST mG
//    below it. The crossing time is interpolated between samples, and the
//    cadence is 60 / the mean of the last periods.
//  * DFT: when a model computes a spectrum (dft > 0), predictor.cpp passes
//    the magnitudes of the up-down axis to cad_spectrum(); the strongest bin
//    between 1 and 4.5 Hz, refined by a parabola through it and its two
//    neighbours, gives the step frequency. While spectra keep coming this
//    value is used; counting takes over again after 5 s without one, or
//    when the strongest bin sits on the band edge (a stronger peak outside
//    it, e.g. the stride at walking pace). Either way there is a cadence
//    only while counting finds steps: a spectrum always has some peak,
//    also when standing still.
// The value goes through a low-pass (settings.cadLpS, 0 = off) in
// cad_service() (loop), which also writes a CADENCE word every second while
// logging, if settings.logCad is on.
#include "prec.h"

#define HYST 150.0f            // mG below the mean that arms the next step
#define MEAN_TAU 2.0f          // s, low-pass of the axis mean
#define NPER 8                 // periods averaged
#define MIN_PERIOD 0.2f        // s: faster than 300 steps/min is noise
#define MAX_PERIOD 1.5f        // s: slower than 40 steps/min is no cadence
#define DFT_HOLD_MS 5000

static uint16_t rate = 0;
static uint8_t step = 1;       // use every step-th sample (about 50 Hz)
static float dtS, alphaM, mean, prev;
static bool armed, havePrev;
static uint32_t n;             // samples used
static float lastCross;        // s (sample clock) of the previous step, < 0 = none
static float periods[NPER];
static uint8_t np, pi, skip;
static volatile float countSpm = 0, dftSpm = 0;
static volatile uint32_t countAt = 0, dftAt = 0;
static uint32_t lastLog = 0;

// cad_reset(r): a new ACC stream at r Hz (0: none).
void cad_reset(uint16_t r) {
  rate = r;
  step = r >= 100 ? r / 50 : 1;
  dtS = r ? (float)step / r : 0;
  alphaM = r ? 1.0f - expf(-dtS / MEAN_TAU) : 0;
  mean = -1000.0f;
  armed = havePrev = false;
  n = 0;
  lastCross = -1;
  np = pi = skip = 0;
  countSpm = dftSpm = 0;
  countAt = dftAt = 0;
}

// cad_feed(samples, n): x,y,z int16 samples of a PMD ACC frame (BLE task).
void cad_feed(const uint8_t* samples, uint16_t count) {
  if (!rate) return;
  uint8_t axis = settings.cadAxis < 3 ? settings.cadAxis : 0;
  for (uint16_t i = 0; i < count; i++) {
    if (skip) { skip--; continue; }
    skip = step - 1;
    const uint8_t* s = samples + 6 * i + 2 * axis;
    float v = (int16_t)(s[0] | s[1] << 8);
    mean += alphaM * (v - mean);
    float t = n++ * dtS;
    if (v < mean - HYST) armed = true;
    else if (armed && havePrev && v >= mean && prev < mean) {
      armed = false;
      float tc = t - dtS * (v - mean) / (v - prev);     // where the line through the two samples crosses the mean
      if (lastCross >= 0) {
        float p = tc - lastCross;
        if (p >= MIN_PERIOD && p <= MAX_PERIOD) {
          periods[pi] = p;
          pi = (pi + 1) % NPER;
          if (np < NPER) np++;
          float sum = 0;
          for (uint8_t k = 0; k < np; k++) sum += periods[k];
          if (np >= 3) { countSpm = 60.0f * np / sum; countAt = millis() | 1; }
        } else if (p > MAX_PERIOD) np = 0;             // stopped: start again
      }
      lastCross = tc;
    }
    prev = v;
    havePrev = true;
  }
}

// cad_spectrum(mag, bins, binHz): magnitudes |X_k| (k < bins, bin k at k * binHz Hz)
// of the up-down axis from a DFT model (loop task).
void cad_spectrum(const float* mag, int bins, float binHz) {
  int lo = (int)ceilf(1.0f / binHz), hi = (int)(4.5f / binHz);
  if (lo < 1) lo = 1;
  if (hi > bins - 2) hi = bins - 2;
  if (hi < lo) return;
  int k = lo;
  for (int i = lo + 1; i <= hi; i++) if (mag[i] > mag[k]) k = i;
  float a = mag[k - 1], b = mag[k], c = mag[k + 1], d = a - 2 * b + c;
  if (a > b || c > b || d >= 0) return;  // the peak lies outside the band (often the stride, at walking pace): count
  float off = 0.5f * (a - c) / d;
  dftSpm = 60.0f * (k + off) * binHz;
  dftAt = millis() | 1;
}

// cad_raw(method): steps per minute before the low-pass (0 = none) and how (0 counting, 1 DFT).
static float cad_raw(uint8_t* method) {
  uint32_t now = millis();
  // signed: "| 1" (0 = never) can put the stamp 1 ms ahead of now
  bool stepping = countAt && (int32_t)(now - countAt) < 3000;   // a spectrum always has a peak: only with steps
  if (method) *method = 0;
  if (!stepping) return 0;
  if (dftAt && (int32_t)(now - dftAt) < DFT_HOLD_MS) { if (method) *method = 1; return dftSpm; }
  return countSpm;
}

// The cadence after a first-order low-pass with time constant settings.cadLpS
// (0 = off), updated by cad_service(); it starts again from the raw value
// after a stop (no steps).
static float filtered = 0;
static uint8_t filteredM = 0;
static uint32_t lastFilter = 0;

// cad_get(method): steps per minute after the low-pass (0 = none) and how (0 counting, 1 DFT).
float cad_get(uint8_t* method) {
  if (method) *method = filteredM;
  return filtered;
}

// cad_service(): loop(); updates the low-pass every 100 ms and writes the
// CADENCE word once a second while logging (settings.logCad).
void cad_service() {
  uint32_t now = millis();
  if (now - lastFilter >= 100) {
    float dt = (now - lastFilter) / 1000.0f;
    lastFilter = now;
    float c = rate ? cad_raw(&filteredM) : 0;
    if (c <= 0 || filtered <= 0 || !settings.cadLpS) filtered = c;
    else filtered += (1.0f - expf(-dt / settings.cadLpS)) * (c - filtered);
  }
  if (!settings.logCad || !rate || !rec_is_open() || now - lastLog < 1000) return;
  lastLog = now;
  uint8_t m, p[3];
  float c = cad_get(&m);
  if (c <= 0) return;
  put_le16(p, (uint16_t)(c * 10 + 0.5f));
  p[2] = m;
  rec_put(W_CADENCE, 0, p, 3);
}
