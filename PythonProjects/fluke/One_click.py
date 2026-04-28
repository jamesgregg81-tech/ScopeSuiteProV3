import serial
import time
from pathlib import Path
from PIL import Image
import subprocess

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
    print("TX:", cmd)
    ser.reset_input_buffer()
    ser.write(cmd.encode() + b"\r")
    ser.flush()
    ack = read_until_cr(ser)
    print("ACK:", ack)
    return ack


def get_qp_data(ser):
    """
    Fluke 199C QP response:
    ACK already read.
    Then usually ASCII length + comma + binary.
    Some firmware mixes timing, so we robustly parse only digits until comma.
    """

    digits = bytearray()

    while True:
        b = ser.read(1)
        if not b:
            raise TimeoutError("Timeout reading QP header")

        # comma ends header
        if b == b",":
            break

        # keep only digits
        if 48 <= b[0] <= 57:
            digits.extend(b)

        # ignore noise / ESC bytes before comma

    if not digits:
        raise RuntimeError("No byte-count digits received from QP")

    total = int(digits.decode("ascii"))
    print("Receiving bytes:", total)

    data = bytearray()

    while len(data) < total:
        chunk = ser.read(min(512, total - len(data)))
        if chunk:
            data.extend(chunk)
            print(f"{len(data)}/{total}", end="\r")

    print()
    return bytes(data)


def decode_to_png(raw_bytes, outfile):
    from PIL import Image

    bands = []
    i = 0

    while i < len(raw_bytes) - 5:
        if raw_bytes[i] == 27 and raw_bytes[i + 1] == ord("*"):
            mode = raw_bytes[i + 2]
            n1 = raw_bytes[i + 3]
            n2 = raw_bytes[i + 4]
            columns = n1 + 256 * n2

            if mode in (0, 1, 4):        # 8-dot graphics
                bytes_per_column = 1
                band_height = 8
            elif mode in (32, 33):    # 24-dot graphics
                bytes_per_column = 3
                band_height = 24
            else:
                print("Skipping unknown ESC/P mode:", mode)
                i += 1
                continue

            start = i + 5
            end = start + columns * bytes_per_column

            if end > len(raw_bytes):
                break

            block = raw_bytes[start:end]

            band = Image.new("1", (columns, band_height), 1)

            for x in range(columns):
                for byte_index in range(bytes_per_column):
                    b = block[x * bytes_per_column + byte_index]
                    for bit in range(8):
                        if b & (1 << (7 - bit)):
                            y = byte_index * 8 + bit
                            band.putpixel((x, y), 0)

            bands.append(band)
            i = end
        else:
            i += 1

    if not bands:
        raise RuntimeError("No ESC/P raster graphics bands found")

    width = max(b.width for b in bands)
    height = sum(b.height for b in bands)

    final = Image.new("1", (width, height), 1)

    y = 0
    for band in bands:
        final.paste(band, (0, y))
        y += band.height

    final.save(outfile)
    print("Saved:", outfile)
    print("Image size:", width, "x", height)


def main():
    ts = time.strftime("%Y%m%d_%H%M%S")

    png_file = OUTDIR / f"fluke199c_{ts}.png"

    print("Connecting to Fluke 199C...")

    with serial.Serial(
        PORT,
        baudrate=BAUD,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=2,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    ) as ser:

        send_cmd(ser, "GR")
        send_cmd(ser, "ID")
        ident = read_until_cr(ser)
        print("Instrument:", ident.decode(errors="replace"))

        send_cmd(ser, "QP")
        raw = get_qp_data(ser)

    print("Decoding image...")
    decode_to_png(raw, png_file)

    print("Saved:", png_file)

    subprocess.run(["open", str(png_file)])


if __name__ == "__main__":
    main()