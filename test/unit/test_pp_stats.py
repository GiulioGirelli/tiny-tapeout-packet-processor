# SPDX-License-Identifier: Apache-2.0
"""Unit bench for src/pp_stats.sv (8 statistics counters, S4 -- no
error-occurrences counter, no error_event port).

Directed tests + seeded random differential against a bench-local Python
reference written spec-first (description.md S4 STATISTICS / MAP DETAILS),
NOT translated from the RTL. Shared address constants come from
test/ppctl.py. The 16-bit wrap preloads the packed counter via cocotb
deposit (RTL-only unit bench, permitted by the phase brief); the 8-bit
wrap is exercised by strobing.
"""

import os
import random
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ppctl import (ADDR_TOTAL_BYTES_HI, ADDR_TOTAL_BYTES_LO,
                   ADDR_TOTAL_PACKETS_HI, ADDR_TOTAL_PACKETS_LO,
                   ADDR_RULE1_HITS, ADDR_RULE2_HITS, ADDR_RULE3_HITS,
                   ADDR_NO_RULE_HITS)


class RefStats:
    """Spec-first reference (S4): total_bytes +1 per accepted metadata/data
    byte; total_packets +1 per started packet; rule hit (or no-rule) +1 at
    EOP. All wrap modulo their width; each strobe counts independently, so
    simultaneous strobes compose."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.total_bytes = 0
        self.total_packets = 0
        self.rule_hits = [0, 0, 0]
        self.no_rule_hits = 0

    def cycle(self, byte=0, pkt=0, hit_eop=0, hit_rule=0):
        if byte:
            self.total_bytes = (self.total_bytes + 1) & 0xFFFF
        if pkt:
            self.total_packets = (self.total_packets + 1) & 0xFFFF
        if hit_eop:
            if hit_rule == 0:
                self.no_rule_hits = (self.no_rule_hits + 1) & 0xFF
            else:
                self.rule_hits[hit_rule - 1] = \
                    (self.rule_hits[hit_rule - 1] + 1) & 0xFF

    def read(self, addr):
        return {
            ADDR_TOTAL_BYTES_HI: self.total_bytes >> 8,
            ADDR_TOTAL_BYTES_LO: self.total_bytes & 0xFF,
            ADDR_TOTAL_PACKETS_HI: self.total_packets >> 8,
            ADDR_TOTAL_PACKETS_LO: self.total_packets & 0xFF,
            ADDR_RULE1_HITS: self.rule_hits[0],
            ADDR_RULE2_HITS: self.rule_hits[1],
            ADDR_RULE3_HITS: self.rule_hits[2],
            ADDR_NO_RULE_HITS: self.no_rule_hits,
        }.get(addr, 0)


STAT_ADDRS = list(range(0x18, 0x20))


async def tick(dut):
    await RisingEdge(dut.clk)
    await Timer(1, unit="ns")


async def reset(dut):
    dut.stats_reset.value = 0
    dut.byte_accepted.value = 0
    dut.packet_started.value = 0
    dut.hit_eop.value = 0
    dut.hit_rule.value = 0
    dut.rd_addr.value = 0
    dut.rst_n.value = 0
    await tick(dut)
    await tick(dut)
    dut.rst_n.value = 1
    await tick(dut)


async def cycle(dut, ref, byte=0, pkt=0, hit_eop=0, hit_rule=0):
    """Drive one strobe cycle, tick, update the reference."""
    dut.byte_accepted.value = byte
    dut.packet_started.value = pkt
    dut.hit_eop.value = hit_eop
    dut.hit_rule.value = hit_rule
    await tick(dut)
    dut.byte_accepted.value = 0
    dut.packet_started.value = 0
    dut.hit_eop.value = 0
    dut.hit_rule.value = 0
    ref.cycle(byte, pkt, hit_eop, hit_rule)


async def read(dut, ref, addr):
    dut.rd_addr.value = addr
    await Timer(1, unit="ns")
    assert int(dut.rd_data.value) == ref.read(addr), \
        f"rd_data({addr:#x})={int(dut.rd_data.value):#x} != {ref.read(addr):#x}"


async def read_all(dut, ref):
    for addr in STAT_ADDRS:
        await read(dut, ref, addr)


@cocotb.test()
async def reset_state_and_read_map(dut):
    """All counters 0 after reset; 0 outside 0x18..0x1F."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefStats()
    await read_all(dut, ref)
    for addr in (0x00, 0x10, 0x17):
        await read(dut, ref, addr)


