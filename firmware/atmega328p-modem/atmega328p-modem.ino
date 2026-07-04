/*
 * chimera-rawmodem — raw LoRa modem firmware for the Dragino LG01-P
 *
 * Runs on the ATmega328P that sits between the AR9331 (Linux) and the
 * SX1276/RFM95 radio. This sketch is deliberately "dumb": it moves raw
 * bytes between the radio and the AR9331 serial link and applies radio
 * configuration commands. All protocol logic (KISS TCP server, APRS
 * digipeating, APRS-IS, Reticulum) lives on the AR9331 or on an external
 * host. One sketch serves all three operating modes.
 *
 * Serial link: 115200 baud to the AR9331 (/dev/ttyATH0 on the Linux side).
 * Framing: KISS (FEND/FESC escaping) with a command byte per frame.
 * The full wire protocol is specified in docs/architecture.md.
 *
 * Requires RadioHead >= 1.88 (for setSpreadingFactor/setSignalBandwidth/
 * setCodingRate4/setPreambleLength).
 *
 * RNode / LoRa-APRS interoperability note (do not "simplify" this away):
 * RH_RF95 normally prepends a 4-byte RadioHead header (TO/FROM/ID/FLAGS)
 * to every packet on air, which would break interoperability with any
 * non-RadioHead device. To transmit a payload byte-for-byte we map its
 * first 4 bytes onto those header fields and send the rest as "data";
 * on receive we re-assemble the original payload from the received header
 * fields plus the buffer. Net effect: the on-air payload is exactly the
 * bytes handed to us, with no RadioHead framing. Limitation: payloads
 * shorter than 4 bytes cannot be sent (irrelevant for APRS and Reticulum).
 */

#include <SPI.h>
#include <RH_RF95.h>

// ---------------- KISS framing ----------------
#define FEND  0xC0
#define FESC  0xDB
#define TFEND 0xDC
#define TFESC 0xDD

// ---------------- Frame command bytes ----------------
// Host -> modem
#define CMD_DATA        0x00  // payload: raw bytes to transmit
#define CMD_SETFREQ     0x10  // payload: uint32 BE, Hz
#define CMD_SETSF       0x11  // payload: uint8, 6..12
#define CMD_SETBW       0x12  // payload: uint32 BE, Hz
#define CMD_SETCR       0x13  // payload: uint8, denominator 5..8 (4/5..4/8)
#define CMD_SETPOWER    0x14  // payload: uint8, dBm
#define CMD_SETSYNC     0x15  // payload: uint8, LoRa sync word
#define CMD_SETPREAMBLE 0x16  // payload: uint16 BE, symbols
#define CMD_GETSTATUS   0x20  // payload: none
// Modem -> host
// CMD_DATA (0x00) also: payload = raw bytes received over the air
#define CMD_SIGREPORT   0x07  // payload: int16 BE RSSI(dBm), int8 SNR(dB) — follows each RX CMD_DATA
#define CMD_STATUS      0x21  // payload: freq u32, bw u32, sf u8, cr u8, power u8, sync u8, preamble u16 (all BE)

// Defaults: EU LoRa APRS convention (433.775 MHz, SF12, BW125, CR4/5).
// These are start-up values only — the AR9331 bridge pushes the profile
// for the active mode from config at boot. Never rely on these on air.
static uint32_t cfg_freq_hz   = 433775000UL;
static uint32_t cfg_bw_hz     = 125000UL;
static uint8_t  cfg_sf        = 12;
static uint8_t  cfg_cr        = 5;
static uint8_t  cfg_power_dbm = 10;
static uint8_t  cfg_sync      = 0x12;  // matches RNode firmware (sx127x.cpp SYNC_WORD_7X 0x12, verified 2026-07)
static uint16_t cfg_preamble  = 8;

// LG01-P wiring matches the RadioHead defaults used in Dragino's own
// example sketches (CS=10, DIO0=INT0/pin 2).
RH_RF95 rf95;

// ---------------- Serial RX state machine ----------------
// 256 = command byte + 255 bytes of payload: a full-size LoRa frame
// (RNode frames use all 255 bytes: 1 framing header + 254 data).
#define SER_BUF_LEN 256
static uint8_t  ser_buf[SER_BUF_LEN];
static uint16_t ser_len = 0;
static bool     in_frame = false;
static bool     escaped = false;
static bool     overflow = false;

static void kissPutByte(uint8_t b) {
  if (b == FEND)      { Serial.write(FESC); Serial.write(TFEND); }
  else if (b == FESC) { Serial.write(FESC); Serial.write(TFESC); }
  else                { Serial.write(b); }
}

static void kissSendFrame(uint8_t cmd, const uint8_t* p1, uint8_t l1,
                          const uint8_t* p2 = NULL, uint8_t l2 = 0) {
  Serial.write(FEND);
  kissPutByte(cmd);
  for (uint8_t i = 0; i < l1; i++) kissPutByte(p1[i]);
  for (uint8_t i = 0; i < l2; i++) kissPutByte(p2[i]);
  Serial.write(FEND);
}

static void applyRadioConfig() {
  rf95.setFrequency(cfg_freq_hz / 1000000.0);
  rf95.setSignalBandwidth((long)cfg_bw_hz);
  rf95.setSpreadingFactor(cfg_sf);
  rf95.setCodingRate4(cfg_cr);
  rf95.setPreambleLength(cfg_preamble);
  rf95.setTxPower(cfg_power_dbm, false);
  rf95.spiWrite(RH_RF95_REG_39_SYNC_WORD, cfg_sync);
}

