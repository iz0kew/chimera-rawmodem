# Architecture

## Overview

One device, three interoperable personalities, sharing a single "raw radio
modem" firmware layer. Mode switching is runtime configuration — no reflash.

```
                          Dragino LG01-P
            ┌──────────────────────────────────────────┐
            │  AR9331 (OpenWrt/Linux)   ATmega328P      │
 Ethernet/  │  ┌──────────────────┐    ┌────────────┐  │      433MHz
 WiFi       │  │ chimera-bridge   │UART│ atmega328p │  │   ┌─────────┐
◄───TCP────►│  │ (KISS TCP server)│◄──►│ -modem.ino │◄─┼──►│ SX1276  │──► RF
            │  └───────┬──────────┘115k│ (RH_RF95)  │SPI│  │ /RFM95  │
            │          │ TCP (local)   └────────────┘  │   └─────────┘
            │  ┌───────┴──────────┐                    │
            │  │ digipeater.py    │  (each an ordinary │
            │  │ igate.py         │   TCP client of    │
            │  └──────────────────┘   the bridge)      │
            └──────────────────────────────────────────┘
                     ▲
        TCP clients: │ KISS APRS clients (Xastir, YAAC, APRSIS32…)
                     │ ChimeraInterface.py (RNS host: rnsd/MeshChat/Sideband)
```

Layering rule (project convention §9): the ATmega328P sketch and the bridge
are "dumb" byte movers. Protocol/business logic (APRS path handling, APRS-IS
formatting, RNS framing) lives in the per-function daemons or on the
external host.

## Modes

- **TNC**: an external KISS client connects to the bridge TCP port. The
  bridge forwards CMD_DATA frames verbatim in both directions — it *is* a
  KISS TNC as seen from the network.
- **Digipeater + iGate**: `digipeater.py` and `igate.py` run on the AR9331
  as two independent TCP clients of the bridge. Either can be enabled
  without the other (`digipeater.enabled` / `igate.enabled` in config);
  the digipeater has zero internet dependency.
- **Reticulum**: `ChimeraInterface.py` (on the external host, inside
  rnsd/MeshChat/Sideband) connects to the same bridge port. The Dragino acts
  as a pure pass-through radio (see "RNode interoperability" below).

The bridge pushes the radio profile for the active `mode:` at startup; the
three `radio_*` profiles live in `config/config.yaml`.

## ATmega328P ↔ AR9331 wire protocol

KISS framing (FEND `0xC0`, FESC `0xDB`, TFEND `0xDC`, TFESC `0xDD`) over the
UART at 115200 baud. First byte of each frame is a command:

| Cmd | Direction | Payload | Meaning |
|---|---|---|---|
| `0x00` DATA | both | raw bytes | host→modem: transmit over the air; modem→host: bytes received over the air |
| `0x07` SIGREPORT | modem→host | int16 BE RSSI dBm, int8 SNR dB | follows every received DATA frame; consumed/logged by the bridge, never forwarded to TCP clients |
| `0x10` SETFREQ | host→modem | uint32 BE, Hz | set frequency |
| `0x11` SETSF | host→modem | uint8, 6–12 | set spreading factor |
| `0x12` SETBW | host→modem | uint32 BE, Hz | set bandwidth |
| `0x13` SETCR | host→modem | uint8, 5–8 | set coding rate denominator (4/5…4/8) |
| `0x14` SETPOWER | host→modem | uint8, dBm | set TX power |
| `0x15` SETSYNC | host→modem | uint8 | set LoRa sync word |
| `0x16` SETPREAMBLE | host→modem | uint16 BE, symbols | set preamble length |
| `0x20` GETSTATUS | host→modem | none | request current config |
| `0x21` STATUS | modem→host | freq u32, bw u32, sf u8, cr u8, power u8, sync u8, preamble u16 (BE) | current config |

The same framing is reused on the TCP link (bridge ↔ clients): DATA frames
pass through unmodified, so a plain KISS client sees a standard KISS TNC
(port 0, command 0) — with one exception: in TNC mode with
`bridge.kiss_text_translation` on (default), the bridge converts DATA
payloads between the on-air LoRa APRS text format and binary AX.25 at this
boundary (see design decision 6). Config commands from TCP clients are
dropped unless `bridge.allow_client_config` is set.

