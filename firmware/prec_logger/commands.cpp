// Text command protocol, served on two channels:
//   * BLE: Nordic UART Service, password protected (challenge-response)
//   * USB serial: trusted, no password needed
// A request is one line "COMMAND arg ...". The reply is zero or more data
// lines starting with "+ " followed by exactly one "OK ..." or "ERR <code> <text>"
// line. Lines starting with "! " are unsolicited events. See docs/COMMANDS.md.
#include "prec.h"
#include <stdarg.h>
#include "tusb.h"

BLEUart bleuart;

struct Channel {
  Stream* io;
  bool ble;
  bool authed;
  uint8_t fails;
  char nonce[17];
  char line[160];
  uint8_t n;
};

static Channel usb = {&Serial, false, true, 0, "", "", 0};
static Channel ble = {&bleuart, true, false, 0, "", "", 0};
static volatile bool bleConnected = false;
static uint16_t bleConn = BLE_CONN_HANDLE_INVALID;
static uint32_t lastActivity = 0;
extern uint32_t resetReason;

// Hosts autosuspend an idle USB serial port (Linux: after 2 s, even while it
// is open). A reply produced by a slow command would then be discarded, so
// wake the host first.
static void usb_wake() {
  if (!tud_mounted() || !tud_suspended()) return;
  tud_remote_wakeup();
  uint32_t t0 = millis();
  while (tud_suspended() && millis() - t0 < 100) delay(1);
}

// Send over the BLE UART one notification (MTU - 3 bytes) at a time,
// retrying while the SoftDevice's notification queue is full, so long
// replies such as READ are never silently truncated.
static bool ble_write(const uint8_t* p, size_t n) {
  uint32_t t0 = millis();
  while (n && bleConnected) {
    BLEConnection* c = Bluefruit.Connection(bleConn);
    if (!c) return false;
    size_t k = min(n, (size_t)(c->getMtu() - 3));
    if (bleuart.write(p, k) == k) {
      p += k;
      n -= k;
      t0 = millis();
    } else {
      if (millis() - t0 > 3000) return false;
      watchdog_feed();
      delay(5);
    }
  }
  return n == 0;
}

static void send(Channel& ch, const char* fmt, ...) {
  char out[400];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(out, sizeof(out) - 1, fmt, ap);
  va_end(ap);
  if (n > (int)sizeof(out) - 2) n = sizeof(out) - 2;
  out[n++] = '\n';
  if (ch.ble) {
    ble_write((const uint8_t*)out, n);
  } else {
    usb_wake();
    xfer_usb_flush();      // never put text inside a binary transfer frame
    if (Serial) Serial.write(out, n);
  }
}

// cmd_event(fmt, ...): send "! <text>" to USB and to an authenticated BLE client.
void cmd_event(const char* fmt, ...) {
  char text[160];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(text, sizeof(text), fmt, ap);
  va_end(ap);
  send(usb, "! %s", text);
  if (bleConnected && ble.authed) send(ble, "! %s", text);
}

// cmd_ble_connected(): true while a command device is connected over BLE.
bool cmd_ble_connected() { return bleConnected; }
// cmd_last_activity(): millis() of the last received command byte.
uint32_t cmd_last_activity() { return lastActivity; }

static void reset_channel(Channel& ch) {
  ch.authed = !ch.ble;
  ch.fails = 0;
  ch.nonce[0] = 0;
  ch.n = 0;
}

static void on_ble_connect(uint16_t h) {
  // Long packets and the 2M PHY; the connection interval is left to the
  // client except during file transfers (see transfer.cpp).
  BLEConnection* c = Bluefruit.Connection(h);
  if (c) {
    c->requestDataLengthUpdate();
    c->requestPHY(BLE_GAP_PHY_2MBPS);
  }
  bleConn = h;
  reset_channel(ble);
  bleConnected = true;
}

static void on_ble_disconnect(uint16_t h, uint8_t) {
  if (h != bleConn) return;
  bleConnected = false;
  bleConn = BLE_CONN_HANDLE_INVALID;
  reset_channel(ble);
  xfer_stop();
}

static void to_hex(const uint8_t* b, size_t n, char* out) {
  for (size_t i = 0; i < n; i++) sprintf(out + 2 * i, "%02x", b[i]);
}

