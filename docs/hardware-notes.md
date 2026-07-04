# Hardware notes — Dragino LG01-P

These facts were established through hands-on research/verification during
project planning. Do not silently re-assume them differently; document any
deviation with a reason (project convention §9).

## Board identity

- Device: **Dragino LG01-P**, 433MHz version — **not** LG01-N, **not**
  LG02/LG08 (different, incompatible radio architecture, see below)
- SoC: Atheros **AR9331**, 400MHz MIPS 24Kc, 64MB RAM, 16MB flash
- OS: the reference unit runs the **factory firmware, OpenWrt Chaos Calmer
  15.05.1** (`ar71xx/generic`), verified on-device 2026-07-04. The Dragino
  18.06 fork (<https://github.com/dragino/openwrt_lede-18.06>) exists but
  is **not** what ships on this board; this project targets the factory
  firmware as-is (deviation from the original brief, reason below under
  "Python runtime").
- Radio MCU: **ATmega328P** — the critical architectural fact for this
  project

## Radio architecture — read before touching any driver code

On the LG01-N / LG02 / LG08 the SX127x is wired via SPI **directly** to the
AR9331, so Linux userspace can drive the radio (`lg02_single_rx_tx` etc.).

**The LG01-P is different: an ATmega328P sits between the AR9331 and the
LoRa module.** The AR9331 cannot talk SPI to the radio at all. The board is
architecturally an Arduino Yún:

```
AR9331 (Linux) ◄─UART 115200─► ATmega328P ◄─SPI─► SX1276/RFM95
```

Consequences:

- All raw radio control runs on the ATmega328P as an Arduino sketch.
- The Linux side only ever talks to the ATmega328P over the serial link
  (`/dev/ttyATH0`) and to the network.
- Do **not** port `lg02_single_rx_tx`, `lg01_pkt_fwd` or any SPI-direct
  tooling from LG01-N/LG02 documentation — it cannot work on this board
  without physically removing the ATmega328P and rewiring SPI, which is
  explicitly out of scope.

## Radio driver

**RadioHead `RH_RF95`** (≥ 1.88), not `arduino-lmic`: LMIC drags in the
whole LoRaWAN MAC, which is useless overhead for raw packet TX/RX. RH_RF95
is confirmed present in official Dragino examples for this exact board
(`LG01_ThingSpeak_RESTful_Single_Data.ino`) with the default constructor
pin mapping (CS=10, DIO0=pin 2).

Interoperability warning: RH_RF95 adds a 4-byte header to packets on air by
default. This firmware neutralizes it — see "design decision 2" in
`architecture.md` before changing anything in the TX/RX path.

## Serial link ATmega328P ↔ AR9331

- 115200 baud, the Yún-style internal UART (`/dev/ttyATH0` on Linux).
- This project uses raw serial with KISS framing instead of the
  `Bridge`/`Console` libraries (rationale in `architecture.md`).
- **Deployment requirement — verified on the factory firmware**: out of the
  box `/dev/ttyATH0` is owned by Dragino's Yún-style bridge
  (`python -u bridge.py`, launched via the console mechanism because
  `sensor.poweruart.uartmode='bridge'` in UCI). It must be released before
  chimera-bridge can run:

  ```sh
  uci set sensor.poweruart.uartmode='noconsole'
  uci commit sensor
  /usr/bin/set_uart_console 0
  kill <pid of bridge.py>          # or reboot
  /etc/init.d/iotd disable         # factory iot-daemon, not needed
  # keep /etc/init.d/dragino.init enabled: it drives GPIO24 (UART power)
  ```

  To revert to factory behaviour: `uci set sensor.poweruart.uartmode='bridge'`,
  `uci commit sensor`, `/etc/init.d/iotd enable`, reboot.
- Opening the serial port may reset the ATmega328P (Yún-class behaviour);
  the bridge waits 2 s after opening before pushing config.
- **Deployment requirement — the kernel console also lives on ttyATH0**
  (`console=ttyATH0,115200` in the kernel cmdline, baked into u-boot).
  Two consequences, both verified the hard way on the factory firmware
  (Chaos Calmer 15.05.1):
  1. `/etc/inittab` ships with `::askconsole:/bin/ash --login`. procd's
     askconsole watches the console tty; bytes arriving from the ATmega
     (any KISS frame ending in a byte it reads as "Enter") trigger console
     activation and a tty hangup that invalidates every other open fd on
     the port — chimera-bridge dies with `EIO` on write, procd respawns
     it, and it dies again forever. Note `set_uart_console 0` does NOT fix
     this: it comments out a `ttyATH0::` inittab line that doesn't exist
     in this firmware's inittab. Fix (backup first, procd re-reads inittab
     only at boot):

     ```sh
     cp /etc/inittab /etc/inittab.bak
     sed -i 's|^::askconsole:|#::askconsole:|' /etc/inittab
     reboot
     ```

     Shell access remains available over SSH; a serial console on ttyATH0
     is useless anyway — the ATmega is wired to it, not a terminal.
  2. Kernel messages (`printk`) go out on the same UART and corrupt the
     serial stream toward the ATmega. Silence them persistently:

     ```sh
     echo 'kernel.printk = 1 4 1 7' >> /etc/sysctl.conf
     sysctl -w kernel.printk='1 4 1 7'
     ```

     Boot-time messages (before userspace) still reach the ATmega; the
     sketch's KISS deframer discards them and the bridge re-pushes the
     radio profile afterwards, so this is harmless.

