// Speed prediction on the live accelerometer stream.
//
// The models are compiled in from model_data.h (written by
// `prec_train.py feather`, or the TUI's F5 > Build models into feather): per
// model the same feature code as on the watch (window, FFT, normalisation)
// plus a layer table run by model_engine.h. There may be a speed model, a
// classifier (run, walk, still, stretch) and run / walk speed models.
//
// While a session streams ACC from the Polar, predict_feed() (BLE task)
// copies the samples into a ring buffer; predict_service() (loop) runs every
// settings.predictMs milliseconds on the newest window:
//   1. the classifier, if there is one: its probabilities go through a
//      first-order low-pass (time constant settings.classLpS, 0 = off) and
//      the most likely class is taken;
//   2. run -> the run model, walk -> the walk model (each falling back to
//      the speed model), still / stretch -> speed 0; without a classifier
//      the speed model;
//   3. the speed goes through its own low-pass (settings.speedLpS, 0 = off),
//      except still / stretch, which set it to 0; the distance integrates it
//      (over the whole session: a Polar reconnect does not reset it).
// The first model that ran with DFT features (the classifier, else the speed
// model) hands the spectrum of the up-down axis to cadence.cpp.
// Nothing is written to the data file.
#include "prec.h"
#include "model_data.h"

static const char* const CLASS_NAMES[] = {"run", "walk", "still", "stretch"};
enum { C_RUN = 0, C_WALK = 1, C_STILL = 2, C_STRETCH = 3, C_COUNT = 4 };

const char* predict_class_name(int8_t cls) { return cls >= 0 && cls < C_COUNT ? CLASS_NAMES[cls] : "-"; }

#if MODEL_PRESENT

#define RING (MODEL_MAXWIN * 8 + 512)   // samples; room for 8x decimation (200 Hz data, 25 Hz model)

static int16_t ring[RING][3];
static volatile uint32_t head = 0;  // samples written so far
static uint8_t decim = 0;           // ACC rate / model rate; 0 = not running
static const char* why = "not logging";
static const MSlot *sSpeed, *sClass, *sRun, *sWalk;
static uint32_t span = 0;           // samples the largest window needs
static float probs[C_COUNT], speedF = 0, speedRaw = 0, distance = 0;
static int8_t cls = -1;
static bool first = true;
static uint32_t lastAt = 0, lastRun = 0, lastHead = 0, count = 0, lastMs = 0;
static int16_t win[MODEL_MAXWIN * 3];
static float buf[MODEL_MAXWIN * 3], feat[MODEL_MAXNIN], work[2 * MODEL_MAXWIN + 2], actA[MODEL_MAXACT], actB[MODEL_MAXACT];
static bool specDone;               // the cadence got a spectrum in this round

static void find_slots() {
  if (span) return;
  for (int i = 0; i < MODEL_NSLOTS; i++) {
    const MSlot* s = &MODEL_SLOTS[i];
    if (s->kind == MK_SPEED) sSpeed = s;
    else if (s->kind == MK_CLASS) sClass = s;
    else if (s->kind == MK_RUN) sRun = s;
    else if (s->kind == MK_WALK) sWalk = s;
  }
  span = 1;
}

// predict_reset(rate): a new ACC stream at rate Hz starts (0: none). The models
// run when rate is a whole multiple of their rate (then decimated).
void predict_reset(uint16_t rate) {
  find_slots();
  decim = 0;
  head = lastHead = count = lastAt = 0;
  cls = -1;
  first = true;
  speedF = speedRaw = 0;
  if (!logger_runtime_ms()) distance = 0;   // a new session; a reconnect keeps the distance
  if (!rate) why = "not logging";
  else if (rate < MODEL_FREQ || rate % MODEL_FREQ) why = "ACC rate does not fit the model";
  else {
    why = "waiting for data";
    decim = rate / MODEL_FREQ;
    span = (uint32_t)(MODEL_MAXWIN - 1) * decim + 1;
  }
}

