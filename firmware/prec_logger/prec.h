// PREC logger - shared declarations.
//
// The firmware is split into modules, each with a short prefix:
//   clock_*    real time clock (PCF8523 on the Adalogger) with millisecond anchoring
//   storage_*  SD card, settings and known-device files
//   rec_*      PREC binary stream writer with a double RAM buffer
//   polar_*    Polar Measurement Data (PMD) client for the Polar H10
//   logger_*   logging session state machine
//   power_*    battery, motion detection and sleep
//   cmd_*      text command protocol over BLE (Nordic UART) and USB serial
// See docs/ for the full description of every function.
#pragma once
#include <Arduino.h>
#include <bluefruit.h>
#include <SdFat.h>

#define PREC_FW_VERSION      "1.0.0"
#define PREC_FORMAT_VERSION  1

// ---- hardware -------------------------------------------------------------
#define PIN_SD_CS    10   // Adalogger FeatherWing SD chip select
#define PIN_IMU_INT  3    // LSM6DS3TR-C INT1 (P1.11)
#define IMU_ADDR     0x6A

// ---- file system layout ---------------------------------------------------
#define DIR_PREC       "/prec"
#define DIR_DATA       "/prec/data"
#define DIR_SETTINGS   "/prec/settings"
#define FILE_SETTINGS  "/prec/settings/settings.txt"
#define FILE_DEVICES   "/prec/settings/devices.txt"
#define DATA_EXT       ".prc"

// ---- PREC word types (see docs/PREC_FORMAT.md) -----------------------------
#define WORD_HEADER_SIZE 19
enum : uint8_t {
  W_SESSION_START   = 0x01,
  W_SESSION_END     = 0x02,
  W_STREAM_START    = 0x03,
  W_STREAM_STOP     = 0x04,
  W_POLAR_BATTERY   = 0x10,
  W_FEATHER_BATTERY = 0x11,
  W_TIME_SET        = 0x12,
  W_DROPPED         = 0x13,
  W_HEART_RATE      = 0x14,  // the Polar's Heart Rate Measurement, as received (settings.logHr)
  W_CADENCE         = 0x15,  // u16 steps/min x 10, u8 method (0 counting, 1 DFT) (settings.logCad)
  W_ERROR           = 0x20,
  W_MESSAGE         = 0x21,
  W_PMD_BASE        = 0x80,  // 0x80 | PMD measurement type
};

// SESSION_END reasons
enum : uint8_t { END_STOP = 0, END_POLAR_LOST = 1, END_LOW_BATTERY = 2, END_SD_ERROR = 3, END_NO_STREAMS = 4 };
// ERROR word sources
enum : uint8_t { ERRSRC_FEATHER = 0, ERRSRC_SD = 1, ERRSRC_BLE = 2, ERRSRC_PMD = 3 };

// ---- PMD measurement types ------------------------------------------------
enum : uint8_t { MT_ECG = 0, MT_PPG = 1, MT_ACC = 2, MT_PPI = 3, MT_GYRO = 5, MT_MAG = 6 };
#define MT_COUNT 7
const char* mt_name(uint8_t mt);       // "acc", ... or NULL
int mt_from_name(const char* name);    // -1 if unknown

// PMD setting types used in requests
enum : uint8_t { PMD_SET_RATE = 0, PMD_SET_RES = 1, PMD_SET_RANGE = 2, PMD_SET_RANGE_MILLI = 3, PMD_SET_CHANNELS = 4, PMD_SET_FACTOR = 5 };

// ---- settings -------------------------------------------------------------
struct StreamFormat {
  bool     on;
  uint16_t rate;    // Hz, 0 = not sent
  uint16_t res;     // bits, 0 = not sent
  uint16_t range;   // G / dps / gauss, 0 = not sent
  uint8_t  ch;      // channels, 0 = not sent
};

