"""Run a panel page's Berry source offline: a small Berry-subset compiler.

The panel is the only real Berry, so a hand-written Python mirror of a page
can only ever be as faithful as its transcription.  This module removes the
transcription: it parses the ``.ax`` text itself and compiles it to Python,
so a test can execute the exact bytes that will be uploaded.

Covered: top-level ``var``, ``class`` with ``var`` members and ``def``
methods, ``def``, ``var`` lists, ``if/elif/else``, ``while``, ``for x : list``,
inclusive integer ``a..b`` ranges (default increment 1), ``break``, ``continue``,
``return``, ``= += -= *= /= %=``, the ternary, every
arithmetic, bitwise, comparison and logical operator with Berry's precedence
(bitwise binds tighter than comparison), calls, members, indexing, list
literals, ``nil true false self``, ``int() real() str() bytes()``, and the
list/bytes methods ``push size resize`` and typed bytes ``get/geti set/seti
getfloat/setfloat``. Same-scope local redeclarations are rejected. Anything else is a compile error,
so an unsupported construct cannot be silently misread.

Semantics, as on this firmware (single-precision Berry), with one deliberate
exception marked below:

* reals are float32 (``real_type``, numpy.float32 or a compatible type);
  an int meeting a real is converted first;
* ``int / int`` and ``%`` truncate toward zero; division by zero raises;
* ``&&`` / ``||`` / ``!`` yield booleans; nil, false, 0 and 0.0 are false;
* comparing different types with ``==`` is false, not an error;
* ``bytes("hex")`` drops an odd trailing nibble;
* ``resize`` fills new list slots with nil, and new bytes with
  ``resize_fill``.  That fill is NOT a firmware fact: it is a pessimistic
  stand-in, non-zero by default so that a page which reads grown bytes
  before writing them shows the dependency.  Upstream Berry's be_byteslib
  zero-fills grown bytes, and what this panel's build does has never been
  observed (v2.0's ``map.resize(256)`` only shows that resize sets the size);
* indexing out of range raises (Berry's index_error).
* typed byte accesses follow AWTRIX NG 1.2.2's byte library: full-width
  bounds checks, negative offsets, endian selection and binary32 float storage;
  unlike indexing, out-of-range typed reads return zero and writes do nothing.
* bytes are indexable but have no iterator in this firmware's byte library;
  a for-loop over bytes raises instead of borrowing Python's bytearray iterator.

Natives (``pixel``, ``now_ms``, ...) are supplied by the caller.
"""

from __future__ import annotations

import math
import re
import struct
from typing import Any, Callable

__all__ = ["BerryError", "BerryBytes", "Program", "compile_source"]


class BerryError(Exception):
    """A Berry runtime or compile error."""


class BerryBytes(bytearray):
    """Berry ``bytes``: a mutable byte buffer, distinct from strings."""


class BerryObject:
    """Base of every compiled Berry class."""

    __berry_members__: tuple[str, ...] = ()


# --- lexer --------------------------------------------------------------------

_TOKEN = re.compile(
    r"""
    (?P<ws>[ \t\r\n]+)
  | (?P<comment>\#-.*?-\#|\#[^\n]*)
  | (?P<real>(?:\d+\.(?!\.)\d*|\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)
  | (?P<int>0[xX][0-9A-Fa-f]+|\d+)
  | (?P<str>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><<=|>>=|\.\.|<<|>>|<=|>=|==|!=|&&|\|\||\+=|-=|\*=|/=|%=|&=|\|=|\^=|[-+*/%&|^~!<>=()\[\]{},.:?])
    """,
    re.VERBOSE | re.DOTALL,
)

KEYWORDS = {
    "var", "def", "class", "end", "if", "elif", "else", "while", "for", "return",
    "break", "continue", "nil", "true", "false", "self", "import", "static", "do",
    "try", "except", "raise", "as",
}


