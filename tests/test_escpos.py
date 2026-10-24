import unittest

from PIL import Image

from gpta.printing.escpos import ESC_FEED_AND_CUT, ESC_INIT, raster_rows, to_escpos


def image(width, height, black=()):
    img = Image.new("1", (width, height), 1)
    for x, y in black:
        img.putpixel((x, y), 0)
    return img


def bands(data):
    """Parse GS v 0 commands: [(bytes_per_row, rows, payload)], rest."""
    assert data.startswith(ESC_INIT)
    pos = len(ESC_INIT)
    found = []
    while data[pos : pos + 4] == b"\x1dv0\x00":
        width = int.from_bytes(data[pos + 4 : pos + 6], "little")
        rows = int.from_bytes(data[pos + 6 : pos + 8], "little")
        start = pos + 8
        found.append((width, rows, data[start : start + width * rows]))
        pos = start + width * rows
    return found, data[pos:]


class RasterTests(unittest.TestCase):
    def test_bit_packing_black_is_one_msb_first(self):
        width, rows, data = raster_rows(image(16, 2, [(0, 0), (9, 1), (15, 1)]))
        self.assertEqual((width, rows), (2, 2))
        self.assertEqual(data, bytes([0b10000000, 0, 0, 0b01000001]))

    def test_grayscale_input_is_thresholded(self):
        gray = Image.new("L", (8, 1), 255)
        gray.putpixel((0, 0), 0)
        gray.putpixel((1, 0), 200)
        self.assertEqual(raster_rows(gray)[2], bytes([0b10000000]))

    def test_width_padded_to_multiple_of_8(self):
        width, rows, data = raster_rows(image(10, 1, [(9, 0)]))
        self.assertEqual((width, rows), (2, 1))
        self.assertEqual(data, bytes([0, 0b01000000]))

    def test_header_and_bands(self):
        data = to_escpos(image(576, 600, [(0, 0), (575, 599)]), cut=False)
        found, rest = bands(data)
        self.assertEqual(
            [(w, r) for w, r, _ in found], [(72, 256), (72, 256), (72, 88)]
        )
        self.assertEqual(data[:10], b"\x1b@\x1dv0\x00\x48\x00\x00\x01")
        self.assertEqual(found[0][2][0], 0b10000000)
        self.assertEqual(found[-1][2][-1], 0b00000001)
        self.assertEqual(sum(len(p) for _, _, p in found), 72 * 600)
        self.assertEqual(rest, b"")

    def test_feed_and_cut_follow_the_image(self):
        data = to_escpos(image(384, 10), feed_lines=3, cut=True)
        found, rest = bands(data)
        self.assertEqual([(w, r) for w, r, _ in found], [(48, 10)])
        self.assertEqual(rest, b"\n\n\n" + ESC_FEED_AND_CUT)
        self.assertEqual(ESC_FEED_AND_CUT, b"\x1dV\x41\x00")

    def test_custom_band_size(self):
        found, _ = bands(to_escpos(image(8, 5), band_rows=2))
        self.assertEqual([r for _, r, _ in found], [2, 2, 1])


if __name__ == "__main__":
    unittest.main()
