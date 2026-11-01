// Polar Measurement Data (PMD) client and Polar device scanner.
//
// Follows "Polar Measurement Data Specification for 3rd Party": connect,
// exchange an ATT MTU of at least 232, request the 2M PHY, enable indications
// on the PMD control point and notifications on PMD data, then read features,
// request settings and start/stop measurements with control point commands.
#include "prec.h"

// FB005C80-02E7-F387-1CAD-8ACD2D8DF0C8 and its characteristics, little endian
static const uint8_t UUID_PMD_SVC[16]  = {0xC8, 0xF0, 0x8D, 0x2D, 0xCD, 0x8A, 0xAD, 0x1C, 0x87, 0xF3, 0xE7, 0x02, 0x80, 0x5C, 0x00, 0xFB};
static const uint8_t UUID_PMD_CP[16]   = {0xC8, 0xF0, 0x8D, 0x2D, 0xCD, 0x8A, 0xAD, 0x1C, 0x87, 0xF3, 0xE7, 0x02, 0x81, 0x5C, 0x00, 0xFB};
static const uint8_t UUID_PMD_DATA[16] = {0xC8, 0xF0, 0x8D, 0x2D, 0xCD, 0x8A, 0xAD, 0x1C, 0x87, 0xF3, 0xE7, 0x02, 0x82, 0x5C, 0x00, 0xFB};

static BLEClientService pmdService(UUID_PMD_SVC);
static BLEClientCharacteristic pmdControl(UUID_PMD_CP);
static BLEClientCharacteristic pmdData(UUID_PMD_DATA);
static BLEClientBas polarBas;
static BLEClientService hrService(UUID16_SVC_HEART_RATE);
static BLEClientCharacteristic hrMeasure(UUID16_CHR_HEART_RATE_MEASUREMENT);

// Latest heart rate (monitor mode only; never written to the data file)
static volatile uint16_t hrBpm = 0;
static volatile uint8_t hrContact = 2;        // 0 no contact, 1 contact, 2 not reported
static volatile uint32_t hrAt = 0;            // millis() of the last measurement, 0 = none
static volatile uint32_t hrCount = 0;         // measurements received on this connection
static uint16_t hrRr[4];                      // newest RR intervals, ms
static volatile uint8_t hrRrN = 0;
static volatile int batPercent = -1;          // cached Polar battery level

static volatile uint16_t conn = BLE_CONN_HANDLE_INVALID;
static volatile bool lost = false;
static volatile uint8_t lostReason = 0;
static volatile uint8_t sink = 0;   // bit per measurement type
static volatile uint32_t packets[MT_COUNT];

static uint8_t cpResp[64];
static volatile uint16_t cpRespLen = 0;
static volatile bool cpRespReady = false;
static const char* lastError = "";

#define CP_TIMEOUT_MS 3000

static const char* const PMD_ERRORS[] = {
  "success", "invalid op code", "invalid measurement type", "not supported", "invalid length",
  "invalid parameter", "already in state", "invalid resolution", "invalid sample rate",
  "invalid range", "invalid mtu", "invalid number of channels", "invalid state", "device in charger"};

// pmd_error_name(code): text for a PMD control point error code, or for the
// negative transport errors returned by the functions below.
const char* pmd_error_name(int code) {
  if (code >= 0 && code < (int)(sizeof(PMD_ERRORS) / sizeof(PMD_ERRORS[0]))) return PMD_ERRORS[code];
  switch (code) {
    case -1: return "not connected";
    case -2: return "write failed";
    case -3: return "no response";
    case -4: return "bad response";
    default: return "unknown error";
  }
}

static void on_data(BLEClientCharacteristic*, uint8_t* d, uint16_t len) {
  if (len < 10) return;
  uint8_t mt = d[0];
  uint64_t ts = get_le64(d + 1);
  if (mt < MT_COUNT) packets[mt]++;
  if (mt == MT_ACC && d[9] == 1) {                 // 16 bit x,y,z frames
    predict_feed(d + 10, (len - 10) / 6);
    cad_feed(d + 10, (len - 10) / 6);
  }
  rec_set_polar_ts(ts);
  // payload = frame type + samples, exactly as sent by the Polar
  if (mt < 8 && (sink & (1 << mt))) rec_put(W_PMD_BASE | mt, ts, d + 9, len - 9);
}

static void on_control(BLEClientCharacteristic*, uint8_t* d, uint16_t len) {
  if (len > sizeof(cpResp)) len = sizeof(cpResp);
  memcpy(cpResp, d, len);
  cpRespLen = len;
  cpRespReady = true;
}

