// File transfer: binary streaming of data files over BLE notifications or USB.
//
// The command channel's SEND command starts a transfer; the file is then
// pushed as fast as the link allows as packets:
//
//   [u32 offset][file data]                           data packet
//   [u32 0xFFFFFFFF][u32 bytes sent][u32 CRC-32]      end packet
//
// Over BLE each packet is one notification on the data characteristic of the
// PREC file transfer service (up to MTU-7 bytes of data). Over USB each packet
// travels inside a frame in the serial stream, between text lines:
//
//   0xFE 0x50 [u16 packet length] [packet]            (up to 4800 bytes of data)
//
// Text never contains the byte 0xFE, so a client tells frames and lines apart
// by the first byte. A frame is always completed before a text line is sent.
// The client checks that offsets are contiguous and the CRC-32 (zlib
// polynomial) matches; on a gap it sends SEND again from the last good offset.
// Streaming runs from loop() in short slices, so logging and commands carry on.
// See docs/COMMANDS.md (SEND) and docs/FIRMWARE.md.
#include "prec.h"
#include "tusb.h"

// 9f3e0001-5052-4543-a5d3-6c7b4a1e2f80 (service) and ...0002... (data), little endian
static const uint8_t UUID_FT_SVC[16]  = {0x80, 0x2F, 0x1E, 0x4A, 0x7B, 0x6C, 0xD3, 0xA5, 0x43, 0x45, 0x52, 0x50, 0x01, 0x00, 0x3E, 0x9F};
static const uint8_t UUID_FT_DATA[16] = {0x80, 0x2F, 0x1E, 0x4A, 0x7B, 0x6C, 0xD3, 0xA5, 0x43, 0x45, 0x52, 0x50, 0x02, 0x00, 0x3E, 0x9F};

#define USB_PAYLOAD 4800                // data bytes per USB packet
#define XFER_BUF   (2 * USB_PAYLOAD)    // SD read size: whole USB packets and whole 240 byte BLE packets
#define SLICE_MS   30                   // longest time xfer_service() keeps loop() busy
#define FRAME_HDR  4                    // 0xFE 0x50 u16 length

// Connection interval (1.25 ms units) asked for during a BLE transfer, and after it.
// Laptop controllers typically move one packet per connection event, so the
// interval sets the speed: 7.5 ms gives ~30 kB/s, 15 ms ~15 kB/s. While a
// session is logging the slower value leaves radio time for the Polar link.
#define INTERVAL_FAST     6      // 7.5 ms, not logging
#define INTERVAL_LOGGING  12     // 15 ms, logging
#define INTERVAL_RELAXED  24     // 30 ms, after the transfer

static BLEService ftService(UUID_FT_SVC);
static BLECharacteristic ftData(UUID_FT_DATA);

static FsFile file;
static bool active = false, overUsb = false;
static volatile bool abortReq = false;
static uint16_t conn = BLE_CONN_HANDLE_INVALID;
static uint32_t pos, end, crc, sent;
static uint8_t buf[XFER_BUF];
static uint32_t bufStart, bufLen;      // file offset and length of buf's contents
static uint32_t startedAt;
static uint8_t frame[FRAME_HDR + 4 + USB_PAYLOAD];
static uint32_t frameLen = 0, framePos = 0;   // USB frame being written
static uint32_t crcTable[256];

static uint32_t crc32_update(uint32_t c, const uint8_t* p, size_t n) {
  c = ~c;
  while (n--) c = crcTable[(c ^ *p++) & 0xFF] ^ (c >> 8);
  return ~c;
}

// xfer_begin(): add the file transfer service and build the CRC table.
// Call before advertising starts.
void xfer_begin() {
  for (uint32_t i = 0; i < 256; i++) {
    uint32_t c = i;
    for (int k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320 & (0 - (c & 1)));
    crcTable[i] = c;
  }
  ftService.begin();
  ftData.setProperties(CHR_PROPS_NOTIFY);
  ftData.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  ftData.setMaxLen(244);
  ftData.begin();
}

// xfer_start(usb, connHandle, name, offset, sizeOut): start streaming
// /prec/data/<name> from offset, over USB (usb = true) or to the BLE client on
// connHandle. Returns 0 or a protocol error code (404 no file, 409 being
// logged or a transfer is running, 412 the BLE client has not enabled
// notifications on the data characteristic).
int xfer_start(bool usb, uint16_t connHandle, const char* name, uint32_t offset, uint32_t* sizeOut) {
  if (active) return 409;
  char path[64];
  snprintf(path, sizeof(path), DIR_DATA "/%s", name);
  if (!strcmp(path, logger_file())) return 409;
  if (!usb && !ftData.notifyEnabled(connHandle)) return 412;
  file = sd.open(path, O_RDONLY);
  if (!file) return 404;
  end = file.fileSize();
  pos = offset > end ? end : offset;
  *sizeOut = end;
  overUsb = usb;
  conn = connHandle;
  crc = 0;
  sent = 0;
  bufLen = 0;
  bufStart = pos;
  frameLen = framePos = 0;
  abortReq = false;
  active = true;
  startedAt = millis();
  if (!usb) {
    BLEConnection* c = Bluefruit.Connection(conn);
    if (c) c->requestConnectionParameter(logger_state() == LOG_IDLE ? INTERVAL_FAST : INTERVAL_LOGGING);
  }
  return 0;
}

