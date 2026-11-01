// PREC logger for the Adafruit Feather nRF52840 Sense + Adalogger FeatherWing.
//
// Logs Polar H10 (PMD) data to the SD card in the PREC binary stream format,
// controlled by a command device (Bangle.js 2 app) over BLE.
// See docs/FIRMWARE.md, docs/COMMANDS.md and docs/PREC_FORMAT.md.
#include "prec.h"

uint32_t resetReason = 0;
static uint32_t resetCrumbRaw = 0;
static uint32_t lastMotion = 0;
static uint32_t lastMotionPoll = 0;

// idle_service(): work that must continue while a command blocks in a wait
// loop (connecting, waiting for a Polar response, scanning).
void idle_service() {
  watchdog_feed();
  rec_service(false);
  delay(2);
}

// The watchdog resets the board if loop() (or a blocking wait) stops running
// for WATCHDOG_SEC; an interrupted logging session then resumes on its own.
#define WATCHDOG_SEC 15

static void watchdog_begin() {
  NRF_WDT->CONFIG = (WDT_CONFIG_HALT_Pause << WDT_CONFIG_HALT_Pos) | (WDT_CONFIG_SLEEP_Run << WDT_CONFIG_SLEEP_Pos);
  NRF_WDT->CRV = WATCHDOG_SEC * 32768;
  NRF_WDT->RREN = WDT_RREN_RR0_Msk;
  NRF_WDT->TASKS_START = 1;
}

// watchdog_feed(): tell the watchdog the firmware is alive.
void watchdog_feed() { NRF_WDT->RR[0] = WDT_RR_RR_Reload; }

static uint8_t bootCrumb = 0;
static uint8_t lastCrumb = 0xFF;

// breadcrumb(step): remember the step being executed in a retained register,
// so after a watchdog reset STATUS can tell where the firmware was stuck.
void breadcrumb(uint8_t step) {
  if (step == lastCrumb) return;
  lastCrumb = step;
  sd_power_gpregret_clr(1, 0xFF);
  sd_power_gpregret_set(1, step);
}

// breadcrumb_at_boot(): the step recorded before the last reset.
uint8_t breadcrumb_at_boot() { return bootCrumb; }

void setup() {
  resetReason = NRF_POWER->RESETREAS;
  NRF_POWER->RESETREAS = 0xFFFFFFFF;
  Serial.begin(115200);
  power_begin();
  power_led(true);

  // Command link: large MTU and notification queue, 7.5 ms radio events
  // (BANDWIDTH_MAX would reserve 125 ms per event and starve the Polar link).
  Bluefruit.configPrphConn(247, 6, 12, BLE_GATTC_WRITE_CMD_TX_QUEUE_SIZE_DEFAULT);
  Bluefruit.configCentralBandwidth(BANDWIDTH_MAX);
  Bluefruit.begin(1, 1);
  Bluefruit.setTxPower(4);
  sd_power_gpregret_get(1, &resetCrumbRaw);
  bootCrumb = resetCrumbRaw;
  watchdog_begin();
  polar_init();
  xfer_begin();
  cmd_begin();

  clock_begin();
  if (storage_begin()) {
    settings_load();
    devices_load();
  } else {
    settings_defaults();
  }
  power_led(false);
  lastMotion = millis();
}

void loop() {
  watchdog_feed();
  breadcrumb(BC_LOOP);
  cmd_poll();
  breadcrumb(BC_LOGGER);
  logger_service();
  logger_resume_service();
  xfer_service();
  predict_service();
  cad_service();
  breadcrumb(BC_MOTION);

  uint32_t now = millis();
  bool moved = false;
  if (now - lastMotionPoll >= 250) {
    lastMotionPoll = now;
    moved = power_motion();
  }
  if (moved || cmd_ble_connected() || Serial || logger_state() != LOG_IDLE)
    lastMotion = now;
  if (now - cmd_last_activity() < 1000) lastMotion = now;

  // short red blink every 2 s while logging
  power_led(logger_state() != LOG_IDLE && (now % 2000) < 30);

  if (settings.sleepSec && logger_state() == LOG_IDLE && now - lastMotion > settings.sleepSec * 1000UL)
    power_sleep();

  if (!xfer_active()) delay(10);   // no pause while streaming a file
}