struct Settings {
  char     password[33];
  char     defaultAddr[18];   // "AA:BB:CC:DD:EE:FF" or ""
  StreamFormat fmt[MT_COUNT]; // indexed by PMD measurement type
  uint16_t sleepSec;          // idle seconds without motion before sleeping, 0 = never
  uint16_t reconnectSec;      // how long to try to get a lost Polar back while logging
  uint16_t flushSec;          // max seconds logged data stays in RAM
  uint16_t lowBatteryMv;      // stop logging below this battery voltage, 0 = never
  uint16_t predictMs;         // run the built-in speed model this often while logging, 0 = never
  uint16_t classLpS;          // low-pass time constant (s) of the class probabilities, 0 = off
  uint16_t speedLpS;          // low-pass time constant (s) of the predicted speed, 0 = off
  bool     logHr;             // write HEART_RATE words while logging
  bool     logCad;            // write CADENCE words while logging
  uint8_t  cadAxis;           // ACC axis that points up and down (0 x, 1 y, 2 z) for the cadence
  uint16_t cadLpS;            // low-pass time constant (s) of the cadence, 0 = off
  bool     resume;            // a session was running: restart it after a reset
};
extern Settings settings;

struct PolarDevice {
  ble_gap_addr_t addr;
  char name[32];
};
#define MAX_DEVICES 16
extern PolarDevice devices[MAX_DEVICES];
extern uint8_t deviceCount;

// ---- clock.cpp ------------------------------------------------------------
bool     clock_begin();
void     clock_anchor();
uint64_t clock_now_ms();
void     clock_set_ms(uint64_t unixMs, uint32_t receivedAtMillis);
void     clock_format(uint64_t unixMs, char* out, size_t n, bool compact);
bool     clock_valid();

// ---- storage.cpp ----------------------------------------------------------
extern SdFs sd;
bool storage_begin();
bool storage_ok();
void settings_defaults();
bool settings_load();
bool settings_save();
bool devices_load();
bool devices_save();
int  devices_find(const ble_gap_addr_t& addr);
int  devices_add(const ble_gap_addr_t& addr, const char* name);
bool addr_parse(const char* s, ble_gap_addr_t* out);
void addr_format(const ble_gap_addr_t& a, char out[18]);
bool format_apply(StreamFormat& f, int argc, char** argv);
void format_print(uint8_t mt, const StreamFormat& f, char* out, size_t n);

// ---- recorder.cpp ---------------------------------------------------------
bool     rec_open(const char* path);
bool     rec_put(uint8_t type, uint64_t polarTs, const void* payload, uint16_t len);
bool     rec_put_text(uint8_t type, const char* fmt, ...);
bool     rec_error(uint8_t source, uint8_t code, const char* text);
void     rec_set_polar_ts(uint64_t ts);
uint64_t rec_polar_ts();
bool     rec_service(bool force);
void     rec_close();
bool     rec_is_open();
uint32_t rec_bytes();
uint32_t rec_dropped();
bool     rec_failed();

// ---- polar.cpp ------------------------------------------------------------
void polar_init();
bool polar_connect(const ble_gap_addr_t& addr, uint32_t timeoutMs);
void polar_disconnect();
const char* polar_last_error();
uint16_t polar_conn_handle();
struct HeartRate { uint16_t bpm; uint8_t contact; uint32_t ageMs; uint32_t count; uint16_t rr[4]; uint8_t rrN; };
bool polar_heart_rate(HeartRate* out);
int  polar_battery_cached();
bool polar_connected();
bool polar_take_lost(uint8_t* reason);
int  polar_read_features(uint8_t* mask);
int  polar_get_settings(uint8_t mt, uint8_t* out, uint16_t* len);
int  polar_start(uint8_t mt, const StreamFormat& f, uint8_t* req, uint16_t* reqLen, uint8_t* resp, uint16_t* respLen);
int  polar_stop(uint8_t mt);
int  polar_battery();
uint16_t polar_mtu();
void polar_set_sink(uint8_t mask);
void polar_reset_packets();
uint32_t polar_packets(uint8_t mt);
const char* pmd_error_name(int code);

// ---- scan (polar.cpp) -----------------------------------------------------
struct ScanResult { ble_gap_addr_t addr; int8_t rssi; char name[32]; };
#define MAX_SCAN 16
int polar_scan(uint16_t seconds, ScanResult* out, int max);

