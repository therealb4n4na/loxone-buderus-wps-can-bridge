#!/usr/bin/env python3
"""Buderus WPS / Rego1000 CAN bridge for Loxone.

Purpose
-------
Read selected Rego1000 registers and observed CAN states from ``can0`` and
expose a compact HTTP API for the Loxone Miniserver.

Operational boundaries
----------------------
* HTTP bind address and port are configurable through environment variables.
* CAN runs in listen-only mode except for short, whitelisted read-only polling
  windows. Those windows transmit RTR reads only and always restore listen-only.
* The production bridge contains no CAN write/data-frame helpers. Future WPS
  control functions must be implemented separately and only after persistence
  (RAM vs. EEPROM/flash) has been verified for the target register.
* Unknown CAN/register mappings stay diagnostic/provisional until they have
  been verified against a real state change; do not guess mappings.
* Change logging records state changes, not every raw CAN frame, and uses
  buffered writes to reduce SD-card wear. Runtime logs under
  ``/opt/wps-can-bridge/logs`` are not source.

Important maintenance rule: preserve the listen-only restore logic. Read-only
polling must never be turned into an implicit write path.
"""

import json
import os
import queue
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlparse

def _primary_ipv4():
    """Return the primary local IPv4 address without sending application data."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect selects the outbound interface locally; no payload is sent.
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


CAN_IF = os.environ.get("WPS_CAN_IF", "can0")
CAN_BITRATE = int(os.environ.get("WPS_CAN_BITRATE", "125000"))
DEFAULT_LOCAL_IP = _primary_ipv4()
BIND_IP = os.environ.get("WPS_BIND_IP", DEFAULT_LOCAL_IP)
PORT = int(os.environ.get("WPS_PORT", "8097"))

CHANGE_LOG_DIR = os.environ.get("WPS_CHANGE_LOG_DIR", "/opt/wps-can-bridge/logs")
CHANGE_LOG_RETENTION_DAYS = 14
CHANGE_LOG_FLUSH_S = 60.0
CHANGE_LOG_BATCH_SIZE = 250
CHANGE_LOG_QUEUE_MAX = 5000
CHANGE_LOG_UNKNOWN_THROTTLE_S = 2.0
CHANGE_LOG_ANALOG_THROTTLE_S = 30.0
DHW_SNAPSHOT_INTERVAL_S = 300.0
READ_ONLY_POLL_INTERVAL_S = 60.0
CIRCULATION_REFRESH_INTERVAL_S = 900.0
KM200_LOXONE_URL = os.environ.get(
    "WPS_KM200_URL",
    f"http://{DEFAULT_LOCAL_IP}:8095/loxone",
)
KM200_LOCAL_TIMEOUT_S = float(os.environ.get("WPS_KM200_TIMEOUT_S", "1.0"))
CHANGE_LOG_EXCLUDED_REGS = {0x0135, 0x0137, 0x0138, 0x0139, 0x013A, 0x013B}

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_FRAME_FMT = "=IB3x8s"

# Rego1000 Programmversion 3.6.0.
# Temperatur-/Sollwertregister verwenden Faktor 0.1. Bezeichnungen stammen
# aus dem 3.6.0-Variablenverzeichnis; xdhw_weekprogram_* bleibt absichtlich
# technisch benannt, bis die Zuordnung zur thermischen Desinfektion live
# vollständig verifiziert ist.
REGISTERS = {
    0x0182: ("dhw_setpoint_c", 0.1),
    0x027F: ("brine_in_temp_c", 0.1),       # GT10_TEMP, gemeinsamer Sole-Eintritt
    0x028D: ("brine_out_temp_c", 0.1),      # GT11_TEMP, gemeinsamer Sole-Austritt
    0x0299: ("gt1_temp_c", 0.1),
    0x02A1: ("outdoor_temp_c", 0.1),
    0x02AA: ("dhw_temp_c", 0.1),
    0x02CB: ("hot_gas_temp_c", 0.1),
    0x02F0: ("heat_carrier_out_temp_c", 0.1),
    0x02F8: ("heat_carrier_in_temp_c", 0.1),
    0x0323: ("heating_curve_parallel_offset_raw", 1.0),
    0x0325: ("heating_curve_parallel_offset_global_raw", 1.0),
    0x0371: ("heating_season_active", 1.0),
    0x0374: ("heating_season_mode_raw", 1.0),
    0x0377: ("heating_setpoint_c", 0.1),
    0x09AA: ("extra_dhw_available", 1.0),
    0x09AD: ("extra_dhw_request", 1.0),
    0x09AE: ("extra_dhw_stop_temp_c", 0.1),
    0x09B0: ("extra_dhw_time_h", 1.0),
    0x09B1: ("xdhw_weekprogram_day_raw", 1.0),
    0x09B2: ("xdhw_weekprogram_duration_raw", 1.0),
    0x09B3: ("xdhw_weekprogram_failed", 1.0),
    0x09B4: ("xdhw_weekprogram_finished", 1.0),
    0x09B5: ("xdhw_weekprogram_hour", 1.0),
    0x09B6: ("xdhw_weekprogram_max_time_raw", 1.0),
    0x09B7: ("xdhw_weekprogram_request", 1.0),
    0x09B8: ("xdhw_weekprogram_saved_day_raw", 1.0),
    0x09B9: ("xdhw_weekprogram_stop_temp_c", 0.1),
    0x09BB: ("xdhw_weekprogram_week_raw", 1.0),
    0x09BC: ("xdhw_weekprogram_warmkeeping_timer_raw", 1.0),
}

# Diese Werte werden von der WPS nicht zuverlässig selbst zyklisch auf dem
# Bus abgefragt. Deshalb lesen wir sie einmal pro Minute gesammelt per RTR.
# RTR-Lesezugriffe verändern keine WPS-Parameter und erzeugen keine
# EEPROM-/Flash-Schreibvorgänge.
READ_ONLY_POLL_REGISTERS = (
    0x027F,  # GT10_TEMP, gemeinsamer Sole-Eintritt
    0x028D,  # GT11_TEMP, gemeinsamer Sole-Austritt
    0x0323,  # HEATING_CURVE_PARALLEL_OFFSET, Rohwert bis Bedienfeld-Abgleich
    0x0325,  # HEATING_CURVE_PARALLEL_OFFSET_GLOBAL, Rohwert bis Bedienfeld-Abgleich
    0x0371,  # HEATING_SEASON_ACTIVE
    0x0374,  # HEATING_SEASON_MODE
    0x09AA,  # XDHW_ABLE
    0x09AD,  # XDHW_REQUEST
    0x09AE,  # XDHW_STOP_TEMP
    0x09B0,  # XDHW_TIME
    0x09B1,  # XDHW_WEEKPROGRAM_DAY
    0x09B2,  # XDHW_WEEKPROGRAM_DURATION_TIME
    0x09B3,  # XDHW_WEEKPROGRAM_FAILED
    0x09B4,  # XDHW_WEEKPROGRAM_HAS_FINISHED
    0x09B5,  # XDHW_WEEKPROGRAM_HOUR
    0x09B6,  # XDHW_WEEKPROGRAM_MAX_TIME
    0x09B7,  # XDHW_WEEKPROGRAM_REQUEST
    0x09B8,  # XDHW_WEEKPROGRAM_SAVED_DAY
    0x09B9,  # XDHW_WEEKPROGRAM_STOP_TEMP
    0x09BB,  # XDHW_WEEKPROGRAM_WEEK
    0x09BC,  # XDHW_WEEKPROGRAM_WARM_KEEPING_TIMER
)

# Diese direkten Zustands-IDs sehen wir bereits passiv.
# Die Funktionszuordnung bleibt bis zur Verifikation bei echten
# Zustandswechseln als provisional zu betrachten.
DIRECT_STATES = {
    0x00028270: "heating_carrier_pump_on",
    0x0002C270: "heating_circuit_pump_on",
    0x00038270: "three_way_valve",
    0x0003C270: "additional_heater_on",
    0x00048270: "compressor_on",
    0x00054270: "brine_pump_on",
}

lock = threading.Lock()
active_can_operation = threading.Event()
active_can_lock = threading.Lock()
change_log_lock = threading.Lock()
change_log_queue = queue.Queue(maxsize=CHANGE_LOG_QUEUE_MAX)
change_log_last_emit = {}
change_log_stats = {
    "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    "queued": 0,
    "written": 0,
    "dropped": 0,
    "last_event_at": None,
    "last_flush_at": None,
    "last_error": None,
}
read_poll_lock = threading.Lock()
read_poll_stats = {
    "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    "polls": 0,
    "last_poll_at": None,
    "last_success_at": None,
    "last_error": None,
    "register_errors": {},
}
circulation_cache_lock = threading.Lock()
circulation_cache = {
    "updated_at": None,
    "updated_monotonic": None,
    "result": None,
    "last_error": None,
}

state = {
    "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    "discovery_started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    "can_online": False,
    "last_can_error": None,
    "last_frame_time": None,
    "last_frame_monotonic": None,
    "frames_total": 0,
    "values": {},
    "value_meta": {},
    "discovery_registers": {},
    "discovery_requests": {},
    "discovery_can_ids": {},
}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _is_analog_name(name):
    if not name:
        return False
    return (
        "temp" in name
        or "setpoint" in name
        or name.endswith("_pct")
        or name.endswith("_value")
    )


def queue_change_event(kind, key, can_id, old_hex, new_hex, raw_value=None,
                       reg=None, known_name=None):
    """Queue a compact change event; never log every raw CAN frame."""
    now_mono = time.monotonic()
    throttle_s = (
        CHANGE_LOG_ANALOG_THROTTLE_S
        if _is_analog_name(known_name)
        else (0.0 if known_name in DIRECT_STATES.values() else CHANGE_LOG_UNKNOWN_THROTTLE_S)
    )

    with change_log_lock:
        last_emit = change_log_last_emit.get(key)
        if last_emit is not None and (now_mono - last_emit) < throttle_s:
            return
        change_log_last_emit[key] = now_mono

    event = {
        "ts": now_iso(),
        "kind": kind,
        "can_id": f"0x{can_id:08X}",
        "old_hex": old_hex,
        "new_hex": new_hex,
    }
    if reg is not None:
        event["register"] = f"0x{reg:04X}"
    if known_name:
        event["name"] = known_name
    if raw_value is not None:
        event["raw"] = raw_value

    try:
        change_log_queue.put_nowait(event)
        with change_log_lock:
            change_log_stats["queued"] += 1
            change_log_stats["last_event_at"] = event["ts"]
    except queue.Full:
        with change_log_lock:
            change_log_stats["dropped"] += 1


def _cleanup_old_change_logs():
    cutoff = time.time() - (CHANGE_LOG_RETENTION_DAYS * 86400)
    try:
        for name in os.listdir(CHANGE_LOG_DIR):
            if not (name.startswith("can-changes-") and name.endswith(".jsonl")):
                continue
            path = os.path.join(CHANGE_LOG_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def change_log_worker():
    """Buffered JSONL writer with daily files and 14-day retention."""
    try:
        os.makedirs(CHANGE_LOG_DIR, exist_ok=True)
    except Exception as exc:
        with change_log_lock:
            change_log_stats["last_error"] = str(exc)
        return

    buffer = []
    last_flush = time.monotonic()
    last_cleanup_day = None

    while True:
        timeout_s = max(0.1, CHANGE_LOG_FLUSH_S - (time.monotonic() - last_flush))
        try:
            item = change_log_queue.get(timeout=timeout_s)
            buffer.append(item)
        except queue.Empty:
            pass

        should_flush = (
            len(buffer) >= CHANGE_LOG_BATCH_SIZE
            or (buffer and (time.monotonic() - last_flush) >= CHANGE_LOG_FLUSH_S)
        )
        if not should_flush:
            continue

        try:
            groups = {}
            for event in buffer:
                day = event["ts"][:10]
                groups.setdefault(day, []).append(event)

            for day, events in groups.items():
                path = os.path.join(CHANGE_LOG_DIR, f"can-changes-{day}.jsonl")
                with open(path, "a", encoding="utf-8") as handle:
                    for event in events:
                        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                        handle.write("\n")

            stamp = now_iso()
            with change_log_lock:
                change_log_stats["written"] += len(buffer)
                change_log_stats["last_flush_at"] = stamp
                change_log_stats["last_error"] = None
        except Exception as exc:
            with change_log_lock:
                change_log_stats["last_error"] = str(exc)
        finally:
            buffer.clear()
            last_flush = time.monotonic()

        today = datetime.now().astimezone().date().isoformat()
        if today != last_cleanup_day:
            _cleanup_old_change_logs()
            last_cleanup_day = today


def change_log_snapshot():
    with change_log_lock:
        result = dict(change_log_stats)
    result.update({
        "enabled": True,
        "mode": "change-only buffered JSONL + 5-minute DHW snapshots",
        "log_dir": CHANGE_LOG_DIR,
        "retention_days": CHANGE_LOG_RETENTION_DAYS,
        "flush_interval_s": CHANGE_LOG_FLUSH_S,
        "queue_depth": change_log_queue.qsize(),
        "unknown_throttle_s": CHANGE_LOG_UNKNOWN_THROTTLE_S,
        "analog_throttle_s": CHANGE_LOG_ANALOG_THROTTLE_S,
        "dhw_snapshot_interval_s": DHW_SNAPSHOT_INTERVAL_S,
    })
    return result


def dhw_snapshot_worker():
    """Persist a low-rate absolute DHW temperature/state sample for loss analysis."""
    while True:
        time.sleep(DHW_SNAPSHOT_INTERVAL_S)
        with lock:
            values = dict(state.get("values", {}))

        dhw_temp = values.get("dhw_temp_c")
        if dhw_temp is None:
            continue

        event = {
            "ts": now_iso(),
            "kind": "dhw_snapshot",
            "dhw_temp_c": dhw_temp,
            "outdoor_temp_c": values.get("outdoor_temp_c"),
            "compressor_on": values.get("compressor_on"),
            "brine_pump_on": values.get("brine_pump_on"),
            "brine_in_temp_c": values.get("brine_in_temp_c"),
            "brine_out_temp_c": values.get("brine_out_temp_c"),
            "brine_delta_k": values.get("brine_delta_k"),
            "heating_carrier_pump_on": values.get("heating_carrier_pump_on"),
            "three_way_valve": values.get("three_way_valve"),
            "additional_heater_on": values.get("additional_heater_on"),
            "heating_season_active": values.get("heating_season_active"),
            "heating_season_mode_raw": values.get("heating_season_mode_raw"),
            "extra_dhw_request": values.get("extra_dhw_request"),
            "xdhw_weekprogram_request": values.get("xdhw_weekprogram_request"),
        }
        try:
            change_log_queue.put_nowait(event)
            with change_log_lock:
                change_log_stats["queued"] += 1
                change_log_stats["last_event_at"] = event["ts"]
        except queue.Full:
            with change_log_lock:
                change_log_stats["dropped"] += 1


def update_value(name, value, source):
    with lock:
        state["values"][name] = value
        state["value_meta"][name] = {
            "source": source,
            "updated_at": now_iso(),
            "updated_monotonic": time.monotonic(),
        }


def configure_can(listen_only=True):
    commands = [
        ["ip", "link", "set", CAN_IF, "down"],
        [
            "ip", "link", "set", CAN_IF, "type", "can",
            "bitrate", str(CAN_BITRATE),
            "loopback", "off",
            "listen-only", "on" if listen_only else "off",
        ],
        ["ip", "link", "set", CAN_IF, "up"],
    ]

    for cmd in commands:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def active_read_register(reg, timeout_s=1.0):
    """Send exactly one whitelisted Rego1000 RTR read and restore LISTEN-ONLY."""
    allowed = {0x0478, 0x07E4, 0x07E5, 0x07E6, 0x07E7, 0x07E8, 0x07E9, 0x07EA, 0x07EB, 0x07EC}
    if reg not in allowed:
        raise ValueError("register not allowed for active test")

    poll_id = (reg << 14) | 0x04003FE0
    response_id = (reg << 14) | 0x0C003FE0
    sock = None

    with active_can_lock:
        active_can_operation.set()
        time.sleep(0.35)
        try:
            configure_can(listen_only=False)
            time.sleep(0.08)

            sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            sock.settimeout(timeout_s)
            sock.bind((CAN_IF,))

            frame = struct.pack(
                CAN_FRAME_FMT,
                CAN_EFF_FLAG | CAN_RTR_FLAG | poll_id,
                0,
                b"\x00" * 8,
            )
            sock.send(frame)

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                sock.settimeout(max(0.01, remaining))
                raw = sock.recv(16)
                can_id_raw, dlc, payload = struct.unpack(CAN_FRAME_FMT, raw)
                if not (can_id_raw & CAN_EFF_FLAG) or (can_id_raw & CAN_RTR_FLAG):
                    continue
                can_id = can_id_raw & CAN_EFF_MASK
                if can_id != response_id:
                    continue

                data = payload[:min(dlc, 8)]
                raw_value = None
                if len(data) in (1, 2, 4):
                    raw_value = int.from_bytes(data, byteorder="big", signed=True)

                return {
                    "ok": True,
                    "register": f"0x{reg:04X}",
                    "request_can_id": f"0x{poll_id:08X}",
                    "response_can_id": f"0x{response_id:08X}",
                    "dlc": len(data),
                    "hex": data.hex().upper(),
                    "raw": raw_value,
                }

            return {
                "ok": False,
                "register": f"0x{reg:04X}",
                "error": "timeout waiting for response",
            }
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            try:
                configure_can(listen_only=True)
            finally:
                active_can_operation.clear()


def _read_register_from_socket(sock, reg, timeout_s=1.0):
    poll_id = (reg << 14) | 0x04003FE0
    response_id = (reg << 14) | 0x0C003FE0
    frame = struct.pack(
        CAN_FRAME_FMT,
        CAN_EFF_FLAG | CAN_RTR_FLAG | poll_id,
        0,
        b"\x00" * 8,
    )
    sock.send(frame)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        raw = sock.recv(16)
        can_id_raw, dlc, payload = struct.unpack(CAN_FRAME_FMT, raw)
        if not (can_id_raw & CAN_EFF_FLAG) or (can_id_raw & CAN_RTR_FLAG):
            continue
        can_id = can_id_raw & CAN_EFF_MASK
        if can_id != response_id:
            continue
        data = payload[:min(dlc, 8)]
        raw_value = None
        if len(data) in (1, 2, 4):
            raw_value = int.from_bytes(data, byteorder="big", signed=True)
        return raw_value

    raise TimeoutError(f"timeout reading register 0x{reg:04X}")


def _decode_register_value(reg, raw):
    """Decode one known Rego1000 register without changing controller state."""
    known = REGISTERS.get(reg)
    if known is None or raw is None:
        return None, raw

    name, factor = known
    if factor == 0.1:
        value = round(raw / 10.0, 1)
    elif factor == 1.0:
        value = raw
    else:
        value = round(raw * factor, 3)
    return name, value


def _update_derived_brine_values():
    """Publish the common WPS brine temperature spread for Loxone/statistics."""
    with lock:
        brine_in = state["values"].get("brine_in_temp_c")
        brine_out = state["values"].get("brine_out_temp_c")

    if brine_in is not None and brine_out is not None:
        update_value(
            "brine_delta_k",
            round(brine_in - brine_out, 1),
            "DERIVED:GT10-GT11",
        )


def read_poll_snapshot():
    with read_poll_lock:
        result = dict(read_poll_stats)
        result["register_errors"] = dict(read_poll_stats["register_errors"])
    result["interval_s"] = READ_ONLY_POLL_INTERVAL_S
    result["registers"] = [f"0x{reg:04X}" for reg in READ_ONLY_POLL_REGISTERS]
    return result


def active_read_registers(registers, timeout_s=0.40):
    """Read a fixed register batch using RTR frames only, then restore LISTEN-ONLY.

    This function deliberately has no data-frame send path. It can therefore
    request values, but cannot write a WPS parameter or consume EEPROM/flash
    write cycles.
    """
    registers = tuple(registers)
    if not registers or any(reg not in READ_ONLY_POLL_REGISTERS for reg in registers):
        raise ValueError("register batch contains a non-whitelisted read target")

    result = {
        "ok": False,
        "read": 0,
        "errors": {},
        "listen_only_restored": False,
        "error": None,
    }
    sock = None

    with active_can_lock:
        active_can_operation.set()
        time.sleep(0.35)
        try:
            configure_can(listen_only=False)
            time.sleep(0.08)
            sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            sock.bind((CAN_IF,))

            for reg in registers:
                try:
                    raw = _read_register_from_socket(sock, reg, timeout_s=timeout_s)
                    if raw is None:
                        raise RuntimeError("unsupported response length")
                    name, value = _decode_register_value(reg, raw)
                    if name is None:
                        raise RuntimeError("register has no decoder")
                    update_value(name, value, f"REGO-RTR:0x{reg:04X}")
                    result["read"] += 1
                except Exception as exc:
                    result["errors"][f"0x{reg:04X}"] = str(exc)

            _update_derived_brine_values()
            result["ok"] = result["read"] > 0

        except Exception as exc:
            result["error"] = str(exc)

        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            try:
                configure_can(listen_only=True)
                result["listen_only_restored"] = True
            except Exception as exc:
                if result["error"]:
                    result["error"] += f"; listen-only restore failed: {exc}"
                else:
                    result["error"] = f"listen-only restore failed: {exc}"
                result["ok"] = False
            active_can_operation.clear()

    stamp = now_iso()
    with read_poll_lock:
        read_poll_stats["polls"] += 1
        read_poll_stats["last_poll_at"] = stamp
        read_poll_stats["register_errors"] = dict(result["errors"])
        read_poll_stats["last_error"] = result["error"]
        if result["ok"] and result["listen_only_restored"]:
            read_poll_stats["last_success_at"] = stamp

    return result


def read_only_poll_worker():
    """Refresh slow WPS telemetry once per minute using read-only RTR frames."""
    time.sleep(8.0)
    while True:
        try:
            active_read_registers(READ_ONLY_POLL_REGISTERS)
        except Exception as exc:
            with read_poll_lock:
                read_poll_stats["last_poll_at"] = now_iso()
                read_poll_stats["last_error"] = str(exc)
        time.sleep(READ_ONLY_POLL_INTERVAL_S)


def get_circulation_status():
    """Read the complete circulation configuration/output without changing values."""
    reg_active = 0x07E4
    reg_output = 0x0478
    program_regs = list(range(0x07E5, 0x07ED))
    result = {
        "ok": False,
        "active": None,
        "intervals": [],
        "output": None,
        "listen_only_restored": False,
        "error": None,
    }
    sock = None

    def slot_to_time(value):
        if value == 96:
            return "24:00"
        hours = value // 4
        minutes = (value % 4) * 15
        return f"{hours:02d}:{minutes:02d}"

    with active_can_lock:
        active_can_operation.set()
        time.sleep(0.35)
        try:
            configure_can(listen_only=False)
            time.sleep(0.08)
            sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            sock.bind((CAN_IF,))

            result["active"] = _read_register_from_socket(sock, reg_active)
            values = {
                reg: _read_register_from_socket(sock, reg)
                for reg in program_regs
            }
            for index in range(4):
                start_reg = 0x07E5 + (index * 2)
                stop_reg = start_reg + 1
                start = values[start_reg]
                stop = values[stop_reg]
                result["intervals"].append({
                    "program": index + 1,
                    "start_raw": start,
                    "stop_raw": stop,
                    "start": slot_to_time(start),
                    "stop": slot_to_time(stop),
                })
            result["output"] = _read_register_from_socket(sock, reg_output)
            result["ok"] = True

        except Exception as exc:
            result["error"] = str(exc)

        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            try:
                configure_can(listen_only=True)
                result["listen_only_restored"] = True
            except Exception as exc:
                if result["error"]:
                    result["error"] += f"; listen-only restore failed: {exc}"
                else:
                    result["error"] = f"listen-only restore failed: {exc}"
                result["ok"] = False
            active_can_operation.clear()

    return result


def refresh_circulation_cache():
    """Refresh circulation settings read-only and keep the last good snapshot."""
    result = get_circulation_status()
    stamp = now_iso()
    with circulation_cache_lock:
        if result.get("ok") and result.get("listen_only_restored"):
            circulation_cache["result"] = result
            circulation_cache["updated_at"] = stamp
            circulation_cache["updated_monotonic"] = time.monotonic()
            circulation_cache["last_error"] = None
        else:
            circulation_cache["last_error"] = (
                result.get("error") or "circulation read failed"
            )
    return result


def circulation_cache_snapshot():
    """Return cached circulation data without touching CAN."""
    with circulation_cache_lock:
        result = circulation_cache.get("result")
        updated_monotonic = circulation_cache.get("updated_monotonic")
        age_s = None
        if updated_monotonic is not None:
            age_s = round(time.monotonic() - updated_monotonic, 1)
        return {
            "result": dict(result) if isinstance(result, dict) else None,
            "updated_at": circulation_cache.get("updated_at"),
            "age_s": age_s,
            "last_error": circulation_cache.get("last_error"),
        }


def circulation_cache_worker():
    """Refresh circulation settings slowly; UI requests never trigger CAN reads."""
    time.sleep(25.0)
    while True:
        try:
            refresh_circulation_cache()
        except Exception as exc:
            with circulation_cache_lock:
                circulation_cache["last_error"] = str(exc)
        time.sleep(CIRCULATION_REFRESH_INTERVAL_S)


def fetch_km200_loxone():
    """Read the optional KM200 bridge cache without polling the WPS."""
    if not KM200_LOXONE_URL:
        return None

    try:
        request = Request(KM200_LOXONE_URL, headers={"Accept": "application/json"})
        with urlopen(request, timeout=KM200_LOCAL_TIMEOUT_S) as response:
            data = json.load(response)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _slot_decimal(slot):
    """Convert a WPS 15-minute slot to HH.MM-style decimal for Loxone."""
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        return 24.0
    if slot >= 96:
        return 24.0
    hour = slot // 4
    minute = (slot % 4) * 15
    return round(hour + (minute / 100.0), 2)


def loxone_ui_snapshot(data=None):
    """Build the compact, presentation-ready numeric view used by Loxone."""
    if data is None:
        data = snapshot()

    values = data.get("values", {})
    km200 = fetch_km200_loxone()
    circ_cache = circulation_cache_snapshot()
    circ = circ_cache.get("result")

    wps_ok = bool(data.get("bridge_ok"))
    km200_ok = bool(km200 and _as_int(km200.get("bridge_ok"), 0) == 1)

    # Interface: 0=alles OK, 1=KM200 gestoert, 2=WPS-CAN gestoert, 3=beide.
    if wps_ok and km200_ok:
        interface_code = 0
    elif wps_ok:
        interface_code = 1
    elif km200_ok:
        interface_code = 2
    else:
        interface_code = 3

    compressor = _as_int(values.get("compressor_on"), 0)
    additional_heater = _as_int(values.get("additional_heater_on"), 0)
    extra_dhw = _as_int(
        km200.get("dhw_extra_active") if km200 else values.get("extra_dhw_request"),
        0,
    )
    xdhw_request = _as_int(values.get("xdhw_weekprogram_request"), 0)
    xdhw_failed = _as_int(values.get("xdhw_weekprogram_failed"), 0)

    # Betrieb: 0=Bereit, 1=Verdichter, 2=Extra-WW, 3=Zusatzheizung,
    # 4=Verdichter+Zusatzheizung, 8=Daten nicht verfuegbar.
    if not wps_ok:
        operation_code = 8
    elif additional_heater and compressor:
        operation_code = 4
    elif additional_heater:
        operation_code = 3
    elif extra_dhw:
        operation_code = 2
    elif compressor:
        operation_code = 1
    else:
        operation_code = 0

    # Warmwasser: 0=normal, 1=Extra-WW, 2=Desinfektion aktiv,
    # 3=Desinfektion fehlgeschlagen, 8=Daten unvollstaendig.
    if not wps_ok or not km200_ok:
        dhw_code = 8
    elif xdhw_failed:
        dhw_code = 3
    elif xdhw_request:
        dhw_code = 2
    elif extra_dhw:
        dhw_code = 1
    else:
        dhw_code = 0

    brine_in = values.get("brine_in_temp_c")
    brine_out = values.get("brine_out_temp_c")
    brine_delta = values.get("brine_delta_k")
    brine_pump = _as_int(values.get("brine_pump_on"), 0)
    if not wps_ok or None in (brine_in, brine_out, brine_delta):
        brine_code = 8
    else:
        brine_code = 1 if brine_pump else 0

    heating_season = values.get("heating_season_active")
    if not wps_ok or heating_season is None:
        heating_code = 8
    else:
        heating_code = 1 if _as_int(heating_season, 0) else 0

    # Zirkulation: 0=aus, 1=ein/idle (1 Fenster), 2=Pumpe an (1 Fenster),
    # 3=ein/idle (>=2 Fenster), 4=Pumpe an (>=2 Fenster), 8=Cache ungueltig.
    active_intervals = []
    circulation_code = 8
    if circ and circ_cache.get("age_s") is not None and circ_cache["age_s"] <= 1800:
        for interval in circ.get("intervals", []):
            if not (
                _as_int(interval.get("start_raw"), 96) == 96
                and _as_int(interval.get("stop_raw"), 96) == 96
            ):
                active_intervals.append(interval)
        circ_active = _as_int(circ.get("active"), 0)
        circ_output = _as_int(circ.get("output"), 0)
        if not circ_active:
            circulation_code = 0
        elif len(active_intervals) >= 2:
            circulation_code = 4 if circ_output else 3
        else:
            circulation_code = 2 if circ_output else 1

    first = active_intervals[0] if active_intervals else {}
    second = active_intervals[1] if len(active_intervals) > 1 else {}

    # Gesamtstatus: Schnittstellen/Fehler zuerst, dann aktive Betriebsarten.
    if not wps_ok:
        overall_code = 9
    elif not km200_ok:
        overall_code = 8
    elif xdhw_failed:
        overall_code = 7
    elif additional_heater:
        overall_code = 4
    elif xdhw_request:
        overall_code = 3
    elif extra_dhw:
        overall_code = 2
    elif compressor:
        overall_code = 1
    else:
        overall_code = 0

    # Semantisch benannte KM200-Werte haben fuer die UI Vorrang. Die vollen
    # CAN-Roh-/Diagnosewerte bleiben unveraendert unter /loxone verfuegbar.
    return {
        "overall_code": overall_code,
        "operation_code": operation_code,
        "dhw_code": dhw_code,
        "dhw_temp_c": km200.get("dhw_actual_temp_c") if km200 else values.get("dhw_temp_c"),
        "dhw_setpoint_c": km200.get("dhw_set_temperature_c") if km200 else values.get("dhw_setpoint_c"),
        "extra_dhw_stop_c": km200.get("dhw_extra_stop_temp_c") if km200 else values.get("extra_dhw_stop_temp_c"),
        "brine_code": brine_code,
        "brine_in_c": brine_in,
        "brine_out_c": brine_out,
        "brine_delta_k": brine_delta,
        "heating_code": heating_code,
        "outdoor_temp_c": km200.get("outdoor_temp_c") if km200 else values.get("outdoor_temp_c"),
        "heating_setpoint_c": km200.get("hc1_supply_setpoint_c") if km200 else values.get("heating_setpoint_c"),
        "heating_curve_pct": km200.get("hc1_curve_percent") if km200 else None,
        "circulation_code": circulation_code,
        "circ1_start": _slot_decimal(first.get("start_raw")),
        "circ1_stop": _slot_decimal(first.get("stop_raw")),
        "circ2_start": _slot_decimal(second.get("start_raw")),
        "circ2_stop": _slot_decimal(second.get("stop_raw")),
        "interface_code": interface_code,
    }


def discover_request(can_id):
    # Rego1000 Remote Request:
    # Request-ID = (Register << 14) | 0x04003FE0
    if (can_id & 0x0C003FFF) != 0x04003FE0:
        return

    reg = (can_id & 0x03FFC000) >> 14
    stamp = now_iso()

    with lock:
        key = f"0x{reg:04X}"
        entry = state["discovery_requests"].get(key)
        if entry is None:
            entry = {
                "register": key,
                "request_can_id": f"0x{can_id:08X}",
                "count": 0,
                "first_seen": stamp,
                "last_seen": stamp,
            }
            state["discovery_requests"][key] = entry

        entry["count"] += 1
        entry["last_seen"] = stamp


def discover_register(can_id, data):
    # Rego1000 Response:
    # Response-ID = (Register << 14) | 0x0C003FE0
    if (can_id & 0x0C003FFF) != 0x0C003FE0:
        return False

    reg = (can_id & 0x03FFC000) >> 14
    stamp = now_iso()
    raw_hex = data.hex().upper()
    raw_signed = None

    if len(data) in (1, 2, 4):
        raw_signed = int.from_bytes(data, byteorder="big", signed=True)

    known = REGISTERS.get(reg)
    known_name = known[0] if known else None
    scaled_value = None
    if known and raw_signed is not None:
        factor = known[1]
        scaled_value = round(raw_signed * factor, 3)

    change_old_hex = None
    with lock:
        key = f"0x{reg:04X}"
        entry = state["discovery_registers"].get(key)

        if entry is None:
            entry = {
                "register": key,
                "response_can_id": f"0x{can_id:08X}",
                "known_name": known_name,
                "dlc": len(data),
                "count": 0,
                "changes": 0,
                "first_seen": stamp,
                "last_seen": stamp,
                "last_hex": None,
                "last_raw": None,
                "min_raw": None,
                "max_raw": None,
                "scaled_value": None,
            }
            state["discovery_registers"][key] = entry

        if entry["last_hex"] is not None and entry["last_hex"] != raw_hex:
            change_old_hex = entry["last_hex"]
            entry["changes"] += 1

        entry["count"] += 1
        entry["last_seen"] = stamp
        entry["dlc"] = len(data)
        entry["last_hex"] = raw_hex
        entry["last_raw"] = raw_signed
        entry["scaled_value"] = scaled_value

        if raw_signed is not None:
            if entry["min_raw"] is None or raw_signed < entry["min_raw"]:
                entry["min_raw"] = raw_signed
            if entry["max_raw"] is None or raw_signed > entry["max_raw"]:
                entry["max_raw"] = raw_signed

    if change_old_hex is not None and reg not in CHANGE_LOG_EXCLUDED_REGS:
        queue_change_event(
            "rego_register",
            f"reg:{reg:04X}",
            can_id,
            change_old_hex,
            raw_hex,
            raw_value=raw_signed,
            reg=reg,
            known_name=known_name,
        )

    return True


def discover_can_id(can_id, data):
    stamp = now_iso()
    key = f"0x{can_id:08X}"
    raw_hex = data.hex().upper()
    raw_signed = None

    if len(data) in (1, 2, 4):
        raw_signed = int.from_bytes(data, byteorder="big", signed=True)

    known_name = DIRECT_STATES.get(can_id)

    change_old_hex = None
    with lock:
        entry = state["discovery_can_ids"].get(key)

        if entry is None:
            entry = {
                "can_id": key,
                "known_name": known_name,
                "dlc": len(data),
                "count": 0,
                "changes": 0,
                "first_seen": stamp,
                "last_seen": stamp,
                "last_hex": None,
                "last_raw": None,
                "min_raw": None,
                "max_raw": None,
            }
            state["discovery_can_ids"][key] = entry

        if entry["last_hex"] is not None and entry["last_hex"] != raw_hex:
            change_old_hex = entry["last_hex"]
            entry["changes"] += 1

        entry["count"] += 1
        entry["last_seen"] = stamp
        entry["dlc"] = len(data)
        entry["last_hex"] = raw_hex
        entry["last_raw"] = raw_signed

        if raw_signed is not None:
            if entry["min_raw"] is None or raw_signed < entry["min_raw"]:
                entry["min_raw"] = raw_signed
            if entry["max_raw"] is None or raw_signed > entry["max_raw"]:
                entry["max_raw"] = raw_signed

    if change_old_hex is not None:
        queue_change_event(
            "can_data",
            f"can:{can_id:08X}",
            can_id,
            change_old_hex,
            raw_hex,
            raw_value=raw_signed,
            known_name=known_name,
        )


def process_frame(can_id_raw, dlc, payload):
    if not (can_id_raw & CAN_EFF_FLAG):
        return

    can_id = can_id_raw & CAN_EFF_MASK

    if can_id_raw & CAN_RTR_FLAG:
        discover_request(can_id)
        return

    data = payload[:min(dlc, 8)]

    # Alle Rego1000-Antworten katalogisieren, auch unbekannte Register.
    is_rego_response = discover_register(can_id, data)

    # Andere Datenframes ebenfalls kompakt katalogisieren.
    if not is_rego_response:
        discover_can_id(can_id, data)

    # Direkte CAN-Zustände
    if can_id in DIRECT_STATES and data:
        name = DIRECT_STATES[can_id]
        raw = data[0]
        update_value(name, 1 if raw else 0, f"CAN:{can_id:08X}")
        return

    if not is_rego_response:
        return

    reg = (can_id & 0x03FFC000) >> 14

    if reg not in REGISTERS:
        return

    if len(data) not in (1, 2, 4):
        return

    raw = int.from_bytes(data, byteorder="big", signed=True)
    name, factor = REGISTERS[reg]

    if factor == 0.1:
        value = round(raw / 10.0, 1)
    else:
        value = raw * factor

    update_value(name, value, f"REGO:0x{reg:04X}")


def can_worker():
    while True:
        if active_can_operation.is_set():
            time.sleep(0.05)
            continue

        sock = None

        try:
            configure_can()

            sock = socket.socket(
                socket.AF_CAN,
                socket.SOCK_RAW,
                socket.CAN_RAW
            )

            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_RCVBUF,
                1024 * 1024
            )
            sock.settimeout(0.25)
            sock.bind((CAN_IF,))

            with lock:
                state["can_online"] = True
                state["last_can_error"] = None

            while True:
                if active_can_operation.is_set():
                    break

                try:
                    frame = sock.recv(16)
                except socket.timeout:
                    continue

                can_id, dlc, data = struct.unpack(
                    CAN_FRAME_FMT,
                    frame
                )

                with lock:
                    state["frames_total"] += 1
                    state["last_frame_time"] = now_iso()
                    state["last_frame_monotonic"] = time.monotonic()

                process_frame(can_id, dlc, data)

            try:
                sock.close()
            except Exception:
                pass
            sock = None

            with lock:
                state["can_online"] = False

            while active_can_operation.is_set():
                time.sleep(0.05)

        except Exception as exc:
            with lock:
                state["can_online"] = False
                state["last_can_error"] = str(exc)

            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

            if active_can_operation.is_set():
                while active_can_operation.is_set():
                    time.sleep(0.05)
                continue

            time.sleep(3)


def snapshot():
    controlled_read = active_can_operation.is_set()
    poll_status = read_poll_snapshot()

    with lock:
        listener_online = state["can_online"]
        last_frame_monotonic = state["last_frame_monotonic"]

        if last_frame_monotonic is None:
            frame_age = None
        else:
            frame_age = round(
                time.monotonic() - last_frame_monotonic,
                2
            )

        # During a controlled RTR read the passive worker deliberately yields
        # the socket for a short time. Treat that handover as online as long as
        # frames were fresh immediately before it; this prevents false Loxone/
        # rpi-health alarms during the once-per-minute read window.
        can_online = listener_online or (
            controlled_read
            and frame_age is not None
            and frame_age < 10
        )
        bridge_ok = (
            can_online
            and frame_age is not None
            and frame_age < 10
        )

        values = dict(state["values"])

        meta = {}
        for key, entry in state["value_meta"].items():
            age = round(
                time.monotonic()
                - entry["updated_monotonic"],
                1
            )

            meta[key] = {
                "source": entry["source"],
                "updated_at": entry["updated_at"],
                "age_s": age,
            }

        return {
            "bridge_ok": bridge_ok,
            "can_online": can_online,
            "can_listener_online": listener_online,
            "active_read_in_progress": controlled_read,
            "can_interface": CAN_IF,
            "can_bitrate": CAN_BITRATE,
            # Normal/resting interface policy. Controlled reads are disclosed
            # separately above and always restore listen-only in finally.
            "listen_only": True,
            "program_version": "3.6.0",
            "last_frame": state["last_frame_time"],
            "last_frame_age_s": frame_age,
            "frames_total": state["frames_total"],
            "last_can_error": state["last_can_error"],
            "started_at": state["started_at"],
            "read_only_poll": poll_status,
            "values": values,
            "value_meta": meta,
        }


def discovery_snapshot():
    with lock:
        registers = [
            dict(v) for _, v in sorted(state["discovery_registers"].items())
        ]
        requests = [
            dict(v) for _, v in sorted(state["discovery_requests"].items())
        ]
        can_ids = [
            dict(v) for _, v in sorted(state["discovery_can_ids"].items())
        ]

        changed_registers = sum(1 for item in registers if item["changes"] > 0)
        changed_can_ids = sum(1 for item in can_ids if item["changes"] > 0)

        return {
            "mode": "passive-discovery",
            "listen_only": True,
            "program_version": "3.6.0",
            "started_at": state["discovery_started_at"],
            "frames_total": state["frames_total"],
            "register_count": len(registers),
            "request_register_count": len(requests),
            "can_id_count": len(can_ids),
            "changed_register_count": changed_registers,
            "changed_can_id_count": changed_can_ids,
            "registers": registers,
            "requests": requests,
            "can_ids": can_ids,
        }


class Handler(BaseHTTPRequestHandler):

    def send_json(self, obj, status=200):
        raw = json.dumps(
            obj,
            ensure_ascii=False,
            separators=(",", ":")
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header(
            "Content-Length",
            str(len(raw))
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/discover":
            self.send_json(discovery_snapshot())
            return

        if path == "/logging/status":
            self.send_json(change_log_snapshot())
            return

        if path == "/circulation/status":
            result = get_circulation_status()
            self.send_json(result, 200 if result.get("ok") else 500)
            return

        data = snapshot()

        if path == "/health":
            self.send_json({
                "ok": data["bridge_ok"],
                "bridge_ok": data["bridge_ok"],
                "can_online": data["can_online"],
                "listen_only": data["listen_only"],
                "last_frame_age_s": data["last_frame_age_s"],
                "frames_total": data["frames_total"],
                "last_can_error": data["last_can_error"],
            })
            return

        if path == "/status":
            self.send_json(data)
            return

        if path == "/loxone":
            result = {
                "bridge_ok": 1 if data["bridge_ok"] else 0,
                "can_online": 1 if data["can_online"] else 0,
            }

            if data["last_frame_age_s"] is not None:
                result["can_age_s"] = data["last_frame_age_s"]

            # Nur tatsächlich empfangene und bereits validierte Werte ausgeben.
            result.update(data["values"])

            self.send_json(result)
            return

        if path == "/loxone/ui":
            self.send_json(loxone_ui_snapshot(data))
            return

        self.send_json({
            "service": "wps-can-bridge",
            "mode": "passive monitoring + controlled read diagnostics",
            "endpoints": [
                "/health",
                "/status",
                "/loxone",
                "/loxone/ui",
                "/discover",
                "/logging/status",
                "/circulation/status"
            ]
        })

    def do_POST(self):
        path = urlparse(self.path).path

        # Debug endpoint: exactly one active Rego1000 READ of 0x07E4.
        # No value is written. The interface is restored to LISTEN-ONLY
        # in active_read_register() even if the request fails.
        if path == "/debug/read-circulation-state":
            try:
                result = active_read_register(0x07E4)
                self.send_json(result, 200 if result.get("ok") else 504)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return
        if path == "/debug/read-circulation-output":
            try:
                result = active_read_register(0x0478)
                self.send_json(result, 200 if result.get("ok") else 504)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return

        if path == "/debug/read-circulation-program":
            # READ-ONLY probe of the candidate circulation program registers.
            # Names are tentative because the public 3.6.0 variable dump does
            # not label this 0x07E4..0x07EC block.
            names = {
                0x07E4: "candidate_active",
                0x07E5: "candidate_program1_start",
                0x07E6: "candidate_program1_stop",
                0x07E7: "candidate_program2_start",
                0x07E8: "candidate_program2_stop",
                0x07E9: "candidate_program3_start",
                0x07EA: "candidate_program3_stop",
                0x07EB: "candidate_program4_start",
                0x07EC: "candidate_program4_stop",
            }
            results = []
            for reg, name in names.items():
                try:
                    item = active_read_register(reg)
                except Exception as exc:
                    item = {"ok": False, "register": f"0x{reg:04X}", "error": str(exc)}
                item["candidate_name"] = name
                results.append(item)
            self.send_json({
                "ok": all(item.get("ok") for item in results),
                "mode": "read-only",
                "results": results,
            })
            return

        self.send_json({"ok": False, "error": "not found"}, 404)

    def log_message(self, fmt, *args):
        return


threading.Thread(
    target=change_log_worker,
    daemon=True,
    name="wps-can-change-log",
).start()

threading.Thread(
    target=can_worker,
    daemon=True,
    name="wps-can-worker",
).start()

threading.Thread(
    target=read_only_poll_worker,
    daemon=True,
    name="wps-read-only-poll",
).start()

threading.Thread(
    target=dhw_snapshot_worker,
    daemon=True,
    name="wps-dhw-snapshot",
).start()

threading.Thread(
    target=circulation_cache_worker,
    daemon=True,
    name="wps-circulation-cache",
).start()

server = ThreadingHTTPServer(
    (BIND_IP, PORT),
    Handler
)

server.serve_forever()
