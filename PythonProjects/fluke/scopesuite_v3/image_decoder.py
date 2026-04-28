from dataclasses import dataclass
from io import BytesIO

from PIL import Image


@dataclass
class ScreenDecodeResult:
    image: Image.Image
    source_format: str
    decoded_size: tuple[int, int]
    expected_size: tuple[int, int]
    crop_rect: None


def decode_printer_stream_to_image(raw_bytes):
    bands = []
    i = 0

    while i < len(raw_bytes) - 5:
        if raw_bytes[i] == 27 and raw_bytes[i + 1] == ord("*"):
            mode = raw_bytes[i + 2]
            n1 = raw_bytes[i + 3]
            n2 = raw_bytes[i + 4]
            columns = n1 + 256 * n2

            if mode in (0, 1, 4):
                bytes_per_column = 1
                band_height = 8
            elif mode in (32, 33):
                bytes_per_column = 3
                band_height = 24
            else:
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
                    value = block[x * bytes_per_column + byte_index]
                    for bit in range(8):
                        if value & (1 << (7 - bit)):
                            y = byte_index * 8 + bit
                            band.putpixel((x, y), 0)

            bands.append(band)
            i = end
        else:
            i += 1

    if not bands:
        raise RuntimeError("No ESC/P raster image bands found.")

    width = max(b.width for b in bands)
    height = sum(b.height for b in bands)
    final = Image.new("1", (width, height), 1)

    y = 0
    for band in bands:
        final.paste(band, (0, y))
        y += band.height

    return final


def decode_printer_stream_to_png(raw_bytes, outfile):
    image = decode_printer_stream_to_image(raw_bytes)
    image.save(outfile)
    return image.size


def decode_screen_capture(raw_bytes, expected_size=(320, 240)):
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        image = Image.open(BytesIO(raw_bytes)).convert("RGB")
        return ScreenDecodeResult(
            image=image,
            source_format="png-block-transfer",
            decoded_size=image.size,
            expected_size=expected_size,
            crop_rect=None,
        )

    image = decode_printer_stream_to_image(raw_bytes).convert("RGB")
    return ScreenDecodeResult(
        image=image,
        source_format="legacy-epson-monochrome",
        decoded_size=image.size,
        expected_size=expected_size,
        crop_rect=None,
    )


def save_screen_debug_files(raw_bytes, output_dir, expected_size=(320, 240), preview_max_size=(900, 675)):
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw_capture.bin"
    decoded_path = output_dir / "decoded_full.png"
    preview_path = output_dir / "rendered_preview.png"

    raw_path.write_bytes(raw_bytes)
    result = decode_screen_capture(raw_bytes, expected_size=expected_size)
    result.image.save(decoded_path)

    preview = result.image.copy()
    preview.thumbnail(preview_max_size)
    preview.save(preview_path)

    return {
        "raw_path": raw_path,
        "decoded_path": decoded_path,
        "preview_path": preview_path,
        "source_format": result.source_format,
        "raw_byte_count": len(raw_bytes),
        "decoded_size": result.decoded_size,
        "expected_size": result.expected_size,
        "crop_rect": result.crop_rect,
        "rendered_size": preview.size,
    }
