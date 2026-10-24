"""Thermal printer support.

:mod:`.printer` holds the print policy; :mod:`.transport` does device I/O.
"""

from .printer import (
    AutoPrintOff,
    Printer,
    PrinterDisabled,
    PrinterRateLimited,
    test_receipt,
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
    "test_receipt",
]
