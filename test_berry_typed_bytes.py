"""Typed byte accesses used by Ocean, against the firmware's byte-library contract.

Source: AWTRIX NG v1.2.2 lib/berry/src/be_byteslib.c (m_get/m_set and float variants).
No dependency on a downloaded runtime is required for the normal test suite.
"""
import math

import pytest

from weather.berry_lite import Program, BerryError
from weather.moon_panel_sim import R


def run(body):
    return Program(body, real_type=R).run({})


@pytest.mark.parametrize('width', [-4,-3,-2,-1,1,2,3,4])
def test_integer_width_and_endian_round_trip_preserve_signed_values(width):
    result = run(f'var b=bytes("0000000000000000") b.set(2,-2,{width}) return [b,b.geti(2,{width})]')
    n = abs(width)
    expected = bytearray(8)
    expected[2:2+n] = ((1 << (n*8))-2).to_bytes(n, 'big' if width < 0 else 'little')
    assert result == [expected, -2]


@pytest.mark.parametrize('expression,expected', [
    ('b.get(0,-3)', 0x123456), ('b.get(0,3)', 0x563412),
    ('b.geti(3,2)', -2), ('b.get(3,2)', 65534),
    ('b.get(-2,2)', 65534), ('b.get(4,2)', 0), ('b.get(-6,2)', 0),
    ('b.get(0,0)', 0), ('b.get(0,true)', 0x12),
])
def test_integer_reads_include_palette_triplets_and_signed_coefficient_boundaries(expression, expected):
    assert run('var b=bytes("123456FEFF") return '+expression) == expected


def test_four_byte_reads_follow_firmwares_int32_return_even_with_64_bit_integers():
    assert run('var b=bytes("FFFFFFFF") return [b.get(0,4),b.geti(0,4)]') == [-1, -1]


@pytest.mark.parametrize('big,expected', [('false','0000C0BF'), ('true','BFC00000')])
def test_float32_encoding_and_negative_offset_are_exact(big, expected):
    result = run(f'var b=bytes("00000000") var r=b.setfloat(-4,-1.5,{big}) return [b,b.getfloat(-4,{big}),r]')
    assert result == [bytearray.fromhex(expected), R(-1.5), None]


def test_float32_round_trip_preserves_negative_zero_and_rounds_once():
    result = run('var b=bytes("00000080") var z=b.getfloat(0) b.setfloat(0,real(1)/real(3)) return [z,b,b.getfloat(0)]')
    assert math.copysign(1.0, float(result[0])) == -1.0
    assert result[1] == bytearray.fromhex('ABAAAA3E')
    assert result[2] == R(1)/R(3)


@pytest.mark.parametrize('call', ['b.set(3,42,2)', 'b.set(-5,42,2)', 'b.set(0,42,0)',
                                 'b.setfloat(1,1.5)', 'b.setfloat(-5,1.5)'])
def test_out_of_range_writes_do_not_resize_or_partially_change_the_buffer(call):
    assert run('var b=bytes("12345678") '+call+' return b') == bytearray.fromhex('12345678')


@pytest.mark.parametrize('call', ['b.getfloat(1)', 'b.getfloat(-5)'])
def test_incomplete_float_read_returns_real_zero(call):
    assert run('var b=bytes("FFFFFFFF") return '+call) == R(0)


@pytest.mark.parametrize('call', ['b.get(true,2)', 'b.get(0,5)', 'b.set(0,1,true)',
                                 'b.set(0,1.5,2)', 'b.setfloat(0,true)', 'b.setfloat(false,1.0)'])
def test_invalid_types_and_widths_are_rejected(call):
    with pytest.raises(BerryError, match='type_error'):
        run('var b=bytes("00000000") '+call)
