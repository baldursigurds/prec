// Logging session: connects to the default Polar, starts the configured
// streams, writes them to /prec/data/<time>.prc and keeps going until STOP,
// low battery, a card error, or the Polar disappearing for longer than
// settings.reconnectSec.
#include "prec.h"

#define CONNECT_TIMEOUT_MS   8000
#define RECONNECT_TRY_MS     5000
#define STATUS_PERIOD_MS     60000

static LoggerState state = LOG_IDLE;
static char path[48];
static ble_gap_addr_t polarAddr;
static char polarName[32];
static uint8_t started = 0;        // bit per measurement type actually streaming
static uint32_t lostAt = 0, nextTry = 0, lastStatus = 0;
static uint8_t lowCount = 0;
static uint32_t startedAt = 0;       // millis() when the session started

LoggerState logger_state() { return state; }
// logger_file(): path of the file being logged, "" when idle.
const char* logger_file() { return state == LOG_IDLE ? "" : path; }
// logger_streams(): bit mask of measurement types currently streaming.
uint8_t logger_streams() { return started; }
// logger_runtime_ms(): how long the session has been running, 0 when idle.
uint32_t logger_runtime_ms() { return state == LOG_IDLE ? 0 : millis() - startedAt; }

static void write_battery_words() {
  int pb = polar_battery();
  if (pb >= 0) {
    uint8_t b = pb;
    rec_put(W_POLAR_BATTERY, 0, &b, 1);
  }
  uint8_t mv[2];
  put_le16(mv, power_battery_mv());
  rec_put(W_FEATHER_BATTERY, 0, mv, 2);
  lastStatus = millis();
}

// Start every enabled stream the Polar supports. Writes STREAM_START / ERROR
// words and fills info with "started=acc,ecg failed=gyro:not supported".
static uint8_t start_streams(char* info, size_t n) {
  // The feature read occasionally fails right after connecting (other link
  // procedures still running): retry, and if it never works just try the
  // configured streams and let the Polar answer.
  uint8_t features = 0;
  int e = -1;
  for (int i = 0; i < 4 && e; i++) {
    if (i) { uint32_t t0 = millis(); while (millis() - t0 < 250) idle_service(); }
    e = polar_read_features(&features);
  }
  if (e) {
    rec_error(ERRSRC_PMD, 0xFF, "feature read failed");
    features = 0xFF;
  }
  char ok[40] = "", bad[96] = "";
  started = 0;
  polar_set_sink(0);
  for (uint8_t mt = 0; mt < MT_COUNT; mt++) {
    if (!mt_name(mt) || !settings.fmt[mt].on) continue;
    int code = 3;   // not supported
    uint8_t req[20], resp[32];
    uint16_t reqLen = 0, respLen = 0;
    if (features & (1 << mt)) code = polar_start(mt, settings.fmt[mt], req, &reqLen, resp, &respLen);
    if (code == 0) {
      uint8_t p[2 + 20 + 32];
      p[0] = mt;
      p[1] = reqLen;
      memcpy(p + 2, req, reqLen);
      memcpy(p + 2 + reqLen, resp, respLen);
      rec_put(W_STREAM_START, 0, p, 2 + reqLen + respLen);
      started |= 1 << mt;
      if (mt == MT_ACC) { predict_reset(settings.fmt[MT_ACC].rate); cad_reset(settings.fmt[MT_ACC].rate); }
      polar_set_sink(started);
      snprintf(ok + strlen(ok), sizeof(ok) - strlen(ok), "%s%s", ok[0] ? "," : "", mt_name(mt));
    } else {
      char t[48];
      snprintf(t, sizeof(t), "%s start: %s", mt_name(mt), pmd_error_name(code));
      rec_error(ERRSRC_PMD, code < 0 ? 0xFF : code, t);
      snprintf(bad + strlen(bad), sizeof(bad) - strlen(bad), "%s%s:%s", bad[0] ? "," : "", mt_name(mt), pmd_error_name(code));
    }
  }
  // spaces in error names would split the reply tokens
  for (char* c = bad; *c; c++) if (*c == ' ') *c = '_';
  if (info) snprintf(info, n, "started=%s%s%s", ok[0] ? ok : "none", bad[0] ? " failed=" : "", bad);
  return started;
}

static void end_session(uint8_t reason) {
  polar_set_sink(0);
  predict_reset(0);
  cad_reset(0);
  rec_put(W_SESSION_END, 0, &reason, 1);
  rec_close();
  state = LOG_IDLE;
  started = 0;
  settings.resume = false;
  settings_save();
}