@cocotb.test()
async def each_strobe(dut):
    """Every strobe and every mapped counter, one at a time."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefStats()
    for _ in range(3):
        await cycle(dut, ref, byte=1)          # total_bytes = 3
    for _ in range(2):
        await cycle(dut, ref, pkt=1)           # total_packets = 2
    await cycle(dut, ref, hit_eop=1, hit_rule=1)
    await cycle(dut, ref, hit_eop=1, hit_rule=2)
    await cycle(dut, ref, hit_eop=1, hit_rule=3)
    for _ in range(2):
        await cycle(dut, ref, hit_eop=1, hit_rule=0)   # no_rule_hits = 2
    await read_all(dut, ref)


@cocotb.test()
async def simultaneous_strobes_compose(dut):
    """byte+packet on the same cycle (first metadata); hit+byte."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefStats()
    # first metadata of a packet: byte_accepted AND packet_started together
    await cycle(dut, ref, byte=1, pkt=1)
    # a data byte together with an EOP-hit strobe
    await cycle(dut, ref, byte=1, hit_eop=1, hit_rule=2)
    # everything at once
    await cycle(dut, ref, byte=1, pkt=1, hit_eop=1, hit_rule=3)
    await read_all(dut, ref)


@cocotb.test()
async def wrap_8bit_counter(dut):
    """An 8-bit counter (rule_1_hits) wraps modulo 256."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefStats()
    for _ in range(255):
        await cycle(dut, ref, hit_eop=1, hit_rule=1)
    await read(dut, ref, ADDR_RULE1_HITS)
    assert int(dut.rd_data.value) == 0xFF
    await cycle(dut, ref, hit_eop=1, hit_rule=1)   # wraps to 0
    await read(dut, ref, ADDR_RULE1_HITS)
    assert int(dut.rd_data.value) == 0x00
    # the other counters were untouched
    await read(dut, ref, ADDR_RULE2_HITS)
    await read(dut, ref, ADDR_NO_RULE_HITS)


@cocotb.test()
async def wrap_16bit_total_bytes(dut):
    """total_bytes wraps modulo 2^16 (state preloaded via deposit)."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefStats()
    dut.total_bytes.value = 0xFFFE             # deposit (RTL-only bench)
    ref.total_bytes = 0xFFFE
    await cycle(dut, ref, byte=1)              # 0xFFFF
    await read(dut, ref, ADDR_TOTAL_BYTES_HI)
    assert int(dut.rd_data.value) == 0xFF
    await read(dut, ref, ADDR_TOTAL_BYTES_LO)
    assert int(dut.rd_data.value) == 0xFF
    await cycle(dut, ref, byte=1)              # wraps to 0x0000
    await read(dut, ref, ADDR_TOTAL_BYTES_HI)
    assert int(dut.rd_data.value) == 0x00
    await read(dut, ref, ADDR_TOTAL_BYTES_LO)
    assert int(dut.rd_data.value) == 0x00
    await cycle(dut, ref, byte=1)              # 0x0001
    await read(dut, ref, ADDR_TOTAL_BYTES_LO)
    assert int(dut.rd_data.value) == 0x01


@cocotb.test()
async def stats_reset_scope_and_priority(dut):
    """stats_reset zeroes every counter and has priority over strobes."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefStats()
    await cycle(dut, ref, byte=1, pkt=1, hit_eop=1, hit_rule=1)
    # pulse stats_reset with all strobes high: reset wins, all counters 0
    dut.stats_reset.value = 1
    dut.byte_accepted.value = 1
    dut.packet_started.value = 1
    dut.hit_eop.value = 1
    dut.hit_rule.value = 3
    await tick(dut)
    dut.stats_reset.value = 0
    dut.byte_accepted.value = 0
    dut.packet_started.value = 0
    dut.hit_eop.value = 0
    ref.reset()
    await read_all(dut, ref)


@cocotb.test()
async def random_differential(dut):
    """Seeded random strobe/reset stream vs the spec-first reference."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    ref = RefStats()
    rng = random.Random(0x5EED)
    for i in range(1000):
        if rng.random() < 0.02:
            dut.stats_reset.value = 1
            await tick(dut)
            dut.stats_reset.value = 0
            ref.reset()
        else:
            await cycle(dut, ref,
                        byte=rng.randint(0, 1),
                        pkt=rng.randint(0, 1),
                        hit_eop=rng.randint(0, 1),
                        hit_rule=rng.randint(0, 3))
        if i % 50 == 49:
            await read_all(dut, ref)
    await read_all(dut, ref)
