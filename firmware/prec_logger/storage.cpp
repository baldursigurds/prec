// SD card, settings file and known Polar devices file.
//
// /prec/settings/settings.txt holds "key=value" lines, /prec/settings/devices.txt
// holds one known Polar device per line: "<address> <addr type> <name>".
#include "prec.h"
#include <RTClib.h>

SdFs sd;
Settings settings;
PolarDevice devices[MAX_DEVICES];
uint8_t deviceCount = 0;
static bool sdOk = false;

static const char* const MT_NAMES[MT_COUNT] = {"ecg", "ppg", "acc", "ppi", NULL, "gyro", "mag"};

// mt_name(mt): short name of a PMD measurement type, NULL if unknown.
const char* mt_name(uint8_t mt) { return mt < MT_COUNT ? MT_NAMES[mt] : NULL; }

// mt_from_name(name): PMD measurement type for a short name, -1 if unknown.
int mt_from_name(const char* name) {
  for (int i = 0; i < MT_COUNT; i++)
    if (MT_NAMES[i] && !strcasecmp(MT_NAMES[i], name)) return i;
  return -1;
}

// FAT timestamps for files we create come from the RTC.
static void fat_datetime(uint16_t* date, uint16_t* time, uint8_t* ms10) {
  uint64_t ms = clock_now_ms();
  DateTime d((uint32_t)(ms / 1000));
  *date = FS_DATE(d.year(), d.month(), d.day());
  *time = FS_TIME(d.hour(), d.minute(), d.second());
  *ms10 = (ms % 2000) / 10;
}

// storage_begin(): mount the SD card and create /prec/data and /prec/settings.
bool storage_begin() {
  sdOk = sd.begin(SdSpiConfig(PIN_SD_CS, SHARED_SPI, SD_SCK_MHZ(16)));
  if (!sdOk) return false;
  FsDateTime::setCallback(fat_datetime);
  sd.mkdir(DIR_DATA, true);
  sd.mkdir(DIR_SETTINGS, true);
  return true;
}

// storage_ok(): true if the SD card was mounted.
bool storage_ok() { return sdOk; }

// settings_defaults(): factory settings (password "prec", ACC 200 Hz / 16 bit / 8 G).
void settings_defaults() {
  memset(&settings, 0, sizeof(settings));
  strcpy(settings.password, "prec");
  settings.fmt[MT_ACC]  = {true, 200, 16, 8, 0};
  settings.fmt[MT_ECG]  = {false, 130, 14, 0, 0};
  settings.fmt[MT_GYRO] = {false, 52, 16, 2000, 0};
  settings.fmt[MT_MAG]  = {false, 50, 16, 50, 0};
  settings.fmt[MT_PPG]  = {false, 0, 0, 0, 0};
  settings.fmt[MT_PPI]  = {false, 0, 0, 0, 0};
  settings.sleepSec = 120;
  settings.reconnectSec = 60;
  settings.flushSec = 5;
  settings.lowBatteryMv = 3400;
  settings.predictMs = 250;
  settings.classLpS = 10;
  settings.speedLpS = 10;
  settings.logHr = false;
  settings.logCad = false;
  settings.cadAxis = 0;
  settings.cadLpS = 5;
}

static int split(char* s, char** argv, int max) {
  int argc = 0;
  while (*s && argc < max) {
    while (*s == ' ') *s++ = 0;
    if (!*s) break;
    argv[argc++] = s;
    while (*s && *s != ' ') s++;
  }
  return argc;
}

