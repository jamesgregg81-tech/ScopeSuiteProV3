import serial
import time
from pathlib import Path

PORT = "/dev/cu.usbserial-14340"
BAUD = 1200
OUTDIR = Path("captures")
OUTDIR.mkdir(exist_ok=True)

def read_until_cr(ser):
    data = bytearray()
    while True:
        b = ser.read(1)
        if not b:
            raise TimeoutError("Timeout waiting for CR")
        if b == b"\r":
            return bytes(data)
        data.extend(b)

def send_cmd(ser, cmd):
    print(f"TX: {cmd}")
    ser.reset_input_buffer()
    ser.write(cmd.encode("ascii") + b"\r")
    ser.flush()
    ack = read_until_cr(ser)
    print("ACK:", ack)
    return ack

def read_qp_payload(ser):
    # After ACK, QP returns: decimal_length, then binary data
    length_digits = bytearray()

    while True:
        b = ser.read(1)
        if not b:
            raise TimeoutError("Timeout reading QP length")
        if b == b",":
            break
        length_digits.extend(b)

    total_len = int(length_digits.decode("ascii"))
    print("QP length:", total_len)

    payload = bytearray()
    while len(payload) < total_len:
        chunk = ser.read(min(512, total_len - len(payload)))
        if chunk:
            payload.extend(chunk)
            print(f"Read {len(payload)} / {total_len}")
        else:
            time.sleep(0.05)

    return bytes(payload)

def main():
    print("Fluke 199C Full Display Capture")
    print("Port:", PORT)

    with serial.Serial(
        PORT,
        baudrate=BAUD,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=2,
        write_timeout=2,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    ) as ser:

        send_cmd(ser, "GR")

        send_cmd(ser, "ID")
        ident = read_until_cr(ser)
        print("ID:", ident.decode(errors="replace"))

        send_cmd(ser, "QP")
        payload = read_qp_payload(ser)

        ts = time.strftime("%Y%m%d_%H%M%S")
        out = OUTDIR / f"fluke199c_screen_raw_{ts}.bin"
        out.write_bytes(payload)

        print("Saved:", out)

if __name__ == "__main__":
    main()