static int from_hex(const char* s, uint8_t* out, size_t max) {
  size_t n = strlen(s);
  if (n % 2 || n / 2 > max) return -1;
  for (size_t i = 0; i < n / 2; i++) {
    unsigned v;
    if (sscanf(s + 2 * i, "%2x", &v) != 1) return -1;
    out[i] = v;
  }
  return n / 2;
}

// digest = SHA-256(nonce + password + suffix)
static void keyed_hash(const char* nonce, const char* suffix, uint8_t out[32]) {
  char msg[17 + 33 + 16];
  snprintf(msg, sizeof(msg), "%s%s%s", nonce, settings.password, suffix);
  sha256((const uint8_t*)msg, strlen(msg), out);
}

// ---- command handlers ----------------------------------------------------
typedef void (*Handler)(Channel& ch, int argc, char** argv);

static void c_help(Channel& ch, int, char**);

static void c_hello(Channel& ch, int, char**) {
  uint8_t r[8];
  for (int i = 0; i < 8; i++) r[i] = random(256);
  sd_rand_application_vector_get(r, 8);
  to_hex(r, 8, ch.nonce);
  char name[32] = {0};
  Bluefruit.getName(name, sizeof(name) - 1);
  send(ch, "OK PREC %s %s %s", PREC_FW_VERSION, name, ch.nonce);
}

static void c_auth(Channel& ch, int argc, char** argv) {
  if (argc != 2 || !ch.nonce[0]) return send(ch, "ERR 400 send HELLO first, then AUTH <hex>");
  uint8_t want[32], got[32];
  keyed_hash(ch.nonce, "", want);
  int n = from_hex(argv[1], got, 32);
  uint8_t diff = n == 32 ? 0 : 1;
  for (int i = 0; i < 32; i++) diff |= want[i] ^ got[i];
  ch.nonce[0] = 0;   // one attempt per nonce
  if (!diff) {
    ch.authed = true;
    ch.fails = 0;
    return send(ch, "OK");
  }
  send(ch, "ERR 401 wrong password");
  if (ch.ble && ++ch.fails >= 3) {
    delay(100);
    Bluefruit.disconnect(bleConn);
  }
}

static void c_status(Channel& ch, int, char**) {
  HeartRate h;
  char hrText[8] = "-", pbatText[8] = "-";
  if (polar_heart_rate(&h)) snprintf(hrText, sizeof(hrText), "%u", h.bpm);
  if (polar_battery_cached() >= 0) snprintf(pbatText, sizeof(pbatText), "%d", polar_battery_cached());
  static const char* const STATES[] = {"idle", "logging", "reconnecting"};
  char streams[40] = "", pk[64] = "", t[32];
  for (uint8_t mt = 0; mt < MT_COUNT; mt++)
    if (logger_streams() & (1 << mt)) {
      snprintf(streams + strlen(streams), sizeof(streams) - strlen(streams), "%s%s", streams[0] ? "," : "", mt_name(mt));
      snprintf(pk + strlen(pk), sizeof(pk) - strlen(pk), "%s%s:%lu", pk[0] ? "," : "", mt_name(mt), (unsigned long)polar_packets(mt));
    }
  uint64_t now = clock_now_ms();
  clock_format(now, t, sizeof(t), false);
  send(ch, "OK state=%s file=%s bytes=%lu dropped=%lu polar=%s streams=%s packets=%s bat=%u time=%s clock=%s sd=%s default=%s reset=0x%lx crumb=0x%02x up=%lu hr=%s pbat=%s model=%s",
       STATES[logger_state()], logger_state() ? logger_file() : "-", (unsigned long)(logger_state() ? rec_bytes() : 0), (unsigned long)rec_dropped(),
       polar_connected() ? "connected" : "disconnected", streams[0] ? streams : "-", pk[0] ? pk : "-",
       power_battery_mv(), t, clock_valid() ? "ok" : "unset", storage_ok() ? "ok" : "fail",
       settings.defaultAddr[0] ? settings.defaultAddr : "-", (unsigned long)resetReason, breadcrumb_at_boot(), (unsigned long)(millis() / 1000), hrText, pbatText, predict_model());
}

