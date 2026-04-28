import time

import serial
from serial.tools import list_ports

from .config import BAUD, PORT_PREFIXES, SERIAL_TIMEOUT, SERIAL_TOTAL_TIMEOUT


def available_ports():
    return [
        p.device for p in list_ports.comports()
        if p.device.startswith(PORT_PREFIXES)
    ]


def abort_binary_transfer_and_close(ser, logger=None, drain_quiet_s=1.5, max_drain_s=8.0):
    logger = logger or (lambda _msg: None)
    if ser is None:
        return

    try:
        if not getattr(ser, "is_open", False):
            return
    except Exception:
        return

    try:
        old_timeout = getattr(ser, "timeout", None)
        ser.timeout = 0.1
    except Exception:
        old_timeout = None

    try:
        logger("Binary cleanup: sending transfer abort byte 0x02")
        ser.write(b"\x02")
        ser.flush()
    except Exception as exc:
        logger(f"Binary cleanup: transfer abort failed or was not accepted: {exc}")

    drained = 0
    start = time.monotonic()
    last_data = start
    try:
        logger("Binary cleanup: draining input until quiet")
        while time.monotonic() - start < max_drain_s:
            chunk = ser.read(256)
            if chunk:
                drained += len(chunk)
                last_data = time.monotonic()
                preview = " ".join(f"0x{b:02X}" for b in chunk[:16])
                logger(f"Binary cleanup: drained {len(chunk)} bytes ({preview})")
                continue
            if time.monotonic() - last_data >= drain_quiet_s:
                break
    except Exception as exc:
        logger(f"Binary cleanup: drain failed: {exc}")
    finally:
        logger(f"Binary cleanup: total drained bytes={drained}")
        try:
            if old_timeout is not None:
                ser.timeout = old_timeout
        except Exception:
            pass
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        try:
            ser.close()
            logger("Binary cleanup: serial port closed immediately")
        except Exception as exc:
            logger(f"Binary cleanup: close failed: {exc}")


