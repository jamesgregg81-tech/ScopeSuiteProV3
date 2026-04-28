import time

import serial
from serial.tools import list_ports

from .config import BAUD, PORT_PREFIXES, SERIAL_TIMEOUT, SERIAL_TOTAL_TIMEOUT


def available_ports():
    return [
        p.device for p in list_ports.comports()
        if p.device.startswith(PORT_PREFIXES)
    ]


def safe_release_scope(ser, client, logger=None):
    logger = logger or (lambda _msg: None)

    if ser is None:
        logger("Release failed, port already closed")
        return

    if not getattr(ser, "is_open", False):
        logger("Release failed, port already closed")
        return

    try:
        logger("Releasing ScopeMeter to LOCAL mode")
        client.send_cmd(ser, "GL")
        logger("GL ACK received")
    except Exception as exc:
        logger(f"Release failed: {exc}")
    finally:
        try:
            ser.flush()
        except Exception:
            pass
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        try:
            ser.close()
            logger("Serial port closed")
        except Exception as exc:
            logger(f"Release failed while closing port: {exc}")


class FlukeSerialClient:
    def __init__(self, port, logger=None, timeout=SERIAL_TIMEOUT, baudrate=BAUD, xonxoff=False):
        self.port = port
        self.logger = logger or (lambda _msg: None)
        self.timeout = timeout
        self.baudrate = baudrate
        self.xonxoff = xonxoff

    def open(self):
        if not self.port:
            raise RuntimeError("No serial port selected.")

        self.logger(f"Opening serial port {self.port} at {self.baudrate} baud")
        return serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self.timeout,
            write_timeout=2,
            xonxoff=self.xonxoff,
            rtscts=False,
            dsrdtr=False,
        )

    def read_until_cr(self, ser, timeout=8.0):
        data = bytearray()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            b = ser.read(1)
            if not b:
                continue
            if b == b"\r":
                return bytes(data)
            data.extend(b)

        preview = bytes(data[:40])
        raise TimeoutError(f"Timeout waiting for CR from ScopeMeter. Partial={preview!r}")

    def send_cmd(self, ser, cmd, clear_input=True):
        self.logger(f"TX: {cmd}")
        if clear_input:
            ser.reset_input_buffer()
        ser.write(cmd.encode("ascii") + b"\r")
        ser.flush()

        ack = self.read_until_cr(ser)
        self.logger(f"ACK: {ack!r}")

        if ack != b"0":
            raise RuntimeError(f"ScopeMeter rejected command {cmd}: ACK={ack!r}")

    def query_ascii(self, ser, cmd, timeout=8.0):
        self.send_cmd(ser, cmd)
        return self.read_until_cr(ser, timeout=timeout).decode("ascii", errors="replace")

    def enter_remote_and_identify(self, ser):
        self.send_cmd(ser, "GR")
        self.send_cmd(ser, "ID")
        return self.read_until_cr(ser).decode(errors="replace")

    def read_qp_payload(self, ser, progress_callback=None, total_timeout=SERIAL_TOTAL_TIMEOUT):
        digits = bytearray()
        header_deadline = time.monotonic() + total_timeout

        while time.monotonic() < header_deadline:
            b = ser.read(1)
            if not b:
                continue
            if b == b",":
                break
            if 48 <= b[0] <= 57:
                digits.extend(b)
        else:
            raise TimeoutError("Timeout reading QP byte count.")

        if not digits:
            raise RuntimeError("No QP byte count received.")

        total = int(digits.decode("ascii"))
        self.logger(f"Receiving screen bytes: {total}")
        data = bytearray()
        last_data = time.monotonic()
        transfer_timeout = max(60.0, (total * 12.0 / max(self.baudrate, 1)) + 45.0)
        deadline = time.monotonic() + transfer_timeout
        stall_timeout = max(12.0, self.timeout * 2.0)
        self.logger(f"Screen transfer timeout: {transfer_timeout:.0f}s at {self.baudrate} baud")

        while len(data) < total:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Timeout reading QP data ({len(data)} / {total} bytes).")

            chunk = ser.read(min(1024, total - len(data)))
            if chunk:
                data.extend(chunk)
                last_data = time.monotonic()
                if progress_callback:
                    progress_callback(len(data), total)
            elif time.monotonic() - last_data > stall_timeout:
                raise TimeoutError(f"Serial stalled during QP data ({len(data)} / {total} bytes).")

        return bytes(data)

    def read_qp_png_payload(self, ser, progress_callback=None, total_timeout=SERIAL_TOTAL_TIMEOUT):
        digits = bytearray()
        header_deadline = time.monotonic() + total_timeout

        while time.monotonic() < header_deadline:
            b = ser.read(1)
            if not b:
                continue
            if b == b",":
                break
            if 48 <= b[0] <= 57:
                digits.extend(b)
        else:
            raise TimeoutError("Timeout reading QP PNG byte count.")

        if not digits:
            raise RuntimeError("No QP PNG byte count received.")

        total = int(digits.decode("ascii"))
        self.logger(f"Receiving PNG screen bytes: {total}")
        data = bytearray()
        block_no = 0
        transfer_timeout = max(60.0, (total * 12.0 / max(self.baudrate, 1)) + 45.0)
        deadline = time.monotonic() + transfer_timeout
        self.logger(f"PNG transfer timeout: {transfer_timeout:.0f}s at {self.baudrate} baud")

        while len(data) < total:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Timeout reading QP PNG data ({len(data)} / {total} bytes).")

            block_no += 1
            self.send_cmd(ser, "0", clear_input=False)
            header = self.read_exact(ser, 5, "QP PNG block header")
            if header[0:2] != b"#0":
                raise RuntimeError(f"Bad QP PNG block preamble: {header!r}")

            is_last = bool(header[2] & 0x80)
            block_len = int.from_bytes(header[3:5], byteorder="big", signed=False)
            block = self.read_exact(ser, block_len, "QP PNG block data")
            checksum = self.read_exact(ser, 1, "QP PNG checksum")[0]
            term = self.read_exact(ser, 1, "QP PNG block terminator")
            if term != b"\r":
                raise RuntimeError(f"Expected QP PNG block CR, got {term!r}")

            calculated = sum(block) % 256
            if calculated != checksum:
                self.send_cmd(ser, "2", clear_input=False)
                raise RuntimeError(
                    f"QP PNG checksum failed on block {block_no}: got {checksum}, expected {calculated}"
                )

            data.extend(block)
            if progress_callback:
                progress_callback(min(len(data), total), total)

            self.logger(f"PNG block {block_no}: {len(data)} / {total}")
            if is_last:
                break

        if len(data) != total:
            raise RuntimeError(f"QP PNG length mismatch: received {len(data)} / {total} bytes.")

        return bytes(data)

    def read_waveform_response_raw(self, ser, progress_callback=None, total_timeout=30.0):
        data = bytearray()
        start = time.monotonic()
        last_data = start
        last_log = start
        min_bytes = 100
        quiet_threshold = 2.0

        while time.monotonic() - start < total_timeout:
            waiting = getattr(ser, "in_waiting", 0)
            chunk_size = waiting if waiting else 512
            chunk = ser.read(chunk_size)

            if chunk:
                data.extend(chunk)
                last_data = time.monotonic()
                if progress_callback:
                    progress_callback(len(data), None)

                now = time.monotonic()
                if now - last_log > 0.5:
                    rate = len(data) / max(0.1, now - start)
                    self.logger(f"Waveform RX: {len(data)} bytes (~{rate:.1f} B/s)")
                    last_log = now
            elif data and time.monotonic() - last_data > quiet_threshold:
                if len(data) >= min_bytes:
                    self.logger(f"End-of-transmission detected (quiet for {quiet_threshold}s)")
                    break
                last_data = time.monotonic()

        if not data:
            raise RuntimeError("No waveform data received after QW ACK.")

        if len(data) < min_bytes:
            self.logger(f"Warning: received only {len(data)} bytes, may be incomplete")

        return bytes(data)

    def read_exact(self, ser, size, label="serial data"):
        data = bytearray()
        deadline = time.monotonic() + self.timeout

        while len(data) < size:
            chunk = ser.read(size - len(data))
            if chunk:
                data.extend(chunk)
                deadline = time.monotonic() + self.timeout
            elif time.monotonic() > deadline:
                raise TimeoutError(f"Timeout reading {label} ({len(data)} / {size} bytes).")

        return bytes(data)