// HR: everything a monitor screen shows, cheap enough to poll every second.
static void c_hr(Channel& ch, int, char**) {
  static const char* const STATES[] = {"idle", "logging", "reconnecting"};
  static const char* const CONTACT[] = {"no", "yes", "-"};
  HeartRate h;
  bool have = polar_heart_rate(&h);
  char hr[8] = "-", rr[32] = "-", pbat[8] = "-";
  if (have) {
    snprintf(hr, sizeof(hr), "%u", h.bpm);
    if (h.rrN) {
      rr[0] = 0;
      for (int i = 0; i < h.rrN; i++) snprintf(rr + strlen(rr), sizeof(rr) - strlen(rr), "%s%u", i ? "," : "", h.rr[i]);
    }
  }
  int pb = polar_battery_cached();
  if (pb >= 0) snprintf(pbat, sizeof(pbat), "%d", pb);
  Prediction pr;
  char speed[12] = "-", raw[12] = "-";
  bool fresh = predict_get(&pr);
  if (fresh) {
    snprintf(speed, sizeof(speed), "%.2f", pr.speed);
    snprintf(raw, sizeof(raw), "%.2f", pr.raw);
  }
  char mstate[40];
  strlcpy(mstate, predict_state(), sizeof(mstate));
  for (char* q = mstate; *q; q++) if (*q == ' ') *q = '_';
  uint8_t cm;
  float cad = cad_get(&cm);
  char cadText[12] = "-";
  if (cad > 0) snprintf(cadText, sizeof(cadText), "%.0f", cad);
  send(ch, "OK state=%s polar=%s hr=%s contact=%s age=%lu beats=%lu rr=%s pbat=%s bat=%u file=%s bytes=%lu "
           "speed=%s rspeed=%s cls=%s dist=%.0f sage=%lu preds=%lu pms=%lu model=%s mstate=%s cad=%s cadm=%s rt=%lu",
       STATES[logger_state()], polar_connected() ? "connected" : "disconnected", hr,
       have ? CONTACT[h.contact] : "-", have ? (unsigned long)h.ageMs : 0UL, have ? (unsigned long)h.count : 0UL,
       rr, pbat, power_battery_mv(), logger_state() ? logger_file() : "-",
       (unsigned long)(logger_state() ? rec_bytes() : 0), speed, raw, predict_class_name(pr.cls), pr.dist,
       (unsigned long)(fresh ? pr.ageMs : 0), (unsigned long)pr.n, (unsigned long)pr.ms, predict_model(), mstate,
       cadText, cad > 0 ? (cm ? "dft" : "count") : "-", (unsigned long)(logger_runtime_ms() / 1000));
}

static void c_time(Channel& ch, int argc, char** argv) {
  if (argc >= 2) {
    uint32_t at = millis();
    char* end;
    uint64_t ms = strtoull(argv[1], &end, 10);
    if (*end || ms < 1500000000000ULL) return send(ch, "ERR 400 TIME <unix milliseconds>");
    uint64_t old = clock_now_ms();
    clock_set_ms(ms, at);
    logger_note_time_set(old, clock_now_ms());
  }
  char t[32];
  uint64_t now = clock_now_ms();
  clock_format(now, t, sizeof(t), false);
  send(ch, "OK %lu%03u %s", (unsigned long)(now / 1000), (unsigned)(now % 1000), t);
}

static void c_ls(Channel& ch, int, char**) {
  FsFile dir = sd.open(DIR_DATA, O_RDONLY);
  if (!dir) return send(ch, "ERR 500 cannot open " DIR_DATA);
  FsFile f;
  int count = 0;
  char name[64];
  while (f.openNext(&dir, O_RDONLY)) {
    if (!f.isDir()) {
      f.getName(name, sizeof(name));
      send(ch, "+ %s %lu", name, (unsigned long)f.fileSize());
      count++;
    }
    f.close();
  }
  dir.close();
  send(ch, "OK %d", count);
}

static void c_read(Channel& ch, int argc, char** argv) {
  if (argc != 4 || strchr(argv[1], '/') || strstr(argv[1], ".."))
    return send(ch, "ERR 400 READ <file> <offset> <length (max 4096)>");
  uint32_t off = strtoul(argv[2], NULL, 10), len = strtoul(argv[3], NULL, 10);
  if (len > 4096) len = 4096;
  char path[64];
  snprintf(path, sizeof(path), DIR_DATA "/%s", argv[1]);
  if (!strcmp(path, logger_file())) return send(ch, "ERR 409 file is being logged");
  FsFile f = sd.open(path, O_RDONLY);
  if (!f) return send(ch, "ERR 404 no such file");
  uint32_t size = f.fileSize();
  f.seekSet(off);
  uint8_t b[96];
  char hex[193];
  uint32_t total = 0;
  while (total < len) {
    int n = f.read(b, min((uint32_t)sizeof(b), len - total));
    if (n <= 0) break;
    to_hex(b, n, hex);
    send(ch, "+ %s", hex);
    total += n;
  }
  f.close();
  send(ch, "OK %lu %lu", (unsigned long)total, (unsigned long)size);
}

