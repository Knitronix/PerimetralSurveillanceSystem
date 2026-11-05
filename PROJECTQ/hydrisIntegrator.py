"""
hydrisIntegrator.py — PROJECTQ
------------------------------
Our installation (fiber sensor + daisy-chained mats) as seen by Hydris: it
listens to the fiber, wakes the mats when it hears an event, and sends sensors
and alarms to Hydris. Lighter version of WAKEUPeSTALTA/3main.py: no plots and no
calibration, which stay in 3main. Single self-contained file: it imports nothing
from the rest of the project.

Three threads, so that detection never waits for anyone:
  detection   receives the fiber UDP stream and runs the Core
  archive     writes WAV files and the event log to disk
  hydris      sends entities to Hydris (retries if Hydris is not there)
The window only reads the Core's latest snapshot.

State machine (same as 3main):
  LISTENING --strong event on the fiber--> WAKING    fiber alarm to Hydris
  WAKING    --PARLA sent to the ESP------> ARMED
  ARMED     --mat closed-----------------> ARMED     mat alarm to Hydris
  ARMED     --timeout--------------------> LISTENING

Intended differences from 3main:
  - zero marker accepted from 150 to 300 ms (3main: 170-230). Measured on the
    03/11/2026 WAV files: the 200 ms marker reads as 200/220/240 ms depending on
    the volume, and at 240 ms it was rejected: the counter climbed to 3, 4...10.
  - a slot beyond the configured number of mats never raises an alarm.
  - starts active straight away, with the thresholds from the configuration.
  - if the ESP does not answer the fiber keeps listening, so a second event is
    not lost while retrying to wake the mats.

Coordinates and thresholds are in configuration.json, next to this file.
Cannot run together with 3main: both need UDP port 12345.

The master firmware speaks Italian: PARLA (talk), ZITTO (quiet), STATO (status)
and the lines parsed in _read_master_line are its protocol and must not be
translated here unless master.ino changes too.

Dependencies: numpy, PySide6, grpcio, protobuf and the Hydris client (platform_proto).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
import wave
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("hydris_integrator")

FOLDER = Path(__file__).resolve().parent
CONFIGURATION_FILE = FOLDER / "configuration.json"
ARCHIVE_FOLDER = FOLDER / "archive"

# ============================================================
# Fiber: packet format (verified with Wireshark on 02/11/2026)
# ============================================================
UDP_PORT = 12345
SAMPLE_RATE = 12000
SAMPLES_PER_PACKET = 127
BYTES_PER_PACKET = 508
SAMPLE_DTYPE = np.dtype("<i4")

# ============================================================
# f1/f2 protocol: MUST stay equal to those in
# WAKEUPeSTALTA/arduino/master/master.ino
# ============================================================
F1_HZ = 4400.0
F2_HZ = 3200.0
MARKER_MIN_S = 0.150
MARKER_MAX_S = 0.300
T1_S = 0.050
SLOT_TOLERANCE_S = 0.030
GOERTZEL_BLOCK_SAMPLES = round(SAMPLE_RATE * 0.020)

STALTA_SETTLING_S = 1.0
DAISY_RETRY_DELAY_S = 2.0
PARLA_INTERVAL_S = 2.0  # without PARLA for 10 s the master goes quiet by itself
DAISY_CHAIN_BOOT_S = 0.2
NO_FIBER_DATA_S = 3.0  # the interrogator sends in bursts, with ~170 ms pauses

# Device states, as Hydris names them
ACTIVE = "DeviceStateActive"
DEGRADED = "DeviceStateDegraded"
FAILED = "DeviceStateFailed"

SENSOR_SYMBOL = "SFGPESE---"
PRESSED_SENSOR_SYMBOL = "SHGPESE---"  # same sensor, red while the mat is pressed
FIBER_ALARM_SYMBOL = "SUGP-----------"  # yellow: ground object, unknown
MAT_ALARM_SYMBOL = "SHGP-----------"  # red: confirmed event at a precise point

DEFAULT_CONFIGURATION = {
    "esp_host": "10.19.161.225",
    "hydris": {
        "address": "localhost:50051",
        "alarm_duration_s": 60,
        "alarm_renewal_s": 20,
        "sensor_lifetime_s": 60,
        "sensor_heartbeat_s": 10,
    },
    "fiber": {
        "id": "knx.fiber1",
        "name": "Fiber 1",
        "lat": 45.0,
        "lon": 9.0,
        "alarm_radius_m": 500,
        "path": [[45.0, 9.0], [45.0, 9.001], [45.0006, 9.001]],
    },
    "mats": [
        {"number": 1, "id": "knx.mat1", "name": "Mat 1", "lat": 45.0001, "lon": 9.0004, "alarm_radius_m": 2},
        {"number": 2, "id": "knx.mat2", "name": "Mat 2", "lat": 45.0001, "lon": 9.0008, "alarm_radius_m": 2},
    ],
    "stalta": {"sta_s": 0.5, "lta_s": 3.0, "threshold_on": 1.5, "threshold_off": 1.2},
    "f1f2_thresholds": {"on_f1": 0.000248, "off_f1": 0.000160, "on_f2": 0.000232, "off_f2": 0.000152},
    "timing": {"inactivity_s": 120, "maximum_s": 300, "mats_unheard_s": 5},
}


def load_configuration(path: Path = CONFIGURATION_FILE) -> dict:
    """The defaults, overridden by what is in the file."""
    def merge(base: dict, other: dict) -> dict:
        for key, value in other.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = value
        return base

    cfg = copy.deepcopy(DEFAULT_CONFIGURATION)
    if path.exists():
        merge(cfg, json.loads(path.read_text(encoding="utf-8")))
    return cfg


# ============================================================
# f1/f2 and STA/LTA detection: 3main logic, only the marker changes
# ============================================================
class GoertzelDetector:
    """Power at a single known frequency (1 = tone of amplitude 32768)."""

    def __init__(self, frequency_hz, sample_rate, block_size):
        self.block_size = block_size
        k = round(block_size * frequency_hz / sample_rate)
        self.coeff = 2.0 * np.cos(2.0 * np.pi * k / block_size)

    def power(self, block):
        coeff = self.coeff
        q1 = 0.0
        q2 = 0.0
        for x in block.tolist():
            q0 = coeff * q1 - q2 + x
            q2 = q1
            q1 = q0
        squared_magnitude = q1 * q1 + q2 * q2 - coeff * q1 * q2
        return squared_magnitude / ((self.block_size / 2.0) ** 2) / (32768.0 ** 2)


class FrequencyStateMachine:
    """ON/OFF with hysteresis; when it goes back OFF it returns how long ON lasted."""

    def __init__(self, threshold_on, threshold_off, block_duration_s):
        self.threshold_on = threshold_on
        self.threshold_off = threshold_off
        self.block_duration_s = block_duration_s
        self.active = False
        self.on_duration_s = 0.0

    def update(self, power):
        """Returns (went_on, completed_duration_or_None)."""
        went_on = False
        completed_duration = None
        if not self.active:
            if power > self.threshold_on:
                self.active = True
                self.on_duration_s = self.block_duration_s
                went_on = True
        elif power > self.threshold_off:
            self.on_duration_s += self.block_duration_s
        else:
            self.active = False
            completed_duration = self.on_duration_s
            self.on_duration_s = 0.0
        return went_on, completed_duration


class F1F2ProtocolDetector:
    """f1 paces the cycle (zero marker, then one slot per mat);
    f2 on during a slot means the mat is closed."""

    def __init__(self, thresholds: dict):
        block_duration_s = GOERTZEL_BLOCK_SAMPLES / SAMPLE_RATE
        self.goertzel_f1 = GoertzelDetector(F1_HZ, SAMPLE_RATE, GOERTZEL_BLOCK_SAMPLES)
        self.goertzel_f2 = GoertzelDetector(F2_HZ, SAMPLE_RATE, GOERTZEL_BLOCK_SAMPLES)
        self.machine_f1 = FrequencyStateMachine(thresholds["on_f1"], thresholds["off_f1"], block_duration_s)
        self.machine_f2 = FrequencyStateMachine(thresholds["on_f2"], thresholds["off_f2"], block_duration_s)
        self.counter = 0
        self.f2_active_during_slot = False
        self._zero_marker_seen = False

    @staticmethod
    def classify_f1_duration(duration_s):
        if MARKER_MIN_S <= duration_s <= MARKER_MAX_S:
            return "zero"
        if abs(duration_s - T1_S) <= SLOT_TOLERANCE_S:
            return "slot"
        return "noise"

    def process_block(self, block):
        """One block of GOERTZEL_BLOCK_SAMPLES: returns the generated events."""
        on_f1, f1_duration = self.machine_f1.update(self.goertzel_f1.power(block))
        if on_f1:
            self.f2_active_during_slot = False
        self.machine_f2.update(self.goertzel_f2.power(block))
        if self.machine_f1.active and self.machine_f2.active:
            self.f2_active_during_slot = True

        if f1_duration is None:
            return []
        kind = self.classify_f1_duration(f1_duration)
        if kind == "zero":
            previous = self.counter if self._zero_marker_seen else None
            self._zero_marker_seen = True
            self.counter = 0
            return [{"type": "zero", "duration_s": f1_duration, "previous_cycle_count": previous}]
        if kind == "slot" and not self._zero_marker_seen:
            # Mid-cycle we do not know yet which mat this slot belongs to.
            return [{"type": "slot_before_marker", "duration_s": f1_duration}]
        if kind == "slot":
            self.counter += 1
            state = "closed" if self.f2_active_during_slot else "open"
            return [{"type": "slot", "counter": self.counter, "state": state, "duration_s": f1_duration}]
        return [{"type": "noise", "duration_s": f1_duration}]


class StaLtaDetector:
    """STA/LTA with exponential moving averages, updated at every packet."""

    def __init__(self, sta_s, lta_s, threshold_on, threshold_off):
        packet_duration = SAMPLES_PER_PACKET / SAMPLE_RATE
        self.alpha_sta = min(1.0, packet_duration / sta_s)
        self.alpha_lta = min(1.0, packet_duration / lta_s)
        self.threshold_on = threshold_on
        self.threshold_off = threshold_off
        self._floor = 1e-6
        self._packet_duration = packet_duration
        self.sta = 0.0
        self.lta = self._floor
        self._initialized = False
        self.in_event = False
        self.last_ratio = 0.0
        self.seconds_of_data = 0.0

    def update(self, samples: np.ndarray) -> bool:
        """True only at the instant the ratio goes above threshold_on."""
        power = float(np.mean(samples.astype(np.float64) ** 2))
        self.seconds_of_data += self._packet_duration
        if not self._initialized:
            self.sta = self.lta = max(power, self._floor)
            self._initialized = True
        self.sta += self.alpha_sta * (power - self.sta)
        self.lta = max(self.lta + self.alpha_lta * (power - self.lta), self._floor)
        ratio = self.sta / self.lta
        self.last_ratio = ratio
        if not self.in_event:
            if ratio > self.threshold_on:
                self.in_event = True
                return True
        elif ratio < self.threshold_off:
            self.in_event = False
        return False


# ============================================================
# Archive: continuous audio + event log, on its own thread.
# A disk problem turns it off and reports it; detection carries on.
# ============================================================
class SessionArchive:
    FILE_DURATION_S = 600
    MAX_QUEUE = 2000  # ~20 s of packets: beyond that, the disk cannot keep up

    def __init__(self, base_folder: Path, configuration: dict):
        self.active = True
        self.error: str | None = None
        self.total_samples = 0
        self.folder = Path(base_folder) / time.strftime("%Y-%m-%d") / time.strftime("integrator_%H%M%S")
        self._queue: queue.Queue = queue.Queue(self.MAX_QUEUE)
        self._wav: wave.Wave_write | None = None
        self._wav_name: str | None = None
        self._file_samples = 0
        self._events = None
        try:
            (self.folder / "audio").mkdir(parents=True, exist_ok=False)
            (self.folder / "session.json").write_text(json.dumps({
                "start": datetime.now().isoformat(timespec="seconds"),
                "sample_rate": SAMPLE_RATE,
                "configuration": configuration,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            self._events = open(self.folder / "events.jsonl", "a", encoding="utf-8")
        except OSError as e:
            self._shut_down(e)
        self._thread = threading.Thread(target=self._work, name="archive", daemon=True)
        self._thread.start()

    def write_audio(self, samples: np.ndarray) -> None:
        self._put(("audio", samples))

    def event(self, kind: str, **data) -> None:
        line = {"time": datetime.now().isoformat(timespec="milliseconds"), "type": kind, **data}
        self._put(("event", line))

    def close(self) -> None:
        self.event("closing")
        self._put(None)
        self._thread.join(timeout=5)

    def _put(self, item) -> None:
        if not self.active:
            return
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._shut_down(OSError("disk too slow"))

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            if not self.active:
                continue
            try:
                if item[0] == "audio":
                    self._write_samples(item[1])
                else:
                    self._write_event(item[1])
            except (OSError, wave.Error) as e:
                self._shut_down(e)
        for closable in (self._wav, self._events):
            try:
                if closable is not None:
                    closable.close()
            except (OSError, wave.Error):
                pass
        self._wav = self._events = None

    def _write_samples(self, samples: np.ndarray) -> None:
        per_file = self.FILE_DURATION_S * SAMPLE_RATE
        start = 0
        while start < len(samples):
            if self._wav is None or self._file_samples >= per_file:
                self._open_wav()
            n = min(len(samples) - start, per_file - self._file_samples)
            self._wav.writeframes(samples[start:start + n].astype("<i4").tobytes())
            self._file_samples += n
            self.total_samples += n
            start += n

    def _write_event(self, line: dict) -> None:
        if self._events is None:
            return
        line = {**line, "audio_file": self._wav_name,
                "second_in_file": round(self._file_samples / SAMPLE_RATE, 3),
                "total_sample": self.total_samples}
        self._events.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        self._events.flush()

    def _open_wav(self) -> None:
        if self._wav is not None:
            self._wav.close()
        self._wav_name = f"audio/{time.strftime('%H%M%S')}.wav"
        self._wav = wave.open(str(self.folder / self._wav_name), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(4)
        self._wav.setframerate(SAMPLE_RATE)
        self._file_samples = 0

    def _shut_down(self, error: Exception) -> None:
        self.active = False
        self.error = f"Recording turned off: {error}"
        log.error(self.error)


# ============================================================
# Daisy chain ESP (esp-link, port 23 = master serial)
# ============================================================
class DaisyCommands:
    PORT = 23

    def __init__(self, host: str):
        self.host = host.strip()
        self.connected = False
        self.last_error: str | None = None
        self._commands: queue.Queue[str] = queue.Queue()
        self._lines: queue.Queue[str] = queue.Queue()
        self._active = True
        self._thread = threading.Thread(target=self._work, name="daisy_commands", daemon=True)
        self._thread.start()

    def talk(self) -> None:
        self._commands.put("PARLA")

    def quiet(self) -> None:
        self._commands.put("ZITTO")

    def command(self, text: str) -> None:
        self._commands.put(text)

    def received_lines(self) -> list[str]:
        lines = []
        while True:
            try:
                lines.append(self._lines.get_nowait())
            except queue.Empty:
                return lines

    def close(self) -> None:
        self.quiet()
        end = time.monotonic() + 1.5
        while not self._commands.empty() and time.monotonic() < end and self.connected:
            time.sleep(0.05)
        self._active = False
        self._thread.join(timeout=2.5)

    def _work(self) -> None:
        sock: socket.socket | None = None
        leftover = b""
        while self._active:
            if sock is None:
                try:
                    sock = socket.create_connection((self.host, self.PORT), timeout=2.0)
                    sock.settimeout(0.1)
                    self.connected, self.last_error = True, None
                except OSError as e:
                    self.connected, self.last_error = False, str(e)
                    time.sleep(1.0)
                    continue
            try:
                while True:
                    command = self._commands.get_nowait()
                    try:
                        sock.sendall((command + "\r\n").encode())
                    except OSError:
                        self._commands.put(command)  # retry after reconnecting
                        raise
            except queue.Empty:
                pass
            except OSError as e:
                sock.close()
                sock, self.connected, self.last_error = None, False, str(e)
                continue
            try:
                data = sock.recv(4096)
                if not data:
                    raise OSError("connection closed by the ESP")
                leftover += data
                *lines, leftover = leftover.split(b"\n")
                for line in lines:
                    text = line.decode("latin-1", "replace").strip()
                    if text:
                        self._lines.put(text)
            except socket.timeout:
                pass
            except OSError as e:
                sock.close()
                sock, self.connected, self.last_error = None, False, str(e)
        if sock is not None:
            sock.close()
        self.connected = False


# ============================================================
# Core: decides what happens. No Qt, no network: it receives samples and time,
# so it can be tested on a recording. Runs in a single thread.
# ============================================================
class Core:
    LISTENING = "LISTENING"
    WAKING = "WAKING"
    ARMED = "ARMED"

    def __init__(self, cfg: dict, daisy, archive, on_event: Callable[[dict], None]):
        self.cfg = cfg
        self.daisy = daisy
        self.archive = archive
        self.on_event = on_event
        self.mat_numbers = {m["number"] for m in cfg["mats"]}
        self.state = self.LISTENING
        self.stalta = self._new_stalta()
        self.protocol: F1F2ProtocolDetector | None = None
        self._buffer = np.empty(0, dtype=np.int32)
        self._parla_confirmed = False
        self._waking_start_s = 0.0
        self._next_attempt_s = 0.0
        self._last_parla_s = 0.0
        self._last_closure_s = 0.0
        self._armed_start_s = 0.0
        self._last_f1f2_s = 0.0
        self._last_data_s: float | None = None
        self._rms = 0.0
        self._pressed: dict[int, float] = {}  # number -> last send to Hydris
        self._not_responding: set[int] = set()
        self._cut_cables: set[int] = set()
        self._master_status_requested = False
        self._reset_requested = False
        self._mats_unheard = False
        self.volume: tuple[int, int] | None = None
        self.internal_error: str | None = None
        self.snapshot: dict = {}
        self.update_snapshot(0.0)

    def _new_stalta(self) -> StaLtaDetector:
        s = self.cfg["stalta"]
        return StaLtaDetector(s["sta_s"], s["lta_s"], s["threshold_on"], s["threshold_off"])

    # --- commands from the window (safe from another thread) ---------------
    def request_reset(self) -> None:
        self._reset_requested = True

    def set_volume(self, percent: int) -> None:
        self.daisy.command(f"VOL {percent} {percent}")
        self.archive.event("volume_set", percent=percent)

    # --- data from the fiber ------------------------------------------------
    def process(self, samples: np.ndarray, now: float) -> None:
        self.archive.write_audio(samples)
        self._last_data_s = now
        self._rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        if self.state == self.ARMED:
            self._process_mats(samples, now)
        elif self.state == self.LISTENING or not self._parla_confirmed:
            # In WAKING with no PARLA sent the tones are not playing: the fiber
            # can still hear, so it keeps listening.
            self._process_listening(samples, now)

    def _process_listening(self, samples: np.ndarray, now: float) -> None:
        if not self.stalta.update(samples):
            return
        ratio = self.stalta.last_ratio
        settling = self.stalta.seconds_of_data < STALTA_SETTLING_S
        self.archive.event("stalta_trigger", ratio=ratio, threshold_on=self.stalta.threshold_on,
                           outcome="ignored_settling" if settling else "alarm")
        if settling:
            return
        self.on_event({"type": "fiber_alarm", "ratio": ratio})
        if self.state == self.LISTENING:
            self._go_to_waking(now)

    def _go_to_waking(self, now: float) -> None:
        self._buffer = np.empty(0, dtype=np.int32)
        self.protocol = F1F2ProtocolDetector(self.cfg["f1f2_thresholds"])
        self._parla_confirmed = False
        self.state = self.WAKING
        self.archive.event("state", state=self.state)
        self._try_parla(now)

    def _try_parla(self, now: float) -> None:
        if self.daisy.connected:
            self.daisy.talk()
            self._parla_confirmed = True
            self._waking_start_s = self._last_parla_s = now
            self.archive.event("parla_sent")
        else:
            self._next_attempt_s = now + DAISY_RETRY_DELAY_S
            self.archive.event("esp_unreachable", error=self.daisy.last_error)

    def _process_mats(self, samples: np.ndarray, now: float) -> None:
        self._buffer = np.concatenate((self._buffer, samples))
        while len(self._buffer) >= GOERTZEL_BLOCK_SAMPLES:
            block = self._buffer[:GOERTZEL_BLOCK_SAMPLES]
            self._buffer = self._buffer[GOERTZEL_BLOCK_SAMPLES:]
            for event in self.protocol.process_block(block):
                self.archive.event("f1f2_" + event["type"], **{k: v for k, v in event.items() if k != "type"})
                if event["type"] in ("zero", "slot"):
                    self._last_f1f2_s = now
                if event["type"] == "slot":
                    self._handle_slot(event["counter"], event["state"] == "closed", now)

    def _handle_slot(self, number: int, closed: bool, now: float) -> None:
        if number not in self.mat_numbers:
            return  # counter beyond the real mats: noise, never an alarm
        renewal_s = self.cfg["hydris"]["alarm_renewal_s"]
        if closed:
            self._last_closure_s = now
            was_pressed = number in self._pressed
            if not was_pressed or now - self._pressed[number] >= renewal_s:
                self._pressed[number] = now
                self.on_event({"type": "mat_pressed", "number": number, "renewal": was_pressed})
        elif number in self._pressed:
            del self._pressed[number]
            self.on_event({"type": "mat_released", "number": number})

    # --- every ~50 ms -------------------------------------------------------
    def tick(self, now: float) -> None:
        self._handle_daisy(now)
        if self._reset_requested:
            self._reset_requested = False
            self._back_to_listening("reset from the window")
        if self.state == self.WAKING:
            if not self._parla_confirmed:
                if now >= self._next_attempt_s:
                    self._try_parla(now)
            elif now - self._waking_start_s >= DAISY_CHAIN_BOOT_S:
                self._last_closure_s = self._armed_start_s = self._last_f1f2_s = now
                self.state = self.ARMED
                self.archive.event("state", state=self.state)
        elif self.state == self.ARMED:
            timing = self.cfg["timing"]
            self._mats_unheard = now - self._last_f1f2_s >= timing["mats_unheard_s"]
            if now - self._last_closure_s >= timing["inactivity_s"]:
                self._back_to_listening("no closure (inactivity timeout)")
            elif now - self._armed_start_s >= timing["maximum_s"]:
                self._back_to_listening("maximum timeout")
        self.update_snapshot(now)

    def _handle_daisy(self, now: float) -> None:
        if not self.daisy.connected:
            self._master_status_requested = False
        elif not self._master_status_requested:
            self.daisy.command("STATO")
            self._master_status_requested = True
        for line in self.daisy.received_lines():
            self.archive.event("master", text=line)
            self._read_master_line(line)
        if self._parla_confirmed and self.state != self.LISTENING and now - self._last_parla_s >= PARLA_INTERVAL_S:
            self.daisy.talk()
            self._last_parla_s = now

    def _read_master_line(self, line: str) -> None:
        # These strings come from the master firmware: keep them as they are.
        m = re.search(r"VOL=(\d+) (\d+)", line) or re.search(r"VOL f1=(\d+)% f2=(\d+)%", line)
        if m:
            self.volume = (int(m.group(1)), int(m.group(2)))
        if line.startswith("Pronto"):  # the master restarted: it will print everything again
            self._not_responding.clear()
            self._cut_cables.clear()
        m = re.match(r"(?:Nodo|TAPPETO) (\d+):", line)
        if not m:
            return
        number = int(m.group(1))
        if "NON RISPONDE" in line:
            self._not_responding.add(number)
        elif "risponde" in line:
            self._not_responding.discard(number)
        elif "GUASTO" in line:
            self._cut_cables.add(number)
        elif "di nuovo a posto" in line:
            self._cut_cables.discard(number)

    def _back_to_listening(self, reason: str) -> None:
        if self.state == self.LISTENING:
            return
        self.archive.event("state", state=self.LISTENING, reason=reason)
        self.daisy.quiet()
        self._parla_confirmed = False
        self._next_attempt_s = 0.0
        self._mats_unheard = False
        self.protocol = None
        for number in list(self._pressed):
            self.on_event({"type": "mat_released", "number": number})
        self._pressed.clear()
        self.stalta = self._new_stalta()
        self.state = self.LISTENING

    # --- state for the window and for Hydris --------------------------------
    def update_snapshot(self, now: float) -> None:
        """The snapshot is replaced whole: a reader on another thread never
        sees half-written data."""
        no_data = self._last_data_s is None or now - self._last_data_s >= NO_FIBER_DATA_S
        fiber = (FAILED, "No data from the interrogator") if no_data else (ACTIVE, "")
        mats = {}
        for number in sorted(self.mat_numbers):
            if no_data:
                state = (FAILED, "No data from the interrogator")
            elif number in self._not_responding:
                state = (FAILED, "The node does not respond")
            elif number in self._cut_cables:
                state = (DEGRADED, "Mat cable cut?")
            elif not self.daisy.connected:
                state = (DEGRADED, f"ESP {self.daisy.host} unreachable")
            elif self._mats_unheard:
                state = (DEGRADED, "Daisy chain not heard on the fiber")
            else:
                state = (ACTIVE, "")
            mats[number] = {"state": state[0], "error": state[1], "pressed": number in self._pressed}
        # While the mats are playing the fiber is not listening: no stale value.
        listening = self.state == self.LISTENING or not self._parla_confirmed
        self.snapshot = {
            "state": self.state,
            "ratio": self.stalta.last_ratio if listening else None,
            "rms": self._rms,
            "fiber": {"state": fiber[0], "error": fiber[1]},
            "mats": mats,
            "esp_connected": self.daisy.connected,
            "volume": self.volume,
        }


# ============================================================
# How fiber, mats and alarms appear in Hydris
# ============================================================
def _iso(instant: datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _lifetime(seconds: float) -> dict:
    now = _utc_now()
    return {"from": _iso(now), "fresh": _iso(now), "until": _iso(now + timedelta(seconds=seconds))}


def _sensor(identifier, name, lat, lon, state, error, lifetime_s, parent=None, symbol=SENSOR_SYMBOL) -> dict:
    device = {"class": "ugs", "category": "Ground", "state": state}
    if parent:
        device["parent"] = parent
    if error:
        device["error"] = error
    return {
        "id": identifier, "label": name,
        "geo": {"latitude": lat, "longitude": lon},
        "symbol": {"milStd2525C": symbol},
        "device": device,
        "classification": {"taxonomy": [{"confidence": {"confidence": 1.0},
                                         "equipment": {"sensor": {"emplaced": {}}}}]},
        "lifetime": _lifetime(lifetime_s),
        "routing": {"channels": [{}]},
    }


def sensor_entities(cfg: dict, snapshot: dict) -> list[dict]:
    """Fiber, path and mats. They expire and get renewed: if this program
    hangs, no fake "active" sensors stay in Hydris."""
    lifetime_s = cfg["hydris"]["sensor_lifetime_s"]
    fiber = cfg["fiber"]
    fiber_sensor = _sensor(fiber["id"], fiber["name"], fiber["lat"], fiber["lon"],
                           snapshot["fiber"]["state"], snapshot["fiber"]["error"], lifetime_s)
    if snapshot["ratio"] is not None:
        fiber_sensor["metric"] = {"metrics": [{"id": 1, "label": "STA/LTA ratio", "double": snapshot["ratio"]}]}
    entities = [fiber_sensor, {
        "id": f"{fiber['id']}.path", "label": f"{fiber['name']} path",
        "shape": {"geometry": {"planar": {"line": {"points": [
            {"latitude": lat, "longitude": lon} for lat, lon in fiber["path"]]}}}},
        "lifetime": _lifetime(lifetime_s),
        "routing": {"channels": [{}]},
    }]
    for m in cfg["mats"]:
        state = snapshot["mats"][m["number"]]
        entities.append(_sensor(m["id"], m["name"], m["lat"], m["lon"], state["state"], state["error"],
                                lifetime_s, parent=fiber["id"],
                                symbol=PRESSED_SENSOR_SYMBOL if state["pressed"] else SENSOR_SYMBOL))
    return entities


def _alarm(alarm_id: str, sensor_id: str, controller_id: str, label: str, radius_m: float,
           duration_s: float, confidence: float, to_verify: bool, symbol: str) -> dict:
    """With `track` and `symbol` the alarm also appears in the Hydris Tracks
    list (tested on 04/11/2026: without either it does not, and a plain detection
    only shows on the map, with the layer on). The symbol must be set by hand:
    Hydris computes none for the `other` taxonomy."""
    variance = float(radius_m) ** 2  # Hydris draws a circle of radius sqrt(mxx)
    return {
        "id": alarm_id,
        "label": label,
        "controller": {"id": controller_id},
        "priority": "PriorityImmediate",
        "detection": {"detectorEntityId": sensor_id, "confidence": confidence},
        "track": {},
        "symbol": {"milStd2525C": symbol},
        "classification": {"taxonomy": [{"confidence": {"confidence": confidence, "pending": to_verify},
                                         "other": {}}]},
        "pose": {"parent": sensor_id, "cartesian": {"covariance": {"mxx": variance, "myy": variance}}},
        "lifetime": _lifetime(duration_s),
        "routing": {"channels": [{}]},
    }


def fiber_alarm_entity(cfg: dict, test: bool = False) -> dict:
    """The fiber knows something is there, not where or what: large circle and
    a class to verify."""
    fiber = cfg["fiber"]
    # The test has its own id: it must not overwrite a real alarm in progress.
    alarm_id = f"{fiber['id']}~det~{'test' if test else 'event'}"
    text = "TEST - test alarm" if test else "Fiber event"
    return _alarm(alarm_id, fiber["id"], fiber["id"], f"{text} {time.strftime('%H:%M:%S')}",
                  fiber["alarm_radius_m"], cfg["hydris"]["alarm_duration_s"], 0.5, True, FIBER_ALARM_SYMBOL)


def mat_alarm_entity(cfg: dict, number: int) -> dict:
    mat = next(m for m in cfg["mats"] if m["number"] == number)
    return _alarm(f"{mat['id']}~det~event", mat["id"], cfg["fiber"]["id"],
                  f"{mat['name']} pressed {time.strftime('%H:%M:%S')}",
                  mat["alarm_radius_m"], cfg["hydris"]["alarm_duration_s"], 1.0, False, MAT_ALARM_SYMBOL)


class HydrisSender:
    """Sends entities to Hydris from its own thread. Keeps only the latest
    version per id, so the queue never grows; retries if Hydris does not answer.
    An alarm that expired in the meantime restarts with a new expiry: better to
    know late (the label has the true time) than not at all."""

    def __init__(self, address: str, alarm_duration_s: float):
        self.address = address
        self.alarm_duration_s = alarm_duration_s
        self.connected: bool | None = None  # None = not tried yet
        self.last_error: str | None = None
        self.sent = 0
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._active = True
        self._thread = threading.Thread(target=self._work, name="hydris", daemon=True)
        self._thread.start()

    def send(self, entities: list[dict]) -> None:
        with self._lock:
            for e in entities:
                self._pending[e["id"]] = e
        self._wake.set()

    @property
    def pending(self) -> int:
        return len(self._pending)

    def close(self, wait_s: float = 2.0) -> None:
        end = time.monotonic() + wait_s
        while self._pending and self.connected and time.monotonic() < end:
            time.sleep(0.05)
        self._active = False
        self._wake.set()
        self._thread.join(timeout=4)

    def _work(self) -> None:
        try:
            import grpc
            from google.protobuf.json_format import ParseDict
            from platform_proto.world_pb2 import Entity, EntityChangeRequest
            from platform_proto.world_pb2_grpc import WorldServiceStub
        except ImportError as e:
            self.connected, self.last_error = False, f"Hydris libraries missing: {e}"
            log.error(self.last_error)
            return
        channel = grpc.insecure_channel(self.address)
        client = WorldServiceStub(channel)
        while self._active:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            with self._lock:
                batch, self._pending = self._pending, {}
            if not batch:
                continue
            for e in batch.values():
                if "detection" in e and datetime.fromisoformat(e["lifetime"]["until"]) <= _utc_now():
                    e["lifetime"]["until"] = _iso(_utc_now() + timedelta(seconds=self.alarm_duration_s))
            try:
                messages = [ParseDict(e, Entity()) for e in batch.values()]
            except Exception as e:  # a wrong entity is our own bug: do not retry
                self.connected, self.last_error = False, f"Invalid entity: {e}"
                log.error(self.last_error)
                continue
            try:
                client.Push(EntityChangeRequest(changes=messages), timeout=3)
                self.connected, self.last_error = True, None
                self.sent += len(messages)
            except grpc.RpcError as e:
                self.connected, self.last_error = False, e.code().name
                with self._lock:
                    for identifier, entity in batch.items():
                        self._pending.setdefault(identifier, entity)
                self._wake.clear()
                time.sleep(2.0)
                self._wake.set()
        channel.close()


# ============================================================
# Detection thread: UDP -> Core, and sensors to Hydris at every change
# ============================================================
class DetectionThread(threading.Thread):
    TICK_INTERVAL_S = 0.05

    def __init__(self, core: Core, sender: HydrisSender, cfg: dict):
        super().__init__(name="detection", daemon=True)
        self.core = core
        self.sender = sender
        self.cfg = cfg
        self.port_error: str | None = None
        self._active = True
        self._last_heartbeat = 0.0
        self._sent_states = None

    def stop(self) -> None:
        self._active = False
        self.join(timeout=2)

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Exclusive port: on Windows, with SO_REUSEADDR, 3main and this program
        # would steal each other's packets with no error at all.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        try:
            sock.bind(("0.0.0.0", UDP_PORT))
        except OSError as e:
            self.port_error = f"UDP port {UDP_PORT} busy (is 3main open?): {e}"
            log.error(self.port_error)
            sock = None
        if sock is not None:
            sock.settimeout(self.TICK_INTERVAL_S)
        next_tick = 0.0
        while self._active:
            now = time.monotonic()
            try:
                if sock is None:
                    time.sleep(self.TICK_INTERVAL_S)
                else:
                    self._receive(sock)
                now = time.monotonic()
                if now >= next_tick:
                    next_tick = now + self.TICK_INTERVAL_S
                    self.core.tick(now)
                    self._publish_sensors(now)
            except Exception as e:  # must not die silently: the window shows it
                self.core.internal_error = f"{type(e).__name__}: {e}"
                log.exception("Error in detection")
                time.sleep(0.2)
        if sock is not None:
            sock.close()

    def _receive(self, sock: socket.socket) -> None:
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            return
        except ConnectionResetError:  # Windows, after a failed send: not a receive error
            return
        if len(data) == BYTES_PER_PACKET:
            self.core.process(np.frombuffer(data, dtype=SAMPLE_DTYPE), time.monotonic())

    def _publish_sensors(self, now: float) -> None:
        snapshot = self.core.snapshot
        states = (snapshot["fiber"]["state"], snapshot["fiber"]["error"],
                  tuple((m["state"], m["error"], m["pressed"]) for m in snapshot["mats"].values()))
        if states != self._sent_states or now - self._last_heartbeat >= self.cfg["hydris"]["sensor_heartbeat_s"]:
            self._sent_states = states
            self._last_heartbeat = now
            self.sender.send(sensor_entities(self.cfg, snapshot))


# ============================================================
# Window
# ============================================================
BG = "#e9edf2"
PAPER = "#ffffff"
FIELD = "#f3f5f8"
BORDER = "#d3d9e1"
STRONG_BORDER = "#a9b2be"
TEXT = "#11151b"
MUTED = "#46505e"
ACCENT = "#00796b"
RED = "#d0212f"
AMBER = "#b06200"
BLUE = "#1a64c4"
GREEN = "#12803f"

STYLE = f"""
QWidget {{ color: {TEXT}; font-family: 'Segoe UI'; font-size: 13px; }}
QMainWindow, QWidget#Root {{ background-color: {BG}; }}
QLabel {{ background: transparent; }}
QFrame#Card {{ background-color: {PAPER}; border: 1px solid {BORDER}; border-radius: 12px; }}
QLabel#Title {{ color: {ACCENT}; font-size: 11px; font-weight: 800; letter-spacing: 1px; }}
QLabel#Note {{ color: {MUTED}; font-size: 12px; }}
QPushButton {{ background-color: {FIELD}; border: 1px solid {STRONG_BORDER}; border-radius: 8px;
               padding: 7px 14px; font-weight: 600; }}
QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QSpinBox {{ background-color: {FIELD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 5px 8px; font-weight: 600; }}
QTableWidget, QListWidget {{ background-color: #fbfcfd; border: 1px solid {BORDER}; border-radius: 8px;
                             gridline-color: {BORDER}; alternate-background-color: #fbfcfd; }}
QHeaderView::section {{ background-color: {FIELD}; border: none; border-bottom: 1px solid {BORDER};
                        padding: 6px; font-weight: 700; color: {MUTED}; }}
QListWidget {{ font-family: 'Consolas'; font-size: 12px; }}
"""

STATE_NAME = {
    Core.LISTENING: ("Listening on the fiber", BLUE),
    Core.WAKING: ("Waking the mats", AMBER),
    Core.ARMED: ("Mats active", ACCENT),
}
DEVICE_NAME = {ACTIVE: ("Active", GREEN), DEGRADED: ("Problem", AMBER), FAILED: ("Failed", RED)}


def _chip_style(color: str) -> str:
    c = QtGui.QColor(color)
    return (f"color: {color}; background: rgba({c.red()},{c.green()},{c.blue()},26); "
            f"border: 1px solid rgba({c.red()},{c.green()},{c.blue()},110); "
            "border-radius: 11px; padding: 3px 10px; font-size: 12px; font-weight: 700;")


def _card(title: str) -> tuple[QtWidgets.QFrame, QtWidgets.QVBoxLayout]:
    card = QtWidgets.QFrame()
    card.setObjectName("Card")
    layout = QtWidgets.QVBoxLayout(card)
    layout.setContentsMargins(14, 10, 14, 12)
    layout.setSpacing(8)
    label = QtWidgets.QLabel(title.upper())
    label.setObjectName("Title")
    layout.addWidget(label)
    return card, layout


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("Hydris Integrator - fiber and mats")
        self.resize(760, 640)

        self.archive = SessionArchive(ARCHIVE_FOLDER, cfg)
        self.daisy = DaisyCommands(cfg["esp_host"])
        self.sender = HydrisSender(cfg["hydris"]["address"], cfg["hydris"]["alarm_duration_s"])
        self._events_to_show: deque[dict] = deque(maxlen=200)
        self._last_event: dict[int, str] = {}
        self.core = Core(cfg, self.daisy, self.archive, self._on_event)
        self.detection = DetectionThread(self.core, self.sender, cfg)

        self._build()
        self.detection.start()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(200)

    # Called from the detection thread: no widgets in here.
    def _on_event(self, event: dict) -> None:
        kind = event["type"]
        if kind == "fiber_alarm":
            self.sender.send([fiber_alarm_entity(self.cfg)])
            self.archive.event("hydris_fiber_alarm", ratio=event["ratio"])
        elif kind == "mat_pressed":
            self.sender.send([mat_alarm_entity(self.cfg, event["number"])])
            self.archive.event("hydris_mat_alarm", number=event["number"], renewal=event["renewal"])
        if not event.get("renewal"):
            self._events_to_show.append({**event, "time": time.strftime("%H:%M:%S")})

    def _build(self) -> None:
        root = QtWidgets.QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # General status
        card, body = _card("Status")
        row = QtWidgets.QHBoxLayout()
        self.state_label = QtWidgets.QLabel()
        self.hydris_label = QtWidgets.QLabel()
        self.fiber_label = QtWidgets.QLabel()
        self.esp_label = QtWidgets.QLabel()
        for label in (self.state_label, self.hydris_label, self.fiber_label, self.esp_label):
            row.addWidget(label)
        row.addStretch(1)
        body.addLayout(row)
        self.warning_label = QtWidgets.QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(f"color: {RED}; font-weight: 700;")
        self.warning_label.hide()
        body.addWidget(self.warning_label)
        self.detail_label = QtWidgets.QLabel()
        self.detail_label.setObjectName("Note")
        body.addWidget(self.detail_label)
        layout.addWidget(card)

        # Controls
        card, body = _card("Controls")
        row = QtWidgets.QHBoxLayout()
        reset = QtWidgets.QPushButton("Back to listening")
        reset.setToolTip("Silences the mats and puts the system back to listening on the fiber")
        reset.clicked.connect(self.core.request_reset)
        test = QtWidgets.QPushButton("Test alarm")
        test.setToolTip("Sends Hydris an alarm labelled TEST, without touching detection")
        test.clicked.connect(self._test_alarm)
        row.addWidget(reset)
        row.addWidget(test)
        row.addSpacing(20)
        row.addWidget(QtWidgets.QLabel("Volume"))
        for percent in (20, 50, 100):
            b = QtWidgets.QPushButton(f"{percent}%")
            b.clicked.connect(lambda _=False, p=percent: self.core.set_volume(p))
            row.addWidget(b)
        self.volume_spin = QtWidgets.QSpinBox()
        self.volume_spin.setRange(0, 100)
        self.volume_spin.setSingleStep(5)
        self.volume_spin.setValue(20)
        self.volume_spin.setSuffix("%")
        apply_volume = QtWidgets.QPushButton("Set")
        apply_volume.clicked.connect(lambda: self.core.set_volume(self.volume_spin.value()))
        row.addWidget(self.volume_spin)
        row.addWidget(apply_volume)
        self.volume_label = QtWidgets.QLabel()
        self.volume_label.setObjectName("Note")
        row.addWidget(self.volume_label)
        row.addStretch(1)
        body.addLayout(row)
        layout.addWidget(card)

        # Sensors
        card, body = _card("Sensors sent to Hydris")
        self.table = QtWidgets.QTableWidget(1 + len(self.cfg["mats"]), 5)
        self.table.setHorizontalHeaderLabels(["Sensor", "Latitude", "Longitude", "Status", "Last event"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(QtCore.Qt.NoFocus)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        fiber = self.cfg["fiber"]
        rows = [(fiber["name"], fiber["lat"], fiber["lon"])] + [(m["name"], m["lat"], m["lon"]) for m in self.cfg["mats"]]
        for r, (name, lat, lon) in enumerate(rows):
            for c, value in enumerate((name, f"{lat:.6f}", f"{lon:.6f}", "", "")):
                self.table.setItem(r, c, QtWidgets.QTableWidgetItem(value))
        height = self.table.horizontalHeader().sizeHint().height() + self.table.rowHeight(0) * len(rows) + 4
        self.table.setFixedHeight(height)
        body.addWidget(self.table)
        row = QtWidgets.QHBoxLayout()
        note = QtWidgets.QLabel("Coordinates and thresholds: configuration.json (applied at next start).")
        note.setObjectName("Note")
        open_config = QtWidgets.QPushButton("Open configuration")
        open_config.clicked.connect(self._open_configuration)
        row.addWidget(note)
        row.addStretch(1)
        row.addWidget(open_config)
        body.addLayout(row)
        layout.addWidget(card)

        # Events
        card, body = _card("Events")
        self.event_list = QtWidgets.QListWidget()
        body.addWidget(self.event_list)
        layout.addWidget(card, stretch=1)

    def _test_alarm(self) -> None:
        self.sender.send([fiber_alarm_entity(self.cfg, test=True)])
        self.archive.event("hydris_test_alarm")
        self._events_to_show.append({"type": "test", "time": time.strftime("%H:%M:%S")})

    @staticmethod
    def _open_configuration() -> None:
        if CONFIGURATION_FILE.exists():
            os.startfile(CONFIGURATION_FILE)

    def _refresh(self) -> None:
        snapshot = self.core.snapshot
        text, color = STATE_NAME[snapshot["state"]]
        self.state_label.setText(text)
        self.state_label.setStyleSheet(_chip_style(color))

        if self.sender.connected is None:
            text, color = "Hydris: connecting...", MUTED
        elif self.sender.connected:
            text, color = "Hydris connected", GREEN
        else:
            text, color = f"Hydris UNREACHABLE ({self.sender.pending} pending)", RED
        self.hydris_label.setText(text)
        self.hydris_label.setStyleSheet(_chip_style(color))

        fiber_ok = snapshot["fiber"]["state"] == ACTIVE
        self.fiber_label.setText("Fiber: data OK" if fiber_ok else "Fiber: NO DATA")
        self.fiber_label.setStyleSheet(_chip_style(GREEN if fiber_ok else RED))
        self.esp_label.setText("ESP connected" if snapshot["esp_connected"] else "ESP UNREACHABLE")
        self.esp_label.setStyleSheet(_chip_style(GREEN if snapshot["esp_connected"] else RED))

        warnings = [w for w in (self.detection.port_error, self.core.internal_error, self.archive.error,
                                self.sender.last_error if self.sender.connected is False else None) if w]
        self.warning_label.setText("\n".join(warnings))
        self.warning_label.setVisible(bool(warnings))

        try:
            free_gb = shutil.disk_usage(FOLDER).free / 1e9
        except OSError:
            free_gb = float("nan")
        recording = "Recording on" if self.archive.active else "Recording OFF"
        ratio = "--" if snapshot["ratio"] is None else f"{snapshot['ratio']:.2f}"
        self.detail_label.setText(
            f"STA/LTA {ratio}   ·   RMS {snapshot['rms']:.0f}   ·   {recording}   ·   "
            f"{free_gb:.0f} GB free   ·   {self.sender.sent} entities sent")

        if snapshot["volume"]:
            a, b = snapshot["volume"]
            self.volume_label.setText(f"master: {a}%" if a == b else f"master: {a}% / {b}%")
        else:
            self.volume_label.setText("master: not read")

        while self._events_to_show:
            self._show_event(self._events_to_show.popleft())

        self._write_state(0, snapshot["fiber"]["state"], snapshot["fiber"]["error"])
        for r, m in enumerate(self.cfg["mats"], start=1):
            state = snapshot["mats"][m["number"]]
            if state["pressed"]:
                self._write_cell(r, 3, "PRESSED", RED, state["error"])
            else:
                self._write_state(r, state["state"], state["error"])
            self.table.item(r, 4).setText(self._last_event.get(m["number"], ""))

    def _write_state(self, row: int, state: str, error: str) -> None:
        text, color = DEVICE_NAME[state]
        self._write_cell(row, 3, f"{text}: {error}" if error else text, color, error)

    def _write_cell(self, row: int, column: int, text: str, color: str, tooltip: str = "") -> None:
        cell = self.table.item(row, column)
        cell.setText(text)
        cell.setForeground(QtGui.QColor(color))
        cell.setToolTip(tooltip)
        font = cell.font()
        font.setBold(True)
        cell.setFont(font)

    def _show_event(self, event: dict) -> None:
        at, kind = event["time"], event["type"]
        if kind == "fiber_alarm":
            text, color = f"{at}  Fiber ALARM (STA/LTA {event['ratio']:.2f})", RED
        elif kind == "mat_pressed":
            self._last_event[event["number"]] = f"pressed {at}"
            text, color = f"{at}  ALARM mat {event['number']} pressed", RED
        elif kind == "mat_released":
            self._last_event[event["number"]] = f"released {at}"
            text, color = f"{at}  mat {event['number']} released", MUTED
        else:
            text, color = f"{at}  TEST alarm sent", BLUE
        item = QtWidgets.QListWidgetItem(text)
        item.setForeground(QtGui.QColor(color))
        self.event_list.insertItem(0, item)
        while self.event_list.count() > 200:
            self.event_list.takeItem(self.event_list.count() - 1)

    def closeEvent(self, event) -> None:
        self._timer.stop()
        self.detection.stop()
        # Last snapshot: Hydris shows straight away that the system is off.
        snapshot = dict(self.core.snapshot)
        off = {"state": FAILED, "error": "Integrator closed"}
        snapshot["fiber"] = off
        snapshot["mats"] = {n: {**off, "pressed": False} for n in snapshot["mats"]}
        self.sender.send(sensor_entities(self.cfg, snapshot))
        self.daisy.close()
        self.sender.close()
        self.archive.close()
        event.accept()


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    try:
        cfg = load_configuration()
    except (OSError, ValueError) as e:
        QtWidgets.QMessageBox.critical(None, "Hydris Integrator", f"configuration.json cannot be read:\n{e}")
        sys.exit(1)
    window = MainWindow(cfg)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
