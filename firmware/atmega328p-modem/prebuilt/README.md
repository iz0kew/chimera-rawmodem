# Prebuilt firmware images

Compiled from `../atmega328p-modem.ino` with the Dragino AVR core
(`Dragino:avr:unoyun`, ATmega328P @ 16 MHz) and RadioHead 1.143.1.

| File | Use it when |
|---|---|
| `atmega328p-modem.ino.with_bootloader.hex` | **Flashing from the Dragino itself** (`run-avrdude` over SSH, or the web UI "Flash MCU" page if your firmware has one). The on-board ISP programmer erases the whole chip, so this image includes the serial bootloader — without it, later uploads from the Arduino IDE would stop working. |
| `atmega328p-modem.ino.hex` | Flashing through an external tool that expects a plain application image (e.g. `avrdude` with a serial bootloader already on the chip). The Arduino IDE never needs this file — it builds its own. |

See [docs/INSTALL.md](../../../docs/INSTALL.md) for the full flashing
procedure, and [docs/INSTALL.md § Compiling yourself](../../../docs/INSTALL.md#appendix--compiling-the-sketch-yourself)
if you prefer to build from source.