## Design decisions (with rationale)

### 1. KISS-over-raw-serial instead of Bridge/Console (deviation from brief §2.4)

The brief suggests the Yún `Bridge`/`Console` stack. We use plain
`Serial` at 115200 with KISS framing instead, because:

- `Console` is designed for text and does not guarantee binary
  transparency; a TNC must be 8-bit clean.
- The Yún bridge daemon adds latency and memory pressure on a 64MB device
  for no benefit here.
- KISS is a standard, battle-tested framing (we did not invent a serial
  protocol from scratch — the deviation clause in §2.4 is satisfied).

Deployment consequence: the Linux console/getty on `/dev/ttyATH0` must be
disabled (see `hardware-notes.md`), exactly as Yún-class boards require
when using the UART directly.

### 2. Pure pass-through RF despite RadioHead headers (hard constraint §4.3.1)

`RH_RF95` normally prepends a 4-byte header (TO/FROM/ID/FLAGS) to every
on-air packet, which would break interoperability with RNode firmware and
with the LoRa-APRS ecosystem (both transmit bare payloads).

Workaround implemented in the sketch, using only public RadioHead API:

- **TX**: the first 4 bytes of the payload are mapped onto
  `setHeaderTo/From/Id/Flags` and the remainder is passed to `send()` —
  the transmitted air payload is therefore exactly the original bytes.
- **RX**: with `setPromiscuous(true)`, the 4 bytes RadioHead consumed as
  "header" are re-prepended from `headerTo()/headerFrom()/headerId()/
  headerFlags()` before handing the payload up.

Limitations, accepted and documented: payloads shorter than 4 bytes cannot
be sent or received (irrelevant for APRS and Reticulum traffic), and
RadioHead's CRC handling still applies (LoRa CRC on, standard).

### 3. Digipeater logic on the AR9331 (resolves brief §8 open question)

WIDEn-N parsing/dedupe in Python on the Linux side, not on the ATmega328P.
Rationale: the 328P has 2KB RAM and the sketch must stay mode-agnostic;
string parsing and a dedupe table are trivial in Python and awkward in C on
a constrained MCU. Consequence: "standalone" digipeater means "no internet
needed", not "survives a Linux-side crash" — acceptable, since the AR9331
must be up anyway for the bridge to exist.

### 4. AR9331 language: Python 3, stdlib only (resolves brief §8 open question)

`python3-light` is available via opkg for OpenWrt 18.06 / mips_24kc
(verify on the actual Dragino feed at install time — fallback would be a
C rewrite of the bridge). No third-party modules: serial via `termios`,
config via a built-in parser for the two-level YAML subset used in
`config.example.yaml`. Each daemon is a single self-contained file to make
deployment a plain `scp`.

### 5. iGate: uplink always, downlink (APRS-IS → RF) as a runtime toggle

Uplink (RF → APRS-IS) applies the standard no-gate rules (TCPIP/TCPXX/
NOGATE/RFONLY, third-party `}` packets); the q construct is added by the
server.

Downlink (`igate.downlink`, default off; toggled at runtime with
`chimera-mode igate-tx on|off` or from the LuCI page, restarting only the
iGate daemon — bridge and port 8001 untouched) follows the aprs-is.net
IGating conventions rather than retransmitting the server feed: only APRS
messages (`::ADDRESSEE:`), only when the addressee was heard on RF within
`igate.heard_seconds` (default 30 min — the daemon keeps a heard table
updated *before* the no-gate rules, since a NOGATE station is still
reachable on RF), wrapped in third-party format
(`MYCALL>TOCALL:}SRC>DEST,TCPIP,MYCALL*:body`) and transmitted through the
same bridge KISS socket the digipeater uses. Frames that would exceed the
255-byte LoRa payload are dropped, never truncated. A valid APRS-IS
passcode is required: with the receive-only `-1` the server routes nothing
to us, and the daemon forces downlink off with a warning. Tier 2 servers
automatically send messages addressed to stations we recently gated, so no
extra server filter is normally needed (`igate.filter` exists for special
cases). Gating logic covered by `tests/test_igate_downlink.py`.

