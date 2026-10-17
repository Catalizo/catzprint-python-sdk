"""Small builders for ESC/POS receipt commands.

Each function returns a plain ``dict`` in the JSON shape the API expects, so
you can mix them with hand-written dicts::

    from catzprint import commands as c

    client.print_receipt([
        c.text("Corner Cafe", align="center", bold=True, size=2),
        c.line(),
        c.columns(("2 x Latte", 0.7), ("7.00", 0.3, "right")),
        c.qr("https://example.com/r/1042"),
        c.cut(),
    ], printer_id="...")
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Optional, Sequence, Tuple, Union

__all__ = [
    "Command",
    "barcode",
    "beep",
    "column",
    "columns",
    "cut",
    "drawer",
    "each",
    "feed",
    "image",
    "line",
    "qr",
    "text",
]

Command = Dict[str, Any]
ColumnSpec = Union[Dict[str, Any], str, Tuple[Any, ...]]


def text(
    value: str,
    *,
    align: Optional[str] = None,
    bold: bool = False,
    underline: bool = False,
    invert: bool = False,
    size: Optional[int] = None,
    font: Optional[str] = None,
) -> Command:
    """A line of text. ``align``: left/center/right; ``size``: 1-8; ``font``: "a" or "b"."""
    cmd: Command = {"type": "text", "text": value}
    if align:
        cmd["align"] = align
    if bold:
        cmd["bold"] = True
    if underline:
        cmd["underline"] = True
    if invert:
        cmd["invert"] = True
    if size:
        cmd["size"] = size
    if font:
        cmd["font"] = font
    return cmd


def feed(lines: int = 1) -> Command:
    """Feed blank lines (1-20)."""
    return {"type": "feed", "lines": lines}


def line(char: str = "-") -> Command:
    """A full-width rule made of ``char``."""
    return {"type": "line", "char": char}


def column(text: str, width: Optional[float] = None, align: Optional[str] = None, bold: bool = False) -> Command:
    """One cell for :func:`columns`. ``width`` is a fraction of the line (0 shares what's left)."""
    col: Command = {"text": text}
    if width:
        col["width"] = width
    if align:
        col["align"] = align
    if bold:
        col["bold"] = True
    return col


def columns(*cols: ColumnSpec) -> Command:
    """A row of 1-6 columns. Each is a dict, a string, or a tuple ``(text, width, align)``."""
    out = []
    for c in cols:
        if isinstance(c, dict):
            out.append(dict(c))
        elif isinstance(c, str):
            out.append(column(c))
        else:
            out.append(column(*c))
    return {"type": "columns", "columns": out}


def barcode(data: str, format: str = "code128") -> Command:  # noqa: A002
    """A barcode: ``format`` is code128, ean13 or code39."""
    return {"type": "barcode", "data": data, "format": format}


def qr(data: str, size: Optional[int] = None) -> Command:
    """A QR code."""
    cmd: Command = {"type": "qr", "data": data}
    if size:
        cmd["size"] = size
    return cmd


def image(data: Union[bytes, str], align: Optional[str] = None) -> Command:
    """A PNG or JPEG image (raw bytes or base64 text)."""
    b64 = base64.b64encode(data).decode("ascii") if isinstance(data, (bytes, bytearray)) else data
    cmd: Command = {"type": "image", "data": b64}
    if align:
        cmd["align"] = align
    return cmd


def cut(partial: bool = False) -> Command:
    """Feed and cut the paper."""
    cmd: Command = {"type": "cut"}
    if partial:
        cmd["partial"] = True
    return cmd


def drawer(pin: int = 2) -> Command:
    """Kick the cash drawer (pin 2 or 5)."""
    return {"type": "drawer", "pin": pin}


def beep() -> Command:
    """Sound the printer's buzzer."""
    return {"type": "beep"}


def each(items: str, commands: Sequence[Command]) -> Command:
    """Repeat ``commands`` for every element of the ``items`` list (templates only)."""
    return {"type": "each", "items": items, "commands": list(commands)}