// predict_feed(samples, n): n x,y,z int16 samples from a PMD ACC frame (BLE task).
void predict_feed(const uint8_t* samples, uint16_t n) {
  if (!decim) return;
  uint32_t h = head;
  for (uint16_t i = 0; i < n; i++, h++) memcpy(ring[h % RING], samples + 6 * i, 6);
  head = h;                         // one 32 bit store: the consumer sees whole samples
}

// run(s, h): model s on its window ending at sample h; returns its outputs (in actA/actB).
static const float* run(const MSlot* s, uint32_t h) {
  uint32_t start = h - ((uint32_t)(s->win - 1) * decim + 1);
  for (int k = 0; k < s->win; k++) memcpy(&win[k * 3], ring[(start + (uint32_t)k * decim) % RING], 6);
  s->buffer_data((PTR)win, (PTR)buf, 0, s->win * 3);
  s->features((PTR)buf, (PTR)feat, (PTR)work);
  if (s->dft && !specDone) {          // cadence from this model's spectrum (work is free again)
    s->spectrum((PTR)feat, (PTR)work, settings.cadAxis < 3 ? settings.cadAxis : 0);
    cad_spectrum(work, s->dft, (float)MODEL_FREQ / s->win);
    specDone = true;
  }
  return mrun(s->net, s->layers, feat, s->nin, actA, actB);
}

// alpha(dt, tau): weight of a new value in a first-order low-pass; 1 = no filter.
static float alpha(float dt, uint16_t tau) { return tau && !first ? 1.0f - expf(-dt / tau) : 1.0f; }

// predict_service(): from loop(); runs the models when it is time and new data arrived.
void predict_service() {
  if (!decim || !settings.predictMs) return;
  uint32_t h = head;
  if (h < span || h == lastHead || millis() - lastRun < settings.predictMs) return;
  breadcrumb(BC_PREDICT);
  uint32_t t0 = millis();
  float dt = first ? 0 : (t0 - lastRun) / 1000.0f;
  lastRun = t0;
  lastHead = h;
  specDone = false;
  const MSlot* s = sSpeed;
  if (sClass) {
    const float* p = run(sClass, h);
    float a = alpha(dt, settings.classLpS);
    for (int k = 0; k < C_COUNT; k++) probs[k] += a * (p[k] - probs[k]);
    cls = 0;
    for (int k = 1; k < C_COUNT; k++) if (probs[k] > probs[cls]) cls = k;
    s = cls == C_RUN ? (sRun ? sRun : sSpeed) : cls == C_WALK ? (sWalk ? sWalk : sSpeed) : nullptr;
  }
  if (s) {
    speedRaw = run(s, h)[0];
    speedF += alpha(dt, settings.speedLpS) * (speedRaw - speedF);
  } else {
    speedRaw = speedF = 0;              // still, stretch (or no model for this class)
  }
  if (speedF < 0) speedF = 0;
  distance += speedF * dt;
  first = false;
  lastAt = millis();
  lastMs = lastAt - t0;
  count++;
}

// predict_get(p): the latest prediction; false if none yet.
bool predict_get(Prediction* p) {
  p->n = count;
  p->ms = lastMs;
  p->cls = cls;
  p->dist = distance;
  if (!count || !decim) return false;
  p->speed = speedF;
  p->raw = speedRaw;
  p->ageMs = millis() - lastAt;
  return true;
}

const char* predict_model() { return MODEL_NAME; }
const char* predict_state() { return decim ? (count ? "running" : "waiting for data") : why; }

#else   // firmware without a model

void predict_reset(uint16_t) {}
void predict_feed(const uint8_t*, uint16_t) {}
void predict_service() {}
bool predict_get(Prediction* p) { p->n = 0; p->ms = 0; p->cls = -1; p->dist = 0; return false; }
const char* predict_model() { return "-"; }
const char* predict_state() { return "no model"; }

#endif