def safe_release_scope(ser, client, logger=None):
    logger = logger or (lambda _msg: None)

    if ser is None:
        logger("Release failed, port already closed")
        return

    if not getattr(ser, "is_open", False):
        logger("Release failed, port already closed")
        return

    old_timeout = getattr(ser, "timeout", None)
    old_write_timeout = getattr(ser, "write_timeout", None)
    try:
        try:
            ser.timeout = min(float(old_timeout or 1.0), 1.0)
            ser.write_timeout = min(float(old_write_timeout or 1.0), 1.0)
        except Exception:
            pass

        logger("Releasing ScopeMeter to LOCAL mode")
        try:
            logger("Sending transfer abort byte 0x02 before LOCAL release")
            ser.write(b"\x02")
            ser.flush()
            time.sleep(0.2)
        except Exception as exc:
            logger(f"Transfer abort failed or was not needed: {exc}")
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        client.send_cmd(ser, "GL", timeout=2.0)
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
        try:
            if old_timeout is not None:
                ser.timeout = old_timeout
            if old_write_timeout is not None:
                ser.write_timeout = old_write_timeout
        except Exception:
            pass


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
        try:
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
        except serial.SerialException as exc:
            raise RuntimeError(self.format_open_failure(exc)) from exc

    def format_open_failure(self, exc):
        ports = list(list_ports.comports())
        selected = None
        for port in ports:
            if port.device.upper() == str(self.port).upper():
                selected = port
                break

        lines = [
            f"Could not open serial port {self.port} at {self.baudrate} baud before any ScopeMeter command was sent.",
            f"Windows/driver error: {exc}",
        ]
        if selected is None:
            lines.extend([
                f"{self.port} is not currently listed by Windows.",
                "Click Refresh, re-seat the USB/IR adapter, then select the COM port that appears.",
            ])
        else:
            desc = selected.description or "No description"
            hwid = selected.hwid or "No hardware ID"
            lines.extend([
                f"Windows lists {selected.device}: {desc}",
                f"Hardware ID: {hwid}",
            ])
            if "PL2303HXA" in desc.upper() or "VID:PID=067B:2303" in hwid.upper():
                lines.extend([
                    "This is a Prolific PL2303HXA adapter/driver. Windows can list this adapter while still refusing to open it.",
                    "Action: unplug/replug the adapter, move it to a different USB port, or use a working OC4USB/FTDI serial port.",
                    "If Device Manager shows 'PL2303HXA PHASED OUT SINCE 2012', install a compatible Prolific driver or replace the adapter.",
                ])
        if ports:
            listed = "; ".join(f"{p.device} ({p.description})" for p in ports)
            lines.append(f"Currently detected ports: {listed}")
        return "\n".join(lines)

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

    def send_cmd(self, ser, cmd, clear_input=True, timeout=8.0):
        self.logger(f"TX: {cmd}")
        if clear_input:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        ser.write(cmd.encode("ascii") + b"\r")
        ser.flush()

        ack = self.read_until_cr(ser, timeout=timeout)
        self.logger(f"ACK: {ack!r}")

        if ack != b"0":
            raise RuntimeError(f"ScopeMeter rejected command {cmd}: ACK={ack!r}")

        return ack

    def send_cmd_get_ack(self, ser, cmd, clear_input=True):
        self.logger(f"TX: {cmd}")
        if clear_input:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        ser.write(cmd.encode("ascii") + b"\r")
        ser.flush()

        ack = self.read_until_cr(ser)
        self.logger(f"ACK: {ack!r}")
        return ack

    def query_ascii(self, ser, cmd, timeout=8.0):
        self.send_cmd(ser, cmd)
        return self.read_until_cr(ser, timeout=timeout).decode("ascii", errors="replace")

    def query_ascii_allow_ack(self, ser, cmd, timeout=8.0, clear_input=True):
        ack = self.send_cmd_get_ack(ser, cmd, clear_input=clear_input)
        if ack != b"0":
            raise RuntimeError(f"ScopeMeter rejected command {cmd}: ACK={ack!r}")
        response = self.read_until_cr(ser, timeout=timeout).decode("ascii", errors="replace")
        return ack, response

    def enter_remote_and_identify(self, ser):
        self.send_cmd(ser, "GR")
        self.send_cmd(ser, "ID")
        return self.read_until_cr(ser).decode(errors="replace")

    def read_qp_payload(self, ser, progress_callback=None, total_timeout=SERIAL_TOTAL_TIMEOUT):
        # Manual reference: QP / QP 0,0 Epson printer output is raw
        # <printer_data> with no byte-count header. Read until the
        # ScopeMeter has stopped sending data instead of parsing digits.
        self.logger("Receiving Epson QP screen bytes until idle; no byte-count header for QP 0,0")
        data = bytearray()
        first_byte_deadline = time.monotonic() + min(total_timeout, 8.0)
        transfer_timeout = max(45.0, total_timeout)
        deadline = time.monotonic() + transfer_timeout
        idle_timeout = max(1.25, min(3.0, self.timeout))
        last_data = None
        self.logger(f"Epson QP idle timeout: {idle_timeout:.2f}s at {self.baudrate} baud")

        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Timeout reading Epson QP data ({len(data)} bytes received).")

            chunk = ser.read(1024)
            if chunk:
                data.extend(chunk)
                last_data = time.monotonic()
                if progress_callback:
                    progress_callback(len(data), 0)
                continue

            now = time.monotonic()
            if last_data is None:
                if now > first_byte_deadline:
                    raise TimeoutError("Timeout waiting for Epson QP printer data.")
                continue

            if now - last_data >= idle_timeout:
                break

        self.logger(f"Received Epson QP screen bytes: {len(data)}")

        return bytes(data)

    def read_qp_png_block_header(self, ser, block_no, idle_timeout):
        self.logger(f"PNG block {block_no}: waiting for #0 binary block header")
        deadline = time.monotonic() + idle_timeout
        status = bytearray()

        while time.monotonic() < deadline:
            b = ser.read(1)
            if not b:
                continue

            if b == b"#":
                second = self.read_exact(
                    ser,
                    1,
                    f"QP PNG block {block_no} header byte 2",
                    idle_timeout=idle_timeout,
                    total_timeout=idle_timeout,
                    max_chunk=1,
                )
                if second == b"0":
                    rest = self.read_exact(
                        ser,
                        3,
                        f"QP PNG block {block_no} header remainder",
                        idle_timeout=idle_timeout,
                        total_timeout=idle_timeout,
                        max_chunk=1,
                    )
                    header = b"#0" + rest
                    self.logger(f"PNG block {block_no}: block header={header!r}")
                    return header

                self.logger(f"PNG block {block_no}: discarded unexpected header prefix b'#' + {second!r}")
                status.clear()
                continue

            status.extend(b)
            if b == b"\r":
                self.logger(f"PNG block {block_no}: status/ACK before block header={bytes(status)!r}")
                status.clear()

        preview = bytes(status[:40])
        raise TimeoutError(
            f"Timeout waiting for QP PNG block {block_no} #0 header. Partial status={preview!r}"
        )

    def write_qp_png_segment_status(self, ser, status, label):
        payload = status.encode("ascii") + b"\r"
        self.logger(f"{label}: TX status {status!r} bytes={payload!r}")
        ser.write(payload)
        ser.flush()

    def read_qp_png_payload(self, ser, progress_callback=None, total_timeout=SERIAL_TOTAL_TIMEOUT):
        digits = bytearray()
        header_deadline = time.monotonic() + total_timeout

        self.logger("PNG framing stage 2: reading byte count line")
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
        self.logger(f"PNG byte count line: {digits!r}, total length={total}")
        self.logger(f"Receiving PNG screen bytes: {total}")
        data = bytearray()
        block_no = 0
        transfer_timeout = max(90.0, (total * 12.0 / max(self.baudrate, 1)) + 90.0)
        block_idle_timeout = max(10.0, self.timeout)
        deadline = time.monotonic() + transfer_timeout
        self.logger(f"PNG transfer timeout: {transfer_timeout:.0f}s at {self.baudrate} baud")
        self.logger(f"PNG block idle timeout: {block_idle_timeout:.0f}s")

        while len(data) < total:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Timeout reading QP PNG data ({len(data)} / {total} bytes).")

            block_no += 1
            attempts = 0
            while True:
                attempts += 1
                if attempts > 1:
                    self.write_qp_png_segment_status(
                        ser,
                        "1",
                        f"PNG block {block_no}: retransmission requested after checksum failure",
                    )
                    time.sleep(0.05)

                self.logger(f"PNG framing stage 3: reading block {block_no} header (attempt {attempts})")
                try:
                    header = self.read_qp_png_block_header(ser, block_no, 1.0 if attempts == 1 else block_idle_timeout + 5.0)
                except TimeoutError:
                    if attempts != 1:
                        raise
                    self.logger(
                        f"PNG block {block_no}: no unsolicited block header; sending segment status 0"
                    )
                    self.write_qp_png_segment_status(
                        ser,
                        "0",
                        f"PNG block {block_no}: protocol requested first/next segment",
                    )
                    time.sleep(0.05)
                    header = self.read_qp_png_block_header(ser, block_no, block_idle_timeout + 5.0)

                is_last = bool(header[2] & 0x80)
                block_len = int.from_bytes(header[3:5], byteorder="big", signed=False)
                self.logger(
                    f"PNG block {block_no}: block_len={block_len}, is_last={is_last}, "
                    f"cumulative_before={len(data)} / {total}"
                )
                self.logger(f"PNG framing stage 4: reading block {block_no} PNG bytes")
                block = self.read_exact(
                    ser,
                    block_len,
                    f"QP PNG block {block_no} data",
                    idle_timeout=block_idle_timeout,
                    total_timeout=max(block_idle_timeout + 5.0, (block_len * 12.0 / max(self.baudrate, 1)) + block_idle_timeout),
                    max_chunk=64,
                    log_progress=True,
                )
                self.logger(f"PNG block {block_no}: bytes read for block={len(block)} / {block_len}")
                checksum = self.read_exact(
                    ser,
                    1,
                    f"QP PNG block {block_no} checksum",
                    idle_timeout=block_idle_timeout,
                    total_timeout=block_idle_timeout,
                    max_chunk=1,
                    log_progress=True,
                )[0]
                term = self.read_exact(
                    ser,
                    1,
                    f"QP PNG block {block_no} terminator",
                    idle_timeout=block_idle_timeout,
                    total_timeout=block_idle_timeout,
                    max_chunk=1,
                    log_progress=True,
                )
                if term != b"\r":
                    raise RuntimeError(f"Expected QP PNG block {block_no} CR, got {term!r}")

                calculated = sum(block) % 256
                self.logger(
                    f"PNG block {block_no}: checksum received={checksum}, "
                    f"calculated={calculated}, is_last={is_last}"
                )
                if calculated == checksum:
                    break

                if attempts >= 3:
                    try:
                        self.send_cmd(ser, "2", clear_input=False)
                    except Exception as exc:
                        self.logger(f"PNG terminate after checksum failure also failed: {exc}")
                    raise RuntimeError(
                        f"QP PNG checksum failed on block {block_no}: got {checksum}, expected {calculated}"
                    )

                self.logger(
                    f"PNG block {block_no}: checksum failed; requesting retransmission with segment acknowledge 1"
                )

            data.extend(block)
            if progress_callback:
                progress_callback(min(len(data), total), total)

            self.logger(f"PNG block {block_no}: cumulative bytes received={len(data)} / {total}")
            if is_last:
                break
            self.write_qp_png_segment_status(
                ser,
                "0",
                f"PNG block {block_no}: good block received; acknowledging before next block",
            )

        if len(data) != total:
            raise RuntimeError(f"QP PNG length mismatch: received {len(data)} / {total} bytes.")

        self.logger("PNG framing stage 5: final block received; PNG transfer complete")
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

    def read_exact(
        self,
        ser,
        size,
        label="serial data",
        idle_timeout=None,
        total_timeout=None,
        max_chunk=64,
        log_progress=False,
    ):
        data = bytearray()
        start = time.monotonic()
        idle_timeout = max(0.1, idle_timeout if idle_timeout is not None else self.timeout)
        total_timeout = max(idle_timeout, total_timeout if total_timeout is not None else idle_timeout)
        idle_deadline = time.monotonic() + idle_timeout
        total_deadline = start + total_timeout
        last_log = start

        while len(data) < size:
            if time.monotonic() > total_deadline:
                raise TimeoutError(f"Timeout reading {label} ({len(data)} / {size} bytes).")

            remaining = size - len(data)
            chunk_size = max(1, min(max_chunk, remaining))
            waiting = getattr(ser, "in_waiting", 0)
            if waiting:
                chunk_size = max(1, min(chunk_size, waiting, remaining))

            chunk = ser.read(chunk_size)
            if chunk:
                data.extend(chunk)
                idle_deadline = time.monotonic() + idle_timeout
                now = time.monotonic()
                if log_progress and (len(data) == size or now - last_log >= 1.0):
                    self.logger(f"RX {label}: {len(data)} / {size} bytes")
                    last_log = now
            elif time.monotonic() > idle_deadline:
                raise TimeoutError(f"Timeout reading {label} ({len(data)} / {size} bytes).")

        return bytes(data)