static void sendStatus() {
  uint8_t s[14];
  s[0] = cfg_freq_hz >> 24; s[1] = cfg_freq_hz >> 16; s[2] = cfg_freq_hz >> 8; s[3] = cfg_freq_hz;
  s[4] = cfg_bw_hz >> 24;   s[5] = cfg_bw_hz >> 16;   s[6] = cfg_bw_hz >> 8;   s[7] = cfg_bw_hz;
  s[8] = cfg_sf; s[9] = cfg_cr; s[10] = cfg_power_dbm; s[11] = cfg_sync;
  s[12] = cfg_preamble >> 8; s[13] = cfg_preamble;
  kissSendFrame(CMD_STATUS, s, sizeof(s));
}

// Transmit a payload byte-for-byte (see header comment for the 4-byte trick).
static void txRaw(const uint8_t* p, uint8_t len) {
  if (len < 4) return;  // cannot emit <4 raw bytes through RH_RF95 — documented limitation
  rf95.setHeaderTo(p[0]);
  rf95.setHeaderFrom(p[1]);
  rf95.setHeaderId(p[2]);
  rf95.setHeaderFlags(p[3], 0xFF);
  rf95.send(p + 4, len - 4);
  rf95.waitPacketSent();
  rf95.setModeRx();
}

static uint32_t rdU32(const uint8_t* p) {
  return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

static void processFrame(const uint8_t* f, uint16_t len) {
  if (len < 1) return;
  uint8_t cmd = f[0];
  const uint8_t* p = f + 1;
  uint8_t plen = (uint8_t)(len - 1);  // len <= SER_BUF_LEN (256), so this fits

  switch (cmd) {
    case CMD_DATA:
      txRaw(p, plen);
      break;
    case CMD_SETFREQ:
      if (plen == 4) { cfg_freq_hz = rdU32(p); applyRadioConfig(); }
      break;
    case CMD_SETSF:
      if (plen == 1 && p[0] >= 6 && p[0] <= 12) { cfg_sf = p[0]; applyRadioConfig(); }
      break;
    case CMD_SETBW:
      if (plen == 4) { cfg_bw_hz = rdU32(p); applyRadioConfig(); }
      break;
    case CMD_SETCR:
      if (plen == 1 && p[0] >= 5 && p[0] <= 8) { cfg_cr = p[0]; applyRadioConfig(); }
      break;
    case CMD_SETPOWER:
      if (plen == 1) { cfg_power_dbm = p[0]; applyRadioConfig(); }
      break;
    case CMD_SETSYNC:
      if (plen == 1) { cfg_sync = p[0]; applyRadioConfig(); }
      break;
    case CMD_SETPREAMBLE:
      if (plen == 2) { cfg_preamble = ((uint16_t)p[0] << 8) | p[1]; applyRadioConfig(); }
      break;
    case CMD_GETSTATUS:
      sendStatus();
      break;
    default:
      break;  // unknown command: ignore silently, stay a dumb pipe
  }
}

static void pollSerial() {
  while (Serial.available()) {
    uint8_t b = Serial.read();
    if (b == FEND) {
      if (in_frame && ser_len > 0 && !overflow) processFrame(ser_buf, ser_len);
      in_frame = true;
      escaped = false;
      overflow = false;
      ser_len = 0;
      continue;
    }
    if (!in_frame) continue;
    if (escaped) {
      escaped = false;
      if (b == TFEND)      b = FEND;
      else if (b == TFESC) b = FESC;
      else { overflow = true; continue; }  // protocol error: drop frame
    } else if (b == FESC) {
      escaped = true;
      continue;
    }
    if (ser_len < SER_BUF_LEN) ser_buf[ser_len++] = b;
    else overflow = true;  // frame too long: drop it
  }
}

static void pollRadio() {
  if (!rf95.available()) return;
  uint8_t buf[RH_RF95_MAX_MESSAGE_LEN];
  uint8_t len = sizeof(buf);
  if (!rf95.recv(buf, &len)) return;

  // Re-assemble the original on-air payload: RadioHead consumed its first
  // 4 bytes as TO/FROM/ID/FLAGS; put them back in front.
  uint8_t hdr[4] = { rf95.headerTo(), rf95.headerFrom(), rf95.headerId(), rf95.headerFlags() };
  kissSendFrame(CMD_DATA, hdr, 4, buf, len);

  int16_t rssi = rf95.lastRssi();
  int8_t  snr  = rf95.lastSNR();
  uint8_t sig[3] = { (uint8_t)(rssi >> 8), (uint8_t)rssi, (uint8_t)snr };
  kissSendFrame(CMD_SIGREPORT, sig, 3);
}

void setup() {
  Serial.begin(115200);  // to the AR9331 (Yún-style UART link)
  if (!rf95.init()) {
    // Radio init failed: report forever so the bridge can log it.
    while (true) {
      const uint8_t err[] = { 'R', 'F', 'E', 'R', 'R' };
      kissSendFrame(CMD_SIGREPORT, err, sizeof(err));
      delay(5000);
    }
  }
  applyRadioConfig();
  // Accept frames regardless of the RadioHead TO header — mandatory for
  // pass-through RX of packets from non-RadioHead senders.
  rf95.setPromiscuous(true);
  rf95.setModeRx();
}

void loop() {
  pollSerial();
  pollRadio();
}