// format_apply(f, argc, argv): update a stream format from tokens such as
// "on", "off", "rate=200", "res=16", "range=8", "ch=3". Returns false on a bad token.
bool format_apply(StreamFormat& f, int argc, char** argv) {
  StreamFormat n = f;
  for (int i = 0; i < argc; i++) {
    char* a = argv[i];
    if (!strcasecmp(a, "on")) n.on = true;
    else if (!strcasecmp(a, "off")) n.on = false;
    else {
      char* eq = strchr(a, '=');
      if (!eq) return false;
      *eq = 0;
      char* end;
      long v = strtol(eq + 1, &end, 10);
      if (*end || v < 0 || v > 65535) return false;
      if (!strcasecmp(a, "rate")) n.rate = v;
      else if (!strcasecmp(a, "res")) n.res = v;
      else if (!strcasecmp(a, "range")) n.range = v;
      else if (!strcasecmp(a, "ch")) n.ch = v;
      else return false;
    }
  }
  f = n;
  return true;
}

// format_print(mt, f, out, n): "acc on rate=200 res=16 range=8" (zero values omitted).
void format_print(uint8_t mt, const StreamFormat& f, char* out, size_t n) {
  int k = snprintf(out, n, "%s %s", mt_name(mt), f.on ? "on" : "off");
  if (f.rate) k += snprintf(out + k, n - k, " rate=%u", f.rate);
  if (f.res) k += snprintf(out + k, n - k, " res=%u", f.res);
  if (f.range) k += snprintf(out + k, n - k, " range=%u", f.range);
  if (f.ch) snprintf(out + k, n - k, " ch=%u", f.ch);
}

// settings_load(): read settings.txt over the defaults. Unknown keys are ignored.
bool settings_load() {
  settings_defaults();
  if (!sdOk) return false;
  FsFile f = sd.open(FILE_SETTINGS, O_RDONLY);
  if (!f) return false;
  char line[96];
  while (f.available()) {
    int n = f.fgets(line, sizeof(line));
    if (n <= 0) break;
    while (n && (line[n - 1] == '\n' || line[n - 1] == '\r')) line[--n] = 0;
    char* eq = strchr(line, '=');
    if (!eq || line[0] == '#') continue;
    *eq = 0;
    char* key = line;
    char* val = eq + 1;
    int mt = mt_from_name(key);
    if (mt >= 0) {
      char* argv[8];
      int argc = split(val, argv, 8);
      format_apply(settings.fmt[mt], argc, argv);
    } else if (!strcmp(key, "password")) strlcpy(settings.password, val, sizeof(settings.password));
    else if (!strcmp(key, "default")) strlcpy(settings.defaultAddr, val, sizeof(settings.defaultAddr));
    else if (!strcmp(key, "sleep")) settings.sleepSec = atoi(val);
    else if (!strcmp(key, "reconnect")) settings.reconnectSec = atoi(val);
    else if (!strcmp(key, "flush")) settings.flushSec = max(1, atoi(val));
    else if (!strcmp(key, "lowbattery")) settings.lowBatteryMv = atoi(val);
    else if (!strcmp(key, "predict")) settings.predictMs = atoi(val);
    else if (!strcmp(key, "clslp")) settings.classLpS = atoi(val);
    else if (!strcmp(key, "speedlp")) settings.speedLpS = atoi(val);
    else if (!strcmp(key, "loghr")) settings.logHr = atoi(val) != 0;
    else if (!strcmp(key, "logcad")) settings.logCad = atoi(val) != 0;
    else if (!strcmp(key, "cadaxis")) settings.cadAxis = min(atoi(val), 2);
    else if (!strcmp(key, "cadlp")) settings.cadLpS = atoi(val);
    else if (!strcmp(key, "resume")) settings.resume = atoi(val);
  }
  f.close();
  return true;
}

