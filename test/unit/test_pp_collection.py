# SPDX-License-Identifier: Apache-2.0
"""Unit bench for src/pp_collection.sv (3x16-bit collection table, S4).

Directed tests + seeded random differential against a bench-local Python
reference written spec-first (description.md S4 COMPONENTS / Collection
table), NOT translated from the RTL. Shared encodings come from
test/ppctl.py.
"""

import os
import random
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TYPES = (0x00, 0x01, 0x02)              # default type values (S4)
TYPE_VALUES_PACKED = 0x020100           # type_values[8*i +: 8] = i


class RefTable:
    """Spec-first reference (S4): a metadata byte equal to one of the 3
    type values selects that slot for following data bytes (no match ->
    data dropped until the next metadata). Data appends MSB-first, max 2
    bytes per slot; full slots just stop filling. Partial/interleaved fills
    and repeated metadata are legal."""

    def __init__(self):
        self.slots = [[], [], []]
        self.sel = None  # selected slot index, None = no match

    def clear(self):
        self.slots = [[], [], []]
        self.sel = None

    def meta(self, byte, types):
        self.sel = types.index(byte) if byte in types else None

    def data(self, byte):
        if self.sel is not None and len(self.slots[self.sel]) < 2:
            self.slots[self.sel].append(byte)

    def full(self):
        return all(len(s) == 2 for s in self.slots)

    def collected(self):
        packed = 0
        for i, slot in enumerate(self.slots):
            word = 0
            for b in slot:  # shift-append: after 2 bytes the 1st is at [15:8]
                word = (word << 8) | b
            packed |= word << (16 * i)
        return packed


async def tick(dut):
    """Advance one clock edge and let the NBA updates settle."""
    await RisingEdge(dut.clk)
    await Timer(1, unit="ns")


async def reset(dut):
    dut.clear.value = 0
    dut.meta_valid.value = 0
    dut.meta_byte.value = 0
    dut.data_valid.value = 0
    dut.data_byte.value = 0
    dut.type_values.value = TYPE_VALUES_PACKED
    dut.rst_n.value = 0
    await tick(dut)
    await tick(dut)
    dut.rst_n.value = 1
    await tick(dut)


async def cycle(dut, ref, clear=0, meta=None, data=None):
    """Drive one cycle of inputs, tick, update the reference, check outputs."""
    dut.clear.value = clear
    if meta is not None:
        dut.meta_valid.value = 1
        dut.meta_byte.value = meta
    if data is not None:
        dut.data_valid.value = 1
        dut.data_byte.value = data
    await tick(dut)
    dut.clear.value = 0
    dut.meta_valid.value = 0
    dut.data_valid.value = 0
    if clear:
        ref.clear()
    if meta is not None:
        ref.meta(meta, TYPES)
    if data is not None:
        ref.data(data)
    assert int(dut.collected.value) == ref.collected(), \
        f"collected={int(dut.collected.value):012x} != {ref.collected():012x}"
    assert int(dut.full.value) == int(ref.full()), \
        f"full={int(dut.full.value)} != {int(ref.full())}"


@cocotb.test()
async def reset_state(dut):
    """After reset: table empty, full=0."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    assert int(dut.collected.value) == 0
    assert int(dut.full.value) == 0


@cocotb.test()
async def msb_first_fill(dut):
    """AA BB arriving in order build 16'hAABB (1st byte [15:8])."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefTable()
    await cycle(dut, ref, meta=0x00)
    for b in (0xAA, 0xBB):
        await cycle(dut, ref, data=b)
    assert int(dut.collected.value) & 0xFFFF == 0xAABB
    assert int(dut.full.value) == 0  # only one slot filled


@cocotb.test()
async def interleaved_partial_and_repeated(dut):
    """Partial + interleaved fills and repeated metadata; full-flag timing."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefTable()
    stream = [
        ("m", 0x00), ("d", 0xAA),
        ("m", 0x01), ("d", 0x11),
        ("m", 0x00), ("d", 0xBB),          # slot0 = AABB
        ("m", 0x02), ("d", 0xDE),
        ("m", 0x01), ("d", 0x22),          # slot1 = 1122
        ("m", 0x02),
    ]
    for kind, b in stream:
        if kind == "m":
            await cycle(dut, ref, meta=b)
        else:
            await cycle(dut, ref, data=b)
        assert int(dut.full.value) == 0  # table not complete yet
    await cycle(dut, ref, data=0xAD)     # completes the table
    assert int(dut.full.value) == 1      # full exactly after the last byte
    assert int(dut.collected.value) == (
        0xAABB | 0x1122 << 16 | 0xDEAD << 32)


@cocotb.test()
async def zero_length_and_repeated_metadata(dut):
    """Metadata may be immediately followed by the next metadata."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefTable()
    await cycle(dut, ref, meta=0x00)     # zero-length chunk ...
    await cycle(dut, ref, meta=0x00)     # ... immediately re-selected
    for b in (0xAA, 0xBB):
        await cycle(dut, ref, data=b)
    assert int(dut.collected.value) & 0xFFFF == 0xAABB


@cocotb.test()
async def overflow_bytes_dropped(dut):
    """A full slot does not overflow or replace, it just stops filling."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefTable()
    await cycle(dut, ref, meta=0x00)
    for b in (0x01, 0x02, 0x03, 0x04):
        await cycle(dut, ref, data=b)
    assert int(dut.collected.value) & 0xFFFF == 0x0102  # first 2 kept


@cocotb.test()
async def non_matching_metadata_drops_data(dut):
    """Data after a non-matching metadata is dropped until next metadata."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefTable()
    await cycle(dut, ref, meta=0x77)     # not a type value
    await cycle(dut, ref, data=0x99)
    await cycle(dut, ref, data=0x88)
    await cycle(dut, ref, meta=0x00)
    await cycle(dut, ref, data=0xAA)
    assert int(dut.collected.value) & 0xFFFF == 0x00AA


