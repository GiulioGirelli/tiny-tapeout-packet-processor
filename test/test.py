# SPDX-FileCopyrightText: © 2026 Giulio Girelli
# SPDX-License-Identifier: Apache-2.0

"""Top-level cocotb suite for tt_um_GiulioGirelli_packet_processor (S4).

SHARED verification tooling: drives and observes the DUT ONLY through the
Tiny Tapeout pins (ui_in, uio_in, uo_out, uio_out, uio_oe) -- no hierarchy
probes -- so the same suite grades any implementation and runs at gate
level (GL_TEST) unchanged.

Structure:
  * Pin adapter: ppctl.Cycle records <-> TT pin drives; samples mirror
    model.step() semantics (inputs of cycle t are driven before the clock
    edge; the registered uo_out / out_state=uio_out[7:5] are sampled after
    it, i.e. what is visible on cycle t+1).
  * Continuous pin assertions in EVERY driven cycle: uio_oe==8'b1110_0000,
    uio_out[4:0]==0, and uo_out==0 whenever out_state in {000,100,101,111}.
  * Directed tests through the pins (one per spec-level behaviour).
  * Random differential regression vs test/model.py
    (PacketProcessorModel, S4): lockstep over
    a programmed classification followed by mixed-stream fuzzing, comparing
    every cycle, then a legal 32-address READ sweep. Coverage is asserted.
"""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from regression import build_case, RegressionCoverage
import model as ref_model
from ppctl import (Cycle, idle, soft_reset, read_reg, write_reg,
                   set_type_values, write_rule, packet,
                   PS_DATA, PS_META, PS_EOP, RST_TABLE, RST_RULES, RST_STATS)

ST_IDLE, ST_ACTIVE, ST_RESULT = 0b000, 0b001, 0b010
ST_WRITE_OK, ST_EARLY_EOP, ST_WRITE_ERR = 0b011, 0b100, 0b101
ST_READ_OK, ST_INPUT_ERR = 0b110, 0b111
STATES_WITH_ZERO_OUT = (ST_IDLE, ST_EARLY_EOP, ST_WRITE_ERR, ST_INPUT_ERR)

STD_VALUES = (0xAABB, 0x1122, 0xDEAD)
STD_CHUNKS = [(0x00, [0xAA, 0xBB]),
              (0x01, [0x11, 0x22]),
              (0x02, [0xDE, 0xAD])]
STD_HIT_OUT = 0xA9  # (0b1010<<4)|(0b10<<2)|0b01


# ---------------------------------------------------------------------------
# Pin adapter
# ---------------------------------------------------------------------------
def drive(dut, c):
    """Drive one ppctl.Cycle onto the TT pins (uio_in[7:5] are outputs from
    the chip's perspective and are kept 0)."""
    dut.ui_in.value = c.ui_in
    dut.uio_in.value = (c.cfg_mode | (c.packet_status << 1)
                        | (c.rst_type << 3)) & 0x1F
    dut.ena.value = c.ena
    dut.rst_n.value = c.rst_n


async def run_cycles(dut, m, cycles, msg=""):
    """Lockstep the DUT (through TT pins only) and the reference model over
    `cycles`, comparing (uo_out, out_state) every cycle and enforcing the
    continuous pin assertions. Returns the model's expected outputs."""
    exp = m.run(cycles)
    for i, c in enumerate(cycles):
        drive(dut, c)
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        uo = int(dut.uo_out.value)
        uio = int(dut.uio_out.value)
        st = uio >> 5
        # continuous pin assertions, every cycle of every test
        assert int(dut.uio_oe.value) == 0b11100000, \
            f"{msg} cycle {i}: uio_oe={int(dut.uio_oe.value):08b}"
        assert (uio & 0x1F) == 0, f"{msg} cycle {i}: uio_out[4:0]={uio & 0x1F:05b}"
        if st in STATES_WITH_ZERO_OUT:
            assert uo == 0, f"{msg} cycle {i}: uo_out={uo:#x} in state {st:03b}"
        assert (uo, st) == exp[i], \
            f"{msg} cycle {i}: dut=({uo:#x},{st:03b}) != model={exp[i]} ({c})"
    return exp


async def hard_reset_dut(dut):
    # drive benign inputs first so nothing stale is re-evaluated on release
    drive(dut, Cycle())
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 1)


