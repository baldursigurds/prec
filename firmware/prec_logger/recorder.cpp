// PREC binary stream writer.
//
// Words are appended to one of two RAM buffers. rec_put() may be called from
// the BLE task (PMD notifications) and from loop(); it never touches the SD
// card, so incoming Bluetooth data is never held up by a slow card write.
// When the active buffer is full (or flushSec has passed) the buffers swap and
// loop() writes the full one out in a single burst through rec_service().
// Only whole words are dropped when both buffers are full; the count is
// recorded afterwards in a DROPPED word, so the stream always stays parseable.
#include "prec.h"
#include <stdarg.h>

#define REC_BUF_SIZE 16384

static uint8_t buf[2][REC_BUF_SIZE];
static volatile uint16_t fill[2];
static volatile bool full[2];          // waiting to be written to the card
static volatile uint8_t active = 0;
static volatile uint32_t dropWords = 0, dropBytes = 0, dropTotal = 0;
static volatile uint64_t lastPolarTs = 0;
static FsFile file;
static bool fileOpen = false, failed = false;
static uint32_t written = 0;
static uint32_t lastFlush = 0;

// rec_open(path): create a new data file and reset the buffers.
bool rec_open(const char* path) {
  if (fileOpen) rec_close();
  file = sd.open(path, O_WRONLY | O_CREAT | O_EXCL);
  if (!file) return false;
  taskENTER_CRITICAL();
  fill[0] = fill[1] = 0;
  full[0] = full[1] = false;
  active = 0;
  dropWords = dropBytes = dropTotal = 0;
  lastPolarTs = 0;
  taskEXIT_CRITICAL();
  fileOpen = true;
  failed = false;
  written = 0;
  lastFlush = millis();
  return true;
}

// rec_put(type, polarTs, payload, len): append one word. polarTs = 0 means
// "use the latest Polar timestamp seen". Safe from any task. Returns false
// if the word was dropped (no file open, or both buffers full).
bool rec_put(uint8_t type, uint64_t polarTs, const void* payload, uint16_t len) {
  if (!fileOpen) return false;
  uint32_t total = WORD_HEADER_SIZE + len;
  if (total > REC_BUF_SIZE) return false;
  uint8_t hdr[WORD_HEADER_SIZE];
  hdr[0] = type;
  put_le16(hdr + 1, len);
  put_le64(hdr + 3, polarTs ? polarTs : lastPolarTs);
  put_le64(hdr + 11, clock_now_ms());
  bool ok = true;
  taskENTER_CRITICAL();
  uint8_t a = active;
  if (fill[a] + total > REC_BUF_SIZE) {
    if (full[a ^ 1]) {
      dropWords++;
      dropBytes += total;
      dropTotal++;
      ok = false;
    } else {
      full[a] = true;
      a ^= 1;
      active = a;
    }
  }
  if (ok) {
    memcpy(buf[a] + fill[a], hdr, WORD_HEADER_SIZE);
    memcpy(buf[a] + fill[a] + WORD_HEADER_SIZE, payload, len);
    fill[a] += total;
  }
  taskEXIT_CRITICAL();
  return ok;
}

// rec_put_text(type, fmt, ...): append a word whose payload is printf-formatted text.
bool rec_put_text(uint8_t type, const char* fmt, ...) {
  char text[160];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(text, sizeof(text), fmt, ap);
  va_end(ap);
  return rec_put(type, 0, text, min(n, (int)sizeof(text) - 1));
}

// rec_error(source, code, text): append an ERROR word (source, code, UTF-8 text).
bool rec_error(uint8_t source, uint8_t code, const char* text) {
  uint8_t p[2 + 120];
  p[0] = source;
  p[1] = code;
  size_t n = strlen(text);
  if (n > 120) n = 120;
  memcpy(p + 2, text, n);
  return rec_put(W_ERROR, 0, p, 2 + n);
}

// rec_set_polar_ts(ts) / rec_polar_ts(): latest Polar timestamp seen.
void rec_set_polar_ts(uint64_t ts) { lastPolarTs = ts; }
uint64_t rec_polar_ts() { return lastPolarTs; }

static bool write_buffer(uint8_t b) {
  // The producer never writes into a buffer marked full, so no lock is needed here.
  uint16_t n = fill[b];
  bool ok = !n || file.write(buf[b], n) == n;
  if (ok) written += n;
  taskENTER_CRITICAL();
  fill[b] = 0;
  full[b] = false;
  taskEXIT_CRITICAL();
  return ok;
}

// rec_service(force): called from loop(). Writes any full buffer to the card.
// Every flushSec seconds (or when force is true) the partially filled active
// buffer is also written and the file synced, so a power loss costs at most
// flushSec seconds of data. Returns false once a card write has failed.
bool rec_service(bool force) {
  if (!fileOpen || failed) return !failed;
  bool ok = true;
  for (uint8_t b = 0; b < 2; b++)
    if (full[b]) ok &= write_buffer(b);

  if (force || millis() - lastFlush >= settings.flushSec * 1000UL) {
    if (dropWords) {
      uint8_t p[8];
      taskENTER_CRITICAL();
      put_le32(p, dropWords);
      put_le32(p + 4, dropBytes);
      dropWords = dropBytes = 0;
      taskEXIT_CRITICAL();
      rec_put(W_DROPPED, 0, p, 8);
    }
    taskENTER_CRITICAL();
    uint8_t a = active;
    bool swap = fill[a] && !full[a ^ 1];
    if (swap) {
      full[a] = true;
      active = a ^ 1;
    }
    taskEXIT_CRITICAL();
    if (swap) ok &= write_buffer(a);
    ok &= file.sync();
    lastFlush = millis();
  }
  if (!ok) failed = true;
  return ok;
}

// rec_close(): write everything still buffered, sync and close the file.
void rec_close() {
  if (!fileOpen) return;
  rec_service(true);
  rec_service(true);   // the first call may have queued a DROPPED word
  file.close();
  fileOpen = false;
}

bool rec_is_open() { return fileOpen; }
// rec_bytes(): bytes written to the card in the current file.
uint32_t rec_bytes() { return written; }
// rec_dropped(): total words dropped in the current file.
uint32_t rec_dropped() { return dropTotal; }
// rec_failed(): true after a card write error.
bool rec_failed() { return failed; }
