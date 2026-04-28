#!/usr/bin/env python3
"""
fluke19x_easy.py

Simple first-pass Fluke 19x serial capture tool.
For Fluke 196B / 199C testing on macOS.

What it does:
  - Auto-detects your USB serial adapter
  - Opens the port at 9600 baud
  - Tries several common query / hardcopy commands
  - Saves anything received into a captures folder

Run:
  python fluke19x_easy.py

Optional manual port:
  python fluke19x_easy.py --port /dev/cu.usbserial-14340
"""

from __future__ import annotations

import argparse
import datetime
import re
import time
from pathlib import Path

import serial
import serial.tools.list_ports


BAUD_RATE = 9600
TIMEOUT_SECONDS = 1
CAPTURE_SECONDS = 8
OUTPUT_FOLDER = "captures"

COMMANDS = [
    b"*IDN?\r",
    b"ID?\r",
    b"IDN?\r",
    b"PRINT\r",
    b"PRINT?\r",
    b"HCOPY\r",
    b"HCOPY?\r",
    b"HARDCOPY\r",
    b"HARDCOPY?\r",
]


def now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def safe_name(raw: bytes) -> str:
    text = raw.decode("ascii", errors="replace")
    text = text.replace("\r", "_CR").replace("\n", "_LF")
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")
    return text or "command"


def list_ports() -> list:
    return list(serial.tools.list_ports.comports())


def print_ports() -> None:
    ports = list_ports()
    if not ports:
        print("No serial ports found.")
        return

    print("Detected serial ports:")
    for port in ports:
        print(f"  {port.device} | {port.description}")


def auto_detect_usb_serial() -> str | None:
    ports = list_ports()

    keywords = [
        "usbserial",
        "usbmodem",
        "usb-serial",
        "prolific",
        "ftdi",
        "ch340",
        "cp210",
    ]

    matches = []
    for port in ports:
        info = f"{port.device} {port.description} {port.manufacturer or ''}".lower()
        if any(word in info for word in keywords):
            matches.append(port.device)

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        print("More than one USB serial adapter found:")
        for match in matches:
            print(f"  {match}")
        print("Run again with --port and the port you want.")
        return None

    return None


def hexdump(data: bytes, max_bytes: int = 4096) -> str:
    shown = data[:max_bytes]
    lines = []

    for offset in range(0, len(shown), 16):
        chunk = shown[offset : offset + 16]
        hex_text = " ".join(f"{b:02X}" for b in chunk)
        ascii_text = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        lines.append(f"{offset:08X}  {hex_text:<48}  {ascii_text}")

    if len(data) > max_bytes:
        lines.append(f"... truncated. Total bytes: {len(data)}")

    return "\n".join(lines)


def preview_text(data: bytes, max_chars: int = 2000) -> str:
    text = data.decode("latin-1", errors="replace")
    cleaned = []

    for ch in text:
        if ch in "\r\n\t":
            cleaned.append(ch)
        elif 32 <= ord(ch) <= 126:
            cleaned.append(ch)
        else:
            cleaned.append(".")

    return "".join(cleaned)[:max_chars]


def analyze_data(data: bytes) -> str:
    lines = []
    lines.append(f"Total bytes: {len(data)}")

    if not data:
        lines.append("No data returned.")
        return "\n".join(lines)

    lines.append(f"ESC bytes: {data.count(0x1B)}")
    lines.append(f"NUL bytes: {data.count(0x00)}")
    lines.append(f"CR bytes:  {data.count(0x0D)}")
    lines.append(f"LF bytes:  {data.count(0x0A)}")

    if data.startswith(b"BM"):
        lines.append("Possible BMP image header found.")

    if data.startswith(b"\x89PNG"):
        lines.append("Possible PNG image header found.")

    if b"\x1b" in data:
        lines.append("ESC bytes found. This may be printer / ESC-P style hardcopy data.")

    if len(data) < 300 and all((b in (9, 10, 13)) or (32 <= b <= 126) for b in data):
        lines.append("Mostly plain-text response.")

    return "\n".join(lines)


def capture_response(ser: serial.Serial, command: bytes) -> bytes:
    # Drain old input first.
    time.sleep(0.2)
    if ser.in_waiting:
        ser.read(ser.in_waiting)

    print(f"Sending: {command!r}")
    ser.write(command)
    ser.flush()

    chunks = []
    start = time.monotonic()
    last_data = start

    while time.monotonic() - start < CAPTURE_SECONDS:
        waiting = ser.in_waiting

        if waiting:
            chunk = ser.read(waiting)
            chunks.append(chunk)
            last_data = time.monotonic()
            total = sum(len(c) for c in chunks)
            print(f"  received {len(chunk)} bytes, total {total}")
        else:
            time.sleep(0.05)

        # If data started, stop after it goes quiet.
        if chunks and time.monotonic() - last_data > 1.0:
            break

    return b"".join(chunks)


def save_capture(output_dir: Path, stamp: str, command: bytes, data: bytes) -> None:
    label = safe_name(command)
    base = output_dir / f"{stamp}_{label}_{len(data)}bytes"

    raw_file = base.with_suffix(".bin")
    hex_file = output_dir / f"{base.name}_hex.txt"
    txt_file = output_dir / f"{base.name}_preview.txt"

    raw_file.write_bytes(data)
    hex_file.write_text(hexdump(data), encoding="utf-8")
    txt_file.write_text(preview_text(data), encoding="utf-8")

    print(f"Saved raw:     {raw_file}")
    print(f"Saved hex:     {hex_file}")
    print(f"Saved preview: {txt_file}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Easy Fluke 19x serial capture tool")
    parser.add_argument("--port", help="Serial port, example: /dev/cu.usbserial-14340")
    parser.add_argument("--baud", type=int, default=BAUD_RATE, help="Baud rate. Default: 9600")
    parser.add_argument("--list", action="store_true", help="List serial ports and exit")
    args = parser.parse_args()

    if args.list:
        print_ports()
        return 0

    port = args.port
    if not port:
        port = auto_detect_usb_serial()

    if not port:
        print("Could not auto-detect USB serial adapter.")
        print_ports()
        print("Example:")
        print("  python fluke19x_easy.py --port /dev/cu.usbserial-14340")
        return 2

    output_dir = Path(OUTPUT_FOLDER)
    output_dir.mkdir(exist_ok=True)

    stamp = f"fluke19x_{now_stamp()}"
    summary_lines = []

    print("Fluke 19x Easy Capture")
    print(f"Port: {port}")
    print(f"Baud: {args.baud}")
    print(f"Output folder: {output_dir.resolve()}")
    print()

    try:
        with serial.Serial(
            port=port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=TIMEOUT_SECONDS,
            write_timeout=TIMEOUT_SECONDS,
        ) as ser:
            print("Serial port opened.")
            print()

            for command in COMMANDS:
                data = capture_response(ser, command)
                analysis = analyze_data(data)

                print(analysis)
                print("-" * 60)

                summary_lines.append(f"Command: {command!r}")
                summary_lines.append(analysis)
                summary_lines.append("-" * 60)

                if data:
                    save_capture(output_dir, stamp, command, data)

                print()
                time.sleep(0.5)

    except serial.SerialException as exc:
        print("Serial error:")
        print(exc)
        return 1

    summary_file = output_dir / f"{stamp}_summary.txt"
    summary_file.write_text("\n".join(summary_lines), encoding="utf-8")
    print(f"Summary saved: {summary_file}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