// Heart Rate Measurement (Bluetooth SIG): flags, bpm (u8 or u16), optional
// energy expended (u16), optional RR intervals (u16 each, 1/1024 s).
static void on_hr(BLEClientCharacteristic*, uint8_t* d, uint16_t len) {
  if (len < 2) return;
  if (settings.logHr && rec_is_open()) rec_put(W_HEART_RATE, 0, d, len);
  uint8_t flags = d[0];
  uint16_t i = 1;
  uint16_t bpm = d[i++];
  if (flags & 0x01) { if (len < 3) return; bpm |= d[i++] << 8; }
  if (flags & 0x08) i += 2;
  uint16_t rr[4];
  uint8_t n = 0;
  if (flags & 0x10)
    for (; i + 1 < len; i += 2) {
      uint16_t v = (uint32_t)(d[i] | d[i + 1] << 8) * 1000 / 1024;
      if (n < 4) rr[n++] = v;
      else { memmove(rr, rr + 1, 3 * sizeof(rr[0])); rr[3] = v; }
    }
  taskENTER_CRITICAL();
  hrBpm = bpm;
  hrContact = (flags & 0x04) ? ((flags & 0x02) ? 1 : 0) : 2;
  memcpy(hrRr, rr, n * sizeof(rr[0]));
  hrRrN = n;
  hrAt = millis() | 1;
  hrCount++;
  taskEXIT_CRITICAL();
}

static void on_battery(BLEClientCharacteristic*, uint8_t* d, uint16_t len) {
  if (len) batPercent = d[0];
}

static void on_connect(uint16_t h) { conn = h; lost = false; }

static void on_disconnect(uint16_t h, uint8_t reason) {
  if (h != conn) return;
  conn = BLE_CONN_HANDLE_INVALID;
  hrAt = 0;
  batPercent = -1;
  lostReason = reason;
  lost = true;
}

// polar_init(): register the PMD client service and central callbacks. Call after Bluefruit.begin().
void polar_init() {
  pmdService.begin();
  pmdControl.setIndicateCallback(on_control, false);
  pmdControl.begin();
  pmdData.setNotifyCallback(on_data, false);
  pmdData.begin();
  polarBas.begin();
  polarBas.setNotifyCallback(on_battery, false);
  hrService.begin();
  hrMeasure.setNotifyCallback(on_hr, false);
  hrMeasure.begin();
  Bluefruit.Central.setConnectCallback(on_connect);
  Bluefruit.Central.setDisconnectCallback(on_disconnect);
}

// polar_connected(): true while the Polar connection is up.
bool polar_connected() { return conn != BLE_CONN_HANDLE_INVALID; }

// polar_take_lost(reason): true (once) if the Polar dropped the connection since the last call.
bool polar_take_lost(uint8_t* reason) {
  if (!lost) return false;
  lost = false;
  if (reason) *reason = lostReason;
  return true;
}

// polar_mtu(): negotiated ATT MTU of the Polar connection (0 if not connected).
uint16_t polar_mtu() {
  BLEConnection* c = polar_connected() ? Bluefruit.Connection(conn) : NULL;
  return c ? c->getMtu() : 0;
}

// The MTU exchange is asynchronous and must not overlap a GATT discovery,
// so it runs after discovery and is awaited here.
static bool exchange_mtu(uint16_t h) {
  BLEConnection* c = Bluefruit.Connection(h);
  if (!c) return false;
  breadcrumb(BC_MTU);
  if (c->getMtu() < 232) {
    c->requestMtuExchange(247);
    uint32_t t0 = millis();
    while (c->getMtu() < 232 && polar_connected() && millis() - t0 < 2000) idle_service();
  }
  c->requestDataLengthUpdate();
  return c->getMtu() >= 232;
}