## Access / network

- Factory default IP: `10.130.1.1` (WiFi AP mode by default)
- SSH on the Linux side; factory credentials `root`/`dragino` —
  **change before any exposed deployment** (generic hardware fact, not a
  project secret)
- Ethernet available — required for iGate mode internet access
- The USB port is host-only (no OTG/device mode): the LG01-P can never
  appear as a USB serial TNC to a PC; TCP is the only client transport

## Toolchain

- **ATmega328P sketch**: Arduino IDE + Dragino board profile (board manager
  URL on the Dragino wiki). First flash via USB; network flashing
  ("Network Port" in the IDE) may work depending on installed firmware —
  verify in practice before relying on it.
- **Linux side / Python runtime**: the factory firmware ships **Python
  2.7.9 preinstalled** (it powers Dragino's own bridge/IoT stack) and its
  feeds carry **no python3 package at all**; overlay flash free space is
  ~3MB, so installing a big runtime is off the table anyway. The daemons
  are therefore written to run on Python 2.7 *and* 3.x, and the init
  scripts invoke `/usr/bin/python`. This is a deliberate deviation from the
  original "python3-light on 18.06" plan — adapting to the factory OS was
  chosen over a risky firmware sysupgrade (decision 2026-07-04).
- The OpenWrt SDK (<https://github.com/dragino/openwrt_lede-18.06>) is only
  needed if a required package is unavailable precompiled; its checkout is
  gitignored.

## Installation summary (Linux side)

No package installation needed: the factory Python 2.7 is the runtime.
The old dropbear has no SFTP subsystem — use `scp -O` (legacy protocol)
from modern OpenSSH clients, which also need legacy algorithm options
(`-oKexAlgorithms=+diffie-hellman-group14-sha1 -oHostKeyAlgorithms=+ssh-rsa
-oMACs=+hmac-sha1`), best kept in an `~/.ssh/config` host entry.

```sh
# on the Dragino, after changing default passwords:
mkdir -p /etc/chimera
# free the UART from the factory bridge (see "Serial link" section above)
# from the dev machine (DRAGINO = device IP or ssh alias):
scp -O openwrt/bridge/chimera-bridge.py       root@DRAGINO:/usr/bin/
scp -O openwrt/digipeater/digipeater.py       root@DRAGINO:/usr/bin/chimera-digipeater.py
scp -O openwrt/igate/igate.py                 root@DRAGINO:/usr/bin/chimera-igate.py
scp -O openwrt/init.d/chimera-*               root@DRAGINO:/etc/init.d/
scp -O config/config.yaml config/aprs-is.conf root@DRAGINO:/etc/chimera/
# on the Dragino:
chmod +x /usr/bin/chimera-*.py /etc/init.d/chimera-*
/etc/init.d/chimera-bridge enable && /etc/init.d/chimera-bridge start
# enable digipeater/igate services as needed
```
