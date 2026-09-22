# nbus-term

A small terminal UI for poking an NBUS device over its **temperature-sensor port**. The device normally reads temperature readings on this line, but the same port doubles as a debug/command channel that accepts a set of `TEST,*` commands. `nbus_term.py` lets you pick a command, fill in its parameters, send it, and see the (parsed) response — without hand-typing framing bytes into a serial terminal.

It's a single file (`nbus_term.py`) with no config. Point it at a tty, connect, and go.

## Requirements

Nothing to install by hand — a `./run` wrapper bootstraps everything on first use:

- a `uv` binary (fetched into `~/.local/bin` if it isn't already installed)
- a matching Python interpreter (managed by uv; the code needs **3.10+**)
- [`pyserial`](https://pypi.org/project/pyserial/) and [`textual`](https://pypi.org/project/textual/), declared in `pyproject.toml` and pinned in `uv.lock`

If you'd rather manage the environment yourself, any venv on Python 3.10+ with `pip install -e .` (or just `pip install pyserial textual`) works.

## Usage

```bash
./run [PORT] [BAUD]
# examples:
./run                    # defaults to /dev/ttyUSB0 @ 300
./run /dev/ttyUSB0 300
./run /dev/ttyACM0
```

On Windows: `.\run.ps1 COM3 300`. If you manage the environment yourself,
`uv run nbus-term [PORT] [BAUD]` or `python nbus_term.py [PORT] [BAUD]` work too.

`PORT` and `BAUD` are optional and just pre-fill the connection bar; you can also change them in the UI before connecting. The default line settings are **300 baud, 8N1**.

On connect, the tool primes the line with a leading `CRLF` and sends a read-only `TEST,MODEM,` as a liveness check. Because the port is a **single wire** (see below), it hears its own transmission echo back, and uses that to tell three cases apart: *connected* (valid `RESP,`), *device mute* (our echo came back but no reply — wire is fine, firmware isn't answering), and *bus fault* (not even our own echo — the NBUS line, GND, or the device's 12 V is the problem).

On Linux your user usually needs to be in the `dialout` (or `uucp`) group to open `/dev/ttyUSB*`:

```sh
sudo usermod -aG dialout "$USER"   # then log out and back in
```

### Layout

- **Connection bar** (top): port, baud, Connect/Disconnect, **Monitor**, **Scan**, status indicator.
- **Commands** (left): the list of supported commands.
- **Parameters** (right): inputs for the selected command, pre-filled with sensible defaults. Send is here.
- **Transcript** (bottom): `»` shows the raw bytes sent (control chars escaped), `«` shows the response and its decoded meaning.
### Keys

- `q` — quit
- `Ctrl+L` — clear the transcript
## Supported commands

| Command | What it does | Response |
|---|---|---|
| Temperature report | Emulates the thermometer feeding the device (`hum,temp,`; temp is deci-°C, so `0244` = 24.4 °C) | none |
| `TEST,CALIB` | Calibrate measurement (voltage in mV, current in mA) | `RESP,OK,` |
| `TEST,RELAY` | Set relay close/open delays (µs) | `RESP,OK,` |
| `TEST,MEAS` | Read current measurement | `RESP,<mV>,<mA>,` → decoded to V / A |
| `TEST,MODEM` | Read modem IMEI / ICCID (either may be empty) | `RESP,<imei>,<iccid>,` |
| `TEST,OUT` | Switch an output on/off | `RESP,OK,` |
| `TEST,BAUD` | Change the debug-port baud rate | `RESP,OK,` at the **new** rate |
| `TEST,MQTT` | Configure MQTT (host, port, ssl, user, password) | `RESP,OK,` |
| Raw line | Escape hatch: send arbitrary bytes after the leading CRLF | shown verbatim |

Units are not uniform across the protocol, so the tool decodes them for you: `CALIB`/`MEAS` are in mV/mA (e.g. `232500` → 232.5 V, `700` → 0.7 A), while the temperature report is deci-°C.

## Diagnosing a bad line

If the port opens but replies come back garbled, null-padded, or not at all, that's almost always wiring or baud — not the protocol. Two buttons in the connection bar help localise it, and both are read-mostly (Monitor sends nothing; Scan sends only the read-only `TEST,MODEM,`).

The bus is one wire, so **the echo is the first thing to check**: every command you send loops straight back onto your RX. If you hear it back intact, the `NBUS` conductor, its transceiver, GND and the device's power are all good and any remaining fault is in the firmware or the baud rate. If you *don't*, the wire itself (or the 12 V feed) is the problem. `Send` prints the echo verdict on every command; `Scan` has an `echo=` column.

- **Monitor** — connect first, then listen for ~6 s *sending nothing* and hex-dump whatever arrives.
  - *Silent* → the bus is quiet when idle (correct). Send a command next: no echo → open wire/GND; echo but no reply → mute or resetting device.
  - *Unsolicited bytes, readable* → the device is booting/logging on its own, or a console shares the wire.
  - *Unsolicited bytes, garbled* → the device is transmitting but the bit timing is off — classically a brown-out reset loop. Check the +12 V feed, then run Scan.
- **Scan** — disconnect first (Scan needs the port to itself), then sweep `300 … 115200` plus `150` and `110`. At each rate it listens ~1 s, sends one `TEST,MODEM,`, and prints `echo=` (`ok` / `mangled` / `none`), `idle=` and `reply=`.
  - A row with a valid `RESP,` is flagged — that's your baud.
  - `echo=ok` at every rate but no `RESP,` → wire is fine; the device is mute or rebooting (check +12 V). Readable text at **115200** starting `rst:0x..` / `boot:` confirms a reset loop.
  - `echo=none` at every rate → the `NBUS` conductor or GND is open/miswired, or the device is unpowered. Meter +12 V-to-GND and check continuity of all three wires.

## How it's built

Two layers, kept separate so the protocol can be tested without a UI:

- `NbusDevice` — the protocol layer. Owns the serial port, does the framing, and (single-wire bus) consumes the echo of each command before reading the reply. Every exchange returns a `Reply` dataclass — `echo_ok` / `echo_seen` / `resp` / `raw` — so callers can distinguish a dead bus from a mute device. `listen()` (passive read, sends nothing) and `open(probe=False)` (open without priming the line) back the diagnostics. Reply waits are budgeted from the baud rate. No UI code.
- `NbusApp` — the Textual TUI. Serial I/O runs in worker threads so the interface never blocks; results are posted back to the transcript.
- A handful of small `parse_*` functions decode `RESP,...` lines into human-readable text; `scan_bauds()`, `hexdump()` and `preview()` are the UI-free helpers behind Scan and Monitor.
### Design decisions worth knowing

These are choices the source spec left open. If you touch them, see the maintenance note below.

- **Command framing.** The spec's examples show a *leading* `CRLF` and a trailing comma (`\r\nTEST,MODEM,`) but no trailing `CRLF`, which is ambiguous about whether a command completes on the trailing comma or on the next end-of-line. `nbus_term` frames every command with a `CRLF` on **both** sides. This works under either interpretation and preserves the trailing comma. If your firmware strictly completes on the comma and objects to the extra trailing `CRLF`, change this in `NbusDevice.send`.
- **`TEST,BAUD`.** The device replies at the *new* rate. The tool sends the request, waits for it (and its echo) to clear the wire at the old rate, discards that echo, retunes its own port, then reads `RESP,OK,` at the new baud — the session stays live across the change. The echo is dropped here, so the echo verdict isn't shown for this command.
- **Leading newline.** The spec requires a newline before the first command; the tool sends one on connect and also prefixes one to every command, so this is handled. If a first command ever silently does nothing on real hardware, a missing leading newline is the usual suspect — but this tool already covers it.
- **NBUS is a single wire.** The temperature port is a half-duplex bus (one data line, plus 12 V and ground). Everything sent echoes back on the same wire, so `NbusDevice.send` reads that echo first, treats only a post-echo `RESP,`-prefixed line as a reply, and surfaces a missing echo as a distinct "bus fault". There is no TX/RX pair — a wiring fault appears as a missing or mangled echo, not a one-way link. Reply timeouts are derived from the baud rate (frame shift-out + shift-back time) rather than fixed, so 300 baud doesn't spuriously time out.
### Testing

The protocol layer and the full connect→send path are exercised against a simulated device over a PTY loopback (a fake device answers on the master side while `NbusDevice` talks to the slave). Note that a PTY does **not** emulate real baud timing, so 300-baud behaviour on actual hardware — especially the drain-then-switch in `TEST,BAUD` — is still worth a sanity check on the bench.

---

## Maintenance note for LLM agents (and humans)

**This README is part of the code. If your change makes anything here inaccurate, update the README in the same change.** A stale README is worse than none — it actively misleads the next reader.

Treat a README edit as required (not optional) when your change touches any of:

- **The command set** — adding, removing, or renaming a command, or changing its parameters, defaults, or expected response. Update the *Supported commands* table.
- **Protocol framing or the wire format** — e.g. the both-sided `CRLF` decision, the trailing-comma handling, the leading-newline behaviour, or the `TEST,BAUD` switch logic. Update *Design decisions worth knowing*.
- **Defaults or invocation** — the default port/baud, line settings (300 8N1), CLI arguments, or how the tool is launched. Update *Usage*.
- **Dependencies or the Python version required.** Update *Requirements*.
- **The UI** — keybindings, pane layout, the transcript format, or the diagnostic buttons. Update *Layout* / *Keys* / *Diagnosing a bad line*.
- **Code structure** — renaming `NbusDevice` / `NbusApp` / the `parse_*` functions, or splitting the single file into several. Update *How it's built*.
- **Testing approach** — how the tool is tested or what the tests cover. Update *Testing*.
Guidelines when you do update it:

- Change only what your edit actually made untrue; don't rewrite sections that are still correct.
- Keep the description honest about what the tool *does*, not what it aspires to. If you add something half-finished, say so rather than documenting it as done.
- If you make one of the "Design decisions worth knowing" obsolete (say, the firmware settles the framing question and you drop the extra `CRLF`), remove or rewrite that bullet rather than leaving a decision documented that the code no longer makes.
- If you're unsure whether a change warrants a README edit, it probably does — err toward updating.