static void c_send(Channel& ch, int argc, char** argv) {
  if (argc == 2 && !strcasecmp(argv[1], "STOP")) {
    xfer_stop();
    return send(ch, "OK");
  }
  if (argc < 2 || argc > 3 || strchr(argv[1], '/') || strstr(argv[1], ".."))
    return send(ch, "ERR 400 SEND <file> [offset] | SEND STOP");
  uint32_t size = 0;
  int e = xfer_start(!ch.ble, bleConn, argv[1], argc == 3 ? strtoul(argv[2], NULL, 10) : 0, &size);
  switch (e) {
    case 0: return send(ch, "OK %lu %s", (unsigned long)size, argc == 3 ? argv[2] : "0");
    case 404: return send(ch, "ERR 404 no such file");
    case 412: return send(ch, "ERR 412 enable notifications on the transfer characteristic first");
    default: return send(ch, "ERR 409 %s", xfer_active() ? "a transfer is running" : "file is being logged");
  }
}

static void link_line(Channel& ch, const char* who, uint16_t h) {
  BLEConnection* c = h == BLE_CONN_HANDLE_INVALID ? NULL : Bluefruit.Connection(h);
  if (!c) return send(ch, "+ %s none", who);
  static const char* const PHY[] = {"auto", "1M", "2M", "?", "coded"};
  uint8_t phy = c->getPHY();
  send(ch, "+ %s interval=%u.%02ums latency=%u timeout=%ums mtu=%u datalength=%u phy=%s", who,
       c->getConnectionInterval() * 125 / 100, (c->getConnectionInterval() * 125) % 100,
       c->getSlaveLatency(), c->getSupervisionTimeout() * 10, c->getMtu(), c->getDataLength(),
       phy <= 4 ? PHY[phy] : "?");
}

static void c_link(Channel& ch, int, char**) {
  link_line(ch, "client", bleConnected ? bleConn : BLE_CONN_HANDLE_INVALID);
  link_line(ch, "polar", polar_conn_handle());
  uint32_t sent, ms;
  xfer_progress(&sent, &ms);
  send(ch, "+ transfer %s sent=%lu ms=%lu", xfer_active() ? "active" : "idle", (unsigned long)sent, (unsigned long)ms);
  send(ch, "OK");
}

static void c_scan(Channel& ch, int argc, char** argv) {
  if (logger_state() != LOG_IDLE) return send(ch, "ERR 409 busy logging");
  int secs = argc >= 2 ? atoi(argv[1]) : 10;
  if (secs < 1 || secs > 60) return send(ch, "ERR 400 SCAN [1..60 seconds]");
  static ScanResult found[MAX_SCAN];
  int n = polar_scan(secs, found, MAX_SCAN);
  int added = 0;
  char a[18];
  for (int i = 0; i < n; i++) {
    bool known = devices_find(found[i].addr) >= 0;
    if (!known && devices_add(found[i].addr, found[i].name) >= 0) added++;
    addr_format(found[i].addr, a);
    send(ch, "+ %s %d %s %s", a, found[i].rssi, known ? "known" : "new", found[i].name);
  }
  if (added && !devices_save()) return send(ch, "ERR 500 cannot save devices");
  send(ch, "OK %d %d", n, added);
}

static void c_devs(Channel& ch, int, char**) {
  char a[18];
  for (int i = 0; i < deviceCount; i++) {
    addr_format(devices[i].addr, a);
    send(ch, "+ %d %s %c %s", i, a, strcmp(a, settings.defaultAddr) ? '-' : '*', devices[i].name);
  }
  send(ch, "OK %d", deviceCount);
}

static void c_default(Channel& ch, int argc, char** argv) {
  if (argc != 2) return send(ch, "OK %s", settings.defaultAddr[0] ? settings.defaultAddr : "-");
  ble_gap_addr_t addr;
  int i = -1;
  if (addr_parse(argv[1], &addr)) i = devices_find(addr);
  else if (isdigit(argv[1][0]) && atoi(argv[1]) < deviceCount) i = atoi(argv[1]);
  if (i < 0) return send(ch, "ERR 404 unknown device (SCAN first)");
  addr_format(devices[i].addr, settings.defaultAddr);
  if (!settings_save()) return send(ch, "ERR 500 cannot save settings");
  send(ch, "OK %s %s", settings.defaultAddr, devices[i].name);
}