### 6. APRS on-air format: LoRa-APRS "OE" convention, AX.25 on the KISS side

Payloads are `0x3C 0xFF 0x01` + TNC2-style ASCII, matching the de-facto
433.775 LoRa APRS ecosystem (OE5BPA trackers, iGates etc.). KISS clients
(PinPoint, Xastir, APRSIS32…) expect binary AX.25 UI frames instead, and
silently discard the raw text. In **TNC mode**, with
`bridge.kiss_text_translation` on (default), the bridge therefore converts
bidirectionally at the TCP↔serial boundary:

- **RX (air → client)**: OE-header text (or headerless TNC2 text, which
  some firmwares transmit) is parsed and re-encoded as an AX.25 UI frame
  (control `0x03`, PID `0xF0`); the TNC2 `*` becomes the H
  (has-been-repeated) bit on that digipeater **and every digi before it**.
  The info field is copied verbatim (base91-compressed positions contain
  arbitrary printable bytes and significant spaces). Payloads that already
  look like valid AX.25 pass through; anything else is dropped and logged
  in hex — no more 3-byte junk frames reaching the client.
- **TX (client → air)**: the AX.25 UI frame is serialized back to TNC2
  text (`*` only on the last digi with the H bit set), prefixed with the
  OE header and transmitted. Packets that would exceed the 255-byte LoRa
  payload are dropped and logged, never truncated. Payloads already
  starting with the OE header pass through (legacy text-speaking clients).

The conversion is strictly local to the KISS TCP link — the on-air format
does not change, consistent with the layering rule (§9) and the "nothing
leaks on air" constraint (§4.3.1). It is gated on `mode: tnc`: in aprs
mode `digipeater.py`/`igate.py` parse the raw text themselves, and in
reticulum mode the bridge must stay byte-exact pass-through. Round-trip
(text→AX.25→text and AX.25→text→AX.25) is byte-identical; see
`tests/test_kiss_translation.py`, built on packets captured off the air
from the Italian RadioGroup/PIRS network (tocall `APLRG1`).

### 7. Serial TX pacing in the bridge (resolves "TX flow control")

Found on hardware 2026-07-11 during RNode interop testing: the modem is
effectively **deaf while it transmits**. `txRaw()` blocks in
`waitPacketSent()` for the whole airtime (~0.73 s for a full 255-byte frame
at SF8/BW125, ~9 s at SF12) and the ATmega328P hardware serial buffer holds
only 64 bytes, so any frame written to the serial link during an ongoing
transmission arrives truncated (~26–28 bytes lost per overrun). Symptom:
RNode split packets (two back-to-back LoRa frames, any RNS payload over
~135 bytes) systematically lost their second frame; the same corruption
would hit any close burst of APRS or KISS traffic.

Fix: the bridge routes **every** serial write through a paced queue
(`TxPacer`): the next frame is written only after the previous DATA frame's
computed time-on-air (Semtech AN1200.13 formula, explicit header + CRC as
RH_RF95 configures them) plus serial transfer time and a 100 ms guard. The
pacer tracks radio parameter changes (CMD_SET*) so airtime estimates follow
the active profile. Queue cap 32 frames, overflow dropped and logged —
KISS gives no delivery guarantee, and unbounded buffering would just add
latency. Chosen over an ATmega-side fix (explicit READY flow control or
bigger buffers) because it requires no reflash and protects all three modes
at the single point that owns the serial port. Verified on hardware: split
packets now pass intact in both directions (`tests/test_bridge_pacing.py`
covers the airtime math and queue behaviour).

Same root cause, RX side (found 2026-07-12): the modem is not ready to
accept a TX frame right after a reception either — it is still streaming
the received frame + SIGREPORT up the serial link and re-arming the radio.
Frames written in that window never reach the air: locally-generated
Reticulum link proofs (sent ~50–100 ms after the link request arrives) were
systematically lost, while internet-relayed replies (≥ 0.5 s later) went
through. The pacer therefore also holds off TX for 2 s after any frame is
received from the modem (`TxPacer.rx_seen()`); an RX only ever extends the
ready time, it never shortens a pending airtime wait.

## RNode interoperability (Reticulum mode)