// xfer_stop(): abort a running transfer (safe from any task; takes effect in xfer_service()).
void xfer_stop() { if (active) abortReq = true; }

// xfer_active(): true while a transfer is running.
bool xfer_active() { return active; }

static void finish() {
  file.close();
  active = false;
  if (!overUsb) {
    BLEConnection* c = Bluefruit.Connection(conn);
    if (c) c->requestConnectionParameter(INTERVAL_RELAXED);
  }
  conn = BLE_CONN_HANDLE_INVALID;
}

// Write as much of the pending USB frame as the CDC buffer takes without blocking.
// Returns true when the frame is completely written.
static bool usb_push() {
  while (framePos < frameLen) {
    int room = Serial.availableForWrite();
    if (room <= 0) {
      tud_cdc_write_flush();
      return false;
    }
    framePos += Serial.write(frame + framePos, min((uint32_t)room, frameLen - framePos));
  }
  return true;
}

// xfer_usb_flush(): finish a partly written USB frame (waiting at most 1 s)
// so that a text line can follow. Called by the command channel before it
// writes to USB. If the host does not read, the transfer is aborted.
void xfer_usb_flush() {
  if (!active || !overUsb || framePos >= frameLen) return;
  uint32_t t0 = millis();
  while (!usb_push()) {
    if (millis() - t0 > 1000 || !Serial) {
      abortReq = true;
      frameLen = framePos = 0;
      return;
    }
    watchdog_feed();
    delay(1);
  }
}

// Fill pkt (4 byte offset + data) with the next packet of at most payload bytes.
// Returns its length; 12 for the end packet, 0 if the card read failed.
static uint32_t next_packet(uint8_t* pkt, uint32_t payload) {
  if (pos >= end) {
    put_le32(pkt, 0xFFFFFFFF);
    put_le32(pkt + 4, sent);
    put_le32(pkt + 8, crc);
    return 12;
  }
  if (pos >= bufStart + bufLen) {               // refill the read buffer
    breadcrumb(BC_XFER);
    file.seekSet(pos);
    int n = file.read(buf, min((uint32_t)XFER_BUF, end - pos));
    if (n <= 0) {
      end = pos;                                // treat as end of file: the CRC/size check tells the client
      return next_packet(pkt, payload);
    }
    bufStart = pos;
    bufLen = n;
  }
  uint32_t k = min(payload, bufStart + bufLen - pos);
  put_le32(pkt, pos);
  memcpy(pkt + 4, buf + (pos - bufStart), k);
  return 4 + k;
}

// Account for a data packet that has been handed to the transport.
static void commit(const uint8_t* pkt, uint32_t len) {
  crc = crc32_update(crc, pkt + 4, len - 4);
  pos += len - 4;
  sent += len - 4;
}

static void service_usb() {
  uint32_t t0 = millis();
  while (millis() - t0 < SLICE_MS) {
    if (!usb_push()) return;                    // CDC buffer full: continue next loop
    if (frameLen && get_le32(frame + FRAME_HDR) == 0xFFFFFFFF) {
      tud_cdc_write_flush();
      return finish();                          // end packet written
    }
    uint32_t n = next_packet(frame + FRAME_HDR, USB_PAYLOAD);
    frame[0] = 0xFE;
    frame[1] = 0x50;
    put_le16(frame + 2, n);
    frameLen = FRAME_HDR + n;
    framePos = 0;
    if (n != 12 || get_le32(frame + FRAME_HDR) != 0xFFFFFFFF) commit(frame + FRAME_HDR, n);
    watchdog_feed();
  }
}

static void service_ble() {
  BLEConnection* c = Bluefruit.Connection(conn);
  if (!c) return finish();
  uint32_t payload = min((uint16_t)(c->getMtu() - 3), (uint16_t)244) - 4;
  uint8_t pkt[248];
  uint32_t t0 = millis();
  while (millis() - t0 < SLICE_MS) {
    uint32_t n = next_packet(pkt, payload);
    bool last = n == 12 && get_le32(pkt) == 0xFFFFFFFF;
    if (!ftData.notify(conn, pkt, n)) return;   // queue full: the same packet is rebuilt next loop
    if (last) return finish();
    commit(pkt, n);
    watchdog_feed();
  }
}

// xfer_service(): called from loop(). Sends packets for up to SLICE_MS.
void xfer_service() {
  if (!active) return;
  if (abortReq) return finish();
  if (overUsb) {
    if (!Serial) return finish();               // port closed
    service_usb();
  } else {
    if (!Bluefruit.connected(conn)) return finish();
    service_ble();
  }
}

// xfer_progress(sent, ms): bytes sent and milliseconds since the transfer started.
void xfer_progress(uint32_t* sentOut, uint32_t* msOut) {
  *sentOut = sent;
  *msOut = millis() - startedAt;
}
