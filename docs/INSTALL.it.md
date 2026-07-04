# Guida di installazione — da LG01-P di fabbrica a chimera-rawmodem

🇬🇧 [English version](INSTALL.md)

Questa guida trasforma un **Dragino LG01-P (433 MHz)** con firmware di
fabbrica in un nodo chimera-rawmodem, passo per passo. Non serve riflashare
il lato Linux: tutto si installa sopra l'OpenWrt di fabbrica (verificato su
Chaos Calmer 15.05.1, il firmware con cui la board viene venduta).

Ogni passo è stato verificato su hardware reale. Tempo previsto: 30–45 minuti.

> **⚠️ Legalità radio**: trasmettere sulle allocazioni radioamatoriali dei
> 433 MHz richiede una licenza da radioamatore nella maggior parte dei
> paesi. Le modalità APRS richiedono un nominativo valido. Verifica il band
> plan locale prima di trasmettere.

---

## Cosa serve

- Un Dragino **LG01-P**, versione 433 MHz (non LG01-N, non LG02 — hanno
  un'architettura radio diversa e non sono supportati)
- Un PC con **Linux, macOS o Windows 10/11** e un client SSH (integrato in
  tutti e tre)
- Un cavo Ethernet (consigliato) oppure l'AP WiFi del Dragino stesso
- Questo repository, clonato o scaricato:

  ```sh
  git clone https://github.com/IZ0KEW/chimera-rawmodem.git
  cd chimera-rawmodem
  ```

**Non** servono l'Arduino IDE, un cavo USB né una toolchain OpenWrt:
l'ATmega328P si flasha dal Dragino stesso usando l'immagine precompilata in
`firmware/atmega328p-modem/prebuilt/`. (La compilazione da sorgente è
nell'[appendice](#appendice--compilare-lo-sketch-da-soli).)

I comandi qui sotto sono mostrati per le tre piattaforme dove differiscono.
Dove c'è un solo blocco, è identico su Linux, macOS e Windows PowerShell.

---

## Passo 1 — Collegarsi al Dragino ed effettuare il login

Valori di fabbrica:

| | |
|---|---|
| AP WiFi | `dragino2-xxxxxx`, rete aperta |
| IP (via suo WiFi o sua porta LAN) | `10.130.1.1` |
| Login SSH / web | `root` / `dragino` |

Puoi collegarti al suo AP WiFi, oppure collegare la **porta WAN** del
Dragino al tuo router (prende un IP via DHCP — guarda la lista client del
router) e usare quell'IP. La via WAN è comunque quella che ti serve per
l'iGate.

Nei comandi che seguono, sostituisci `10.130.1.1` con l'IP reale del tuo
dispositivo se diverso.

Primo contatto — il dropbear di fabbrica è vecchio e parla solo algoritmi
SSH legacy, quindi un semplice `ssh root@...` fallisce sui sistemi moderni
con "no matching key exchange method". Crea una volta per tutte un alias
host SSH:

**Linux / macOS** — aggiungi a `~/.ssh/config` (crea il file se manca):

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

**Windows (PowerShell)** — stesso contenuto, percorso Windows:

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

> Se il tuo OpenSSH è più vecchio della 8.5 potrebbe rifiutare la riga
> `PubkeyAcceptedAlgorithms` — cancellala: serve solo per il login con
> chiave.

Ora entra e **cambia subito la password di default**:

```sh
ssh dragino          # password: dragino
passwd               # imposta la tua
```

Tieni aperta questa sessione SSH — i passi 2 e 3 si eseguono sul Dragino.

## Passo 2 — Liberare la porta seriale dal bridge di fabbrica

Sul firmware di fabbrica, `/dev/ttyATH0` (la UART interna verso
l'ATmega328P) è occupata dal bridge stile-Yún di Dragino. Rilasciala
(**sul Dragino**, nella sessione SSH):

```sh
uci set sensor.poweruart.uartmode='noconsole'
uci commit sensor
/usr/bin/set_uart_console 0
/etc/init.d/iotd disable
```

**Non** disabilitare `dragino.init` — pilota GPIO24, che alimenta la UART.

Reversibile in qualsiasi momento (ritorno al comportamento di fabbrica):
`uci set sensor.poweruart.uartmode='bridge'; uci commit sensor;
/etc/init.d/iotd enable; reboot`.

## Passo 3 — Sistemare la console kernel (obbligatorio, o il bridge muore al boot)

La console del kernel condivide la stessa UART dell'ATmega328P. Vanno
cambiate due impostazioni di fabbrica, altrimenti il bridge crasha in loop
con errori di I/O dopo ogni riavvio (scoperto a caro prezzo — dettagli in
[hardware-notes.md](hardware-notes.md)). Sempre **sul Dragino**:

```sh
# 1. impedisci alla askconsole di procd di sequestrare la tty quando arrivano byte radio
cp /etc/inittab /etc/inittab.bak
sed -i 's|^::askconsole:|#::askconsole:|' /etc/inittab

# 2. impedisci ai messaggi runtime del kernel di corrompere il flusso seriale
echo 'kernel.printk = 1 4 1 7' >> /etc/sysctl.conf
sysctl -w kernel.printk='1 4 1 7'

# 3. già che ci siamo, crea la directory di configurazione
mkdir -p /etc/chimera
```

Non perdi nulla: una console seriale su quella UART non è mai stata
utilizzabile (all'altro capo c'è l'ATmega, non un terminale) e l'accesso
SSH non viene toccato. Revert = ripristinare `/etc/inittab.bak` e riavviare.

## Passo 4 — Creare i file di configurazione (sul PC)

Copia i due template e inserisci i **tuoi** dati. Mai rimettere nominativi
o passcode reali nel repo — i nomi dei file reali sono nel `.gitignore`
apposta.

**Linux / macOS:**

```sh
cp config/config.example.yaml config/config.yaml
cp config/aprs-is.example.conf config/aprs-is.conf
nano config/config.yaml config/aprs-is.conf   # o il tuo editor
```

**Windows (PowerShell):**

```powershell
Copy-Item config\config.example.yaml config\config.yaml
Copy-Item config\aprs-is.example.conf config\aprs-is.conf
notepad config\config.yaml
notepad config\aprs-is.conf
```

Cosa modificare:

- `config.yaml` → sezione `digipeater:`: imposta `callsign:` col tuo
  nominativo-SSID (es. `N0CALL-1`; il daemon rifiuta di partire col
  placeholder `N0CALL`). Lascia `mode: tnc` ed entrambi gli
  `enabled: false` per ora — cambierai personalità dopo, con un comando.
- `aprs-is.conf` (serve solo per l'iGate): il tuo nominativo e il tuo
  [passcode APRS-IS](https://apps.magicbug.co.uk/passcode/). `passcode -1`
  ti tiene in solo-ricezione.
- I profili radio hanno come default la convenzione europea LoRa APRS
  (433.775 MHz, SF12, BW125, CR4/5) — verifica il band plan locale.

## Passo 5 — Copiare tutto sul Dragino

Dalla radice del repo, **sul PC**. I comandi sono identici sulle tre
piattaforme (PowerShell compreso — OpenSSH è integrato in Windows 10/11):

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

> Note su `scp -O`: il dropbear di fabbrica non ha il sottosistema SFTP,
> quindi gli scp moderni richiedono `-O` (protocollo legacy). Se il tuo scp
> dice `unknown option -- O`, è un client più vecchio che usa già il
> protocollo legacy — togli il flag. Su Windows usa PowerShell, non cmd.

Poi rendi tutto eseguibile (**sul Dragino**):

```sh
ssh dragino "chmod +x /usr/bin/chimera-*.py /usr/bin/chimera-mode /etc/init.d/chimera-*"
```

## Passo 6 — Flashare l'ATmega328P

Niente USB: l'AR9331 flasha l'ATmega attraverso le linee ISP della board
stessa. L'immagine è già in `/tmp/chimera.hex` dal passo 5. **Sul Dragino:**

```sh
run-avrdude /tmp/chimera.hex
```

Attendi `avrdude done. Thank you.` — circa un minuto; `32768 bytes of
flash verified` significa successo.

> Usa l'immagine **`with_bootloader`** (come fa il passo 5): il
> programmatore di bordo cancella l'intero chip, e questa immagine
> conserva il bootloader seriale, così i futuri upload dall'Arduino IDE
> restano possibili.
>
> Alternativa: alcune versioni del firmware di fabbrica espongono nella
> web UI una pagina di flash MCU (menu **Sensor**) che accetta l'upload di
> un `.hex` ed esegue lo stesso `run-avrdude` sotto il cofano. Se la tua
> ce l'ha, puoi usarla con lo stesso file `with_bootloader` al posto del
> comando qui sopra. Il metodo SSH è quello verificato da questo progetto.

## Passo 7 — Avviare il bridge e verificare

**Sul Dragino:**

```sh
/etc/init.d/chimera-bridge enable
/etc/init.d/chimera-bridge start
```

> Su questo vecchio OpenWrt, `enable` può restituire un exit code diverso
> da zero anche quando ha funzionato — ignoralo (e non concatenarlo mai
> con `&&`).

Verifica l'intera catena:

```sh
chimera-mode status
```

Output atteso:

```
mode:     tnc
bridge: boot:on  running
digipeater: boot:off stopped
igate: boot:off stopped
profile:  433775000Hz SF12 BW125000 CR4/5 10dBm sync 0x12 pre 8
port 8001: LISTEN
```

Se `profile:` mostra valori reali, l'intero percorso funziona: daemon
Linux → seriale → ATmega328P → chip radio configurato. Se qualcosa non
torna, vedi [Risoluzione problemi](#risoluzione-problemi).

Infine, riavvia una volta e ripeti `chimera-mode status` — deve ripartire
tutto da solo (questo convalida il passo 3):

```sh
reboot
# attendi ~90 s, poi:
ssh dragino chimera-mode status
```

## Passo 8 — Scegliere una personalità

Un solo comando, persistente a riavvii e mancanze di corrente:

```sh
chimera-mode tnc         # puro KISS TNC su TCP (default)
chimera-mode aprs        # digipeater + iGate
chimera-mode reticulum   # modem radio Reticulum
chimera-mode status      # cosa sta girando adesso
```

### Modalità TNC

Nient'altro da configurare sul dispositivo. Punta qualsiasi client
KISS-over-TCP (Xastir, YAAC, APRSIS32, PinPoint, direwolf come client, …)
verso:

```
host: <IP del Dragino>    porta: 8001    protocollo: KISS over TCP
```

### Modalità APRS (digipeater + iGate)

`chimera-mode aprs` li abilita entrambi. Sono indipendenti — modifica
`/etc/chimera/config.yaml` sul dispositivo per usarne uno solo:

- solo digipeater (niente internet richiesta): `igate:` → `enabled: false`
- solo iGate (nessuna ritrasmissione RF): `digipeater:` → `enabled: false`

poi `/etc/init.d/chimera-digipeater restart` /
`/etc/init.d/chimera-igate restart` a seconda del caso. L'iGate si collega
di default a `euro.aprs2.net:14580` — scegli la rotate address della tua
regione in `config.yaml` (`noam`/`soam`/`euro`/`asia`/`aunz`.aprs2.net).

Controllo che funzioni: `logread | grep chimera` sul dispositivo e, dopo il
primo pacchetto ricevuto, cerca nei raw packet su aprs.fi la stringa
`qAR,TUONOMINATIVO-SSID`.

### Modalità Reticulum

`chimera-mode reticulum` sul dispositivo, poi configura il lato **host**
(il tuo PC — questa parte non gira mai sul Dragino):

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

Poi aggiungi a `~/.reticulum/config` (identico per `rnsd`, MeshChat e
Sideband — Sideband non ha un editor di interfacce nella UI, modifica il
suo file di config a mano):

```ini
[[Chimera LoRa]]
  type = ChimeraInterface
  enabled = yes
  target_host = 10.130.1.1   # l'IP del tuo Dragino
  target_port = 8001
  spreading_factor = 8       # da tenere allineati a radio_reticulum
  bandwidth_hz = 125000      # nel config.yaml del Dragino
  coding_rate = 5
```

Richiede RNS ≥ 0.7.0. L'interfaccia replica il framing on-air di RNode,
quindi è progettata per interoperare con qualsiasi dispositivo con firmware
RNode ufficiale a parità di parametri radio — **ma non è ancora stata
validata contro hardware RNode reale** (vedi lo stato del progetto nel
README).

## Passo 9 (opzionale) — Cambio modalità dalla web UI

Se vuoi cambiare personalità da browser invece che da SSH:

```sh
scp -O openwrt/luci/controller/chimera.lua dragino:/usr/lib/lua/luci/controller/chimera.lua
ssh dragino "rm -f /tmp/luci-indexcache"
```

La pagina compare in **System → Chimera Mode** nell'interfaccia web del
Dragino (`http://<IP del Dragino>/cgi-bin/luci`, dietro il normale login).

---

## Risoluzione problemi

**`port 8001: NOT listening`** — il bridge non sta girando. Controlla
`logread | grep chimera-bridge`.

**Il bridge crasha in loop con `OSError [Errno 5]` / EIO dopo un
riavvio** — il passo 3 è stato saltato o `/etc/inittab` è stato
ripristinato. Verifica che la riga `::askconsole:` sia commentata,
riavvia. (`set_uart_console 0` NON risolve — la riga askconsole è un
meccanismo diverso.)

**La riga `profile:` è vuota in `chimera-mode status`** — l'ATmega non
risponde: sketch non flashato (passo 6), oppure il bridge di fabbrica
occupa ancora la seriale (passo 2), oppure hai riavviato tra il passo 2 e
il passo 3 e la console si è ripresa la tty.

**`scp: unknown option -- O`** — client OpenSSH più vecchio: togli `-O`,
il protocollo legacy è già il suo default.

**`ssh: no matching key exchange method`** — la voce in `~/.ssh/config`
del passo 1 manca o non viene letta (su Windows, controlla che il file non
abbia l'estensione `.txt`).

**Il digipeater rifiuta di partire** — hai lasciato `callsign: N0CALL-1`
in `config.yaml`. Rifiuta i nominativi placeholder di proposito.

**Ritorno alla fabbrica** — riabilita il bridge originale
(`uci set sensor.poweruart.uartmode='bridge'; uci commit sensor;
/etc/init.d/iotd enable`), ripristina `/etc/inittab.bak`, disabilita i
servizi chimera, riavvia. Per tornare completamente allo stato originale
va riflashato lo sketch gateway LoRaWAN dagli esempi Dragino.

---

## Appendice — Compilare lo sketch da soli

Serve solo se modifichi `atmega328p-modem.ino`. Le immagini precompilate
rendono questo passaggio opzionale per tutti gli altri.

1. Installa **Arduino IDE 2.x**.
2. File → Preferences → *Additional boards manager URLs*, aggiungi
   l'indice board di Dragino (dal
   [wiki Dragino](https://wiki1.dragino.com/index.php/Main_Page)):
   `http://www.dragino.com/downloads/downloads/YunShield/package_dragino_yun_test_index.json`
3. Boards Manager: installa le board **Dragino Yún** e blocca **Arduino
   AVR Boards alla 1.6.9** — il core Dragino richiede la vecchia toolchain
   gcc 4.8.1 e si rompe con i core AVR più recenti. Non lasciare che l'IDE
   lo "aggiorni".
4. Library Manager: installa **RadioHead** (≥ 1.88; compilato e testato
   con la 1.143.1).
5. Se il preprocessore dell'IDE 2.x fallisce su questo core, crea
   `platform.local.txt` accanto al `platform.txt` del core Dragino
   (dentro `Arduino15/packages/Dragino/hardware/avr/...`) con dentro:

   ```
   recipe.preproc.macros="{compiler.path}{compiler.cpp.cmd}" {compiler.cpp.flags} -w -x c++ -E -CC -mmcu={build.mcu} -DF_CPU={build.f_cpu} -DARDUINO={runtime.ide.version} -DARDUINO_{build.board} -DARDUINO_ARCH_{build.arch} {includes} "{source_file}" -o "{preprocessed_file_path}"
   ```

6. Board: **Dragino Yún + UNO or LG01/OLG01** (FQBN
   `Dragino:avr:unoyun`). Sketch → *Export compiled binary*, poi flasha
   l'hex `with_bootloader` esattamente come al passo 6.

Equivalente da CLI:

```sh
arduino-cli compile --fqbn Dragino:avr:unoyun --output-dir build firmware/atmega328p-modem
```