@cocotb.test()
async def clear_empties_table_and_selection(dut):
    """clear empties slots, counts and the metadata selection."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefTable()
    await cycle(dut, ref, meta=0x00)
    await cycle(dut, ref, data=0xAA)
    await cycle(dut, ref, clear=1)
    # a data byte now has no selected slot (selection was cleared too)
    await cycle(dut, ref, data=0xBB)
    assert int(dut.collected.value) == 0
    assert int(dut.full.value) == 0
    # and a full refill works from scratch
    await cycle(dut, ref, meta=0x00)
    for b in (0xAA, 0xBB):
        await cycle(dut, ref, data=b)
    assert int(dut.collected.value) & 0xFFFF == 0xAABB


@cocotb.test()
async def random_differential(dut):
    """Seeded random op stream vs the spec-first reference."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefTable()
    rng = random.Random(20260911)
    for _ in range(400):
        kind = rng.choices(["meta", "data", "clear", "idle"],
                           weights=[3, 5, 1, 2])[0]
        if kind == "meta":
            byte = rng.choice(TYPES) if rng.random() < 0.8 else rng.randint(0, 255)
            await cycle(dut, ref, meta=byte)
        elif kind == "data":
            await cycle(dut, ref, data=rng.randint(0, 255))
        elif kind == "clear":
            await cycle(dut, ref, clear=1)
        else:
            await cycle(dut, ref)


# ---------------------------------------------------------------------------
# collected_next / full_next (effective next state)
# ---------------------------------------------------------------------------
def peek_next(ref, clear=0, data=None):
    """Expected (collected_next, full_next) for the given in-flight inputs:
    the current data byte applied to the selected matched slot (if it holds
    <2 bytes); clear has priority and yields the emptied table."""
    slots = [list(s) for s in ref.slots]
    if clear:
        slots = [[], [], []]
    elif data is not None and ref.sel is not None \
            and len(slots[ref.sel]) < 2:
        slots[ref.sel].append(data)
    packed = 0
    for i, slot in enumerate(slots):
        word = 0
        for b in slot:
            word = (word << 8) | b
        packed |= word << (16 * i)
    return packed, int(all(len(s) == 2 for s in slots))


@cocotb.test()
async def next_state_in_flight_byte(dut):
    """collected_next shows the in-flight byte BEFORE the clock edge, while
    collected still holds the pre-byte contents."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefTable()
    await cycle(dut, ref, meta=0x00)
    await cycle(dut, ref, data=0xAA)
    # drive the 2nd byte and sample the next-state outputs combinationally
    dut.data_valid.value = 1
    dut.data_byte.value = 0xBB
    await Timer(1, unit="ns")
    exp_packed, exp_full = peek_next(ref, data=0xBB)
    assert int(dut.collected_next.value) == exp_packed
    assert int(dut.collected_next.value) & 0xFFFF == 0xAABB
    assert int(dut.collected.value) & 0xFFFF == 0x00AA  # not yet registered
    assert int(dut.full_next.value) == exp_full == 0    # slot0 full, not all
    await tick(dut)
    dut.data_valid.value = 0
    ref.data(0xBB)
    assert int(dut.collected.value) & 0xFFFF == 0xAABB  # registered
    # no effective write: collected_next == collected
    await Timer(1, unit="ns")
    assert int(dut.collected_next.value) == int(dut.collected.value)


@cocotb.test()
async def full_next_timing(dut):
    """full_next rises combinationally on the completing byte's cycle, one
    cycle before the registered full."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefTable()
    # fill slots 0-1 fully, slot 2 with 1 byte (5 data bytes total)
    for meta, data_bytes in [(0x00, [1, 2]), (0x01, [3, 4]), (0x02, [5])]:
        await cycle(dut, ref, meta=meta)
        for b in data_bytes:
            await cycle(dut, ref, data=b)
    assert int(dut.full.value) == 0
    # drive the completing byte: full_next must already be 1 pre-edge
    dut.data_valid.value = 1
    dut.data_byte.value = 6
    await Timer(1, unit="ns")
    exp_packed, exp_full = peek_next(ref, data=6)
    assert int(dut.collected_next.value) == exp_packed
    assert int(dut.full_next.value) == exp_full == 1
    assert int(dut.full.value) == 0      # registered full still low pre-edge
    await tick(dut)
    dut.data_valid.value = 0
    assert int(dut.full.value) == 1      # registered on the next cycle
    assert int(dut.full_next.value) == 1


@cocotb.test()
async def clear_priority_over_in_flight_byte(dut):
    """clear wins over an in-flight byte: next-state outputs reflect the
    emptied table."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefTable()
    await cycle(dut, ref, meta=0x00)
    await cycle(dut, ref, data=0xAA)
    dut.clear.value = 1
    dut.data_valid.value = 1
    dut.data_byte.value = 0xBB
    await Timer(1, unit="ns")
    exp_packed, exp_full = peek_next(ref, clear=1, data=0xBB)
    assert int(dut.collected_next.value) == exp_packed == 0
    assert int(dut.full_next.value) == exp_full == 0
    await tick(dut)
    dut.clear.value = 0
    dut.data_valid.value = 0
    ref.clear()
    assert int(dut.collected.value) == 0
    assert int(dut.full.value) == 0
