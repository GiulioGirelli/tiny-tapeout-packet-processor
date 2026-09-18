# SPDX-License-Identifier: Apache-2.0
"""Unit bench for src/pp_rules.sv (3 type registers + 3x7 rule registers,
S4 5-bit map).

Directed tests + seeded random differential against a bench-local Python
reference written spec-first (description.md S4 ADDRESS MAP / MAP DETAILS /
Configuration mode WRITE rules + the owner rulings, CLARIFICATIONS), NOT
translated from the RTL. Shared address constants come from test/ppctl.py.
"""

import os
import random
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ppctl import RULE_BASE, rule_flags  # shared map/flag encodings

RULE_BASES = RULE_BASE          # (0x03, 0x0A, 0x11)
TYPE_RESET = (0x00, 0x01, 0x02)  # ruling (CLARIFICATIONS)


class RefRules:
    """Spec-first reference (S4): 24-byte writable address space;
    0x00-0x02 type values (unique), 0x03-0x17 rules, 0x18-0x1F statistics
    (read-only, read as 0 on this block). wr_accept rejects: statistics
    addresses, duplicate type values across the 3 registers (own value in
    the SAME register is legal), and the FLAGS pattern XXXX_0001. A
    rejected write leaves every register unchanged."""

    def __init__(self):
        self.regs = [0] * 24
        self.reset()

    def reset(self):
        self.regs = [0] * 24
        self.regs[0:3] = TYPE_RESET

    def accept(self, addr, data):
        if addr >= 0x18:
            return False
        if addr < 0x03:
            return not any(self.regs[i] == data for i in range(3) if i != addr)
        if addr in RULE_BASES and (data & 0x0F) == 0x01:
            return False
        return True

    def write(self, addr, data):
        if self.accept(addr, data):
            self.regs[addr] = data

    def read(self, addr):
        return self.regs[addr] if addr < 0x18 else 0

    def type_values(self):
        return sum(self.regs[i] << (8 * i) for i in range(3))

    def rule_flags(self):
        return sum(self.regs[b] << (8 * r) for r, b in enumerate(RULE_BASES))

    def rule_values(self):
        packed = 0
        for r, base in enumerate(RULE_BASES):
            for t in range(3):
                # base+1+2t is the MSB, base+2+2t the LSB
                word = (self.regs[base + 1 + 2 * t] << 8) \
                    | self.regs[base + 2 + 2 * t]
                packed |= word << (16 * (3 * r + t))
        return packed


async def tick(dut):
    await RisingEdge(dut.clk)
    await Timer(1, unit="ns")


async def reset(dut):
    dut.rules_reset.value = 0
    dut.wr_en.value = 0
    dut.wr_addr.value = 0
    dut.wr_data.value = 0
    dut.rd_addr.value = 0
    dut.rst_n.value = 0
    await tick(dut)
    await tick(dut)
    dut.rst_n.value = 1
    await tick(dut)


async def check_outputs(dut, ref):
    assert int(dut.type_values.value) == ref.type_values(), \
        f"type_values={int(dut.type_values.value):06x}"
    assert int(dut.rule_flags.value) == ref.rule_flags()
    assert int(dut.rule_values.value) == ref.rule_values()


async def write(dut, ref, addr, data):
    """One WRITE cycle; checks combinational wr_accept then commits."""
    dut.wr_en.value = 1
    dut.wr_addr.value = addr
    dut.wr_data.value = data
    await Timer(1, unit="ns")  # settle combinational wr_accept before edge
    assert int(dut.wr_accept.value) == int(ref.accept(addr, data)), \
        f"wr_accept({addr:#x},{data:#x})={int(dut.wr_accept.value)}"
    await tick(dut)
    dut.wr_en.value = 0
    ref.write(addr, data)
    await check_outputs(dut, ref)


async def read(dut, ref, addr):
    """Combinational READ check."""
    dut.rd_addr.value = addr
    await Timer(1, unit="ns")
    assert int(dut.rd_data.value) == ref.read(addr), \
        f"rd_data({addr:#x})={int(dut.rd_data.value):#x} != {ref.read(addr):#x}"