// polar_connect(addr, timeoutMs): connect to the Polar at addr and prepare
// the PMD service (discovery, MTU, PHY, CCCDs). Blocking; returns success.
// On failure polar_last_error() names the step that failed.
bool polar_connect(const ble_gap_addr_t& addr, uint32_t timeoutMs) {
  if (polar_connected()) polar_disconnect();
  if (Bluefruit.Scanner.isRunning()) Bluefruit.Scanner.stop();
  lost = false;
  lastError = "connect request rejected";
  breadcrumb(BC_CONNECT);
  if (!Bluefruit.Central.connect(&addr)) return false;
  uint32_t t0 = millis();
  while (!polar_connected() && millis() - t0 < timeoutMs) idle_service();
  if (!polar_connected()) {
    sd_ble_gap_connect_cancel();
    lastError = "not found (is the Polar on and not connected elsewhere?)";
    return false;
  }
  uint16_t h = conn;
  BLEConnection* c = Bluefruit.Connection(h);
  if (c) c->requestPHY(BLE_GAP_PHY_2MBPS);
  bool ok = false;
  breadcrumb(BC_DISCOVER);
  if (!pmdService.discover(h)) lastError = "no PMD service";
  else if (!pmdControl.discover() || !pmdData.discover()) lastError = "no PMD characteristics";
  else {
    polarBas.discover(h);
    // Heart rate and battery for monitor mode: optional, a failure does not matter
    if (hrService.discover(h) && hrMeasure.discover()) hrMeasure.enableNotify();
    hrAt = 0;
    hrCount = 0;
    if (polarBas.discovered()) {
      batPercent = polarBas.read();
      polarBas.enableNotify();
    }
    if (!pmdControl.enableIndicate() || !pmdData.enableNotify()) lastError = "cannot enable PMD notifications";
    else if (!exchange_mtu(h)) lastError = "MTU too small";   // the Polar refuses to stream with less
    else ok = true;
  }
  if (!ok) polar_disconnect();
  else lastError = "";
  return ok;
}

// polar_conn_handle(): connection handle of the Polar link (BLE_CONN_HANDLE_INVALID if none).
uint16_t polar_conn_handle() { return conn; }

// polar_last_error(): why the last polar_connect() failed ("" after success).
const char* polar_last_error() { return lastError; }

// polar_disconnect(): drop the Polar connection (the Polar stops all streams).
void polar_disconnect() {
  uint16_t h = conn;
  if (h == BLE_CONN_HANDLE_INVALID) return;
  breadcrumb(BC_DISCONNECT);
  Bluefruit.disconnect(h);
  uint32_t t0 = millis();
  while (polar_connected() && millis() - t0 < 2000) idle_service();
  lost = false;
}

// Send a control point command and wait for its response.
// Returns the PMD error code (0 = success) or a negative transport error.
static int cp_request(const uint8_t* req, uint16_t len) {
  if (!polar_connected()) return -1;
  breadcrumb(BC_CP);
  cpRespReady = false;
  if (!pmdControl.write_resp(req, len)) return -2;
  uint32_t t0 = millis();
  while (!cpRespReady && millis() - t0 < CP_TIMEOUT_MS) idle_service();
  if (!cpRespReady) return -3;
  if (cpRespLen < 4 || cpResp[0] != 0xF0 || cpResp[1] != req[0] || cpResp[2] != req[1]) return -4;
  return cpResp[3];
}

// polar_read_features(mask): read the PMD control point; *mask gets one bit per
// supported measurement type (bit 0 ECG, 2 ACC, 5 gyro, 6 mag...). Returns 0 or an error.
int polar_read_features(uint8_t* mask) {
  if (!polar_connected()) return -1;
  uint8_t b[20];
  uint16_t n = pmdControl.read(b, sizeof(b));
  if (n < 2 || b[0] != 0x0F) return -4;
  *mask = b[1];
  return 0;
}

// polar_get_settings(mt, out, len): "Get measurement settings" for type mt.
// out receives the setting TLVs (type, count, values...) from the response.
int polar_get_settings(uint8_t mt, uint8_t* out, uint16_t* len) {
  uint8_t req[2] = {0x01, mt};
  int e = cp_request(req, 2);
  *len = 0;
  if (e == 0 && cpRespLen > 5) {
    *len = cpRespLen - 5;
    memcpy(out, cpResp + 5, *len);
  }
  return e;
}

// polar_start(mt, f, req, reqLen, resp, respLen): "Start measurement" for type mt
// with the non-zero settings in f. req/reqLen receive the settings bytes sent
// (after op code and type), resp/respLen the response parameters (after the
// 5 byte response header, e.g. reserved byte or FACTOR). Returns the PMD error code.
int polar_start(uint8_t mt, const StreamFormat& f, uint8_t* req, uint16_t* reqLen, uint8_t* resp, uint16_t* respLen) {
  uint8_t r[24] = {0x02, mt};
  uint16_t n = 2;
  if (f.rate)  { r[n++] = PMD_SET_RATE;  r[n++] = 1; put_le16(r + n, f.rate);  n += 2; }
  if (f.res)   { r[n++] = PMD_SET_RES;   r[n++] = 1; put_le16(r + n, f.res);   n += 2; }
  if (f.range) { r[n++] = PMD_SET_RANGE; r[n++] = 1; put_le16(r + n, f.range); n += 2; }
  if (f.ch)    { r[n++] = PMD_SET_CHANNELS; r[n++] = 1; r[n++] = f.ch; }
  int e = cp_request(r, n);
  if (e == 6) {            // already streaming (e.g. after a reconnect): restart it
    polar_stop(mt);
    e = cp_request(r, n);
  }
  memcpy(req, r + 2, n - 2);
  *reqLen = n - 2;
  *respLen = 0;
  if (e >= 0 && cpRespLen > 5) {
    *respLen = cpRespLen - 5;
    memcpy(resp, cpResp + 5, *respLen);
  }
  return e;
}