static void c_format(Channel& ch, int argc, char** argv) {
  char buf[80];
  if (argc >= 2) {
    int mt = mt_from_name(argv[1]);
    if (mt < 0) return send(ch, "ERR 400 unknown measurement %s", argv[1]);
    if (argc > 2 && !format_apply(settings.fmt[mt], argc - 2, argv + 2))
      return send(ch, "ERR 400 FORMAT <type> [on|off] [rate=N] [res=N] [range=N] [ch=N]");
    if (argc > 2 && !settings_save()) return send(ch, "ERR 500 cannot save settings");
    format_print(mt, settings.fmt[mt], buf, sizeof(buf));
    return send(ch, "OK %s", buf);
  }
  int n = 0;
  for (int mt = 0; mt < MT_COUNT; mt++)
    if (mt_name(mt)) {
      format_print(mt, settings.fmt[mt], buf, sizeof(buf));
      send(ch, "+ %s", buf);
      n++;
    }
  send(ch, "OK %d", n);
}

static void print_settings(Channel& ch, uint8_t mt, const uint8_t* p, uint16_t len) {
  static const char* const NAMES[] = {"rate", "res", "range", "range_milli", "ch", "factor"};
  static const uint8_t SIZES[] = {2, 2, 2, 4, 1, 4};
  char out[160];
  int k = snprintf(out, sizeof(out), "+ %s", mt_name(mt));
  uint16_t i = 0;
  while (i + 2 <= len && k < (int)sizeof(out) - 16) {
    uint8_t type = p[i], count = p[i + 1];
    i += 2;
    if (type > 5) break;
    k += snprintf(out + k, sizeof(out) - k, " %s=", NAMES[type]);
    for (uint8_t c = 0; c < count && i + SIZES[type] <= len; c++, i += SIZES[type]) {
      uint32_t v = 0;
      for (int b = SIZES[type] - 1; b >= 0; b--) v = (v << 8) | p[i + b];
      if (type == 5) { float f; memcpy(&f, &v, 4); k += snprintf(out + k, sizeof(out) - k, "%s%g", c ? "," : "", f); }
      else k += snprintf(out + k, sizeof(out) - k, "%s%lu", c ? "," : "", (unsigned long)v);
    }
  }
  send(ch, "%s", out);
}

static void c_query(Channel& ch, int, char**) {
  if (logger_state() != LOG_IDLE) return send(ch, "ERR 409 busy logging");
  ble_gap_addr_t addr;
  if (!addr_parse(settings.defaultAddr, &addr)) return send(ch, "ERR 404 no default device");
  int di = devices_find(addr);
  if (di >= 0) addr = devices[di].addr;
  if (!polar_connect(addr, 8000)) return send(ch, "ERR 503 cannot connect to %s: %s", settings.defaultAddr, polar_last_error());
  uint8_t mask = 0;
  int e = -1;
  for (int i = 0; i < 4 && e; i++) {
    if (i) { uint32_t t0 = millis(); while (millis() - t0 < 250) idle_service(); }
    e = polar_read_features(&mask);
  }
  if (e) { polar_disconnect(); return send(ch, "ERR 502 feature read: %s", pmd_error_name(e)); }
  char f[48] = "";
  for (int mt = 0; mt < 8; mt++)
    if ((mask & (1 << mt)) && mt_name(mt)) snprintf(f + strlen(f), sizeof(f) - strlen(f), "%s%s", f[0] ? "," : "", mt_name(mt));
  send(ch, "+ features %s", f[0] ? f : "-");
  for (int mt = 0; mt < MT_COUNT; mt++) {
    if (!(mask & (1 << mt)) || !mt_name(mt)) continue;
    uint8_t p[64];
    uint16_t len;
    if (polar_get_settings(mt, p, &len) == 0) print_settings(ch, mt, p, len);
  }
  int bat = polar_battery();
  send(ch, "+ battery %d", bat);
  send(ch, "+ mtu %u", polar_mtu());
  polar_disconnect();
  send(ch, "OK");
}

