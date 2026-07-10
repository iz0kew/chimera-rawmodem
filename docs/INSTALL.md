# Installation guide — from factory LG01-P to chimera-rawmodem

🇮🇹 [Versione italiana](INSTALL.it.md)

This guide takes a **Dragino LG01-P (433 MHz)** with factory firmware and
turns it into a chimera-rawmodem node, step by step. No firmware reflash of
the Linux side is needed: everything installs on top of the factory OpenWrt
(verified on Chaos Calmer 15.05.1, the firmware the board ships with).

Every step was verified on real hardware. Expect 30–45 minutes.

> **⚠️ Radio legality**: transmitting on 433 MHz amateur allocations
> requires an amateur radio license in most countries. The APRS modes
> require a valid callsign. Check your local band plan before transmitting.

---

## What you need

- A Dragino **LG01-P**, 433 MHz version (not LG01-N, not LG02 — those have
  a different radio architecture and are not supported)
- A PC running **Linux, macOS or Windows 10/11** with an SSH client
  (built into all three)
- An Ethernet cable (recommended) or the Dragino's own WiFi AP
- This repository, cloned or downloaded:

  ```sh
  git clone https://github.com/IZ0KEW/chimera-rawmodem.git
  cd chimera-rawmodem
  ```

You do **not** need the Arduino IDE, a USB cable, or any OpenWrt toolchain:
the ATmega328P is flashed from the Dragino itself using the prebuilt image
in `firmware/atmega328p-modem/prebuilt/`. (Building from source is covered
in the [appendix](#appendix--compiling-the-sketch-yourself).)

All shell commands below are shown for the three platforms where they
differ. Where a single block is shown, it is identical on Linux, macOS and
Windows PowerShell.

---

## Step 1 — Connect to the Dragino and log in

Factory defaults:

| | |
|---|---|
| WiFi AP | `dragino2-xxxxxx`, open network |
| IP (via its WiFi or its LAN port) | `10.130.1.1` |
| SSH / web login | `root` / `dragino` |

Either join its WiFi AP, or plug the Dragino's **WAN port** into your
router (it takes a DHCP address — check your router's client list) and use
that IP. The WAN route is what you want anyway for the iGate.

In the commands below, replace `10.130.1.1` with your device's actual IP
if it differs.

First contact — the factory dropbear is old and speaks only legacy SSH
algorithms, so a plain `ssh root@...` fails on modern systems with "no
matching key exchange method". Create an SSH host alias once and forget
about it:

**Linux / macOS** — append to `~/.ssh/config` (create the file if missing):

```sh
cat >> ~/.ssh/config <<'EOF'
Host dragino
    HostName 10.130.1.1
    User root
    KexAlgorithms +diffie-hellman-group14-sha1
    HostKeyAlgorithms +ssh-rsa
    PubkeyAcceptedAlgorithms +ssh-rsa
    MACs +hmac-sha1
EOF
```

**Windows (PowerShell)** — same content, Windows path:

```powershell
Add-Content -Encoding ascii "$env:USERPROFILE\.ssh\config" @'
Host dragino
    HostName 10.130.1.1
    User root
    KexAlgorithms +diffie-hellman-group14-sha1
    HostKeyAlgorithms +ssh-rsa
    PubkeyAcceptedAlgorithms +ssh-rsa
    MACs +hmac-sha1
'@
```

> If your OpenSSH is older than 8.5 it may reject the
> `PubkeyAcceptedAlgorithms` line — just delete it; it only matters for
> key-based login.

Now log in and **change the default password immediately**:

```sh
ssh dragino          # password: dragino
passwd               # set your own
```

Keep this SSH session open — steps 2 and 3 run on the Dragino.

## Step 2 — Free the serial port from the factory bridge

On the factory firmware, `/dev/ttyATH0` (the internal UART to the
ATmega328P) is owned by Dragino's Yún-style bridge. Release it
(**on the Dragino**, in your SSH session):

```sh
uci set sensor.poweruart.uartmode='noconsole'
uci commit sensor
/usr/bin/set_uart_console 0
/etc/init.d/iotd disable
```

Do **not** disable `dragino.init` — it drives GPIO24, which powers the UART.

This is reversible at any time (back to stock behaviour):
`uci set sensor.poweruart.uartmode='bridge'; uci commit sensor;
/etc/init.d/iotd enable; reboot`.

## Step 3 — Fix the kernel console (required, or the bridge dies at boot)

The kernel console shares the same UART as the ATmega328P. Two factory
settings must be changed, or the bridge will crash in a loop with I/O
errors after every reboot (found the hard way — details in
[hardware-notes.md](hardware-notes.md)). Still **on the Dragino**:

```sh
# 1. stop procd's askconsole from hijacking the tty when radio bytes arrive
cp /etc/inittab /etc/inittab.bak
sed -i 's|^::askconsole:|#::askconsole:|' /etc/inittab

# 2. stop runtime kernel messages from corrupting the serial stream
echo 'kernel.printk = 1 4 1 7' >> /etc/sysctl.conf
sysctl -w kernel.printk='1 4 1 7'

# 3. create the config directory while we're here
mkdir -p /etc/chimera
```

You lose nothing: a serial console on that UART was never usable (the
ATmega is wired to it, not a terminal), and SSH access is unaffected.
Revert = restore `/etc/inittab.bak` and reboot.

## Step 4 — Create your configuration files (on the PC)

Copy the two templates and fill in **your own** data. Never put real
callsigns/passcodes back into the repo — the real filenames are gitignored
by design.

**Linux / macOS:**

```sh
cp config/config.example.yaml config/config.yaml
cp config/aprs-is.example.conf config/aprs-is.conf
nano config/config.yaml config/aprs-is.conf   # or your editor
```

**Windows (PowerShell):**

```powershell
Copy-Item config\config.example.yaml config\config.yaml
Copy-Item config\aprs-is.example.conf config\aprs-is.conf
notepad config\config.yaml
notepad config\aprs-is.conf
```

What to edit:

- `config.yaml` → `digipeater:` section: set `callsign:` to your
  callsign-SSID (e.g. `N0CALL-1`; the daemon refuses to start with the
  `N0CALL` placeholder). Leave `mode: tnc` and both `enabled: false` for
  now — you will switch personalities later with one command.
- `aprs-is.conf` (only needed for the iGate): your callsign and your
  [APRS-IS passcode](https://apps.magicbug.co.uk/passcode/). `passcode -1`
  keeps you receive-only (and disables the iGate downlink, which needs a
  valid passcode).
- The radio profiles default to the European LoRa APRS convention
  (433.775 MHz, SF12, BW125, CR4/5) — verify against your local band plan.

## Step 5 — Copy everything to the Dragino

From the repo root **on the PC**. The commands are identical on all three
platforms (PowerShell included — OpenSSH is built into Windows 10/11):

```sh
scp -O openwrt/bridge/chimera-bridge.py   dragino:/usr/bin/chimera-bridge.py
scp -O openwrt/digipeater/digipeater.py   dragino:/usr/bin/chimera-digipeater.py
scp -O openwrt/igate/igate.py             dragino:/usr/bin/chimera-igate.py
scp -O openwrt/chimera-mode               dragino:/usr/bin/chimera-mode
scp -O openwrt/init.d/chimera-bridge      dragino:/etc/init.d/chimera-bridge
scp -O openwrt/init.d/chimera-digipeater  dragino:/etc/init.d/chimera-digipeater
scp -O openwrt/init.d/chimera-igate       dragino:/etc/init.d/chimera-igate
scp -O config/config.yaml                 dragino:/etc/chimera/config.yaml
scp -O config/aprs-is.conf                dragino:/etc/chimera/aprs-is.conf
scp -O firmware/atmega328p-modem/prebuilt/atmega328p-modem.ino.with_bootloader.hex dragino:/tmp/chimera.hex
```

> Notes on `scp -O`: the factory dropbear has no SFTP subsystem, so modern
> scp needs `-O` (legacy protocol). If your scp says `unknown option -- O`,
> it is an older client that uses the legacy protocol anyway — just remove
> the flag. On Windows, run this from PowerShell, not cmd.

Then make everything executable (**on the Dragino**):

```sh
ssh dragino "chmod +x /usr/bin/chimera-*.py /usr/bin/chimera-mode /etc/init.d/chimera-*"
```

## Step 6 — Flash the ATmega328P

No USB needed: the AR9331 flashes the ATmega through the board's own ISP
lines. The image was copied to `/tmp/chimera.hex` in step 5.
**On the Dragino:**

```sh
run-avrdude /tmp/chimera.hex
```

Wait for `avrdude done. Thank you.` — takes about a minute; `32768 bytes of
flash verified` means success.

> Use the **`with_bootloader`** image here (as step 5 does): the on-board
> programmer erases the whole chip, and this image keeps the serial
> bootloader so future Arduino-IDE uploads remain possible.
>
> Alternative: some factory firmware versions expose an MCU-flash page in
> the web UI (menu **Sensor**) that accepts a `.hex` upload and runs the
> same `run-avrdude` under the hood. If yours has it, you can use it with
> the same `with_bootloader` file instead of the command above. The SSH
> method above is the one verified by this project.

## Step 7 — Start the bridge and verify

**On the Dragino:**

```sh
/etc/init.d/chimera-bridge enable
/etc/init.d/chimera-bridge start
```

> On this old OpenWrt, `enable` may return a non-zero exit code even when
> it worked — ignore it (and never chain it with `&&`).

Verify the whole chain:

```sh
chimera-mode status
```

Expected output:

```
mode:     tnc
bridge: boot:on  running
digipeater: boot:off stopped
igate: boot:off stopped
profile:  433775000Hz SF12 BW125000 CR4/5 10dBm sync 0x12 pre 8
port 8001: LISTEN
```

If `profile:` shows real values, the full path works: Linux daemon → serial
→ ATmega328P → radio chip configured. If something is off, see
[Troubleshooting](#troubleshooting).

Finally, reboot once and re-run `chimera-mode status` — everything must
come back on its own (this validates step 3):

```sh
reboot
# wait ~90 s, then:
ssh dragino chimera-mode status
```

## Step 8 — Pick a personality

One command, persistent across reboots and power loss:

```sh
chimera-mode tnc         # pure KISS TNC over TCP (default)
chimera-mode aprs        # digipeater + iGate
chimera-mode reticulum   # Reticulum radio modem
chimera-mode igate-tx on|off   # iGate downlink (APRS-IS -> RF) toggle
chimera-mode status      # what is running right now
```

### TNC mode

Nothing else to configure on the device. Point any KISS-over-TCP client
(Xastir, YAAC, APRSIS32, PinPoint, direwolf as client, …) at:

```
host: <Dragino IP>    port: 8001    protocol: KISS over TCP
```

### APRS mode (digipeater + iGate)

`chimera-mode aprs` enables both. They are independent — edit
`/etc/chimera/config.yaml` on the device to run only one:

- digipeater only (no internet needed): `igate:` → `enabled: false`
- iGate only (no RF retransmission): `digipeater:` → `enabled: false`

then `/etc/init.d/chimera-digipeater restart` /
`/etc/init.d/chimera-igate restart` as appropriate. The iGate logs in to
`euro.aprs2.net:14580` by default — pick your region's rotate address in
`config.yaml` (`noam`/`soam`/`euro`/`asia`/`aunz`.aprs2.net).

The iGate is uplink-only (RF → APRS-IS) by default. `chimera-mode igate-tx
on` also gates APRS **messages** from the internet to RF — only those
addressed to stations heard on RF in the last 30 minutes, in standard
third-party format. It requires a valid passcode in `aprs-is.conf` (with
`-1` it stays off) and only restarts the iGate daemon, so the bridge and
any connected clients are untouched. `chimera-mode igate-tx off` turns it
back off; both persist across reboots and are also available as buttons on
the LuCI page.

Check it works: `logread | grep chimera` on the device, and after the
first received packet, search your callsign's raw packets on aprs.fi for
`qAR,YOURCALL-SSID`.

### Reticulum mode

`chimera-mode reticulum` on the device, then set up the **host** side
(your PC — this part never runs on the Dragino):

**Linux / macOS:**

```sh
mkdir -p ~/.reticulum/interfaces
cp reticulum/interface/ChimeraInterface.py ~/.reticulum/interfaces/
```

**Windows (PowerShell):**

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.reticulum\interfaces" | Out-Null
Copy-Item reticulum\interface\ChimeraInterface.py "$env:USERPROFILE\.reticulum\interfaces\"
```

Then add to `~/.reticulum/config` (works identically for `rnsd`, MeshChat
and Sideband — Sideband has no interface editor UI, edit its config file
by hand):

```ini
[[Chimera LoRa]]
  type = ChimeraInterface
  enabled = yes
  target_host = 10.130.1.1   # your Dragino's IP
  target_port = 8001
  spreading_factor = 8       # keep in sync with radio_reticulum
  bandwidth_hz = 125000      # in the Dragino's config.yaml
  coding_rate = 5
```

Requires RNS ≥ 0.7.0. The interface replicates RNode's on-air framing, so
it is designed to interoperate with any device running the official RNode
firmware on matching radio parameters — **but this has not yet been
validated against real RNode hardware** (see project status in the README).

## Step 9 (optional) — Mode switching from the web UI

If you want to switch personalities from a browser instead of SSH:

```sh
scp -O openwrt/luci/controller/chimera.lua dragino:/usr/lib/lua/luci/controller/chimera.lua
ssh dragino "rm -f /tmp/luci-indexcache"
```

The page appears at **System → Chimera Mode** in the Dragino's web
interface (`http://<Dragino IP>/cgi-bin/luci`, behind the normal login).

---

## Troubleshooting

**`port 8001: NOT listening`** — the bridge is not running. Check
`logread | grep chimera-bridge`.

**Bridge crash-loops with `OSError [Errno 5]` / EIO after a reboot** —
step 3 was skipped or `/etc/inittab` was restored. Verify the
`::askconsole:` line is commented out, reboot. (`set_uart_console 0` does
NOT fix this — the askconsole line is a different mechanism.)

**`profile:` line is empty in `chimera-mode status`** — the ATmega isn't
answering: sketch not flashed (step 6), or the factory bridge still owns
the serial port (step 2), or you rebooted between step 2 and step 3 and
the console grabbed the tty again.

**`scp: unknown option -- O`** — older OpenSSH client: remove the `-O`,
legacy protocol is its default.

**`ssh: no matching key exchange method`** — the `~/.ssh/config` entry
from step 1 is missing or not being read (on Windows, check the file has
no `.txt` extension).

**Digipeater refuses to start** — you left `callsign: N0CALL-1` in
`config.yaml`. It refuses placeholder callsigns on purpose.

**Back to factory** — re-enable the stock bridge
(`uci set sensor.poweruart.uartmode='bridge'; uci commit sensor;
/etc/init.d/iotd enable`), restore `/etc/inittab.bak`, disable the
chimera services, reboot. The stock LoRaWAN gateway sketch would need to
be re-flashed from Dragino's examples to fully return to stock.

---

## Appendix — Compiling the sketch yourself

Only needed if you modify `atmega328p-modem.ino`. The prebuilt images make
this optional for everyone else.

1. Install **Arduino IDE 2.x**.
2. File → Preferences → *Additional boards manager URLs*, add Dragino's
   board index (from the
   [Dragino wiki](https://wiki1.dragino.com/index.php/Main_Page)):
   `http://www.dragino.com/downloads/downloads/YunShield/package_dragino_yun_test_index.json`
3. Boards Manager: install **Dragino Yún** boards, and pin **Arduino AVR
   Boards to 1.6.9** — the Dragino core requires the old gcc 4.8.1
   toolchain and breaks with newer AVR cores. Do not let the IDE
   "update" it.
4. Library Manager: install **RadioHead** (≥ 1.88; built and tested with
   1.143.1).
5. If the IDE 2.x preprocessor fails on this core, create
   `platform.local.txt` next to the Dragino core's `platform.txt`
   (inside `Arduino15/packages/Dragino/hardware/avr/...`) containing:

   ```
   recipe.preproc.macros="{compiler.path}{compiler.cpp.cmd}" {compiler.cpp.flags} -w -x c++ -E -CC -mmcu={build.mcu} -DF_CPU={build.f_cpu} -DARDUINO={runtime.ide.version} -DARDUINO_{build.board} -DARDUINO_ARCH_{build.arch} {includes} "{source_file}" -o "{preprocessed_file_path}"
   ```

6. Board: **Dragino Yún + UNO or LG01/OLG01** (FQBN
   `Dragino:avr:unoyun`). Sketch → *Export compiled binary*, then flash
   the `with_bootloader` hex exactly as in step 6.

CLI equivalent:

```sh
arduino-cli compile --fqbn Dragino:avr:unoyun --output-dir build firmware/atmega328p-modem
```
