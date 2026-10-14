#!/usr/bin/env python3
"""
nbus-term - a little TUI for poking an NBUS device over its temperature-sensor
port. Default line settings are 300 baud, 8N1. Pick a command, fill in the
parameters, send it, and see the (parsed) response.

Usage:
    ./run [PORT] [BAUD]           # bootstraps deps automatically via uv
    e.g.  ./run /dev/ttyUSB0 300

    or, if you manage the environment yourself:
    uv run nbus-term [PORT] [BAUD]
    python nbus_term.py [PORT] [BAUD]

Deps are declared in pyproject.toml (pyserial, textual).
Quit:  q  (or Ctrl+C)

NBUS is a single-wire half-duplex bus (one data line + 12 V + GND), so every
command is echoed back on our own RX. The tool consumes that echo and uses it
as a liveness check: "bus fault" (no echo -> wire / GND / power) is reported
separately from "device mute" (echo fine, but no RESP).

If nothing parses back cleanly:
    Monitor  - open the port, then listen with nothing sent. Unsolicited bytes
               = the device talking on its own, almost always a brown-out reset
               loop (check the 12 V feed).
    Scan     - disconnect, then sweep common baud rates, sending one TEST,MODEM,
               at each; the echo column shows whether the wire itself is intact.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import serial
from serial.tools import list_ports

# ----------------------------------------------------------------------------
# Protocol layer  (no UI in here, so it can be unit-tested on its own)
# ----------------------------------------------------------------------------

DEFAULT_BAUD = 300
EOL = b"\r\n"
RESP_PREFIX = b"RESP,"

# Time to let the device finish booting before asserting DTR (see NbusDevice.open).
# Bench-confirmed directly on this adapter: DTR and RTS both idle low through boot,
# then DTR alone goes high once settled and communication can begin. Tune on the
# bench if boot takes longer.
BOOT_SETTLE_S = 2.0

# Extra margin after flush() before dropping RTS (see NbusDevice._write). flush()
# only guarantees the USB-serial bridge's own buffer is empty, not that the last
# byte has finished shifting onto the wire - too short and the tail of every
# frame gets clipped/corrupted. Tune on the bench if that still happens.
RTS_DROP_MARGIN_S = 0.08


def _extract_resp(buf: bytes) -> Optional[bytes]:
    """Pull the first complete `RESP,...` line out of `buf`, or None if there
    isn't one yet (no prefix, or no CR/LF terminator seen)."""
    if RESP_PREFIX not in buf or not (b"\n" in buf or b"\r" in buf):
        return None
    norm = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    for line in norm.split(b"\n"):
        if line.startswith(RESP_PREFIX):
            return line
    return None


def _echo_done(raw: bytes, frame: bytes) -> bool:
    return frame in raw


def _resp_after_echo(raw: bytes, frame: bytes) -> Optional[bytes]:
    """Look for a RESP line only in what came back *after* our own echo, so a
    Raw-line command whose text happens to contain 'RESP,' can't match itself."""
    i = raw.find(frame)
    tail = raw[i + len(frame):] if i >= 0 else raw
    return _extract_resp(tail)


@dataclass
class Reply:
    """Everything one command exchange produced on the single-wire bus."""

    sent: bytes                      # the framed bytes we put on the wire
    raw: bytes                       # everything read back (echo + reply + noise)
    resp: Optional[bytes] = None     # the parsed RESP,... line, if we got one

    @property
    def echo_ok(self) -> bool:
        """We heard our own transmission back intact - proof the NBUS wire, its
        transceiver and GND are good and the device has power."""
        return self.sent in self.raw

    @property
    def echo_seen(self) -> bool:
        """Bytes came back in the echo window, but not our frame - a marginal
        wire / level / grounding problem."""
        return bool(self.raw) and not self.echo_ok

    @property
    def after_echo(self) -> bytes:
        """Whatever followed our echo (the device's turn to talk)."""
        i = self.raw.find(self.sent)
        return self.raw[i + len(self.sent):] if i >= 0 else self.raw

    @property
    def ok(self) -> bool:
        return self.resp is not None and self.resp.startswith(RESP_PREFIX)