static void c_log(Channel& ch, int, char**) {
  char file[48], info[128] = "";
  int e = logger_start(file, sizeof(file), info, sizeof(info));
  switch (e) {
    case 0: return send(ch, "OK %s %s", file, info);
    case 409: return send(ch, "ERR 409 already logging %s", logger_file());
    case 404: return send(ch, "ERR 404 no default device");
    case 503: return send(ch, "ERR 503 cannot connect to %s: %s", settings.defaultAddr, polar_last_error());
    case 422: return send(ch, "ERR 422 no stream started %s", info);
    default: return send(ch, "ERR 500 cannot create data file");
  }
}

static void c_stop(Channel& ch, int, char**) {
  char file[48];
  strlcpy(file, logger_file(), sizeof(file));
  if (logger_stop(END_STOP)) return send(ch, "ERR 409 not logging");
  send(ch, "OK %s %lu", file, (unsigned long)rec_bytes());
}

static void c_password(Channel& ch, int argc, char** argv) {
  char pw[33];
  if (argc == 3 && !ch.ble && !strcmp(argv[1], "-p")) {
    strlcpy(pw, argv[2], sizeof(pw));
  } else {
    if (argc != 2 || !ch.nonce[0]) return send(ch, "ERR 400 HELLO, then PASSWORD <hex of new XOR SHA256(nonce+old+\"password\")>");
    uint8_t enc[32], key[32];
    int n = from_hex(argv[1], enc, 32);
    if (n < 1) return send(ch, "ERR 400 bad hex");
    keyed_hash(ch.nonce, "password", key);
    ch.nonce[0] = 0;
    for (int i = 0; i < n; i++) pw[i] = enc[i] ^ key[i];
    pw[n] = 0;
    for (int i = 0; i < n; i++)
      if (pw[i] < 0x21 || pw[i] > 0x7E) return send(ch, "ERR 400 password must be printable ASCII without spaces");
  }
  if (!pw[0]) return send(ch, "ERR 400 empty password");
  strlcpy(settings.password, pw, sizeof(settings.password));
  if (!settings_save()) return send(ch, "ERR 500 cannot save settings");
  send(ch, "OK");
}

static void c_set(Channel& ch, int argc, char** argv) {
  if (argc != 3) {
    return send(ch, "OK sleep=%u reconnect=%u flush=%u lowbattery=%u predict=%u clslp=%u speedlp=%u loghr=%u "
                "logcad=%u cadaxis=%u cadlp=%u", settings.sleepSec, settings.reconnectSec, settings.flushSec,
                settings.lowBatteryMv, settings.predictMs, settings.classLpS, settings.speedLpS, settings.logHr,
                settings.logCad, settings.cadAxis, settings.cadLpS);
  }
  long v = atol(argv[2]);
  if (v < 0 || v > 65535) return send(ch, "ERR 400 value out of range");
  if (!strcmp(argv[1], "sleep")) settings.sleepSec = v;
  else if (!strcmp(argv[1], "reconnect")) settings.reconnectSec = v;
  else if (!strcmp(argv[1], "flush") && v > 0) settings.flushSec = v;
  else if (!strcmp(argv[1], "lowbattery")) settings.lowBatteryMv = v;
  else if (!strcmp(argv[1], "predict")) settings.predictMs = v;
  else if (!strcmp(argv[1], "clslp")) settings.classLpS = v;
  else if (!strcmp(argv[1], "speedlp")) settings.speedLpS = v;
  else if (!strcmp(argv[1], "loghr") && v <= 1) settings.logHr = v;
  else if (!strcmp(argv[1], "logcad") && v <= 1) settings.logCad = v;
  else if (!strcmp(argv[1], "cadaxis") && v <= 2) settings.cadAxis = v;
  else if (!strcmp(argv[1], "cadlp")) settings.cadLpS = v;
  else return send(ch, "ERR 400 SET sleep|reconnect|flush|lowbattery|predict|clslp|speedlp|loghr|logcad|cadaxis|cadlp <value>");
  if (!settings_save()) return send(ch, "ERR 500 cannot save settings");
  send(ch, "OK %s=%ld", argv[1], v);
}

static void c_bye(Channel& ch, int, char**) {
  send(ch, "OK %s", logger_state() ? "logging continues" : "idle");
  if (ch.ble) {
    delay(200);
    Bluefruit.disconnect(bleConn);
  }
}

static void c_sleep(Channel& ch, int, char**) {
  if (logger_state() != LOG_IDLE) return send(ch, "ERR 409 busy logging");
  send(ch, "OK sleeping");
  delay(300);
  power_sleep();
}