@cocotb.test()
async def post_reset_idle_outputs(dut):
    """After rst_n: out_state=000, uo_out=0x00, type regs 0,1,2, all
    rules/counters 0 -- observed purely through pins."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await hard_reset_dut(dut)
    m = ref_model.PacketProcessorModel()
    exp = await run_cycles(dut, m, idle(4))
    assert exp == [(0x00, ST_IDLE)] * 4
    for addr, want in enumerate([0x00, 0x01, 0x02]):
        exp = await run_cycles(dut, m, read_reg(addr))
        assert exp == [(want, ST_READ_OK)]
    for addr in range(0x03, 0x20):
        exp = await run_cycles(dut, m, read_reg(addr))
        assert exp == [(0x00, ST_READ_OK)]


@cocotb.test()
async def ena_safe_idle_and_resume(dut):
    """ena=0: inputs ignored, uo_out=0/out_state=000 forced, state held;
    the packet resumes correctly when ena returns."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await hard_reset_dut(dut)
    m = ref_model.PacketProcessorModel()
    await run_cycles(dut, m, write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    exp = await run_cycles(dut, m, packet([(0x00, [0xAA, 0xBB])], eop=False))
    assert exp == [(0x00, ST_ACTIVE)] * 3
    # forced safe idle mid-packet; idle gap and soft reset ignored
    exp = await run_cycles(dut, m, [Cycle(ena=0), Cycle(ena=0),
                                    Cycle(ena=0, rst_type=RST_TABLE)])
    assert exp == [(0x00, ST_IDLE)] * 3
    # resume: the packet continues exactly where it stopped
    exp = await run_cycles(dut, m, packet([(0x01, [0x11, 0x22]),
                                           (0x02, [0xDE, 0xAD])]))
    assert exp[-2:] == [(STD_HIT_OUT, ST_RESULT)] * 2
    exp = await run_cycles(dut, m, read_reg(0x1C) + read_reg(0x1F))
    assert exp == [(1, ST_READ_OK), (0, ST_READ_OK)]


@cocotb.test()
async def full_packet_flow(dut):
    """001 -> 010 with the result byte -> EOP -> 010 held one more cycle
    (the 2-cycle case) -> 000."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await hard_reset_dut(dut)
    m = ref_model.PacketProcessorModel()
    await run_cycles(dut, m, write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    exp = await run_cycles(dut, m, packet(STD_CHUNKS) + idle(2))
    assert exp[:8] == [(0x00, ST_ACTIVE)] * 8
    assert exp[8] == (STD_HIT_OUT, ST_RESULT)   # completing byte -> 010
    assert exp[9] == (STD_HIT_OUT, ST_RESULT)   # EOP: held (2-cycle case)
    assert exp[10] == (0x00, ST_IDLE)
    assert exp[11] == (0x00, ST_IDLE)


@cocotb.test()
async def config_write_read_flow(dut):
    """Config WRITE (011 + written value) and READ (110 + value) through
    the pins; readback of a rule and of a statistics counter."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await hard_reset_dut(dut)
    m = ref_model.PacketProcessorModel()
    exp = await run_cycles(dut, m, write_rule(2, 1, 0b101, 0b0110, 0x1122,
                                              0x5566, 0x99AA))
    assert exp[0] == (0x00, ST_IDLE)         # address cycle: no confirm
    assert exp[1] == (0x6B, ST_WRITE_OK)     # FLAGS = 0x6B
    exp = await run_cycles(dut, m, read_reg(0x0A) + read_reg(0x0B)
                         + read_reg(0x10))
    assert exp == [(0x6B, ST_READ_OK), (0x11, ST_READ_OK),
                   (0xAA, ST_READ_OK)]
    # bump a counter and read it back through the pins
    await run_cycles(dut, m, packet([(0x00, [0xAA])]))
    exp = await run_cycles(dut, m, read_reg(0x1B) + read_reg(0x1F))
    assert exp == [(1, ST_READ_OK), (0, ST_READ_OK)]  # 1 packet, no hit


@cocotb.test()
async def soft_reset_codes_observable_effects(dut):
    """rst_type=01/10/11: each code's exact observable scope through pins."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await hard_reset_dut(dut)
    m = ref_model.PacketProcessorModel()
    await run_cycles(dut, m, set_type_values(0x10, 0x20, 0x30)
                   + write_rule(1, 1, 0b111, 0b1010, *STD_VALUES)
                   + packet([(0x10, [0xAA])], eop=False))
    # 01: packet state + table + result + out_state cleared (000)
    exp = await run_cycles(dut, m, soft_reset(RST_TABLE))
    assert exp == [(0x00, ST_IDLE)]
    # 10: types back to 0,1,2; rules zeroed; out_state reflects state
    exp = await run_cycles(dut, m, soft_reset(RST_RULES))
    assert exp == [(0x00, ST_IDLE)]
    for addr, want in enumerate([0x00, 0x01, 0x02]):
        exp = await run_cycles(dut, m, read_reg(addr))
        assert exp == [(want, ST_READ_OK)]
    exp = await run_cycles(dut, m, read_reg(0x03))
    assert exp == [(0x00, ST_READ_OK)]
    # counters still hold the aborted packet's counts (10 didn't touch them)
    exp = await run_cycles(dut, m, read_reg(0x1B))
    assert exp == [(1, ST_READ_OK)]
    # 11: only the counters go
    exp = await run_cycles(dut, m, soft_reset(RST_STATS))
    assert exp == [(0x00, ST_IDLE)]
    for addr in range(0x18, 0x20):
        exp = await run_cycles(dut, m, read_reg(addr))
        assert exp == [(0x00, ST_READ_OK)]


@cocotb.test()
async def early_eop_100_one_cycle(dut):
    """EOP before the table is full: 100 for exactly one cycle, then 000;
    table cleared (owner ruling); no rule counter moves."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await hard_reset_dut(dut)
    m = ref_model.PacketProcessorModel()
    exp = await run_cycles(dut, m, packet([(0x00, [0xAA])]) + idle(2))
    assert exp == [(0x00, ST_ACTIVE), (0x00, ST_ACTIVE),
                   (0x00, ST_EARLY_EOP), (0x00, ST_IDLE), (0x00, ST_IDLE)]
    # table was cleared: the next packet starts empty, so 5 more data
    # bytes do NOT fill it (a stale 1-byte slot0 would complete it)
    exp = await run_cycles(dut, m, packet([(0x00, [0xBB]),
                                           (0x01, [0x11, 0x22]),
                                           (0x02, [0xDE, 0xAD])]))
    assert exp[-2] == (0x00, ST_ACTIVE)
    assert exp[-1] == (0x00, ST_EARLY_EOP)
    exp = await run_cycles(dut, m, read_reg(0x1B) + read_reg(0x1F))
    assert exp == [(2, ST_READ_OK), (0, ST_READ_OK)]


@cocotb.test()
async def input_error_111_sticky_and_recovery(dut):
    """111: entry, stickiness (streaming and config swallowed), recovery
    only via rst_type=01, re-trigger rule after the reset."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await hard_reset_dut(dut)
    m = ref_model.PacketProcessorModel()
    exp = await run_cycles(dut, m, [Cycle(ui_in=0x55, packet_status=PS_DATA)])
    assert exp == [(0x00, ST_INPUT_ERR)]
    exp = await run_cycles(dut, m, packet([(0x00, [0xAA])], eop=False)
                         + [Cycle(packet_status=PS_EOP)])
    assert exp == [(0x00, ST_INPUT_ERR)] * 3     # sticky, table frozen
    exp = await run_cycles(dut, m, soft_reset(RST_RULES))
    assert exp == [(0x00, ST_INPUT_ERR)]         # 10 does not recover
    exp = await run_cycles(dut, m, soft_reset(RST_TABLE))
    assert exp == [(0x00, ST_IDLE)]              # 01 recovers
    exp = await run_cycles(dut, m, [Cycle(ui_in=0x55, packet_status=PS_DATA)])
    assert exp == [(0x00, ST_INPUT_ERR)]         # re-trigger
    exp = await run_cycles(dut, m, soft_reset(RST_TABLE) + idle(1)
                         + [Cycle(ui_in=0x00, packet_status=PS_META)])
    assert exp == [(0x00, ST_IDLE), (0x00, ST_IDLE), (0x00, ST_ACTIVE)]
    # two error entries, and no counter absorbed them (no error counter);
    # total_bytes saw only the final accepted metadata byte
    exp = await run_cycles(dut, m, soft_reset(RST_TABLE) + read_reg(0x1F)
                         + read_reg(0x19))
    assert exp == [(0x00, ST_IDLE), (0, ST_READ_OK), (1, ST_READ_OK)]


@cocotb.test()
async def back_to_back_packets(dut):
    """No idle gap needed between packets; hit counted once per packet."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await hard_reset_dut(dut)
    m = ref_model.PacketProcessorModel()
    await run_cycles(dut, m, write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    exp = await run_cycles(dut, m, packet(STD_CHUNKS) + packet(STD_CHUNKS))
    assert exp == ([(0x00, ST_ACTIVE)] * 8
                   + [(STD_HIT_OUT, ST_RESULT)] * 2) * 2
    exp = await run_cycles(dut, m, read_reg(0x1B) + read_reg(0x1C))
    assert exp == [(2, ST_READ_OK), (2, ST_READ_OK)]


@cocotb.test()
async def random_differential(dut):
    """Programmed classifications + mixed fuzzing + verified READ sweeps."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    n_seeds = 1500
    coverage = RegressionCoverage()
    for seed in range(n_seeds):
        case = build_case(seed, n_cycles=60)
        await hard_reset_dut(dut)
        m = ref_model.PacketProcessorModel()
        config_outputs = await run_cycles(dut, m, case.configuration, msg=f"seed {seed} setup")
        packet_outputs = await run_cycles(dut, m, case.packet, msg=f"seed {seed} packet")
        await run_cycles(dut, m, case.mixed, msg=f"seed {seed} mixed")
        sweep_outputs = await run_cycles(dut, m, case.sweep, msg=f"seed {seed} sweep")
        coverage.record(case, config_outputs, packet_outputs, sweep_outputs)
    coverage.assert_complete(n_seeds)
    dut._log.info("Random regression coverage: %s", coverage.summary())