def tokenize(source: str) -> list[tuple[str, Any, int]]:
    out: list[tuple[str, Any, int]] = []
    pos = 0
    line = 1
    while pos < len(source):
        m = _TOKEN.match(source, pos)
        if not m:
            raise BerryError(f"line {line}: cannot read {source[pos:pos + 20]!r}")
        kind = m.lastgroup
        text = m.group()
        if kind == "real":
            out.append(("real", text, line))
        elif kind == "int":
            out.append(("int", int(text, 0), line))
        elif kind == "str":
            body = text[1:-1]
            body = re.sub(r"\\(.)", lambda e: {"n": "\n", "t": "\t", "r": "\r"}.get(e.group(1), e.group(1)), body)
            out.append(("str", body, line))
        elif kind == "name":
            out.append(("kw" if text in KEYWORDS else "name", text, line))
        elif kind == "op":
            out.append(("op", text, line))
        line += text.count("\n")
        pos = m.end()
    out.append(("eof", None, line))
    return out


# --- parser ---------------------------------------------------------------------
# AST nodes are tuples: (kind, ...).

# Berry precedence, loosest first (the Berry manual's operator table).
_BINARY = [
    ("||",),
    ("&&",),
    ("==", "!="),
    ("<", "<=", ">", ">="),
    ("..",),
    ("|",),
    ("^",),
    ("&",),
    ("<<", ">>"),
    ("+", "-"),
    ("*", "/", "%"),
]
_ASSIGN = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}
_BLOCK_END = {"end", "elif", "else", "except"}


