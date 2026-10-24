"""Thermal printer support.

:mod:`.printer` holds the print policy, :mod:`.transport` does device I/O,
and :mod:`.escpos` encodes receipt images as ESC/POS raster data.
"""

from .printer import (
    AutoPrintOff,
    Printer,
    PrinterDisabled,
    PrinterRateLimited,
)
from .transport import PrinterError, PrinterWriter, send_to_device

__all__ = [
    "AutoPrintOff",
    "Printer",
    "PrinterDisabled",
    "PrinterError",
    "PrinterRateLimited",
    "PrinterWriter",
    "send_to_device",
]
