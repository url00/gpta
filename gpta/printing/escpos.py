"""ESC/POS encoding of a rendered receipt image as raster bit-image data."""

from __future__ import annotations

from PIL import Image

ESC_INIT = b"\x1b@"
# GS v 0, normal density (m = 0), followed by xL xH yL yH and the dot data.
GS_RASTER = b"\x1dv0\x00"
# GS V 65 0: feed the paper to the cutter, then cut. The plain cut (GS V 0)
# cuts wherever the paper is, which on most printers is through the last
# lines, because the blade sits above the print head.
ESC_FEED_AND_CUT = b"\x1dV\x41\x00"
# Rows per GS v 0 command. Small bands keep each command within the input
# buffer of cheap printers (256 rows x 72 bytes = 18 KB on 80 mm paper).
BAND_ROWS = 256


def raster_rows(image: Image.Image) -> tuple[int, int, bytes]:
    """Pack ``image`` as ESC/POS raster rows: 1 bit per dot, 1 = black, MSB first.

    Returns ``(bytes_per_row, rows, data)``. The width is padded with white
    to a multiple of 8 dots.
    """
    mono = image if image.mode == "1" else image.convert("L").convert("1")
    if mono.width % 8:
        padded = Image.new("1", ((mono.width + 7) // 8 * 8, mono.height), 1)
        padded.paste(mono, (0, 0))
        mono = padded
    # Pillow's "1;I" packer writes set bits for black pixels, MSB first.
    return mono.width // 8, mono.height, mono.tobytes("raw", "1;I")


def to_escpos(
    image: Image.Image,
    feed_lines: int = 0,
    cut: bool = True,
    band_rows: int = BAND_ROWS,
) -> bytes:
    """Initialise, print ``image`` in raster bands, feed, then optionally cut."""
    width_bytes, rows, data = raster_rows(image)
    out = bytearray(ESC_INIT)
    for top in range(0, rows, band_rows):
        height = min(band_rows, rows - top)
        out += GS_RASTER
        out += width_bytes.to_bytes(2, "little") + height.to_bytes(2, "little")
        out += data[top * width_bytes : (top + height) * width_bytes]
    out += b"\n" * max(0, int(feed_lines))
    if cut:
        out += ESC_FEED_AND_CUT
    return bytes(out)
