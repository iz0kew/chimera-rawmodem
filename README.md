# chimera-rawmodem

🇮🇹 [Versione italiana](README.it.md)

Turns a **Dragino LG01-P** into a multi-mode raw LoRa node at 433MHz: KISS
TNC over TCP, standalone APRS digipeater + iGate, and a Reticulum network
interface — no LoRaWAN, no reflash to switch modes.

"Chimera": one device, three interoperable personalities, sharing a single
"raw radio modem" firmware layer.

## Why

The LG01-P ships as a single-channel LoRaWAN gateway. This project replaces
that use case entirely and exposes the radio as a general-purpose raw LoRa
modem instead.

Unlike the LG01-N / LG02 (where the SX1276 hangs directly off the Linux SoC's
SPI bus), the **LG01-P has an ATmega328P between the AR9331 and the radio**,
making it architecturally an Arduino Yún. All radio control therefore runs as
an Arduino sketch on the ATmega328P; the Linux side only bridges the serial
link to the network. See [docs/hardware-notes.md](docs/hardware-notes.md).

## Operating modes

| Mode | What the device does | Client side |
|---|---|---|
| **TNC** | Exposes the radio as a KISS TNC over TCP | PinPoint, Xastir, APRSIS32, YAAC, any KISS-speaking software |
| **Digipeater + iGate** | Standalone LoRa APRS digipeater (WIDEn-N), optionally gating traffic to APRS-IS. Digi and iGate are independently toggleable | None required (standalone) |
| **Reticulum** | Exposes the radio as a raw packet interface for a custom RNS `Interface` class | `rnsd`, [MeshChat](https://github.com/liamcottle/reticulum-meshchat), [Sideband](https://github.com/markqvist/Sideband) on an external host |

In TNC mode the bridge translates between what's on the air and what the
client expects: the 433.775 LoRa APRS ecosystem (OE5BPA trackers,
RadioGroup/PIRS nodes…) transmits **text** packets (`<0xFF0x01` + TNC2
ASCII), while KISS clients expect **binary AX.25** frames. The bridge
converts bidirectionally — received text becomes proper AX.25 UI frames
(callsigns, path and has-been-repeated bits included), and the client's
AX.25 beacons go out as LoRa APRS text the surrounding nodes understand.
Controlled by `bridge.kiss_text_translation` in the config (default on;
turn it off to get raw payloads). Unrecognized on-air payloads are dropped
and logged instead of being forwarded as junk.

In Reticulum mode the device replicates the on-air behaviour of the official
[RNode firmware](https://github.com/markqvist/RNode_Firmware) (1-byte PHY
framing, sync word, preamble — implemented in the host-side interface class),
so it is designed to exchange Reticulum traffic directly with any RNode
device, provided the radio parameters (frequency, SF, BW, CR) match.

Switching personality is one command on the device (`chimera-mode
tnc|aprs|reticulum`), persistent across reboots — or a click in the web UI.
No reflash needed.

## Hardware required

- Dragino **LG01-P**, 433MHz version (not LG01-N, not LG02 — different,
  incompatible radio architecture)
- Ethernet connectivity for the iGate function (optional otherwise)
- For Reticulum mode: any host on the network running RNS (PC, homelab, phone)

No USB cable and no Arduino IDE needed: a prebuilt firmware image is
included and the ATmega328P is flashed from the Dragino itself.

## Installation

Full step-by-step guide (factory device → working node, with commands for
Linux, macOS and Windows): **[docs/INSTALL.md](docs/INSTALL.md)**.

In short:

1. SSH into the Dragino, free the internal serial port from the factory
   Yún bridge, fix the kernel-console settings (required for boot
   reliability).
2. Copy the daemons, init scripts and your config (created from the
   `config/*.example.*` templates — **never commit the real files**).
3. Flash the prebuilt sketch image from the Dragino itself
   (`run-avrdude`), no USB needed.
4. `chimera-mode aprs|tnc|reticulum` and go.

## Repository layout

```
firmware/atmega328p-modem/   Arduino sketch (single sketch, all 3 modes)
firmware/.../prebuilt/       compiled .hex images, ready to flash
openwrt/bridge/              AR9331 serial<->TCP bridge daemon
openwrt/digipeater/          standalone APRS digipeater daemon
openwrt/igate/               APRS-IS client daemon
openwrt/init.d/              OpenWrt boot scripts
openwrt/chimera-mode         one-command personality switcher
openwrt/luci/                web UI page for mode switching
reticulum/interface/         custom RNS Interface class (runs on external host)
config/                      configuration templates (*.example.*)
docs/                        install guide, architecture, hardware notes
```

## Status

Running in production on real hardware (factory OpenWrt Chaos Calmer
15.05.1, Python 2.7):

- **TNC mode**: full chain verified end-to-end (TCP client → bridge →
  serial → sketch → radio and back), boot-safe including power-loss tests.
- **Digipeater + iGate**: deployed and live on APRS-IS.
- **Reticulum mode**: implemented (including RNode on-air framing,
  verified against the RNode firmware source) but **not yet validated
  against real RNode hardware** — treat as experimental. See the open
  questions in [docs/architecture.md](docs/architecture.md).

## Credits

- [Dragino](https://www.dragino.com/) — LG01-P hardware and OpenWrt firmware
- [RadioHead](https://www.airspayce.com/mikem/arduino/RadioHead/) — RH_RF95 driver
- [aprs-is.net](https://www.aprs-is.net/) — APRS-IS connection conventions
- [Reticulum](https://reticulum.network/) / RNode by Mark Qvist

## Author

IZ0KEW

## License

[GPLv3](LICENSE) — consistent with most amateur-radio LoRa software in
this space.