// polar_stop(mt): "Stop measurement" for type mt. Returns the PMD error code.
int polar_stop(uint8_t mt) {
  uint8_t req[2] = {0x03, mt};
  return cp_request(req, 2);
}

// polar_battery(): battery level in percent from the Battery Service, -1 if unavailable.
int polar_battery() {
  if (!polar_connected() || !polarBas.discovered()) return -1;
  breadcrumb(BC_BATTERY);
  batPercent = polarBas.read();
  return batPercent;
}

// polar_battery_cached(): last known Polar battery level without radio traffic,
// -1 if unknown (read at connect, by polar_battery() and from notifications).
int polar_battery_cached() { return polar_connected() ? batPercent : -1; }

// polar_heart_rate(out): latest heart rate measurement. Returns false if none
// has arrived on the current connection.
bool polar_heart_rate(HeartRate* out) {
  taskENTER_CRITICAL();
  out->bpm = hrBpm;
  out->contact = hrContact;
  out->ageMs = hrAt ? millis() - hrAt : 0;
  out->count = hrCount;
  out->rrN = hrRrN;
  memcpy(out->rr, hrRr, sizeof(hrRr));
  bool ok = hrAt != 0 && polar_connected();
  taskEXIT_CRITICAL();
  return ok;
}

// polar_set_sink(mask): forward PMD data notifications of the measurement
// types whose bit is set in mask to the recorder; others are only counted.
void polar_set_sink(uint8_t mask) { sink = mask; }

// polar_reset_packets(): zero the per-type packet counters.
void polar_reset_packets() { memset((void*)packets, 0, sizeof(packets)); }

// polar_packets(mt): PMD data packets received for type mt since polar_reset_packets().
uint32_t polar_packets(uint8_t mt) { return mt < MT_COUNT ? packets[mt] : 0; }

// ---- scanning -------------------------------------------------------------
static ScanResult* scanOut;
static volatile int scanCount, scanMax;

static void on_scan(ble_gap_evt_adv_report_t* r) {
  char name[32] = {0};
  if (!Bluefruit.Scanner.parseReportByType(r, BLE_GAP_AD_TYPE_COMPLETE_LOCAL_NAME, (uint8_t*)name, sizeof(name) - 1))
    Bluefruit.Scanner.parseReportByType(r, BLE_GAP_AD_TYPE_SHORT_LOCAL_NAME, (uint8_t*)name, sizeof(name) - 1);
  if (!strncmp(name, "Polar ", 6)) {
    bool seen = false;
    for (int i = 0; i < scanCount; i++)
      if (!memcmp(scanOut[i].addr.addr, r->peer_addr.addr, 6)) seen = true;
    if (!seen && scanCount < scanMax) {
      scanOut[scanCount].addr = r->peer_addr;
      scanOut[scanCount].rssi = r->rssi;
      strlcpy(scanOut[scanCount].name, name, sizeof(scanOut[0].name));
      scanCount++;
    }
  }
  Bluefruit.Scanner.resume();
}

// polar_scan(seconds, out, max): actively scan for devices whose name starts
// with "Polar " for the given time. Blocking; returns the number found.
int polar_scan(uint16_t seconds, ScanResult* out, int max) {
  breadcrumb(BC_SCAN);
  scanOut = out;
  scanCount = 0;
  scanMax = max;
  Bluefruit.Scanner.setRxCallback(on_scan);
  Bluefruit.Scanner.restartOnDisconnect(false);
  Bluefruit.Scanner.useActiveScan(true);
  Bluefruit.Scanner.setInterval(160, 80);
  Bluefruit.Scanner.clearFilters();
  Bluefruit.Scanner.start(seconds * 100);
  uint32_t t0 = millis();
  while (Bluefruit.Scanner.isRunning() && millis() - t0 < seconds * 1000UL + 500) idle_service();
  Bluefruit.Scanner.stop();
  return scanCount;
}