Acceptance criterion: the Dragino and a stock RNode device (any supported
board) exchange Reticulum traffic over LoRa with no protocol translation.
RNode's KISS command set is host-local only, exactly like our bridge
protocol — but, **contrary to the original project assumption, RNode is
not a fully transparent pipe on air**. Verified against
`markqvist/RNode_Firmware` source (2026-07, `RNode_Firmware.ino`
`transmit()`/`receive_callback()`, `Config.h`, `Framing.h`, `sx127x.cpp`):

- **Every LoRa frame RNode transmits starts with 1 framing byte**: high
  nibble = random per-packet sequence, bit 0 = `FLAG_SPLIT` (0x01).
- Host packets up to `MTU` 508 bytes; anything longer than 254 bytes is
  split into **exactly two** LoRa frames (254 + up to 254 data bytes),
  both carrying the same header byte. The receiver completes reassembly
  when the second frame with a matching sequence arrives; a non-split
  frame or a different sequence discards a pending half.
- **Sync word: `0x12`** on SX127x (`SYNC_WORD_7X`) — the SX127x "private"
  default; our profiles already used it.
- **Preamble: dynamic** — `max(24 ms of symbols, 18 symbols)`
  (`LORA_PREAMBLE_TARGET_MS` 24, `LORA_PREAMBLE_SYMBOLS_MIN` 18). At
  SF8/BW125 and slower this resolves to 18 symbols → `radio_reticulum`
  now uses `preamble_symbols: 18`.
- RNode also does CSMA/CAD before transmitting. We do not replicate this
  (the modem transmits immediately) — acceptable for initial interop,
  tracked as an open question below.

Division of labour: the RNode PHY framing (header byte, split/reassembly)
is implemented **entirely in `ChimeraInterface.py` on the host**
(`build_frames()` / `_process_frame()`). The ATmega sketch and the bridge
remain pass-through; each `CMD_DATA` frame equals one on-air LoRa frame.
This required the sketch's serial buffer to accept 255-byte payloads
(`SER_BUF_LEN` 256) — full-size RNode frames use all 255 bytes.

Both ends must still match: frequency, BW, SF, CR, sync word.

## Open questions (carried from the brief §8, updated)

- [x] **RNode default sync word / preamble length** — verified from source,
      mirrored in `radio_reticulum` (`0x12` / 18 symbols), see above.
- [x] **RNS MTU 500 vs SX127x 255-byte frame limit** — resolved: RNode
      splits >254-byte packets into two framed LoRa frames; replicated in
      `ChimeraInterface.py`, `HW_MTU = 508` confirmed correct. Caveat: RNS
      `Interface.__init__()` shadows the class attribute with an instance
      `HW_MTU = None`, which silently breaks inbound link ids on RNS 1.x;
      `ChimeraInterface.__init__` re-asserts it (see comment in the file).
- [ ] **CSMA/CAD channel access** — RNode senses the channel before TX,
      we do not; add CAD-based hold-off in the sketch if collisions bite.
- [x] **AX.25 ↔ TNC2 conversion for TNC mode** — resolved: bidirectional
      conversion in the bridge, `bridge.kiss_text_translation` (default on,
      TNC mode only), see design decision 6.
- [x] **TX flow control** — it bit (RNode split packets lost their second
      frame): airtime-paced TX queue in the bridge, see design decision 7.
- [x] **iGate downlink (APRS-IS → RF)** — implemented as a runtime toggle
      (`igate.downlink` / `chimera-mode igate-tx`), see design decision 5.
- [ ] License choice before publishing.
- [ ] Interop testing across ≥2 RNode chip families (SX1276/78 + SX1262/68),
      RNode firmware ≥ 1.80. **SX1262/68 done 2026-07-11**: Heltec LoRa32 v3
      (SX1268, RNode 1.86) ↔ Dragino, echo round-trips at 64/180/300-byte
      payloads in both directions incl. split packets, RSSI ≈ -55 dBm /
      SNR ≈ 12 dB on the bench. Still open: an SX1276/78-based board.
- [ ] Verify `python3-light` availability on the actual Dragino 18.06 feed.
- [ ] Confirm RNS custom-interface loading against the RNS versions bundled
      by current MeshChat and Sideband releases.