class NbusDevice:
    """Serial wrapper for the temperature-sensor port, which is a **single-wire,
    half-duplex bus**: one data line (`NBUS`) plus 12 V power and ground.

    What the shared wire means, all handled here:
      * Everything we transmit is echoed straight back onto our own RX, so every
        read starts with a copy of what we just sent. `send()` splits that off,
        and also uses it as a liveness check - no echo means the NBUS line / its
        transceiver / GND is open or the device is unpowered, which is reported
        separately from "device didn't answer" (echo fine, no RESP).
      * There is no TX/RX pair to cross; a wiring fault shows up as a missing or
        mangled echo, never as a silent one-way link.
      * Reads are echo-then-reply, so waits are budgeted from the baud rate
        (time to shift the frame out and back) plus `timeout` for the device.

    Framing rules from the spec:
      * ASCII, every parameter (incl. the last) is terminated by a comma.
      * commands are separated by an end-of-line; a newline must precede the
        very first command. We satisfy both by prefixing EOL to every write.
      * TEST,* commands answer with `RESP,...,\\r\\n`; a temperature report
        (`hum,temp,`) gets no reply (but still echoes).
    """

    def __init__(self, port: str, baud: int = DEFAULT_BAUD, timeout: float = 3.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout          # how long to wait for the reply, past the echo
        self.ser: Optional[serial.Serial] = None
        self.on_line_change: Optional[Callable[[], None]] = None
        """Fired after DTR/RTS is changed from inside this class (boot-time DTR,
        or the per-write RTS pulse), so a UI can mirror the live state without
        this class knowing anything about UIs. Not fired for direct `self.ser.dtr
        = ...` pokes from outside (e.g. a caller's own bench-debug toggle)."""

    def open(self, probe: bool = True) -> None:
        # Built unopened (port=None) so DTR/RTS can be set *before* the port is
        # actually opened - pyserial defaults both high on open, which on this
        # hardware resets the device. Boot with both low, then raise DTR once
        # booted - confirmed on the bench to be needed for the NBUS adapter
        # board to enable at all. RTS is handled separately, per write, by
        # `_write()`: the adapter only drives the host's TX onto the bus while
        # RTS is high, but leaving RTS high afterward blocks the device's own
        # reply (classic RS485-style half-duplex direction control), so it's
        # raised only for the duration of each transmission.
        self.ser = serial.Serial()
        self.ser.port = self.port
        self.ser.baudrate = self.baud
        self.ser.bytesize = serial.EIGHTBITS
        self.ser.parity = serial.PARITY_NONE
        self.ser.stopbits = serial.STOPBITS_ONE
        self.ser.timeout = 0.2  # per-read timeout; overall waits are bounded by callers
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        time.sleep(BOOT_SETTLE_S)
        self._set_line("dtr", True)
        self.ser.reset_input_buffer()
        if probe:
            # spec: a newline must precede the first command
            self._write(EOL)
            self._read_exact(len(EOL), self._wire_time(len(EOL)) + 0.3)  # swallow its echo

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            finally:
                self.ser = None

    @property
    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    # -- timing helpers ------------------------------------------------------

    def _wire_time(self, nbytes: int) -> float:
        """Seconds to shift `nbytes` bytes at the current baud (~10 bits/byte)."""
        return nbytes * 10.0 / max(self.baud, 1)

    def _set_line(self, name: str, value: bool) -> None:
        """Set DTR/RTS and fire `on_line_change` so a UI can mirror it live."""
        try:
            setattr(self.ser, name, value)
        except OSError:
            pass  # pty / virtual port with no real modem-control lines (e.g. tests)
        if self.on_line_change:
            self.on_line_change()

    def _write(self, data: bytes) -> None:
        """Write a frame with RTS asserted only for the transmission itself.
        `flush()` is `tcdrain()` on POSIX, but on a USB-serial bridge that only
        guarantees the bridge's own buffer is empty - the last byte or two can
        still be shifting out to the wire a few ms after it returns (seen on
        the bench as the tail of the frame getting clipped/corrupted when RTS
        dropped right on flush()'s return). RTS_DROP_MARGIN_S pads for that."""
        self._set_line("rts", True)
        self.ser.write(data)
        self.ser.flush()
        time.sleep(RTS_DROP_MARGIN_S)
        self._set_line("rts", False)

    def _read_exact(self, n: int, timeout: float) -> bytes:
        """Read at most `n` bytes, stopping early on `timeout`. Used to consume
        exactly our own echo without risking a bite out of the reply behind it."""
        end = time.monotonic() + timeout
        got = bytearray()
        while len(got) < n and time.monotonic() < end:
            chunk = self.ser.read(n - len(got))
            if chunk:
                got.extend(chunk)
        return bytes(got)

    # -- the exchange -------------------------------------------------------

    def send(self, body: bytes, expect_response: bool = True) -> Reply:
        """Put one command on the bus and collect what comes back.

        `body` is just the payload, e.g. b'TEST,MEAS,1,'. It's framed with EOL on
        both sides: the leading one satisfies "newline before a command", the
        trailing one terminates the line for firmware that completes on
        end-of-line; the trailing comma survives either way.

        Returns a Reply holding the echo, the RESP line (if any) and the raw
        bytes, so a caller can tell a dead bus from a mute device.
        """
        assert self.is_open, "port not open"
        self.ser.reset_input_buffer()
        frame = EOL + body + EOL
        self._write(frame)

        # The echo has to shift out and loop back before the device can reply.
        budget = self._wire_time(len(frame)) + (self.timeout if expect_response else 1.0)
        deadline = time.monotonic() + budget
        raw = bytearray()
        resp: Optional[bytes] = None
        while time.monotonic() < deadline:
            chunk = self.ser.read(128)
            if chunk:
                raw.extend(chunk)
            resp = _resp_after_echo(bytes(raw), frame)
            if resp is not None:
                break
            if not expect_response and _echo_done(bytes(raw), frame):
                break
        return Reply(sent=frame, raw=bytes(raw), resp=resp)

    def change_baud(self, new_baud: int) -> Reply:
        """TEST,BAUD - the device answers at the *new* rate. Send the request,
        let it (and the tail of its old-rate echo) clear the wire, retune our
        port, then read the reply - scanning for the RESP prefix so any mangled
        echo bytes straddling the switch are ignored. The reply itself is never
        flushed, so a device that answers fast isn't missed. Reply.echo_ok is
        not meaningful for this call."""
        assert self.is_open, "port not open"
        self.ser.reset_input_buffer()
        frame = EOL + f"TEST,BAUD,{new_baud},".encode() + EOL
        self._write(frame)                     # also drops RTS once physically sent
        # Consume exactly our echo (at the old rate) - never more, so a fast
        # reply waiting behind it survives the switch.
        self._read_exact(len(frame), self._wire_time(len(frame)) + 0.3)
        self.ser.baudrate = new_baud
        self.baud = new_baud

        deadline = time.monotonic() + self._wire_time(len(frame)) + self.timeout
        raw = bytearray()
        resp: Optional[bytes] = None
        while time.monotonic() < deadline and resp is None:
            chunk = self.ser.read(128)
            if chunk:
                raw.extend(chunk)
            resp = _extract_resp(bytes(raw))
        return Reply(sent=frame, raw=bytes(raw), resp=resp)

    def listen(self, duration: float,
               on_chunk: Optional[Callable[[bytes], None]] = None) -> bytes:
        """Passively read whatever the bus carries for `duration` seconds WITHOUT
        transmitting. The key diagnostic: bytes with nothing sent are the device
        talking unprompted - almost always it brown-out resetting in a loop
        (check the 12 V feed), occasionally a console sharing the wire. A bus
        that stays quiet until addressed is healthy on that count."""
        assert self.is_open, "port not open"
        deadline = time.monotonic() + duration
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                buf.extend(chunk)
                if on_chunk:
                    on_chunk(bytes(chunk))
        return bytes(buf)


# ----------------------------------------------------------------------------
# Line diagnostics  (baud scan + hex dump, for when nothing comes back clean)
# ----------------------------------------------------------------------------

# Common rates worth trying, plus the ESP32 ROM-bootloader default (115200) so a
# device stuck in reset shows itself.
SCAN_BAUDS = [300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 150, 110]


@dataclass
class ScanRow:
    baud: int
    passive: bytes   # bytes seen with nothing sent
    reply: bytes     # the device's reply after one TEST,MODEM, (our echo stripped)
    ok: bool         # reply carried a valid RESP, line
    echo: str        # "ok" | "mangled" | "none" | "?" - did our own bytes come back


def scan_bauds(port: str, bauds: list[int] = SCAN_BAUDS, listen_s: float = 1.2,
               on_row: Optional[Callable[[ScanRow], None]] = None) -> list[ScanRow]:
    """Open `port` at each candidate rate, listen briefly for unsolicited data,
    then send one TEST,MODEM, and report the echo state plus any reply. On a
    single-wire bus the echo column is the tell: 'ok' everywhere means the wire
    is fine and it's a baud / firmware / power-reset issue; 'none' everywhere
    means the NBUS conductor, GND or the device's power is the problem."""
    rows: list[ScanRow] = []
    for b in bauds:
        dev = NbusDevice(port, b, timeout=1.5)
        try:
            dev.open(probe=False)
            passive = dev.listen(listen_s)
            r = dev.send(b"TEST,MODEM,")
        except Exception as e:  # noqa: BLE001 - report, keep scanning
            row = ScanRow(b, b"", f"<error: {e}>".encode(), False, "?")
        else:
            echo = "ok" if r.echo_ok else ("mangled" if r.echo_seen else "none")
            seen = r.after_echo if r.echo_ok else r.raw
            row = ScanRow(b, passive, seen, r.ok, echo)
        finally:
            dev.close()
        rows.append(row)
        if on_row:
            on_row(row)
    return rows


def detect_ports() -> list[tuple[str, str]]:
    """Currently connected serial ports as (device, description) pairs, e.g.
    ('/dev/ttyUSB0', 'CP2102 USB to UART Bridge Controller'). Backs the port
    picker; the port field itself stays a freely-editable Input for anything
    this doesn't see (remote ports, devices that enumerate oddly, etc.)."""
    return sorted((p.device, p.description) for p in list_ports.comports())


def port_select_options() -> list[tuple[str, str]]:
    """`detect_ports()` rendered as (label, value) pairs for `Select`."""
    return [
        (f"{dev}  ({desc})" if desc != "n/a" else dev, dev)
        for dev, desc in detect_ports()
    ]


def hexdump(data: bytes, width: int = 16) -> str:
    if not data:
        return "(nothing)"
    lines = []
    for off in range(0, len(data), width):
        row = data[off:off + width]
        hexs = " ".join(f"{c:02x}" for c in row).ljust(width * 3 - 1)
        asc = "".join(chr(c) if 32 <= c < 127 else "." for c in row)
        lines.append(f"{off:04x}  {hexs}  |{asc}|")
    return "\n".join(lines)


def preview(data: bytes, n: int = 28) -> str:
    """Short, one-line, printable-safe rendering of a byte blob for a table."""
    if not data:
        return "(silent)"
    shown = "".join(chr(c) if 32 <= c < 127 else "." for c in data[:n])
    return f'"{shown}"' + ("..." if len(data) > n else "")


def printable_fraction(data: bytes) -> float:
    if not data:
        return 0.0
    return sum(32 <= c < 127 or c in (9, 10, 13) for c in data) / len(data)


# ----------------------------------------------------------------------------
# Response parsers
# ----------------------------------------------------------------------------

def _fields(resp: bytes) -> list[str]:
    # 'RESP,232500,700,' -> ['RESP','232500','700','']
    return resp.decode(errors="replace").rstrip().split(",")


def parse_ok(resp: bytes) -> str:
    return "OK" if resp.startswith(b"RESP,OK,") else f"unexpected: {resp!r}"


def parse_meas(resp: bytes) -> str:
    f = _fields(resp)
    try:
        mv, ma = int(f[1]), int(f[2])
        return f"voltage = {mv/1000:.3f} V ({mv} mV)    current = {ma/1000:.3f} A ({ma} mA)"
    except (IndexError, ValueError):
        return f"could not parse: {resp!r}"


def parse_modem(resp: bytes) -> str:
    f = _fields(resp)
    imei = f[1] if len(f) > 1 else ""
    iccid = f[2] if len(f) > 2 else ""
    return (
        f"IMEI  = {imei or '(empty - modem not up?)'}\n"
        f"   ICCID = {iccid or '(empty - no SIM / modem not up?)'}"
    )


def parse_raw(resp: bytes) -> str:
    return resp.decode(errors="replace")


# ----------------------------------------------------------------------------
# Command catalogue
# ----------------------------------------------------------------------------

@dataclass
class Param:
    key: str
    label: str
    default: str = ""


@dataclass
class Command:
    key: str
    title: str
    body: str                      # str.format template using the param keys
    params: list[Param] = field(default_factory=list)
    expects_response: bool = True
    changes_baud: bool = False
    parse: Callable[[bytes], str] = parse_ok
    note: str = ""


COMMANDS: list[Command] = [
    Command(
        key="temp",
        title="Temperature report",
        body="{hum},{temp},",
        params=[
            Param("hum", "humidity field (ignored by device)", "121"),
            Param("temp", "temperature, deci-degC (0244 = 24.4 C)", "0244"),
        ],
        expects_response=False,
        note="Emulates the thermometer feeding the device. No reply.",
    ),
    Command(
        key="calib",
        title="TEST,CALIB - calibrate measurement",
        body="TEST,CALIB,{ch},{mv},{ma},",
        params=[
            Param("ch", "channel (always 1 on current hw)", "1"),
            Param("mv", "actual voltage, mV (231500 = 231.5 V)", "231500"),
            Param("ma", "actual current, mA (760 = 0.76 A)", "760"),
        ],
    ),
    Command(
        key="relay",
        title="TEST,RELAY - relay switching delays",
        body="TEST,RELAY,{ch},{on_us},{off_us},",
        params=[
            Param("ch", "channel (always 1)", "1"),
            Param("on_us", "close delay, us", "555"),
            Param("off_us", "open delay, us", "777"),
        ],
    ),
    Command(
        key="meas",
        title="TEST,MEAS - read measurement",
        body="TEST,MEAS,{ch},",
        params=[Param("ch", "channel (always 1)", "1")],
        parse=parse_meas,
    ),
    Command(
        key="modem",
        title="TEST,MODEM - modem info (IMEI / ICCID)",
        body="TEST,MODEM,",
        params=[],
        parse=parse_modem,
    ),
    Command(
        key="out",
        title="TEST,OUT - switch output",
        body="TEST,OUT,{ch},{state},",
        params=[
            Param("ch", "channel (always 1)", "1"),
            Param("state", "0 = off, 1 = on", "0"),
        ],
    ),
    Command(
        key="baud",
        title="TEST,BAUD - change debug baud rate",
        body="TEST,BAUD,{baud},",
        params=[Param("baud", "new baud rate", "9600")],
        changes_baud=True,
        note="Device replies at the NEW rate; the tool switches with it.",
    ),
    Command(
        key="mqtt",
        title="TEST,MQTT - configure MQTT",
        body="TEST,MQTT,{host},{port},{ssl},{user},{password},",
        params=[
            Param("host", "mqtt host", "example.com"),
            Param("port", "port", "8883"),
            Param("ssl", "ssl enabled (1 / 0)", "1"),
            Param("user", "username (also part of topics)", "user1"),
            Param("password", "password", "hunter2"),
        ],
    ),
    Command(
        key="raw",
        title="Raw line (escape hatch)",
        body="{line}",
        params=[Param("line", "exact bytes after the leading CRLF", "TEST,MODEM,")],
        parse=parse_raw,
        note="Sent verbatim (with the required leading CRLF). Expects a reply.",
    ),
]

CMD_BY_KEY = {c.key: c for c in COMMANDS}


# ----------------------------------------------------------------------------
# TUI
# ----------------------------------------------------------------------------

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button, Footer, Header, Input, Label, ListItem, ListView, RichLog, Select, Static,
)


