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
    band_count: int
    command_counts: dict[str, int]
    bottom_status_detected: bool


def bottom_status_region_detected(image):
    rgb = image.convert("RGB")
    width, height = rgb.size
    if width <= 0 or height <= 0:
        return False

    top = int(height * 0.85)
    region = rgb.crop((0, top, width, height))
    pixels = list(region.getdata())
    if not pixels:
        return False

    dark = 0
    non_background = 0
    for r, g, b in pixels:
        if r < 220 or g < 220 or b < 220:
            non_background += 1
        if r < 150 and g < 150 and b < 150:
            dark += 1

    total = len(pixels)
    return (dark / total) > 0.005 or (non_background / total) > 0.02


def raster_band_from_block(columns, bytes_per_column, block):
    band_height = bytes_per_column * 8
    band = Image.new("1", (columns, band_height), 1)

    for x in range(columns):
        for byte_index in range(bytes_per_column):
            value = block[x * bytes_per_column + byte_index]
            for bit in range(8):
                if value & (1 << (7 - bit)):
                    y = byte_index * 8 + bit
                    band.putpixel((x, y), 0)

    return band


def decode_printer_stream_to_image(raw_bytes, return_meta=False):
    bands = []
    command_counts = {}
    i = 0

    while i < len(raw_bytes) - 5:
        if raw_bytes[i] != 27:
            i += 1
            continue

        command = raw_bytes[i + 1]
        if command == ord("*"):
            mode = raw_bytes[i + 2]
            n1 = raw_bytes[i + 3]
            n2 = raw_bytes[i + 4]
            columns = n1 + 256 * n2

            if mode in (0, 1, 4, 6, 32):
                bytes_per_column = 1
            elif mode in (33, 38, 39, 40):
                bytes_per_column = 3
            else:
                command_counts[f"ESC*{mode} skipped"] = command_counts.get(f"ESC*{mode} skipped", 0) + 1
                i += 1
                continue

            start = i + 5
            key = f"ESC*{mode}"
        elif command in (ord("K"), ord("L"), ord("Y"), ord("Z")):
            n1 = raw_bytes[i + 2]
            n2 = raw_bytes[i + 3]
            columns = n1 + 256 * n2
            bytes_per_column = 1
            start = i + 4
            key = f"ESC{chr(command)}"
        else:
            i += 1
            continue

        end = start + columns * bytes_per_column
        if end > len(raw_bytes):
            break

        block = raw_bytes[start:end]
        bands.append(raster_band_from_block(columns, bytes_per_column, block))
        command_counts[key] = command_counts.get(key, 0) + 1
        i = end

    if not bands:
        raise RuntimeError("No ESC/P raster image bands found.")

    width = max(b.width for b in bands)
    height = sum(b.height for b in bands)
    final = Image.new("1", (width, height), 1)

    y = 0
    for band in bands:
        final.paste(band, (0, y))
        y += band.height

    if return_meta:
        return final, {
            "band_count": len(bands),
            "command_counts": command_counts,
            "bottom_status_detected": bottom_status_region_detected(final),
        }
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
            band_count=0,
            command_counts={"PNG": 1},
            bottom_status_detected=bottom_status_region_detected(image),
        )

    image, meta = decode_printer_stream_to_image(raw_bytes, return_meta=True)
    image = image.convert("RGB")
    return ScreenDecodeResult(
        image=image,
        source_format="legacy-epson-monochrome",
        decoded_size=image.size,
        expected_size=expected_size,
        crop_rect=None,
        band_count=meta["band_count"],
        command_counts=meta["command_counts"],
        bottom_status_detected=meta["bottom_status_detected"],
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
    scale = min(preview_max_size[0] / preview.width, preview_max_size[1] / preview.height, 1.0)
    preview_size = (max(1, int(round(preview.width * scale))), max(1, int(round(preview.height * scale))))
    if preview.size != preview_size:
        preview = preview.resize(preview_size, Image.Resampling.LANCZOS)
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
        "preview_max_size": preview_max_size,
        "preview_scale": scale,
        "band_count": result.band_count,
        "command_counts": result.command_counts,
        "bottom_status_detected": result.bottom_status_detected,
        "decoded_bottom_status_detected": bottom_status_region_detected(result.image),
        "preview_bottom_status_detected": bottom_status_region_detected(preview),
    }