// ---- logger.cpp -----------------------------------------------------------
enum LoggerState : uint8_t { LOG_IDLE = 0, LOG_ACTIVE = 1, LOG_RECONNECTING = 2 };
LoggerState logger_state();
int  logger_start(char* fileOut, size_t n, char* info, size_t infoN);
int  logger_stop(uint8_t reason);
void logger_service();
const char* logger_file();
uint8_t logger_streams();
uint32_t logger_runtime_ms();   // since the session started, 0 when idle
void logger_note_time_set(uint64_t oldMs, uint64_t newMs);
void logger_resume_service();

// ---- power.cpp ------------------------------------------------------------
void     power_begin();
bool     power_motion();
uint16_t power_battery_mv();
void     power_sleep();
void     power_led(bool on);

// ---- cadence.cpp ----------------------------------------------------------
void  cad_reset(uint16_t rate);
void  cad_feed(const uint8_t* samples, uint16_t n);
void  cad_spectrum(const float* mag, int bins, float binHz);
float cad_get(uint8_t* method);   // steps/min, 0 = none; method 0 counting, 1 DFT
void  cad_service();

// ---- commands.cpp ---------------------------------------------------------
void cmd_begin();
void cmd_poll();

// ---- transfer.cpp ---------------------------------------------------------
void xfer_begin();
int  xfer_start(bool usb, uint16_t connHandle, const char* name, uint32_t offset, uint32_t* sizeOut);
void xfer_usb_flush();
void xfer_stop();
bool xfer_active();
void xfer_service();
void xfer_progress(uint32_t* sentOut, uint32_t* msOut);

// ---- predictor.cpp --------------------------------------------------------
void predict_reset(uint16_t rate);
void predict_feed(const uint8_t* samples, uint16_t n);
void predict_service();
struct Prediction {
  float speed;     // m/s after the low-pass (0 while still or stretching)
  float raw;       // m/s straight from the model
  float dist;      // m: the filtered speed integrated since the stream started
  int8_t cls;      // class from the classifier (0 run, 1 walk, 2 still, 3 stretch), -1 = none
  uint32_t ageMs;  // since the prediction
  uint32_t n;      // predictions so far
  uint32_t ms;     // time the last prediction took
};
bool predict_get(Prediction* p);
const char* predict_class_name(int8_t cls);
const char* predict_model();
const char* predict_state();
void cmd_event(const char* fmt, ...);
bool cmd_ble_connected();
uint32_t cmd_last_activity();

// ---- sha256.cpp -----------------------------------------------------------
void sha256(const uint8_t* data, size_t len, uint8_t out[32]);

// ---- helpers --------------------------------------------------------------
void idle_service();   // keeps the recorder flowing while waiting inside a blocking call
void watchdog_feed();
void breadcrumb(uint8_t step);   // last step reached, kept across a reset (GPREGRET2)
uint8_t breadcrumb_at_boot();
extern uint32_t resetReason;
// breadcrumb steps (commands use 0x40 + their table index)
enum : uint8_t { BC_LOOP = 0x01, BC_LOGGER = 0x02, BC_MOTION = 0x03, BC_REC = 0x04,
                 BC_CONNECT = 0x10, BC_DISCOVER = 0x11, BC_MTU = 0x12, BC_CP = 0x13,
                 BC_DISCONNECT = 0x14, BC_SCAN = 0x15, BC_BATTERY = 0x16, BC_XFER = 0x17, BC_PREDICT = 0x18 };
static inline void put_le16(uint8_t* p, uint16_t v) { p[0] = v; p[1] = v >> 8; }
static inline void put_le32(uint8_t* p, uint32_t v) { for (int i = 0; i < 4; i++) p[i] = v >> (8 * i); }
static inline void put_le64(uint8_t* p, uint64_t v) { for (int i = 0; i < 8; i++) p[i] = v >> (8 * i); }
static inline uint32_t get_le32(const uint8_t* p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static inline uint64_t get_le64(const uint8_t* p) { uint64_t v = 0; for (int i = 7; i >= 0; i--) v = (v << 8) | p[i]; return v; }