class _Parser:
    def __init__(self, tokens: list[tuple[str, Any, int]]) -> None:
        self.t = tokens
        self.i = 0

    # helpers
    def peek(self, k: int = 0) -> tuple[str, Any, int]:
        return self.t[self.i + k]

    def take(self) -> tuple[str, Any, int]:
        tok = self.t[self.i]
        self.i += 1
        return tok

    def at(self, kind: str, value: Any = None) -> bool:
        tok = self.t[self.i]
        return tok[0] == kind and (value is None or tok[1] == value)

    def accept(self, kind: str, value: Any = None) -> bool:
        if self.at(kind, value):
            self.i += 1
            return True
        return False

    def expect(self, kind: str, value: Any = None) -> tuple[str, Any, int]:
        if not self.at(kind, value):
            tok = self.peek()
            raise BerryError(f"line {tok[2]}: expected {value or kind}, found {tok[1]!r}")
        return self.take()

    def name(self) -> str:
        return self.expect("name")[1]

    # statements
    def program(self) -> list:
        body = []
        while not self.at("eof"):
            body.append(self.statement())
        return body

    def block(self) -> list:
        body = []
        while not (self.at("eof") or (self.peek()[0] == "kw" and self.peek()[1] in _BLOCK_END)):
            body.append(self.statement())
        return body

    def statement(self):
        tok = self.peek()
        line = tok[2]
        if tok[0] == "kw":
            kw = tok[1]
            if kw == "var":
                self.take()
                decls = []
                while True:
                    n = self.name()
                    init = self.expr() if self.accept("op", "=") else None
                    decls.append((n, init))
                    if not self.accept("op", ","):
                        break
                return ("var", decls, line)
            if kw == "def":
                self.take()
                return self.function(self.name(), line)
            if kw == "class":
                self.take()
                return self.klass(line)
            if kw == "if":
                self.take()
                arms = [(self.expr(), self.block())]
                orelse = None
                while True:
                    if self.accept("kw", "elif"):
                        arms.append((self.expr(), self.block()))
                    elif self.accept("kw", "else"):
                        orelse = self.block()
                    else:
                        break
                self.expect("kw", "end")
                return ("if", arms, orelse, line)
            if kw == "while":
                self.take()
                cond = self.expr()
                body = self.block()
                self.expect("kw", "end")
                return ("while", cond, body, line)
            if kw == "for":
                self.take()
                n = self.name()
                self.expect("op", ":")
                it = self.expr()
                body = self.block()
                self.expect("kw", "end")
                return ("for", n, it, body, line)
            if kw == "return":
                self.take()
                nxt = self.peek()
                if nxt[0] == "eof" or (nxt[0] == "kw" and nxt[1] in _BLOCK_END):
                    return ("return", None, line)
                return ("return", self.expr(), line)
            if kw in ("break", "continue"):
                self.take()
                return (kw, line)
            if kw not in ("nil", "true", "false", "self"):
                raise BerryError(f"line {line}: unsupported statement {kw!r}")
        target = self.expr()
        if self.peek()[0] == "op" and self.peek()[1] in _ASSIGN:
            op = self.take()[1]
            if target[0] not in ("name", "member", "index"):
                raise BerryError(f"line {line}: cannot assign to {target[0]}")
            return ("assign", op, target, self.expr(), line)
        return ("expr", target, line)

    def function(self, name: str, line: int):
        self.expect("op", "(")
        params = []
        if not self.at("op", ")"):
            while True:
                params.append(self.name())
                if not self.accept("op", ","):
                    break
        self.expect("op", ")")
        body = self.block()
        self.expect("kw", "end")
        return ("def", name, params, body, line)

    def klass(self, line: int):
        name = self.name()
        if self.accept("op", ":"):
            raise BerryError(f"line {line}: class inheritance is not supported")
        members: list[str] = []
        defs = []
        while not self.accept("kw", "end"):
            if self.accept("kw", "var"):
                while True:
                    members.append(self.name())
                    if not self.accept("op", ","):
                        break
            elif self.accept("kw", "def"):
                defs.append(self.function(self.name(), self.peek()[2]))
            else:
                tok = self.peek()
                raise BerryError(f"line {tok[2]}: unexpected {tok[1]!r} in class body")
        return ("class", name, members, defs, line)

    # expressions
    def expr(self):
        cond = self.binary(0)
        if self.accept("op", "?"):
            a = self.expr()
            self.expect("op", ":")
            b = self.expr()
            return ("ternary", cond, a, b)
        return cond

    def binary(self, level: int):
        if level == len(_BINARY):
            return self.unary()
        left = self.binary(level + 1)
        ops = _BINARY[level]
        while self.peek()[0] == "op" and self.peek()[1] in ops:
            op = self.take()[1]
            right = self.binary(level + 1)
            left = ("bin", op, left, right)
        return left

    def unary(self):
        if self.peek()[0] == "op" and self.peek()[1] in ("-", "!", "~"):
            op = self.take()[1]
            operand = self.unary()
            if op == "-" and operand[0] in ("int", "real"):
                value = operand[1]
                return (operand[0], -value if operand[0] == "int" else "-" + value)
            return ("un", op, operand)
        return self.postfix(self.primary())

    def postfix(self, node):
        while True:
            if self.accept("op", "("):
                args = []
                if not self.at("op", ")"):
                    while True:
                        args.append(self.expr())
                        if not self.accept("op", ","):
                            break
                self.expect("op", ")")
                node = ("call", node, args)
            elif self.accept("op", "."):
                node = ("member", node, self.name())
            elif self.accept("op", "["):
                idx = self.expr()
                self.expect("op", "]")
                node = ("index", node, idx)
            else:
                return node

    def primary(self):
        tok = self.take()
        kind, value, line = tok
        if kind == "int":
            return ("int", value)
        if kind == "real":
            return ("real", value)
        if kind == "str":
            return ("str", value)
        if kind == "name":
            return ("name", value)
        if kind == "kw":
            if value == "nil":
                return ("const", None)
            if value == "true":
                return ("const", True)
            if value == "false":
                return ("const", False)
            if value == "self":
                return ("self",)
        if kind == "op" and value == "(":
            node = self.expr()
            self.expect("op", ")")
            return node
        if kind == "op" and value == "[":
            items = []
            if not self.at("op", "]"):
                while True:
                    items.append(self.expr())
                    if not self.accept("op", ","):
                        break
            self.expect("op", "]")
            return ("list", items)
        raise BerryError(f"line {line}: unexpected {value!r}")


# --- runtime ----------------------------------------------------------------------


