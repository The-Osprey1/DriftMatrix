"""Deterministic emulators of the panel's Moon page.

Two programs, one class each:

* ``MoonV20`` emulates ``moon-panel-v2.0.ax`` (v2.0, installed until 2026-10-10).
* ``MoonV21`` mirrors ``moon-panel.ax`` (v2.1, installed since 2026-10-10) line by line.

Each runs its page's own ``init()`` / ``loop()`` / ``draw()`` against a fake
clock and a 32x8 framebuffer, so a frame is a pure function of the clock
readings and the call sequence.  ``render()`` wraps the usual sequence.

``BerryMoon`` runs either ``.ax`` file's own text through ``berry_lite`` with
the same natives, so the hand transcriptions here can be checked against the
exact bytes that get uploaded.  All three reproduce the live captures in
``moon_panel_captures.json``.

Berry semantics this firmware uses, and how they are reproduced:

* ``real`` is IEEE single precision.  Every real value goes through ``R()``:
  ``numpy.float32`` when numpy imports, otherwise ``F32``, a float subclass
  that rounds each result through ``struct``.  +, -, *, / of two float32
  values computed in double and rounded once give exactly the float32 result,
  so the two backends agree bit for bit (the tests check that).
* An int meeting a real is converted first, so ``x * f`` is ``R(x) * f``.
  Conversions are always written out; nothing relies on numpy's promotion.
* ``int / int`` and ``int % int`` truncate toward zero (C); ``int(real)``
  truncates toward zero.  ``tdiv`` / ``tmod`` do the former.
* ``bytes("hex")`` of odd length drops the trailing nibble.
* ``bytes.resize()`` is modelled as filling with ``RESIZE_FILL`` (0xA5), not
  zero, so a page that read a resized buffer before writing it would draw
  visible garbage here.  Neither version does.  The fill is a pessimistic
  assumption, not an observation: upstream Berry zero-fills, and this
  firmware's behaviour has never been measured.
* ``line()`` paints both endpoints with the exact 24-bit colour.  Evidence:
  ocean's ``line(0, y, 31, y, c)`` rows read back from ``/display/screen`` on
  2026-10-09 with x=0 and x=31 both at ``c`` (fixture ``line_evidence``).

Text: the firmware font is not reproduced.  ``Screen`` logs every ``text()``
call (x, y, string, colour) and, when given a ``Font``, also inks its glyphs;
the capture fixture carries the handful of glyphs the panel was seen drawing.

Tables (nebula, stars, palettes, disc geometry, hour tints, horizon tint) are
parsed from the ``.ax`` files, never retyped, so editing either source changes
what these classes draw.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

try:  # pragma: no cover - exercised implicitly
    import numpy as _np
except Exception:  # pragma: no cover - numpy is optional
    _np = None

ROOT = Path(__file__).resolve().parent
V20_PATH = ROOT / "moon-panel-v2.0.ax"
V21_PATH = ROOT / "moon-panel.ax"

WIDTH = 32
HEIGHT = 8
RESIZE_FILL = 0xA5


# --- Berry numbers ---------------------------------------------------------

_PACK = struct.Struct("<f")


class F32(float):
    """A float32 stand-in for environments without numpy.

    Each operation is done in double and rounded once to single precision,
    which is exact for +, -, *, / of float32 operands.  Mixing with a plain
    Python number is refused so a missing ``R()`` cannot hide.
    """

    __slots__ = ()

    def __new__(cls, value: float = 0.0) -> "F32":
        return float.__new__(cls, _PACK.unpack(_PACK.pack(float(value)))[0])

    @staticmethod
    def _other(value: object) -> float:
        if type(value) is not F32:
            raise TypeError(f"F32 arithmetic with {type(value).__name__}; convert with R() first")
        return float(value)  # type: ignore[arg-type]

    def __add__(self, other: object) -> "F32":
        return F32(float(self) + F32._other(other))

    def __radd__(self, other: object) -> "F32":
        return F32(F32._other(other) + float(self))

    def __sub__(self, other: object) -> "F32":
        return F32(float(self) - F32._other(other))

    def __rsub__(self, other: object) -> "F32":
        return F32(F32._other(other) - float(self))

    def __mul__(self, other: object) -> "F32":
        return F32(float(self) * F32._other(other))

    def __rmul__(self, other: object) -> "F32":
        return F32(F32._other(other) * float(self))

    def __truediv__(self, other: object) -> "F32":
        return F32(float(self) / F32._other(other))

    def __rtruediv__(self, other: object) -> "F32":
        return F32(F32._other(other) / float(self))

    def __neg__(self) -> "F32":
        return F32(-float(self))

    def __repr__(self) -> str:
        return f"F32({float(self)!r})"


R = _np.float32 if _np is not None else F32  # the active real type


def use_backend(name: str) -> None:
    """Switch the real type: ``"numpy"`` or ``"pure"`` (tests use both)."""

    global R
    if name == "numpy":
        if _np is None:
            raise RuntimeError("numpy is not installed")
        R = _np.float32
    elif name == "pure":
        R = F32
    else:
        raise ValueError(name)


def tdiv(a: int, b: int) -> int:
    """Berry ``int / int``: C division, truncating toward zero."""

    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def tmod(a: int, b: int) -> int:
    """Berry ``int % int``: C remainder, sign of the dividend."""

    return a - tdiv(a, b) * b


# --- reading the .ax sources ------------------------------------------------


def berry_hex(literal: str) -> bytes:
    """``bytes("...")`` as Berry builds it: hex pairs, an odd last nibble dropped."""

    return bytes.fromhex(literal[: len(literal) // 2 * 2])


def decode_rle(z: bytes, size: int) -> bytes:
    """Line-by-line mirror of v2.1's nebula decode in ``init()``.

    ``FF nn`` writes nn zero bytes; any other byte is copied.  The buffer is
    ``resize``d first, so its initial contents are ``RESIZE_FILL`` here, and a
    write past ``size`` raises as Berry's ``index_error`` would.
    """

    nb = bytearray([RESIZE_FILL]) * size
    p = 0
    k = 0
    while k < len(z):
        v = z[k]
        if v == 0xFF:
            e = p + z[k + 1]
            while p < e:
                if p >= size:
                    raise IndexError(f"nebula RLE writes past {size}")
                nb[p] = 0
                p += 1
            k += 2
        else:
            if p >= size:
                raise IndexError(f"nebula RLE writes past {size}")
            nb[p] = v
            p += 1
            k += 1
    return bytes(nb)


def code_lines(source: str) -> list[str]:
    """Source lines with whole-line comments and blanks removed (what minify keeps)."""

    out = []
    for line in source.splitlines():
        s = line.strip()
        if not s or (s.startswith("#") and not s.startswith("# @")):
            continue
        out.append(s)
    return out


_OPENERS = re.compile(r"\b(?:if|while|for|def|class|try)\b")
_ENDS = re.compile(r"\bend\b")
_STRINGS = re.compile(r'"[^"]*"')


def _depth_change(line: str) -> int:
    """Berry blocks opened minus closed on one line (elif/else are neutral)."""

    bare = _STRINGS.sub('""', line)
    return len(_OPENERS.findall(bare)) - len(_ENDS.findall(bare))


def methods(source: str) -> dict[str, list[str]]:
    """``def name(...)`` bodies of the page class, as stripped code lines."""

    out: dict[str, list[str]] = {}
    name = None
    depth = 0
    for s in code_lines(source):
        if name is None:
            m = re.match(r"def (\w+)\(", s)
            if not m:
                continue
            name = m.group(1)
            out[name] = []
            depth = 0
        out[name].append(s)
        depth += _depth_change(s)
        if depth == 0:
            name = None
    return out


def _search(pattern: str, source: str, what: str) -> re.Match[str]:
    m = re.search(pattern, source)
    if not m:
        raise ValueError(f"cannot find {what} in the Moon source")
    return m


def _list_literal(source: str, name: str) -> list[str]:
    m = _search(rf"self\.{name} = \[([^\]]*)\]", source, f"self.{name}")
    return [s.strip() for s in m.group(1).split(",") if s.strip()]


def _ints(items: list[str]) -> tuple[int, ...]:
    return tuple(int(s, 0) for s in items)


@dataclass(frozen=True)
class Tables:
    """Every constant table one Moon source defines, parsed from its text."""

    version: str
    horizon_alt: float
    horizon_warm: int
    horizon_tint: int
    start_minute: int
    end_minute: int
    reminder_ms: int
    always_show: bool
    ct_default: tuple[int, int]
    tints: tuple[tuple[int | None, int, int], ...]
    hw: tuple[float, ...]
    span0: tuple[int, ...]
    mc: tuple[int, ...]
    sc: tuple[int, ...]
    neb: bytes
    far: tuple[int, ...] = ()
    near: tuple[int, ...] = ()
    st: bytes = b""
    rle: bytes = b""
    neb_size: int = 0
    run_slots: int = 0
    run_cap: int = 0
    disc_rows: int = 0


def parse_tables(source: str) -> Tables:
    """Parse either version.  v2.1 is recognised by its packed star table."""

    version = _search(r"# @version (\S+)", source, "@version").group(1)
    init = source.split("def init()")[1].split("def on_show")[0]
    tint_body = source.split("def update_hour_tint()")[1].split("def shade")[0]
    tints: list[tuple[int | None, int, int]] = []
    for bound, c0, c1 in re.findall(
        r"(?:if|elif) h < (\d+)\s*\n\s*self\.ct = \[(0x[0-9A-Fa-f]+), (0x[0-9A-Fa-f]+)\]", tint_body
    ):
        tints.append((int(bound), int(c0, 16), int(c1, 16)))
    m = _search(r"else\s*\n\s*self\.ct = \[(0x[0-9A-Fa-f]+), (0x[0-9A-Fa-f]+)\]", tint_body, "final tint")
    tints.append((None, int(m.group(1), 16), int(m.group(2), 16)))
    ct = _search(r"self\.ct = \[(0x[0-9A-Fa-f]+), (0x[0-9A-Fa-f]+)\]", init, "default tint")

    common = dict(
        version=version,
        horizon_alt=float(_search(r"var HORIZON_ALT = ([\d.]+)", source, "HORIZON_ALT").group(1)),
        horizon_warm=int(_search(r"var HORIZON_WARM = (\d+)", source, "HORIZON_WARM").group(1)),
        horizon_tint=int(_search(r"var HORIZON_TINT = (0x[0-9A-Fa-f]+)", source, "HORIZON_TINT").group(1), 16),
        start_minute=int(_search(r"var MOON_START_MINUTE = (\d+)", source, "start").group(1)),
        end_minute=int(_search(r"var MOON_END_MINUTE = (\d+)", source, "end").group(1)),
        reminder_ms=int(_search(r"var MOON_REMINDER_MS = (\d+)", source, "reminder").group(1)),
        always_show=_search(r"var ALWAYS_SHOW = (true|false)", source, "ALWAYS_SHOW").group(1) == "true",
        ct_default=(int(ct.group(1), 16), int(ct.group(2), 16)),
        tints=tuple(tints),
        hw=tuple(float(s) for s in _list_literal(init, "hw")),
        span0=_ints(_list_literal(init, "span0")),
        mc=_ints(_list_literal(init, "mc")),
        sc=_ints(_list_literal(init, "sc")),
    )
    st = re.search(r'self\.st = bytes\("([0-9A-Fa-f]*)"\)', init)
    if st is None:
        neb = berry_hex(_search(r'self\.neb = bytes\("([0-9A-Fa-f]*)"\)', init, "nebula").group(1))
        return Tables(
            neb=neb,
            far=_ints(_list_literal(init, "far")),
            near=_ints(_list_literal(init, "near")),
            **common,
        )
    rle = berry_hex(_search(r'var z = bytes\("([0-9A-Fa-f]*)"\)', init, "nebula RLE").group(1))
    size = int(_search(r"nb\.resize\((\d+)\)", init, "nebula size").group(1))
    slots = int(_search(r"while k < (\d+)\s*\n\s*self\.nr\.push\(0\)", init, "run slots").group(1))
    cap = int(_search(r"if pv > 0 && k < (\d+)", source, "run cap").group(1))
    rows = len(_list_literal(init, "dr"))
    return Tables(
        neb=decode_rle(rle, size),
        st=berry_hex(st.group(1)),
        rle=rle,
        neb_size=size,
        run_slots=slots,
        run_cap=cap,
        disc_rows=rows,
        **common,
    )


_TABLE_CACHE: dict[Path, tuple[float, Tables]] = {}


def load_tables(path: Path) -> Tables:
    stamp = path.stat().st_mtime
    cached = _TABLE_CACHE.get(path)
    if cached is None or cached[0] != stamp:
        cached = (stamp, parse_tables(path.read_text(encoding="utf-8")))
        _TABLE_CACHE[path] = cached
    return cached[1]


# --- framebuffer --------------------------------------------------------------


class Font:
    """Glyphs as rows of ``#`` (ink) and ``.`` strings, keyed by character.

    ``text(x, y, ...)`` inks rows y-height .. y-1 (y is the baseline), one
    blank column between glyphs, as the panel was seen to draw them.
    """

    def __init__(self, glyphs: dict[str, list[str]], height: int = 5) -> None:
        self.glyphs = glyphs
        self.height = height

    def ink_width(self, s: str) -> int:
        if not s:
            return 0
        return sum(len(self.glyphs[ch][0]) for ch in s) + len(s) - 1


class Frame(NamedTuple):
    """One drawn frame: 256 colours (row-major) and the ``text()`` calls."""

    pixels: tuple[int, ...]
    texts: tuple[tuple[int, int, str, int], ...]

    def at(self, x: int, y: int) -> int:
        return self.pixels[y * WIDTH + x]


class Screen:
    """The natives a page draws with: ``clear pixel line text text_ink_width``."""

    def __init__(self, font: Font | None = None) -> None:
        self.font = font
        self.px = [0] * (WIDTH * HEIGHT)
        self.texts: list[tuple[int, int, str, int]] = []

    def clear(self) -> None:
        self.px = [0] * (WIDTH * HEIGHT)
        self.texts = []

    def pixel(self, x: int, y: int, c: int) -> None:
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            self.px[y * WIDTH + x] = c

    def line(self, x0: int, y0: int, x1: int, y1: int, c: int) -> None:
        if y0 != y1:
            raise NotImplementedError("only horizontal line() is modelled")
        if x1 < x0:
            x0, x1 = x1, x0
        for x in range(x0, x1 + 1):
            self.pixel(x, y0, c)

    def text(self, x: int, y: int, s: str, c: int) -> int:
        self.texts.append((x, y, s, c))
        if self.font is None:
            return 4 * len(s)
        cx = x
        for ch in s:
            rows = self.font.glyphs[ch]
            top = y - self.font.height
            for gy, row in enumerate(rows):
                for gx, cell in enumerate(row):
                    if cell == "#":
                        self.pixel(cx + gx, top + gy, c)
            cx += len(rows[0]) + 1
        return cx - x

    def text_ink_width(self, s: str) -> int:
        if self.font is None:
            return 4 * len(s) - 1
        return self.font.ink_width(s)

    def frame(self) -> Frame:
        return Frame(tuple(self.px), tuple(self.texts))


class Clock:
    """What the clock natives return.  Mutable: a test advances it."""

    def __init__(self, now_ms: int = 0, epoch_ms: int = 1_791_565_704_544, year: int = 2026,
                 hour: int = 12, minute: int = 0) -> None:
        self.now_ms = now_ms
        self.epoch_ms = epoch_ms
        self.year = year
        self.hour = hour
        self.minute = minute


# --- the pages ------------------------------------------------------------------


class _MoonPage:
    """Methods whose Berry text is identical in both versions.

    ``test_moon_panel_sim`` checks that identity on the sources, so these
    transcribe both files at once.  ``_phase`` and ``_position`` are
    ``compute_phase`` / ``compute_position`` up to the one line v2.1 appends
    to each (``self.build_disc()`` and ``self.hc = ...``).
    """

    SOURCE: Path = V20_PATH

    def __init__(self, clock: Clock, *, tables: Tables | None = None, font: Font | None = None,
                 phase: tuple[int, float, int] | None = None) -> None:
        """``phase`` = (illum, phf, ecl) replaces the astronomy in compute_phase.

        Everything downstream of it (wax = phf < 0.5, naming, v2.1's disc
        rebuild) still runs exactly as on the panel.
        """

        self.clock = clock
        self.tb = tables if tables is not None else load_tables(self.SOURCE)
        self.screen = Screen(font)
        self.phase_override = phase
        self.init()

    # natives -----------------------------------------------------------------
    def now_ms(self) -> int:
        return self.clock.now_ms

    def hour(self) -> int:
        return self.clock.hour

    def minute(self) -> int:
        return self.clock.minute

    # Berry methods ---------------------------------------------------------------
    def on_show(self) -> None:
        self.shown = self.now_ms()
        self.last_shown = self.shown

    def sn(self, x):
        x = x - R(int(x))
        if x < R(0.0):
            x = x + R(1.0)
        n = False
        if x > R(0.5):
            x = x - R(0.5)
            n = True
        f = x * R(2.0)
        u = f * (R(1.0) - f)
        v = R(16.0) * u / (R(5.0) - R(4.0) * u)
        return -v if n else v

    def cs(self, x):
        return self.sn(x + R(0.25))

    @staticmethod
    def mix(a: int, b: int, n: int) -> int:
        if n < 0:
            n = 0
        if n > 100:
            n = 100
        ar = (a >> 16) & 0xFF
        ag = (a >> 8) & 0xFF
        ab = a & 0xFF
        br = (b >> 16) & 0xFF
        bg = (b >> 8) & 0xFF
        bb = b & 0xFF
        return ((ar + tdiv((br - ar) * n, 100)) << 16) | ((ag + tdiv((bg - ag) * n, 100)) << 8) | (ab + tdiv((bb - ab) * n, 100))

    def fade_pixel(self, x: int, y: int, c: int) -> None:
        if x < 0 or x >= 32 or y < 0 or y >= 8:
            return
        if x >= 3 and x <= 28:
            self.screen.pixel(x, y, c)
            return
        f = R(x) / R(3.0) if x < 3 else R(31 - x) / R(3.0)
        if f <= R(0.0):
            return
        self.screen.pixel(
            x, y,
            (int(R((c >> 16) & 0xFF) * f) << 16) | (int(R((c >> 8) & 0xFF) * f) << 8) | int(R(c & 0xFF) * f),
        )

    def days(self):
        if self.clock.year < 2020:
            return None
        return R(self.clock.epoch_ms) / R(86400000.0)

    def name_phase(self) -> None:
        if self.ecl > 0:
            self.pnm = "ECL"
        elif self.illum <= 1:
            self.pnm = "NEW"
        elif self.illum < 49:
            self.pnm = "CRES"
        elif self.illum <= 51:
            self.pnm = "QRTR"
        elif self.illum < 99:
            self.pnm = "GIBB"
        else:
            self.pnm = "FULL"
        self.pcts = "100" if self.illum == 100 else str(self.illum) + "%"

    def _phase(self) -> bool:
        """compute_phase() up to ``self.name_phase()``; False on the nil-clock return."""

        t = self.days()
        if t is None:
            self.valid = False
            return False
        if self.phase_override is not None:
            illum, phf, ecl = self.phase_override
            dd = R(phf)
            self.illum = illum
            self.wax = bool(dd < R(0.5))
            self.phf = dd
            self.ecl = ecl
            self.valid = True
            self.name_phase()
            return True
        T = (t - R(10957.5)) / R(36525.0)
        D = R(297.8501921) + R(445267.1114034) * T
        M = R(357.5291092) + R(35999.0502909) * T
        Mp = R(134.9633964) + R(477198.8675055) * T
        F = R(93.2720950) + R(483202.0175233) * T

        i = R(180.0) - D
        i = i - R(6.289) * self.sn(Mp / R(360.0))
        i = i + R(2.100) * self.sn(M / R(360.0))
        i = i - R(1.274) * self.sn((R(2.0) * D - Mp) / R(360.0))
        i = i - R(0.658) * self.sn(D / R(180.0))
        i = i - R(0.214) * self.sn(Mp / R(180.0))
        i = i - R(0.110) * self.sn(D / R(360.0))

        k = (R(1.0) + self.cs(i / R(360.0))) / R(2.0)
        if k < R(0.0):
            k = R(0.0)
        if k > R(1.0):
            k = R(1.0)
        self.illum = int(k * R(100.0) + R(0.5))

        dd = D / R(360.0)
        dd = dd - R(int(dd))
        if dd < R(0.0):
            dd = dd + R(1.0)
        self.wax = bool(dd < R(0.5))
        self.phf = dd

        self.ecl = 0
        if self.illum >= 99:
            sf = self.sn(F / R(360.0))
            if sf < R(0.0):
                sf = -sf
            if sf < R(0.36):
                self.ecl = int((R(0.36) - sf) * R(278.0))
                if self.ecl > 100:
                    self.ecl = 100

        self.valid = True
        self.name_phase()
        return True

    def _position(self) -> None:
        """compute_position() of v2.0, which v2.1 extends by one line."""

        tb = self.tb
        tr = R(12.0) + self.phf * R(24.0)
        while tr >= R(24.0):
            tr = tr - R(24.0)
        h = (R(self.hour()) + R(self.minute()) / R(60.0)) - tr
        while h > R(12.0):
            h = h - R(24.0)
        while h < R(-12.0):
            h = h + R(24.0)

        alt = self.cs(h / R(24.0))
        warm = 0
        if alt >= R(0.0) and alt < R(tb.horizon_alt):
            warm = int((R(tb.horizon_alt) - alt) * R(tb.horizon_warm) / R(tb.horizon_alt))
        self.litc = self.mix(0xEEE3CE, tb.horizon_tint, warm)
        self.midc = self.mix(0x717988, 0x8A6740, warm)

        if self.ecl > 0:
            self.litc = self.mix(self.litc, 0x8B2500, self.ecl)
            self.midc = self.mix(self.midc, 0x3D0C00, self.ecl)

    def update_hour_tint(self) -> None:
        h = self.hour()
        if h == self.lh:
            return
        self.lh = h
        for bound, c0, c1 in self.tb.tints:
            if bound is None or h < bound:
                self.ct = [c0, c1]
                return

    def shade(self) -> None:
        h = self.hour()
        m = self.minute()
        if h == 6 and m < 30:
            self.dim = 15 + tdiv(m * 85, 30)
        elif h == 20 and m >= 30:
            self.dim = 100 - tdiv((m - 30) * 85, 30)
            if self.dim < 15:
                self.dim = 15
        elif h >= 21 or h < 6:
            self.dim = 15
        else:
            self.dim = 100

    def build_pal(self) -> None:
        b = self.mix(self.ct[0], self.ct[1], tmod(tdiv(self.now_ms(), 400), 100))
        bc = self.mix(b, 0x301550, 45)
        i = 0
        while i < 16:
            intn = 4 + tdiv(i * 24, 10)
            bs = self.mix(0, b, intn)
            if i < 9:
                co = bs
            else:
                co = self.mix(bs, self.mix(0, bc, intn), (i - 8) * 14)
            self.pal[i] = self.mix(0, co, self.dim)
            i += 1

    def loop(self) -> None:
        t = self.now_ms()
        self.shade()
        self.update_hour_tint()
        if not self.valid or t - self.lpc > 60000:
            self.compute_phase()
            self.lpc = t
        self.compute_position()
        self.build_pal()
        self.build_map()

    def should_show(self) -> bool:
        tb = self.tb
        if not self.valid:
            return False
        if tb.always_show:
            return True
        clock_minute = self.hour() * 60 + self.minute()
        if clock_minute < tb.start_minute or clock_minute >= tb.end_minute:
            return False
        return self.last_shown < 0 or self.now_ms() - self.last_shown >= tb.reminder_ms

    def duration(self) -> int:
        return 11000

    def camera(self, c: int):
        """The camera offset draw() derives from c = (t - shown) % 11000."""

        cam = R(0.0)
        if c >= 3800 and c < 4900:
            p = R(c - 3800) / R(1100.0)
            cam = R(32.0) * (p * p * (R(3.0) - R(2.0) * p))
        elif c >= 4900 and c < 8800:
            cam = R(32.0)
        elif c >= 8800 and c < 9900:
            q = R(c - 8800) / R(1100.0)
            cam = R(32.0) * (R(1.0) - (q * q * (R(3.0) - R(2.0) * q)))
        return cam

    def draw(self) -> Frame:
        self.screen.clear()
        t = self.now_ms()
        c = tmod(t - self.shown, 11000)
        cam = self.camera(c)
        df = R(self.dim) / R(100.0)
        self.draw_nebula()
        self.draw_stars(t, cam, df)
        self.draw_shooting_star(t, df)
        self.draw_moon_body(cam)
        self.draw_labels(cam)
        return self.screen.frame()

    def draw_shooting_star(self, t: int, df) -> None:
        mt = tmod(t, 47000)
        if mt >= 300:
            return
        sl = tdiv(t, 47000)
        pr = R(mt) / R(300.0)
        hx = R(tmod(sl * 7, 26)) + pr * R(9.0)
        hy = R(tmod(sl * 3, 4)) + pr * R(5.0)
        k = 0
        while k < 5:
            px = int(hx - R(k) * R(1.6) + R(0.5))
            py = int(hy - R(k) * R(0.9) + R(0.5))
            if px >= 0 and px < 32 and py >= 0 and py < 8:
                self.screen.pixel(px, py, self.mix(0, self.mc[k], int(R(100 - k * 15) * df)))
            k += 1

    # differs per version ------------------------------------------------------------
    def init(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def compute_phase(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


class MoonV20(_MoonPage):
    """``moon-panel-v2.0.ax``, v2.0 (the moon before 2026-10-10)."""

    SOURCE = V20_PATH

    def init(self) -> None:
        tb = self.tb
        self.illum = 50
        self.wax = True
        self.phf = R(0.5)
        self.pnm = "MOON"
        self.pcts = "50%"
        self.valid = False
        self.dim = 100
        self.lastd = -999
        self.lpc = -999000
        self.lh = -99
        self.ecl = 0
        self.litc = 0xEEE3CE
        self.midc = 0x717988
        self.shown = self.now_ms()
        self.last_shown = -1
        self.ct = list(tb.ct_default)

        self.hw = [R(v) for v in tb.hw]
        self.span0 = list(tb.span0)
        self.mc = list(tb.mc)
        self.sc = list(tb.sc)
        self.far = list(tb.far)
        self.near = list(tb.near)
        self.neb = tb.neb
        self.pal = [0] * 16
        self.map = bytearray([RESIZE_FILL]) * 256  # bytes() then resize(256)

        self.update_hour_tint()
        self.compute_phase()
        self.compute_position()
        self.build_pal()
        self.build_map()

    def compute_phase(self) -> None:
        self._phase()

    def compute_position(self) -> None:
        self._position()

    def build_map(self) -> None:
        d = tmod(tdiv(self.now_ms(), 3400), 64)
        if d == self.lastd:
            return
        self.lastd = d
        idx = 0
        y = 0
        nl = len(self.neb)
        while y < 8:
            yo = y * 64
            x = 0
            while x < 32:
                n = self.neb[tmod(yo + tmod(x + d, 64), nl)]
                if n > 0:
                    n = tdiv(n * (6 + y), 13)
                    if n > 15:
                        n = 15
                self.map[idx] = n
                idx += 1
                x += 1
            y += 1

    def draw_nebula(self) -> None:
        pix = self.screen.pixel
        pal = self.pal
        m = self.map
        idx = 0
        y = 0
        while y < 8:
            x = 0
            while x < 32:
                v = m[idx]
                if v > 0:
                    pix(x, y, pal[v - 1])
                idx += 1
                x += 1
            y += 1

    def draw_stars(self, t: int, cam, df) -> None:
        far, near, sc = self.far, self.near, self.sc
        i = 0
        while i < 16:
            i4 = i * 4
            sx = int(R(far[i4]) - cam * R(0.34) + R(0.5))
            if sx >= 0 and sx < 32:
                pr = R(tmod(t + i * 370, far[i4 + 2])) / R(far[i4 + 2])
                tr = pr * R(2.0) if pr < R(0.5) else R(2.0) - pr * R(2.0)
                tw = self.sn(R(t) / R(2400.0) + R(i) * R(0.4)) * R(0.3) + R(0.7)
                amp = int((R(20.0) + R(80.0) * (tr * tr * tr)) * tw)
                self.screen.pixel(sx, far[i4 + 1], self.mix(0, sc[far[i4 + 3]], int(R(amp) * df)))
            i += 1
        i = 0
        while i < 8:
            i4 = i * 4
            sx2 = int(R(near[i4]) - cam + R(0.5))
            if sx2 >= 0 and sx2 < 32:
                pr2 = R(tmod(t + (i + 20) * 370, near[i4 + 2])) / R(near[i4 + 2])
                tr2 = pr2 * R(2.0) if pr2 < R(0.5) else R(2.0) - pr2 * R(2.0)
                tw2 = self.sn(R(t) / R(9000.0) + R(i + 20) * R(0.35)) * R(0.25) + R(0.75)
                amp2 = int((R(24.0) + R(76.0) * (tr2 * tr2)) * tw2)
                self.screen.pixel(sx2, near[i4 + 1], self.mix(0, sc[near[i4 + 3]], int(R(amp2) * df)))
            i += 1

    def draw_moon_body(self, cam) -> None:
        bx = int(R(12.0) - cam + R(0.5))
        if self.illum >= 96 and bx > -10 and bx < 32:
            hc = self.mix(0, self.litc, 8)
            hr = 0
            while hr < 8:
                hr2 = hr * 2
                self.fade_pixel(bx + self.span0[hr2] - 1, hr, hc)
                self.fade_pixel(bx + self.span0[hr2] + self.span0[hr2 + 1], hr, hc)
                hr += 1
        if bx <= -9 or bx >= 32:
            return

        tcos = R(1.0) - R(2.0) * (R(self.illum) / R(100.0))
        dc = 0x3A3E4B
        if self.illum < 20:
            dc = self.mix(0x3A3E4B, 0x51586B, (20 - self.illum) * 5)
        if self.ecl > 0:
            dc = self.mix(dc, 0x2A0A06, self.ecl)

        r = 0
        while r < 8:
            eg = tcos * self.hw[r]
            r2 = r * 2
            c = self.span0[r2]
            last = c + self.span0[r2 + 1] - 1
            while c <= last:
                xn = R(c) - R(3.5)
                d = (xn - eg) if self.wax else (-eg - xn)
                if d >= R(0.25):
                    self.fade_pixel(bx + c, r, self.litc)
                elif d >= R(-0.75):
                    self.fade_pixel(bx + c, r, self.midc)
                else:
                    pc = dc
                    if self.illum < 40:
                        pc = self.mix(dc, 0x1C2838, (40 - self.illum) * 2)
                    self.fade_pixel(bx + c, r, pc)
                c += 1
            r += 1

    def draw_labels(self, cam) -> None:
        nx = int(R(34.0) - cam + R(0.5))
        if nx > -20 and nx < 32:
            self.screen.text(nx, 6, self.pnm, 0xE87A5A if self.ecl > 0 else 0xEEE3CE)
        pw = self.screen.text_ink_width(self.pcts)
        px = int(R(62.0) - R(pw) - cam + R(0.5))
        if px > -20 and px < 32:
            self.screen.text(px, 6, self.pcts, 0x8C9CBA)


# Bit layout of v2.1's packed caches.  test_moon_panel_sim checks that the
# Berry source spells exactly these shifts and masks.
RUN_X1_SHIFT = 5
RUN_Y_SHIFT = 10
RUN_PAL_SHIFT = 13
RUN_X_MASK = 31
RUN_Y_MASK = 7
SEG_BITS = 9
SEG_PRESENT = 0x100
SEG_CLASS_SHIFT = 6
SEG_X1_SHIFT = 3
SEG_X_MASK = 7
SEG_CLASS_MASK = 3
STAR_STRIDE = 4
FAR_STARS = 16
NEAR_STARS = 8
NEAR_INDEX_OFFSET = 4  # v2.0's near stars used i + 20, i = 0..7; here i = 16..23
PERIOD_UNIT_MS = 100


class MoonV21(_MoonPage):
    """``moon-panel.ax``, v2.1 (installed since 2026-10-10), line by line."""

    SOURCE = V21_PATH

    def init(self) -> None:
        tb = self.tb
        self.illum = 50
        self.wax = True
        self.phf = R(0.5)
        self.pnm = "MOON"
        self.pcts = "50%"
        self.valid = False
        self.dim = 100
        self.lastd = -999
        self.lpc = -999000
        self.lh = -99
        self.ecl = 0
        self.litc = 0xEEE3CE
        self.midc = 0x717988
        self.shown = self.now_ms()
        self.last_shown = -1
        self.ct = list(tb.ct_default)

        self.hw = [R(v) for v in tb.hw]
        self.span0 = list(tb.span0)
        self.mc = list(tb.mc)
        self.sc = list(tb.sc)
        self.st = tb.st
        self.neb = decode_rle(tb.rle, tb.neb_size)
        self.pal = [0] * 16
        self.nr = [0] * tb.run_slots
        self.nn = 0
        self.dr = [0] * tb.disc_rows
        self.dk = 0x3A3E4B
        self.hc = 0
        self.pw = 0
        self.pws = None
        # Diagnostics, not page state: the most runs any build_map produced.
        self.max_runs = 0

        self.update_hour_tint()
        self.build_disc()
        self.compute_phase()
        self.compute_position()
        self.build_pal()
        self.build_map()

    def compute_phase(self) -> None:
        if self._phase():
            self.build_disc()

    def compute_position(self) -> None:
        self._position()
        self.hc = self.mix(0, self.litc, 8)

    def build_disc(self) -> None:
        dc = 0x3A3E4B
        if self.illum < 20:
            dc = self.mix(0x3A3E4B, 0x51586B, (20 - self.illum) * 5)
        if self.ecl > 0:
            dc = self.mix(dc, 0x2A0A06, self.ecl)
        if self.illum < 40:
            dc = self.mix(dc, 0x1C2838, (40 - self.illum) * 2)
        self.dk = dc
        tcos = R(1.0) - R(2.0) * (R(self.illum) / R(100.0))
        H, S, W, D = self.hw, self.span0, self.wax, self.dr
        r = 0
        while r < 8:
            eg = tcos * H[r]
            c = S[r * 2]
            last = c + S[r * 2 + 1] - 1
            p, sh, s, pk = 0, 0, c, -1
            while c <= last + 1:
                k = -1
                if c <= last:
                    xn = R(c) - R(3.5)
                    d = (xn - eg) if W else (-eg - xn)
                    k = 2 if d >= R(0.25) else (1 if d >= R(-0.75) else 0)
                if k != pk:
                    if pk >= 0:
                        p = p | ((SEG_PRESENT | (pk << SEG_CLASS_SHIFT) | ((c - 1) << SEG_X1_SHIFT) | s) << sh)
                        sh += SEG_BITS
                    s = c
                    pk = k
                c += 1
            D[r] = p
            r += 1

    def build_map(self) -> None:
        d = tmod(tdiv(self.now_ms(), 3400), 64)
        if d == self.lastd:
            return
        self.lastd = d
        N, Rn = self.neb, self.nr
        nl = len(N)
        cap = self.tb.run_cap
        k = 0
        y = 0
        while y < 8:
            yo, m, x, s, pv = y * 64, 6 + y, 0, 0, 0
            while x <= 32:
                n = 0
                if x < 32:
                    n = N[tmod(yo + tmod(x + d, 64), nl)]
                    if n > 0:
                        n = tdiv(n * m, 13)
                        if n > 15:
                            n = 15
                if n != pv:
                    if pv > 0 and k < cap:
                        Rn[k] = s | ((x - 1) << RUN_X1_SHIFT) | (y << RUN_Y_SHIFT) | ((pv - 1) << RUN_PAL_SHIFT)
                        k += 1
                    s = x
                    pv = n
                x += 1
            y += 1
        self.nn = k
        if k > self.max_runs:
            self.max_runs = k

    def draw_nebula(self) -> None:
        Rn, P, n = self.nr, self.pal, self.nn
        scr = self.screen
        i = 0
        while i < n:
            p = Rn[i]
            col = P[p >> RUN_PAL_SHIFT]
            if col != 0:
                a = p & RUN_X_MASK
                b = (p >> RUN_X1_SHIFT) & RUN_X_MASK
                y = (p >> RUN_Y_SHIFT) & RUN_Y_MASK
                if a == b:
                    scr.pixel(a, y, col)
                else:
                    scr.line(a, y, b, y, col)
            i += 1

    def draw_stars(self, t: int, cam, df) -> None:
        T, S = self.st, self.sc
        rt = R(t) / R(2400.0)
        cf = cam * R(0.34)
        j, a0, a1, w0, w1, o = R(0.4), R(20.0), R(80.0), R(0.3), R(0.7), 0
        i = 0
        while i < FAR_STARS + NEAR_STARS:
            if i == FAR_STARS:
                rt = R(t) / R(9000.0)
                cf = cam
                j = R(0.35)
                a0 = R(24.0)
                a1 = R(76.0)
                w0 = R(0.25)
                w1 = R(0.75)
                o = NEAR_INDEX_OFFSET
            i4 = i * STAR_STRIDE
            sx = int(R(T[i4]) - cf + R(0.5))
            if sx >= 0 and sx < 32:
                P = T[i4 + 2] * PERIOD_UNIT_MS
                pr = R(tmod(t + (i + o) * 370, P)) / R(P)
                tr = pr * R(2.0) if pr < R(0.5) else R(2.0) - pr * R(2.0)
                w = tr * tr
                if i < FAR_STARS:
                    w = w * tr
                x = rt + R(i + o) * j
                x = x - R(int(x))
                if x < R(0.0):
                    x = x + R(1.0)
                g = x > R(0.5)
                if g:
                    x = x - R(0.5)
                f = x * R(2.0)
                u = f * (R(1.0) - f)
                v = R(16.0) * u / (R(5.0) - R(4.0) * u)
                if g:
                    v = -v
                n = int(R(int((a0 + a1 * w) * (v * w0 + w1))) * df)
                k = S[T[i4 + 3]]
                self.screen.pixel(
                    sx, T[i4 + 1],
                    (tdiv(((k >> 16) & 0xFF) * n, 100) << 16) | (tdiv(((k >> 8) & 0xFF) * n, 100) << 8) | tdiv((k & 0xFF) * n, 100),
                )
            i += 1

    def draw_moon_body(self, cam) -> None:
        bx = int(R(12.0) - cam + R(0.5))
        if self.illum >= 96 and bx > -10 and bx < 32:
            hc, S = self.hc, self.span0
            hr = 0
            while hr < 8:
                hr2 = hr * 2
                self.fade_pixel(bx + S[hr2] - 1, hr, hc)
                self.fade_pixel(bx + S[hr2] + S[hr2 + 1], hr, hc)
                hr += 1
        if bx <= -9 or bx >= 32:
            return
        c0, c1, c2, D = self.dk, self.midc, self.litc, self.dr
        scr = self.screen
        r = 0
        while r < 8:
            p = D[r]
            while p > 0:
                a = bx + (p & SEG_X_MASK)
                b = bx + ((p >> SEG_X1_SHIFT) & SEG_X_MASK)
                k = (p >> SEG_CLASS_SHIFT) & SEG_CLASS_MASK
                col = c2 if k == 2 else (c1 if k == 1 else c0)
                if a < 3:
                    x = 1 if a < 1 else a
                    while x < 3 and x <= b:
                        self.fade_pixel(x, r, col)
                        x += 1
                    a = 3
                if a < b:
                    scr.line(a, r, b, r, col)
                elif a == b:
                    scr.pixel(a, r, col)
                p = p >> SEG_BITS
            r += 1

    def draw_labels(self, cam) -> None:
        nx = int(R(34.0) - cam + R(0.5))
        if nx > -20 and nx < 32:
            self.screen.text(nx, 6, self.pnm, 0xE87A5A if self.ecl > 0 else 0xEEE3CE)
        if self.pws != self.pcts:
            self.pw = self.screen.text_ink_width(self.pcts)
            self.pws = self.pcts
        px = int(R(62.0) - R(self.pw) - cam + R(0.5))
        if px > -20 and px < 32:
            self.screen.text(px, 6, self.pcts, 0x8C9CBA)


VERSIONS = {"2.0": MoonV20, "2.1": MoonV21}


# --- the sources themselves -------------------------------------------------------

_PROGRAMS: dict[tuple[str, object], object] = {}


class BerryMoon:
    """Runs a Moon ``.ax`` file's own text through ``berry_lite``.

    Same interface as the emulators (``clock``, ``screen``, ``loop()``,
    ``on_show()``, ``draw() -> Frame``), so a test can hold the exact source
    that will be uploaded against the Python transcriptions above.
    """

    def __init__(self, path: Path, clock: Clock, *, font: Font | None = None) -> None:
        try:
            from . import berry_lite  # type: ignore[import-not-found]
        except ImportError:
            import berry_lite  # type: ignore[no-redef]

        source = path.read_text(encoding="utf-8")
        key = (source, R)
        program = _PROGRAMS.get(key)
        if program is None:
            program = berry_lite.Program(source, real_type=R, resize_fill=RESIZE_FILL)
            _PROGRAMS[key] = program
        self.clock = clock
        self.screen = Screen(font)
        scr = self.screen
        natives = {
            "now_ms": lambda: clock.now_ms,
            "epoch_ms": lambda: clock.epoch_ms,
            "year": lambda: clock.year,
            "hour": lambda: clock.hour,
            "minute": lambda: clock.minute,
            "clear": scr.clear,
            "pixel": scr.pixel,
            "line": scr.line,
            "text": scr.text,
            "text_ink_width": scr.text_ink_width,
        }
        self.page = program.run(natives)  # type: ignore[attr-defined]

    def on_show(self) -> None:
        self.page.on_show()

    def loop(self) -> None:
        self.page.loop()

    def should_show(self) -> bool:
        return bool(self.page.should_show())

    def draw(self) -> Frame:
        self.page.draw()
        return self.screen.frame()


def render(version: str, t_ms: int, *, epoch_ms: int, hour: int, minute: int, year: int = 2026,
           loop_ms: int | None = None, shown_ms: int | None = None,
           phase: tuple[int, float, int] | None = None, font: Font | None = None) -> Frame:
    """One frame of a freshly installed page.

    The page is constructed (``init``) and shown (``on_show``) at
    ``shown_ms``, runs one ``loop()`` at ``loop_ms`` and draws at ``t_ms``.
    Defaults: ``loop_ms = t_ms`` and ``shown_ms = t_ms``, i.e. the camera at
    rest on the disc (``c = 0``).
    """

    if loop_ms is None:
        loop_ms = t_ms
    if shown_ms is None:
        shown_ms = t_ms
    clock = Clock(shown_ms, epoch_ms, year, hour, minute)
    page = VERSIONS[version](clock, font=font, phase=phase)
    page.on_show()
    clock.now_ms = loop_ms
    page.loop()
    clock.now_ms = t_ms
    return page.draw()