struct Command { const char* name; Handler fn; bool needAuth; const char* help; };
static const Command COMMANDS[] = {
  {"HELP", c_help, true, "list commands"},
  {"HELLO", c_hello, false, "get a fresh nonce: OK PREC <fw> <name> <nonce>"},
  {"AUTH", c_auth, false, "AUTH <hex sha256(nonce+password)>"},
  {"STATUS", c_status, true, "logging state, file, battery, clock"},
  {"TIME", c_time, true, "TIME [unix ms]: read or set the clock"},
  {"LS", c_ls, true, "list data files"},
  {"READ", c_read, true, "READ <file> <offset> <length>: hex dump of a data file"},
  {"SCAN", c_scan, true, "SCAN [seconds]: find Polar devices, remember new ones"},
  {"DEVS", c_devs, true, "list known Polar devices"},
  {"DEFAULT", c_default, true, "DEFAULT [address|index]: get/set default Polar"},
  {"FORMAT", c_format, true, "FORMAT [type [on|off] [rate=] [res=] [range=] [ch=]]"},
  {"QUERY", c_query, true, "connect to default Polar, list supported settings"},
  {"LOG", c_log, true, "start logging"},
  {"STOP", c_stop, true, "stop logging"},
  {"PASSWORD", c_password, true, "PASSWORD <hex> (or -p <plain> on USB)"},
  {"SET", c_set, true, "SET [sleep|reconnect|flush|lowbattery <value>]"},
  {"SEND", c_send, true, "SEND <file> [offset] | SEND STOP: stream a data file (binary)"},
  {"HR", c_hr, true, "heart rate, batteries and state for monitor mode"},
  {"LINK", c_link, true, "Bluetooth link parameters (diagnostics)"},
  {"BYE", c_bye, true, "disconnect (logging continues)"},
  {"SLEEP", c_sleep, true, "go to sleep now (when idle)"},
};

static void c_help(Channel& ch, int, char**) {
  for (const Command& c : COMMANDS) send(ch, "+ %s - %s", c.name, c.help);
  send(ch, "OK %d", (int)(sizeof(COMMANDS) / sizeof(COMMANDS[0])));
}

static void dispatch(Channel& ch) {
  char* argv[12];
  int argc = 0;
  char* s = ch.line;
  while (*s && argc < 12) {
    while (*s == ' ') *s++ = 0;
    if (!*s) break;
    argv[argc++] = s;
    while (*s && *s != ' ') s++;
  }
  if (!argc) return;
  for (char* c = argv[0]; *c; c++) *c = toupper(*c);
  for (const Command& c : COMMANDS) {
    if (strcmp(c.name, argv[0])) continue;
    if (c.needAuth && !ch.authed) return send(ch, "ERR 401 authenticate first (HELLO, AUTH)");
    breadcrumb(0x40 + (&c - COMMANDS));
    return c.fn(ch, argc, argv);
  }
  send(ch, "ERR 400 unknown command %s (try HELP)", argv[0]);
}

static void poll(Channel& ch) {
  while (ch.io->available()) {
    int c = ch.io->read();
    lastActivity = millis();
    if (c == '\r' || c == '\n') {
      ch.line[ch.n] = 0;
      if (ch.n) dispatch(ch);
      ch.n = 0;
    } else if (ch.n < sizeof(ch.line) - 1) {
      ch.line[ch.n++] = c;
    }
  }
}

// cmd_begin(): start the BLE UART service and advertising as "PREC-xxxx".
// Call after Bluefruit.begin().
void cmd_begin() {
  uint8_t mac[6];
  Bluefruit.getAddr(mac);
  char name[16];
  snprintf(name, sizeof(name), "PREC-%02X%02X", mac[1], mac[0]);
  Bluefruit.setName(name);
  Bluefruit.Periph.setConnectCallback(on_ble_connect);
  Bluefruit.Periph.setDisconnectCallback(on_ble_disconnect);
  bleuart.begin();
  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addService(bleuart);
  Bluefruit.ScanResponse.addName();
  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(160, 800);   // 100 ms fast, 500 ms slow
  Bluefruit.Advertising.setFastTimeout(30);
  Bluefruit.Advertising.start(0);
  lastActivity = millis();
}

// cmd_poll(): read and execute complete lines from both channels.
void cmd_poll() {
  poll(usb);
  if (bleConnected) poll(ble);
}