class PortSelect(Select[str]):
    """A `Select` that re-scans connected serial ports each time it's opened,
    so newly plugged-in devices show up without restarting the app."""

    def action_show_overlay(self) -> None:
        self.set_options(port_select_options())
        super().action_show_overlay()


class NbusApp(App):
    CSS = """
    Screen { layout: vertical; }

    #conn-bar {
        height: auto; padding: 1 1 0 1; align-vertical: middle;
    }
    #conn-bar Input { width: 22; }
    #conn-bar #baud { width: 9; }
    #conn-bar #port-select { width: 20; }
    #conn-bar Button { margin: 0 1; }
    #status { width: 1fr; content-align: right middle; padding: 0 1; }
    #status.ok  { color: $success; }
    #status.bad { color: $error; }

    #line-bar { height: auto; padding: 0 1 1 1; align-vertical: middle; }
    #line-bar Label { padding: 0 1; color: $text-muted; }
    #line-bar Button { margin: 0 1; min-width: 12; }
    #line-bar Button.asserted { color: $success; }

    #body { height: 1fr; }
    #cmd-pane  { width: 44; border-right: solid $panel; }
    #param-pane { width: 1fr; padding: 0 1; }
    #cmd-list { height: 1fr; }
    #param-form { height: 1fr; }
    .field-label { color: $text-muted; margin-top: 1; }
    #note { color: $warning; height: auto; }

    #send { margin-top: 1; width: 100%; }

    #log { height: 40%; border-top: solid $panel; padding: 0 1; }
    Label.section { text-style: bold; padding: 0 1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+l", "clear_log", "Clear log"),
        ("ctrl+s", "save_log", "Save log"),
    ]

    def __init__(self, port: str = "/dev/ttyUSB0", baud: int = DEFAULT_BAUD):
        super().__init__()
        self._start_port = port
        self._start_baud = str(baud)
        self.device: Optional[NbusDevice] = None
        self.current: Optional[Command] = None
        self._transcript: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="conn-bar"):
            yield Input(value=self._start_port, id="port", placeholder="/dev/ttyUSB0")
            yield PortSelect(port_select_options(), prompt="Detected ports", id="port-select")
            yield Input(value=self._start_baud, id="baud", placeholder="baud")
            yield Button("Connect", id="connect", variant="success")
            yield Button("Monitor", id="monitor")
            yield Button("Scan", id="scan")
            yield Static("\u25cf disconnected", id="status", classes="bad")
        with Horizontal(id="line-bar"):
            yield Label("Modem lines (bench debug):")
            yield Button("DTR: -", id="dtr-btn", disabled=True)
            yield Button("RTS: -", id="rts-btn", disabled=True)
        with Horizontal(id="body"):
            with Vertical(id="cmd-pane"):
                yield Label("Commands", classes="section")
                yield ListView(
                    *[ListItem(Label(c.title), id=f"cmd-{c.key}") for c in COMMANDS],
                    id="cmd-list",
                )
            with Vertical(id="param-pane"):
                yield Label("Parameters", classes="section", id="param-title")
                yield Static("", id="note")
                yield VerticalScroll(id="param-form")
                yield Button("Send", id="send", variant="primary", disabled=True)
        yield Label("Transcript  (\u00bb sent  \u00ab received)", classes="section")
        yield RichLog(id="log", markup=True, wrap=True, highlight=False)
        yield Footer()

    # --- helpers ------------------------------------------------------------

    def _log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)
        self._transcript.append(Text.from_markup(msg).plain)

    def _set_status(self, connected: bool, text: str) -> None:
        s = self.query_one("#status", Static)
        s.update(f"\u25cf {text}")
        s.set_classes(["ok"] if connected else ["bad"])
        self.query_one("#send", Button).disabled = not (connected and self.current)

    def _refresh_line_buttons(self) -> None:
        self._sync_line_buttons(self.device)

    def _sync_line_buttons(self, dev: Optional[NbusDevice]) -> None:
        """Sync the DTR/RTS bench-debug buttons with a device's actual state.
        Takes an explicit device (rather than always reading self.device) so
        NbusDevice.on_line_change can drive this live, from inside open(),
        before the connect worker has assigned self.device."""
        connected = dev is not None and dev.is_open
        for name in ("dtr", "rts"):
            btn = self.query_one(f"#{name}-btn", Button)
            btn.disabled = not connected
            if connected:
                state = getattr(dev.ser, name)
                btn.label = f"{name.upper()}: {state}"
                btn.set_class(state, "asserted")
            else:
                btn.label = f"{name.upper()}: -"
                btn.set_class(False, "asserted")

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()
        self._transcript.clear()

    def action_save_log(self) -> None:
        """Dump the full transcript to a text file - a reliable way to get it
        out of the terminal even when mouse-drag selection / OSC52 clipboard
        copy isn't available (e.g. over some SSH/tmux setups)."""
        path = Path.cwd() / f"nbus-transcript-{datetime.now():%Y%m%d-%H%M%S}.txt"
        path.write_text("\n".join(self._transcript) + "\n")
        self._log(f"[dim]transcript saved to {path}[/]")

    # --- command selection --------------------------------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        key = event.item.id.removeprefix("cmd-")
        self.current = CMD_BY_KEY[key]
        self.query_one("#param-title", Label).update(f"Parameters - {self.current.title}")
        note = self.query_one("#note", Static)
        note.update(self.current.note)
        form = self.query_one("#param-form", VerticalScroll)
        await form.remove_children()
        widgets = []
        for p in self.current.params:
            widgets.append(Label(p.label, classes="field-label"))
            widgets.append(Input(value=p.default, id=f"pv-{p.key}"))
        if widgets:
            await form.mount(*widgets)
        connected = self.device is not None and self.device.is_open
        self.query_one("#send", Button).disabled = not connected

    # --- port picker ---------------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "port-select" or event.select.is_blank():
            return
        self.query_one("#port", Input).value = str(event.value)
        event.select.clear()

    # --- buttons ------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "connect":
            self._connect_pressed()
        elif event.button.id == "send":
            self._send_pressed()
        elif event.button.id == "monitor":
            self._do_monitor()
        elif event.button.id == "scan":
            self._do_scan(self.query_one("#port", Input).value.strip())
        elif event.button.id in ("dtr-btn", "rts-btn"):
            self._toggle_line(event.button.id.removesuffix("-btn"))

    def _toggle_line(self, name: str) -> None:
        """Flip DTR or RTS on the live port - bench debug for boards whose
        NBUS adapter gates the bus on these lines (see README)."""
        if self.device is None or not self.device.is_open:
            return
        current = getattr(self.device.ser, name)
        try:
            setattr(self.device.ser, name, not current)
        except OSError as e:
            # pyserial records the new state before the ioctl can fail, so the
            # line value may already have changed even though the physical
            # write didn't - refresh regardless so the button reflects reality.
            self._log(f"[red]{name.upper()} toggle failed:[/] {e}")
        else:
            self._log(f"[dim]{name.upper()} -> {not current}[/]")
        self._refresh_line_buttons()

    def _connect_pressed(self) -> None:
        if self.device and self.device.is_open:
            self.device.close()
            self.device = None
            self._set_status(False, "disconnected")
            self.query_one("#connect", Button).label = "Connect"
            self._refresh_line_buttons()
            self._log("[dim]disconnected[/]")
            return
        port = self.query_one("#port", Input).value.strip()
        try:
            baud = int(self.query_one("#baud", Input).value.strip())
        except ValueError:
            self._log("[red]baud must be a number[/]")
            return
        self._do_connect(port, baud)

    def _send_pressed(self) -> None:
        if not self.current:
            return
        values = {}
        for p in self.current.params:
            values[p.key] = self.query_one(f"#pv-{p.key}", Input).value
        self._do_send(self.current, values)

    # --- serial workers (run off the UI thread) -----------------------------

    @work(thread=True, exclusive=True, group="serial")
    def _do_connect(self, port: str, baud: int) -> None:
        self.call_from_thread(self._log, f"[dim]opening {port} @ {baud} 8N1 ...[/]")
        try:
            dev = NbusDevice(port, baud)
            # Wired before open() so even the boot-time DTR raise and the
            # probe's RTS pulse (both inside open()) are reflected live.
            dev.on_line_change = lambda: self.call_from_thread(self._sync_line_buttons, dev)
            dev.open()
        except Exception as e:  # noqa: BLE001 - surface any open error to the user
            self.call_from_thread(self._log, f"[red]open failed:[/] {e}")
            self.call_from_thread(self._set_status, False, "open failed")
            return
        self.device = dev
        self.call_from_thread(self.query_one("#connect", Button).__setattr__, "label", "Disconnect")
        self.call_from_thread(self._refresh_line_buttons)
        reply = dev.send(b"TEST,MODEM,")
        if reply.ok:
            self.call_from_thread(self._log, f"[green]connected[/] - {parse_modem(reply.resp)}")
            self.call_from_thread(self._set_status, True, f"connected @ {dev.baud}")
        elif reply.echo_ok:
            self.call_from_thread(
                self._log,
                "[yellow]bus OK (heard our own echo) but the device didn't answer "
                "TEST,MODEM,[/]\n"
                f"   after the echo: {reply.after_echo!r}\n"
                "   The wire is fine - the device is mute or mid-reset. Try Monitor, "
                "or Scan for the real baud.")
            self.call_from_thread(self._set_status, True, f"open @ {dev.baud} (no reply)")
        else:
            what = "a mangled echo" if reply.echo_seen else "nothing - not even our own echo"
            self.call_from_thread(
                self._log,
                f"[red]bus fault: sent TEST,MODEM, and got {what} back.[/] "
                f"(raw: {reply.raw!r})\n"
                "   On this single-wire port that means the NBUS line, its transceiver "
                "or GND is open, or the device has no 12 V. Check the 3-wire cable.")
            self.call_from_thread(self._set_status, True, f"open @ {dev.baud} (bus fault)")

    @work(thread=True, exclusive=True, group="serial")
    def _do_send(self, cmd: Command, values: dict) -> None:
        if not self.device or not self.device.is_open:
            self.call_from_thread(self._log, "[red]not connected[/]")
            return
        try:
            body = cmd.body.format(**values).encode()
        except KeyError as e:
            self.call_from_thread(self._log, f"[red]missing parameter {e}[/]")
            return
        # the wire framing is EOL on both sides (see NbusDevice.send)
        shown = (EOL + body + EOL).decode(errors="replace").replace("\r", "\\r").replace("\n", "\\n")
        self.call_from_thread(self._log, f"[cyan]\u00bb {shown}[/]")
        try:
            if cmd.changes_baud:
                reply = self.device.change_baud(int(values["baud"]))
                self.call_from_thread(self._log, f"[dim]  (local port switched to {self.device.baud} baud)[/]")
            else:
                reply = self.device.send(body, expect_response=cmd.expects_response)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._log, f"[red]send error:[/] {e}")
            return
        self.call_from_thread(self._refresh_line_buttons)  # RTS drops after each write

        # Echo check first - single-wire bus, so this isolates a bad wire from a
        # mute device. (change_baud discards the echo, so skip it there.)
        if not cmd.changes_baud:
            if reply.echo_ok:
                self.call_from_thread(self._log, "[dim]  (heard our echo - bus OK)[/]")
            elif reply.echo_seen:
                self.call_from_thread(
                    self._log,
                    f"[red]  echo came back mangled[/] ({reply.raw!r}) - the NBUS wire, "
                    "GND or levels are marginal")
            else:
                self.call_from_thread(
                    self._log,
                    "[red]  no echo - nothing came back at all.[/] The single-wire bus "
                    "is open, or the device is unpowered. Check the 3-wire cable.")

        if not cmd.expects_response:
            self.call_from_thread(self._log, "[dim]\u00ab (no response expected)[/]")
            return
        if not reply.ok:
            tail = reply.after_echo
            extra = f" (got {tail!r})" if tail else ""
            self.call_from_thread(self._log, f"[yellow]\u00ab no RESP within timeout[/]{extra}")
            return
        raw = reply.resp.decode(errors="replace")
        parsed = cmd.parse(reply.resp)
        self.call_from_thread(self._log, f"[green]\u00ab {raw}[/]\n   {parsed}")
        if cmd.changes_baud:
            self.call_from_thread(self._set_status, True, f"connected @ {self.device.baud}")

    @work(thread=True, exclusive=True, group="serial")
    def _do_monitor(self, seconds: float = 6.0) -> None:
        if not self.device or not self.device.is_open:
            self.call_from_thread(
                self._log, "[red]not connected[/] - open the port first, then Monitor")
            return
        self.call_from_thread(
            self._log,
            f"[cyan]-- passive monitor: listening {seconds:.0f}s, sending nothing --[/]")
        try:
            data = self.device.listen(seconds)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._log, f"[red]monitor error:[/] {e}")
            return
        if not data:
            self.call_from_thread(
                self._log,
                "[yellow]-- silent: 0 bytes with nothing sent --[/]\n"
                "   The bus is quiet when idle, which is correct. Next, send a command:\n"
                "   if it also draws no echo, the NBUS wire or GND is open; if it echoes\n"
                "   but the device never answers, the device is mute or mid-reset.")
            return
        self.call_from_thread(
            self._log, f"[green]-- {len(data)} bytes arrived with NOTHING sent --[/]")
        self.call_from_thread(self._log, hexdump(data))
        if printable_fraction(data) > 0.85:
            self.call_from_thread(
                self._log,
                "[yellow]   Readable text with no prompt = the device is booting / logging\n"
                "   unprompted, or a console shares the wire. Read it above.[/]")
        else:
            self.call_from_thread(
                self._log,
                "[yellow]   Unsolicited garble = the device is transmitting but the bit\n"
                "   timing is off - classically it is brown-out resetting over and over.\n"
                "   Check the +12 V feed on the 3-wire cable, then run Scan; readable text\n"
                "   at 115200 starting 'rst:0x..' / 'boot:' confirms the reset loop.[/]")

    @work(thread=True, exclusive=True, group="serial")
    def _do_scan(self, port: str) -> None:
        if self.device and self.device.is_open:
            self.call_from_thread(
                self._log, "[red]disconnect first[/] - Scan needs the port to itself")
            return
        if not port:
            self.call_from_thread(self._log, "[red]enter a port first[/]")
            return
        self.call_from_thread(
            self._log,
            f"[cyan]-- baud scan on {port}: listen ~1s, then one TEST,MODEM, per rate --[/]")

        def report(row: ScanRow) -> None:
            tag = " [green]<== RESP! use this baud[/]" if row.ok else ""
            self.call_from_thread(
                self._log,
                f"  [bold]{row.baud:>6}[/]  echo={row.echo:<7} "
                f"idle={preview(row.passive, 16)}  reply={preview(row.reply, 16)}{tag}")

        try:
            rows = scan_bauds(port, on_row=report)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._log, f"[red]scan error:[/] {e}")
            return

        hits = [r.baud for r in rows if r.ok]
        if hits:
            self.call_from_thread(
                self._log,
                f"[green]-- got a valid RESP at {', '.join(map(str, hits))} baud. "
                f"Put that in the baud box and Connect. --[/]")
            return

        any_echo = any(r.echo == "ok" for r in rows)
        boot = [r.baud for r in rows
                if not r.reply.startswith(b"<error")
                and (r.passive or r.reply)
                and printable_fraction(r.passive or r.reply) > 0.7]
        if any_echo:
            tail = (f" - readable boot text at {boot} baud says so"
                    if boot else "")
            msg = ("   The bus echoes our bytes back fine, so the NBUS wire and GND are\n"
                   "   good. The device just never returns a valid RESP: it's mute (wrong\n"
                   f"   firmware / not in test mode) or stuck rebooting{tail}.\n"
                   "   If rebooting: check the +12 V feed - parasitic power = reset loop.\n")
        else:
            msg = ("   We never hear our own echo at any rate. On a single-wire bus that\n"
                   "   is the NBUS conductor or GND open / miswired, or the device has no\n"
                   "   power. Meter +12 V-to-GND at the connector and check continuity of\n"
                   "   all three wires on the cable you made.\n")
        self.call_from_thread(
            self._log, "[yellow]-- no valid RESP at any rate. --[/]\n" + msg)

    def on_unmount(self) -> None:
        if self.device:
            self.device.close()


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BAUD
    NbusApp(port, baud).run()


if __name__ == "__main__":
    main()