// settings_save(): rewrite settings.txt from the current settings.
bool settings_save() {
  if (!sdOk) return false;
  FsFile f = sd.open(FILE_SETTINGS, O_WRONLY | O_CREAT | O_TRUNC);
  if (!f) return false;
  f.printf("password=%s\n", settings.password);
  f.printf("default=%s\n", settings.defaultAddr);
  f.printf("sleep=%u\nreconnect=%u\nflush=%u\nlowbattery=%u\nresume=%u\npredict=%u\nclslp=%u\nspeedlp=%u\nloghr=%u\nlogcad=%u\ncadaxis=%u\ncadlp=%u\n",
           settings.sleepSec, settings.reconnectSec, settings.flushSec, settings.lowBatteryMv, settings.resume,
           settings.predictMs, settings.classLpS, settings.speedLpS, settings.logHr, settings.logCad, settings.cadAxis,
           settings.cadLpS);
  char buf[80];
  for (int mt = 0; mt < MT_COUNT; mt++) {
    if (!mt_name(mt)) continue;
    format_print(mt, settings.fmt[mt], buf, sizeof(buf));
    char* sp = strchr(buf, ' ');
    *sp = '=';
    f.printf("%s\n", buf);
  }
  bool ok = f.sync();
  f.close();
  return ok;
}

// addr_parse(s, out): parse "AA:BB:CC:DD:EE:FF" into a BLE address
// (address type is set to random static, the usual case for Polar devices).
bool addr_parse(const char* s, ble_gap_addr_t* out) {
  unsigned b[6];
  if (strlen(s) != 17 || sscanf(s, "%2x:%2x:%2x:%2x:%2x:%2x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6) return false;
  memset(out, 0, sizeof(*out));
  out->addr_type = BLE_GAP_ADDR_TYPE_RANDOM_STATIC;
  for (int i = 0; i < 6; i++) out->addr[5 - i] = b[i];
  return true;
}

// addr_format(a, out): "AA:BB:CC:DD:EE:FF", most significant byte first.
void addr_format(const ble_gap_addr_t& a, char out[18]) {
  snprintf(out, 18, "%02X:%02X:%02X:%02X:%02X:%02X", a.addr[5], a.addr[4], a.addr[3], a.addr[2], a.addr[1], a.addr[0]);
}

// devices_load(): read devices.txt into devices[].
bool devices_load() {
  deviceCount = 0;
  if (!sdOk) return false;
  FsFile f = sd.open(FILE_DEVICES, O_RDONLY);
  if (!f) return false;
  char line[80];
  while (f.available() && deviceCount < MAX_DEVICES) {
    int n = f.fgets(line, sizeof(line));
    if (n <= 0) break;
    while (n && (line[n - 1] == '\n' || line[n - 1] == '\r')) line[--n] = 0;
    if (n < 21 || line[17] != ' ') continue;
    line[17] = 0;
    PolarDevice& d = devices[deviceCount];
    if (!addr_parse(line, &d.addr)) continue;
    d.addr.addr_type = atoi(line + 18);
    const char* name = strchr(line + 18, ' ');
    strlcpy(d.name, name ? name + 1 : "", sizeof(d.name));
    deviceCount++;
  }
  f.close();
  return true;
}

// devices_save(): rewrite devices.txt from devices[].
bool devices_save() {
  if (!sdOk) return false;
  FsFile f = sd.open(FILE_DEVICES, O_WRONLY | O_CREAT | O_TRUNC);
  if (!f) return false;
  char a[18];
  for (int i = 0; i < deviceCount; i++) {
    addr_format(devices[i].addr, a);
    f.printf("%s %u %s\n", a, devices[i].addr.addr_type, devices[i].name);
  }
  bool ok = f.sync();
  f.close();
  return ok;
}

// devices_find(addr): index of a known device with the same address, or -1.
int devices_find(const ble_gap_addr_t& addr) {
  for (int i = 0; i < deviceCount; i++)
    if (!memcmp(devices[i].addr.addr, addr.addr, 6)) return i;
  return -1;
}

// devices_add(addr, name): add a device if unknown. Returns its index, or -1 if the list is full.
int devices_add(const ble_gap_addr_t& addr, const char* name) {
  int i = devices_find(addr);
  if (i >= 0) return i;
  if (deviceCount >= MAX_DEVICES) return -1;
  devices[deviceCount].addr = addr;
  strlcpy(devices[deviceCount].name, name, sizeof(devices[0].name));
  return deviceCount++;
}