def _make_runtime(real_type: Callable[[Any], Any], resize_fill: int) -> dict[str, Any]:
    RT = real_type

    def is_real(v: Any) -> bool:
        return type(v) is RT

    def to_real(v: Any):
        if type(v) is int:
            return RT(v)
        if is_real(v):
            return v
        raise BerryError(f"type_error: number expected, got {_tname(v)}")

    def num(v: Any) -> bool:
        return type(v) is int or is_real(v)

    def b_add(a, b):
        if type(a) is int and type(b) is int:
            return a + b
        if type(a) is str and type(b) is str:
            return a + b
        return to_real(a) + to_real(b)

    def b_sub(a, b):
        if type(a) is int and type(b) is int:
            return a - b
        return to_real(a) - to_real(b)

    def b_mul(a, b):
        if type(a) is int and type(b) is int:
            return a * b
        return to_real(a) * to_real(b)

    def b_div(a, b):
        if type(a) is int and type(b) is int:
            if b == 0:
                raise BerryError("divzero_error: division by zero")
            q = abs(a) // abs(b)
            return q if (a >= 0) == (b >= 0) else -q
        y = to_real(b)
        if y == RT(0.0):
            raise BerryError("divzero_error: division by zero")
        return to_real(a) / y

    def b_mod(a, b):
        if type(a) is int and type(b) is int:
            if b == 0:
                raise BerryError("divzero_error: division by zero")
            q = abs(a) // abs(b)
            q = q if (a >= 0) == (b >= 0) else -q
            return a - q * b
        return RT(math.fmod(float(to_real(a)), float(to_real(b))))

    def ints(a, b, op):
        if type(a) is not int or type(b) is not int:
            raise BerryError(f"type_error: {op} needs ints, got {_tname(a)} and {_tname(b)}")

    def b_shl(a, b):
        ints(a, b, "<<")
        return a << b

    def b_shr(a, b):
        ints(a, b, ">>")
        return a >> b

    def b_and(a, b):
        ints(a, b, "&")
        return a & b

    def b_or(a, b):
        ints(a, b, "|")
        return a | b

    def b_xor(a, b):
        ints(a, b, "^")
        return a ^ b

    def compare(a, b, op):
        if type(a) is int and type(b) is int:
            return a, b
        if num(a) and num(b):
            return to_real(a), to_real(b)
        if type(a) is str and type(b) is str:
            return a, b
        raise BerryError(f"type_error: {op} on {_tname(a)} and {_tname(b)}")

    def b_lt(a, b):
        x, y = compare(a, b, "<")
        return bool(x < y)

    def b_le(a, b):
        x, y = compare(a, b, "<=")
        return bool(x <= y)

    def b_gt(a, b):
        x, y = compare(a, b, ">")
        return bool(x > y)

    def b_ge(a, b):
        x, y = compare(a, b, ">=")
        return bool(x >= y)

    def b_eq(a, b):
        if num(a) and num(b):
            if type(a) is int and type(b) is int:
                return a == b
            return bool(to_real(a) == to_real(b))
        if type(a) is not type(b):
            return False
        if isinstance(a, BerryObject):
            return a is b
        return bool(a == b)

    def b_ne(a, b):
        return not b_eq(a, b)

    def truth(v) -> bool:
        if v is None or v is False:
            return False
        if v is True:
            return True
        if type(v) is int:
            return v != 0
        if is_real(v):
            return bool(v != RT(0.0))
        return True

    def b_neg(a):
        if type(a) is int:
            return -a
        return -to_real(a)

    def b_inv(a):
        if type(a) is not int:
            raise BerryError("type_error: ~ needs an int")
        return ~a

    def index(o, i):
        if type(i) is not int:
            raise BerryError(f"type_error: index must be int, got {_tname(i)}")
        if isinstance(o, (list, bytearray, str)):
            if i < 0 or i >= len(o):
                raise BerryError(f"index_error: index {i} out of range (size {len(o)})")
            return o[i]
        raise BerryError(f"type_error: cannot index {_tname(o)}")

    def setindex(o, i, v):
        if type(i) is not int:
            raise BerryError(f"type_error: index must be int, got {_tname(i)}")
        if isinstance(o, BerryBytes):
            if i < 0 or i >= len(o):
                raise BerryError(f"index_error: index {i} out of range (size {len(o)})")
            if type(v) is not int:
                raise BerryError("type_error: bytes value must be int")
            o[i] = v & 0xFF
            return
        if isinstance(o, list):
            if i < 0 or i >= len(o):
                raise BerryError(f"index_error: index {i} out of range (size {len(o)})")
            o[i] = v
            return
        raise BerryError(f"type_error: cannot index-assign {_tname(o)}")

    def member(o, name):
        if isinstance(o, BerryObject):
            try:
                return getattr(o, name)
            except AttributeError:
                raise BerryError(f"attribute_error: {type(o).__name__} has no '{name}'") from None
        raise BerryError(f"attribute_error: {_tname(o)} has no member '{name}'")

    def setmember(o, name, v):
        if isinstance(o, type) and issubclass(o, BerryObject):
            if not hasattr(o, name):
                raise BerryError(f"attribute_error: class has no member '{name}'")
            setattr(o, name, v)
            return
        if isinstance(o, BerryObject):
            if name not in o.__berry_members__:
                raise BerryError(f"attribute_error: {type(o).__name__} has no member '{name}'")
            setattr(o, name, v)
            return
        raise BerryError(f"attribute_error: cannot set '{name}' on {_tname(o)}")

    def mcall(o, name, args):
        if isinstance(o, BerryObject):
            return member(o, name)(*args)
        if isinstance(o, BerryBytes):
            if name in ('get', 'geti', 'set', 'seti', 'getfloat', 'setfloat'):
                reading = name.startswith('get')
                floating = name.endswith('float')
                minimum = 1 if reading else 2
                if len(args) not in (minimum, minimum + 1) or type(args[0]) is not int:
                    raise BerryError('type_error: invalid typed bytes arguments')
                offset = args[0] if args[0] >= 0 else len(o) + args[0]
                option = args[minimum] if len(args) > minimum else (False if floating else 1)
                if floating:
                    count = 4
                    endian = '>' if truth(option) else '<'
                else:
                    if reading and type(option) is not int:
                        option = 1  # Firmware get/geti ignore a non-integer optional size.
                    if type(option) is not int or not -4 <= option <= 4:
                        raise BerryError('type_error: invalid typed bytes size')
                    count = abs(option)
                    endian = '>' if option < 0 else '<'
                fits = count > 0 and offset >= 0 and offset + count <= len(o)
                if reading:
                    if not fits:
                        return RT(0) if floating else 0
                    raw = o[offset:offset + count]
                    if floating:
                        return RT(struct.unpack(endian + 'f', raw)[0])
                    # Firmware m_get uses int32_t even for unsigned 4-byte reads.
                    return int.from_bytes(raw, 'big' if endian == '>' else 'little',
                                          signed=name == 'geti' or count == 4)
                value = args[1]
                if floating:
                    if type(value) is not int and not is_real(value):
                        raise BerryError('type_error: setfloat needs an int or real')
                    raw = struct.pack(endian + 'f', float(value))
                else:
                    if type(value) is not int:
                        raise BerryError('type_error: set needs an int')
                    raw = (value & ((1 << (count * 8)) - 1)).to_bytes(count, 'big' if endian == '>' else 'little')
                if fits:
                    o[offset:offset + count] = raw
                return None
            if name == "size" and not args:
                return len(o)
            if name == "resize" and len(args) == 1 and type(args[0]) is int:
                n = max(0, args[0])
                if n < len(o):
                    del o[n:]
                else:
                    o.extend([resize_fill] * (n - len(o)))
                return o
        elif isinstance(o, list):
            if name == "push" and len(args) == 1:
                o.append(args[0])
                return o
            if name == "size" and not args:
                return len(o)
            if name == "resize" and len(args) == 1 and type(args[0]) is int:
                n = max(0, args[0])
                if n < len(o):
                    del o[n:]
                else:
                    o.extend([None] * (n - len(o)))
                return None
        elif isinstance(o, str):
            pass
        raise BerryError(f"attribute_error: {_tname(o)}.{name}() is not modelled")

    def iterate(o):
        if isinstance(o, range):
            return o
        if isinstance(o, list):
            return list(o)
        raise BerryError(f"type_error: cannot iterate {_tname(o)}")

    def integer_range(lower, upper):
        ints(lower, upper, '..')
        # Berry literals include both bounds, with default increment 1.
        # https://berry.readthedocs.io/en/latest/source/en/Chapter-2.html#range
        return range(lower, upper + 1)

    def b_int(v=0):
        if type(v) is int:
            return v
        if v is True or v is False:
            return int(v)
        if type(v) is str:
            return int(v, 0)
        if is_real(v):
            return int(v)
        raise BerryError(f"type_error: int({_tname(v)})")

    def b_real(v=0):
        if type(v) is str:
            return RT(float(v))
        if v is True or v is False:
            return RT(int(v))
        return to_real(v)

    def b_str(v=""):
        if type(v) is str:
            return v
        if v is None:
            return "nil"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if type(v) is int:
            return str(v)
        if is_real(v):
            return "%g" % float(v)
        raise BerryError(f"str({_tname(v)}) is not modelled")

    def b_bytes(v=None):
        if v is None:
            return BerryBytes()
        if type(v) is int:
            return BerryBytes()  # a size hint only: the buffer is still empty
        if type(v) is str:
            if not re.fullmatch(r"[0-9A-Fa-f]*", v):
                raise BerryError("value_error: bytes() takes hex")
            return BerryBytes(bytes.fromhex(v[: len(v) // 2 * 2]))
        raise BerryError(f"type_error: bytes({_tname(v)})")

    return {
        "_add": b_add, "_sub": b_sub, "_mul": b_mul, "_div": b_div, "_mod": b_mod,
        "_shl": b_shl, "_shr": b_shr, "_and": b_and, "_or": b_or, "_xor": b_xor,
        "_lt": b_lt, "_le": b_le, "_gt": b_gt, "_ge": b_ge, "_eq": b_eq, "_ne": b_ne,
        "_truth": truth, "_neg": b_neg, "_inv": b_inv,
        "_index": index, "_setindex": setindex, "_member": member, "_setmember": setmember,
        "_mcall": mcall, "_iter": iterate, "_range": integer_range, "_RT": RT,
        "B_int": b_int, "B_real": b_real, "B_str": b_str, "B_bytes": b_bytes,
        "BerryObject": BerryObject, "BerryError": BerryError,
    }


def _tname(v: Any) -> str:
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, str):
        return "string"
    if isinstance(v, BerryBytes):
        return "bytes"
    if isinstance(v, list):
        return "list"
    if isinstance(v, BerryObject):
        return "instance"
    return "real"


# --- code generation ----------------------------------------------------------------

_BINOP = {
    "+": "_add", "-": "_sub", "*": "_mul", "/": "_div", "%": "_mod",
    "<<": "_shl", ">>": "_shr", "&": "_and", "|": "_or", "^": "_xor",
    "<": "_lt", "<=": "_le", ">": "_gt", ">=": "_ge", "==": "_eq", "!=": "_ne",
}
_BUILTINS = {"int": "B_int", "real": "B_real", "str": "B_str", "bytes": "B_bytes"}


class _Scope:
    def __init__(self, parent: "_Scope | None") -> None:
        self.parent = parent
        self.names: dict[str, str] = {}

    def lookup(self, name: str) -> str | None:
        s: _Scope | None = self
        while s is not None:
            if name in s.names:
                return s.names[name]
            s = s.parent
        return None


class _Gen:
    def __init__(self, real_literal: Callable[[str], Any]) -> None:
        self.lines: list[str] = []
        self.counter = 0
        self.consts: dict[str, Any] = {}
        self.real_literal = real_literal
        self.in_method = False

    def emit(self, depth: int, text: str) -> None:
        self.lines.append("    " * depth + text)

    def fresh(self, name: str) -> str:
        self.counter += 1
        return f"v_{name}_{self.counter}"

    def const(self, value: Any) -> str:
        key = f"K{len(self.consts)}"
        self.consts[key] = value
        return key

    # expressions
    def expr(self, node, scope: _Scope) -> str:
        kind = node[0]
        if kind == "int":
            return repr(node[1])
        if kind == "real":
            return self.const(self.real_literal(node[1]))
        if kind == "str":
            return repr(node[1])
        if kind == "const":
            return repr(node[1])
        if kind == "self":
            if not self.in_method:
                raise BerryError("self outside a method")
            return "self"
        if kind == "name":
            local = scope.lookup(node[1])
            if local is not None:
                return local
            if node[1] in _BUILTINS:
                return _BUILTINS[node[1]]
            return f"G[{node[1]!r}]"
        if kind == "list":
            return "[" + ", ".join(self.expr(e, scope) for e in node[1]) + "]"
        if kind == "bin":
            op, a, b = node[1], node[2], node[3]
            if op == "&&":
                return f"(_truth({self.expr(a, scope)}) and _truth({self.expr(b, scope)}))"
            if op == "||":
                return f"(_truth({self.expr(a, scope)}) or _truth({self.expr(b, scope)}))"
            if op == "..":
                return f"_range({self.expr(a, scope)}, {self.expr(b, scope)})"
            return f"{_BINOP[op]}({self.expr(a, scope)}, {self.expr(b, scope)})"
        if kind == "un":
            op, a = node[1], node[2]
            if op == "-":
                return f"_neg({self.expr(a, scope)})"
            if op == "!":
                return f"(not _truth({self.expr(a, scope)}))"
            return f"_inv({self.expr(a, scope)})"
        if kind == "ternary":
            return f"({self.expr(node[2], scope)} if _truth({self.expr(node[1], scope)}) else {self.expr(node[3], scope)})"
        if kind == "member":
            obj = node[1]
            if obj[0] == "self":
                return f"self.{node[2]}"
            return f"_member({self.expr(obj, scope)}, {node[2]!r})"
        if kind == "index":
            return f"_index({self.expr(node[1], scope)}, {self.expr(node[2], scope)})"
        if kind == "call":
            fn, args = node[1], node[2]
            argv = ", ".join(self.expr(a, scope) for a in args)
            if fn[0] == "member":
                if fn[1][0] == "self":
                    return f"self.{fn[2]}({argv})"
                return f"_mcall({self.expr(fn[1], scope)}, {fn[2]!r}, ({argv}{',' if args else ''}))"
            return f"{self.expr(fn, scope)}({argv})"
        raise BerryError(f"cannot compile {kind}")

    # statements
    def block(self, body: list, scope: _Scope, depth: int) -> None:
        start = len(self.lines)
        for st in body:
            self.statement(st, scope, depth)
        if len(self.lines) == start:
            self.emit(depth, "pass")

    def statement(self, st, scope: _Scope, depth: int) -> None:
        kind = st[0]
        if kind == "var":
            for name, init in st[1]:
                if name in scope.names:
                    raise BerryError(f"syntax_error: redefinition of '{name}'")
                value = "None" if init is None else self.expr(init, scope)
                py = self.fresh(name)
                scope.names[name] = py
                self.emit(depth, f"{py} = {value}")
        elif kind == "assign":
            op, target, value = st[1], st[2], st[3]
            rhs = self.expr(value, scope)
            if op != "=":
                rhs = f"{_BINOP[op[:-1]]}({self.expr(target, scope)}, {rhs})"
            self.assign(target, rhs, scope, depth)
        elif kind == "expr":
            self.emit(depth, self.expr(st[1], scope))
        elif kind == "if":
            first = True
            for cond, body in st[1]:
                self.emit(depth, f"{'if' if first else 'elif'} _truth({self.expr(cond, scope)}):")
                self.block(body, _Scope(scope), depth + 1)
                first = False
            if st[2] is not None:
                self.emit(depth, "else:")
                self.block(st[2], _Scope(scope), depth + 1)
        elif kind == "while":
            self.emit(depth, f"while _truth({self.expr(st[1], scope)}):")
            self.block(st[2], _Scope(scope), depth + 1)
        elif kind == "for":
            inner = _Scope(scope)
            py = self.fresh(st[1])
            inner.names[st[1]] = py
            self.emit(depth, f"for {py} in _iter({self.expr(st[2], scope)}):")
            self.block(st[3], inner, depth + 1)
        elif kind == "return":
            self.emit(depth, "return None" if st[1] is None else f"return {self.expr(st[1], scope)}")
        elif kind in ("break", "continue"):
            self.emit(depth, kind)
        elif kind == "def":
            raise BerryError(f"line {st[4]}: nested functions are not supported")
        else:
            raise BerryError(f"cannot compile statement {kind}")

    def assign(self, target, rhs: str, scope: _Scope, depth: int) -> None:
        if target[0] == "name":
            local = scope.lookup(target[1])
            if local is not None:
                self.emit(depth, f"{local} = {rhs}")
            else:
                self.emit(depth, f"G[{target[1]!r}] = {rhs}")
        elif target[0] == "member":
            if target[1][0] == "self":
                self.emit(depth, f"_setmember(self, {target[2]!r}, {rhs})")
            else:
                self.emit(depth, f"_setmember({self.expr(target[1], scope)}, {target[2]!r}, {rhs})")
        else:
            self.emit(depth, f"_setindex({self.expr(target[1], scope)}, {self.expr(target[2], scope)}, {rhs})")

    def function(self, node, depth: int, method: bool) -> str:
        _, name, params, body, _line = node
        scope = _Scope(None)
        pyparams = []
        for p in params:
            py = self.fresh(p)
            scope.names[p] = py
            pyparams.append(py)
        self.in_method = method
        head = ["self"] if method else []
        pyname = name if method else f"f_{name}"
        self.emit(depth, f"def {pyname}({', '.join(head + pyparams)}):")
        self.block(body, scope, depth + 1)
        self.in_method = False
        return pyname

    def program(self, body: list) -> None:
        top = _Scope(None)
        self.emit(0, "def __berry_main__():")
        for st in body:
            if st[0] == "class":
                _, name, members, defs, _line = st
                self.emit(1, f"class C_{name}(BerryObject):")
                self.emit(2, f"__berry_members__ = {tuple(members)!r}")
                self.emit(2, "def __init__(self, *args):")
                for m in members:
                    self.emit(3, f"object.__setattr__(self, {m!r}, None)")
                if any(d[1] == "init" for d in defs):
                    self.emit(3, "self.init(*args)")
                elif not members:
                    self.emit(3, "pass")
                for d in defs:
                    self.function(d, 2, method=True)
                self.emit(1, f"G[{name!r}] = C_{name}")
            elif st[0] == "def":
                pyname = self.function(st, 1, method=False)
                self.emit(1, f"G[{st[1]!r}] = {pyname}")
            elif st[0] == "var":
                for name, init in st[1]:
                    value = "None" if init is None else self.expr(init, top)
                    self.emit(1, f"G[{name!r}] = {value}")
            elif st[0] == "return":
                self.emit(1, "return None" if st[1] is None else f"return {self.expr(st[1], top)}")
            elif st[0] == "expr":
                self.emit(1, self.expr(st[1], top))
            else:
                self.statement(st, top, 1)
        self.emit(1, "return None")


class Program:
    """A compiled Berry source.  ``run()`` executes it and returns its result."""

    def __init__(self, source: str, *, real_type: Callable[[Any], Any], resize_fill: int = 0xA5,
                 real_literal: Callable[[str], Any] | None = None) -> None:
        tree = _Parser(tokenize(source)).program()
        gen = _Gen(real_literal or (lambda text: real_type(float(text))))
        gen.program(tree)
        self.python = "\n".join(gen.lines) + "\n"
        self.consts = gen.consts
        self.runtime = _make_runtime(real_type, resize_fill)
        self.code = compile(self.python, "<berry>", "exec")

    def run(self, natives: dict[str, Callable[..., Any]]) -> Any:
        """Execute the top level with ``natives`` as globals; return its value."""

        G: dict[str, Any] = dict(natives)
        env: dict[str, Any] = dict(self.runtime)
        env.update(self.consts)
        env["G"] = G
        exec(self.code, env)
        try:
            return env["__berry_main__"]()
        except BerryError:
            raise
        except RecursionError:
            raise
        except (TypeError, AttributeError, KeyError, ZeroDivisionError, IndexError, ValueError) as exc:
            raise BerryError(f"runtime error: {exc}") from exc


def compile_source(source: str, **kw: Any) -> Program:
    return Program(source, **kw)