// logger_start(fileOut, n, info, infoN): begin a logging session. Returns 0
// on success, otherwise an error code of the command protocol (409 already
// logging, 404 no default device, 500 card error, 503 Polar unreachable,
// 422 no stream could be started). fileOut receives the data file path, info
// a summary of started/failed streams.
int logger_start(char* fileOut, size_t n, char* info, size_t infoN) {
  if (state != LOG_IDLE) return 409;
  if (!storage_ok()) return 500;
  if (!settings.defaultAddr[0] || !addr_parse(settings.defaultAddr, &polarAddr)) return 404;
  int di = devices_find(polarAddr);
  if (di >= 0) polarAddr = devices[di].addr;
  strlcpy(polarName, di >= 0 ? devices[di].name : "", sizeof(polarName));

  char stamp[20];
  clock_format(clock_now_ms(), stamp, sizeof(stamp), true);
  snprintf(path, sizeof(path), DIR_DATA "/%s" DATA_EXT, stamp);
  for (int i = 1; sd.exists(path) && i < 100; i++)
    snprintf(path, sizeof(path), DIR_DATA "/%s_%d" DATA_EXT, stamp, i);
  if (!rec_open(path)) return 500;

  uint8_t p[64];
  memcpy(p, "PREC", 4);
  p[4] = PREC_FORMAT_VERSION;
  for (int i = 0; i < 6; i++) p[5 + i] = polarAddr.addr[5 - i];
  p[11] = polarAddr.addr_type;
  uint8_t k = 12, nl = strlen(polarName), fl = strlen(PREC_FW_VERSION);
  p[k++] = nl; memcpy(p + k, polarName, nl); k += nl;
  p[k++] = fl; memcpy(p + k, PREC_FW_VERSION, fl); k += fl;
  rec_put(W_SESSION_START, 0, p, k);

  if (!polar_connect(polarAddr, CONNECT_TIMEOUT_MS)) {
    rec_close();
    sd.remove(path);
    return 503;
  }
  polar_reset_packets();
  if (!start_streams(info, infoN)) {
    polar_disconnect();
    rec_close();
    sd.remove(path);
    return 422;
  }
  write_battery_words();
  rec_service(true);
  state = LOG_ACTIVE;
  startedAt = millis();
  if (!settings.resume) {
    settings.resume = true;
    settings_save();
  }
  lowCount = 0;
  strlcpy(fileOut, path, n);
  return 0;
}

// logger_stop(reason): stop the streams, disconnect the Polar and close the
// file with a SESSION_END word carrying reason. Returns 0, or 409 if idle.
int logger_stop(uint8_t reason) {
  if (state == LOG_IDLE) return 409;
  for (uint8_t mt = 0; mt < MT_COUNT; mt++)
    if (started & (1 << mt)) {
      polar_stop(mt);
      rec_put(W_STREAM_STOP, 0, &mt, 1);
    }
  polar_disconnect();
  end_session(reason);
  return 0;
}

// logger_resume_service(): called from loop(). If the board was reset while
// a session was running (settings.resume), start a new session; retry every
// 10 s for settings.reconnectSec, then give up.
void logger_resume_service() {
  static uint32_t firstTry = 0, nextResume = 0;
  if (!settings.resume || state != LOG_IDLE) return;
  if ((int32_t)(millis() - nextResume) < 0) return;
  if (!firstTry) firstTry = millis() | 1;
  char file[48], info[96];
  if (logger_start(file, sizeof(file), info, sizeof(info)) == 0) {
    rec_put_text(W_MESSAGE, "resumed after reset, reason=0x%lx step=0x%02x",
                 (unsigned long)resetReason, breadcrumb_at_boot());
    cmd_event("LOG resumed %s", file);
    return;
  }
  if (millis() - firstTry > settings.reconnectSec * 1000UL) {
    settings.resume = false;
    settings_save();
    return;
  }
  nextResume = millis() + 10000;
}

// logger_note_time_set(oldMs, newMs): record a clock change in the open file.
void logger_note_time_set(uint64_t oldMs, uint64_t newMs) {
  if (state == LOG_IDLE) return;
  uint8_t p[16];
  put_le64(p, oldMs);
  put_le64(p + 8, newMs);
  rec_put(W_TIME_SET, 0, p, 16);
}

// logger_service(): called from loop(). Flushes the recorder, notices lost
// connections and reconnects, writes periodic battery words and stops on low
// battery or card errors.
void logger_service() {
  if (state == LOG_IDLE) return;
  if (!rec_service(false)) {
    cmd_event("LOG stopped sd_error %s", path);
    rec_error(ERRSRC_SD, 0, "write failed");
    polar_disconnect();
    end_session(END_SD_ERROR);
    return;
  }
  uint8_t reason;
  if (state == LOG_ACTIVE && polar_take_lost(&reason)) {
    rec_put_text(W_MESSAGE, "polar disconnected reason=0x%02X", reason);
    cmd_event("LOG polar_lost");
    state = LOG_RECONNECTING;
    predict_reset(0);
    cad_reset(0);
    lostAt = millis();
    nextTry = 0;
  }
  if (state == LOG_RECONNECTING) {
    if (millis() - lostAt > settings.reconnectSec * 1000UL) {
      cmd_event("LOG stopped polar_lost %s", path);
      end_session(END_POLAR_LOST);
      return;
    }
    if ((int32_t)(millis() - nextTry) >= 0) {
      if (polar_connect(polarAddr, RECONNECT_TRY_MS) && start_streams(NULL, 0)) {
        rec_put_text(W_MESSAGE, "polar reconnected");
        cmd_event("LOG polar_reconnected");
        state = LOG_ACTIVE;
      } else {
        polar_disconnect();
      }
      nextTry = millis() + 2000;
    }
    return;
  }
  if (millis() - lastStatus >= STATUS_PERIOD_MS) {
    write_battery_words();
    uint16_t mv = power_battery_mv();
    lowCount = (settings.lowBatteryMv && mv < settings.lowBatteryMv) ? lowCount + 1 : 0;
    if (lowCount >= 3) {
      cmd_event("LOG stopped low_battery %s", path);
      logger_stop(END_LOW_BATTERY);
    }
  }
}