@cocotb.test()
async def reset_defaults(dut):
    """Type registers 0,1,2 (ruling); rules zero; 0x18+ reads 0."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefRules()
    await check_outputs(dut, ref)
    for addr, want in enumerate(TYPE_RESET):
        await read(dut, ref, addr)
        assert int(dut.rd_data.value) == want
    await read(dut, ref, 0x03)   # rule 1 FLAGS
    await read(dut, ref, 0x17)   # last rule register (boundary)
    await read(dut, ref, 0x18)   # first statistics address -> 0 here


@cocotb.test()
async def write_read_all_address_classes(dut):
    """Write/read back type regs, FLAGS, and boundary match-value bytes."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefRules()
    # type registers (unique values)
    for addr, data in enumerate((0x10, 0x20, 0x30)):
        await write(dut, ref, addr, data)
        await read(dut, ref, addr)
    # every rule: FLAGS + first and last match-value byte (boundaries)
    for base in RULE_BASES:
        await write(dut, ref, base, rule_flags(1, 0b101, 0b0110))
        await read(dut, ref, base)
        await write(dut, ref, base + 0x01, 0xA5)
        await read(dut, ref, base + 0x01)
        await write(dut, ref, base + 0x06, 0x5A)
        await read(dut, ref, base + 0x06)
    # last writable address / first read-only address boundary
    await write(dut, ref, 0x17, 0x7E)
    await read(dut, ref, 0x17)
    await write(dut, ref, 0x18, 0x7E)   # rejected: statistics are read-only
    await read(dut, ref, 0x18)          # reads 0 on this block


@cocotb.test()
async def rejections_leave_registers_unchanged(dut):
    """All three rejection classes; wr_accept=0; state untouched."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefRules()
    # seed known state
    await write(dut, ref, 0x00, 0x10)
    await write(dut, ref, 0x03, rule_flags(1, 0b111, 0b1010))  # 0xAF
    snapshot = list(ref.regs)
    # 1) statistics addresses (first and last)
    await write(dut, ref, 0x18, 0x55)
    await write(dut, ref, 0x1F, 0x55)
    # 2) duplicate type values -- checked across ALL three type registers
    for dst in range(3):
        for src in range(3):
            if src != dst:
                await write(dut, ref, dst, ref.regs[src])
    # 3) FLAGS pattern XXXX_0001 (0x01 and 0xF1); 0xF0 is legal
    for base in RULE_BASES:
        await write(dut, ref, base, 0x01)
        await write(dut, ref, base, 0xF1)
    assert ref.regs == snapshot, "rejected writes changed register state"
    await check_outputs(dut, ref)
    # owner ruling: rewriting a type register with its own value is legal
    await write(dut, ref, 0x00, 0x10)
    # 0xF0 (disabled rule, no type checks) is a legal FLAGS value
    await write(dut, ref, 0x03, 0xF0)
    await read(dut, ref, 0x03)


@cocotb.test()
async def rules_reset_scope_and_priority(dut):
    """rules_reset: types -> 0,1,2, rules -> 0; priority over writes."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefRules()
    await write(dut, ref, 0x00, 0x10)
    await write(dut, ref, 0x01, 0x20)
    await write(dut, ref, 0x03, 0xAF)
    await write(dut, ref, 0x04, 0xAA)
    # pulse rules_reset together with a WRITE: the reset must win
    dut.rules_reset.value = 1
    dut.wr_en.value = 1
    dut.wr_addr.value = 0x04
    dut.wr_data.value = 0xBB
    await tick(dut)
    dut.rules_reset.value = 0
    dut.wr_en.value = 0
    ref.reset()
    await check_outputs(dut, ref)
    for addr in (0x00, 0x01, 0x02, 0x03, 0x04):
        await read(dut, ref, addr)


@cocotb.test()
async def random_differential(dut):
    """Seeded random writes/reads/resets vs the spec-first reference."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefRules()
    rng = random.Random(0xBEEF)
    for _ in range(600):
        op = rng.choices(["write", "read", "reset"], weights=[6, 3, 1])[0]
        if op == "write":
            # bias towards the interesting addresses
            addr = rng.choice(
                [rng.randint(0, 0x1F), rng.randint(0, 2),
                 rng.choice(RULE_BASES), rng.randint(0x18, 0x1F)])
            data = rng.choice([rng.randint(0, 255), 0x01, 0xF1, 0xF0,
                               ref.regs[rng.randint(0, 2)]])
            await write(dut, ref, addr, data)
        elif op == "read":
            await read(dut, ref, rng.randint(0, 0x1F))
        else:
            dut.rules_reset.value = 1
            await tick(dut)
            dut.rules_reset.value = 0
            ref.reset()
            await check_outputs(dut, ref)
    # final full sweep of every address
    for addr in range(0x20):
        await read(dut, ref, addr)
