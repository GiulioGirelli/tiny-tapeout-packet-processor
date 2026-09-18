# SPDX-License-Identifier: Apache-2.0
"""Unit bench for src/pp_chip.sv (full chip: input FSMs + core + stats +
output stage), S4 configuration.

Drives the pp_chip pins EXACTLY like model.step(): inputs of cycle t are
driven before the clock edge, the registered (uo_out, out_state) are
sampled after it -- so every test compares the DUT against
test/model.py's PacketProcessorModel cycle-by-cycle, PLUS explicit
hand-checks of the spec's required cycle (description.md S4 TIMING /
I/O FLAGS / MODES / STATISTICS / CLARIFICATIONS).

S4 note: the error-occurrences counter is gone. Errors are verified via
out_state sequences (100/101/111) plus proof that the surviving counters
did NOT move -- never via a counter.

Part 1: directed suite, one test per spec statement.
Part 2: random differential vs the model, zero mismatches allowed.
"""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from regression import build_case, RegressionCoverage
import model as ref_model
import ppctl
from ppctl import (Cycle, idle, hard_reset, soft_reset, read_reg, write_reg,
                   set_type_values, write_rule, packet,
                   PS_IDLE, PS_DATA, PS_META, PS_EOP,
                   CFG_NONE, CFG_READ, CFG_WADDR, CFG_WDATA,
                   RST_TABLE, RST_RULES, RST_STATS)

ST_IDLE, ST_ACTIVE, ST_RESULT = 0b000, 0b001, 0b010
ST_WRITE_OK, ST_EARLY_EOP, ST_WRITE_ERR = 0b011, 0b100, 0b101
ST_READ_OK, ST_INPUT_ERR = 0b110, 0b111

STD_VALUES = (0xAABB, 0x1122, 0xDEAD)
STD_CHUNKS = [(0x00, [0xAA, 0xBB]),
              (0x01, [0x11, 0x22]),
              (0x02, [0xDE, 0xAD])]
STD_FLAG = 0xAF
STD_HIT_OUT = 0xA9  # (0b1010<<4)|(0b10<<2)|0b01

A_TB_LO, A_TP_LO, A_R1, A_NR = 0x19, 0x1B, 0x1C, 0x1F


async def start(dut):
    """Start the clock and apply a hard reset."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.ui_in.value = 0
    dut.cfg_mode.value = 0
    dut.packet_status.value = 0
    dut.rst_type.value = 0
    dut.ena.value = 1
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 1)


async def check(dut, m, cycles, msg=""):
    """Run model and DUT in lockstep over `cycles`, comparing the
    registered (uo_out, out_state) every cycle. Returns the model's
    expected outputs for hand-checks."""
    exp = m.run(cycles)
    for i, c in enumerate(cycles):
        dut.ui_in.value = c.ui_in
        dut.cfg_mode.value = c.cfg_mode
        dut.packet_status.value = c.packet_status
        dut.rst_type.value = c.rst_type
        dut.ena.value = c.ena
        dut.rst_n.value = c.rst_n
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        got = (int(dut.uo_out.value), int(dut.out_state.value))
        assert got == exp[i], \
            f"{msg} cycle {i}: dut={got} != model={exp[i]} (cycle={c})"
    return exp


async def hard_reset_dut(dut):
    # drive benign inputs first: whatever was last on the pins (e.g. EOP)
    # would otherwise be re-evaluated the moment rst_n releases
    dut.ui_in.value = 0
    dut.cfg_mode.value = 0
    dut.packet_status.value = 0
    dut.rst_type.value = 0
    dut.ena.value = 1
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 1)


# ---------------------------------------------------------------------------
# Directed suite
# ---------------------------------------------------------------------------
@cocotb.test()
async def reset_state_and_default_readback(dut):
    """RESET: outputs 000/0; type regs 0,1,2 (ruling); rules/counters 0."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    exp = await check(dut, m, idle(3))
    assert exp == [(0x00, ST_IDLE)] * 3
    for addr, want in enumerate([0x00, 0x01, 0x02]):
        exp = await check(dut, m, read_reg(addr))
        assert exp == [(want, ST_READ_OK)]
    for addr in range(0x03, 0x20):
        exp = await check(dut, m, read_reg(addr))
        assert exp == [(0x00, ST_READ_OK)]


@cocotb.test()
async def state_001_set_and_hold_during_streaming(dut):
    """TIMING 001: set the cycle after the first metadata; held until EOP
    or error; replaced by 010 once a result exists."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    exp = await check(dut, m, idle(1))
    assert exp == [(0x00, ST_IDLE)]            # before the packet: 000
    # no rules -> table completes with no hit; 001 until then
    exp = await check(dut, m, packet([(0x00, [0xAA])], eop=False))
    assert exp == [(0x00, ST_ACTIVE)] * 2      # 001 right after metadata
    exp = await check(dut, m, packet([(0x77, [0x99])], eop=False))
    assert exp == [(0x00, ST_ACTIVE)] * 2      # unknown metadata: still 001


@cocotb.test()
async def state_010_format_hold_and_two_cycle_case(dut):
    """TIMING/OUT PINS 010: content packing (rule/types/action), hold until
    one cycle after EOP inclusive, and the 2-cycle fill-right-before-EOP."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    await check(dut, m, write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    exp = await check(dut, m, packet(STD_CHUNKS) + idle(2))
    # 8 streaming cycles at 001; completing byte (idx 8) -> 010/0xA9
    assert exp[:8] == [(0x00, ST_ACTIVE)] * 8
    assert exp[8] == (STD_HIT_OUT, ST_RESULT)    # 010 on completing cycle
    assert exp[9] == (STD_HIT_OUT, ST_RESULT)    # EOP: held (2-cycle case)
    assert exp[10] == (0x00, ST_IDLE)            # one cycle after that: 000
    assert exp[11] == (0x00, ST_IDLE)
    # hold while the packet keeps streaming after the result exists
    exp = await check(dut, m, packet(STD_CHUNKS, eop=False)
                  + packet([(0x00, []), (0x01, [])], eop=False))
    assert exp[-2:] == [(STD_HIT_OUT, ST_RESULT)] * 2   # still 010 mid-stream
    exp = await check(dut, m, [Cycle(packet_status=PS_EOP)] + idle(1))
    assert exp == [(STD_HIT_OUT, ST_RESULT), (0x00, ST_IDLE)]


@cocotb.test()
async def state_010_type_count_encodings(dut):
    """OUT PINS: out[3:2] = checked-type count 1,2,3 -> 00,01,10."""
    await start(dut)
    for mask, want_out in [(0b001, 0x21), (0b011, 0x25), (0b111, 0x29)]:
        m = ref_model.PacketProcessorModel()
        values = [STD_VALUES[t] if (mask >> t) & 1 else 0x5A00 + t
                  for t in range(3)]
        await check(dut, m, write_rule(1, 1, mask, 0b010, *values))
        exp = await check(dut, m, packet(STD_CHUNKS))
        assert exp[-2:] == [(want_out, ST_RESULT)] * 2
        await hard_reset_dut(dut)


@cocotb.test()
async def multi_hit_priority_and_no_hit(dut):
    """Rules: highest rule NUMBER wins (3>2>1); no hit -> 010 with zeros;
    hit counters once per packet at EOP."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    await check(dut, m, write_rule(1, 1, 0b111, 1, *STD_VALUES)
              + write_rule(2, 1, 0b111, 2, *STD_VALUES)
              + write_rule(3, 1, 0b111, 3, *STD_VALUES))
    exp = await check(dut, m, packet(STD_CHUNKS))
    assert exp[-2:] == [(0x3B, ST_RESULT)] * 2   # rule 3, action 3
    exp = await check(dut, m, write_reg(0x11, 0x00) + packet(STD_CHUNKS))
    assert exp[-2:] == [(0x2A, ST_RESULT)] * 2   # rule 2, action 2
    exp = await check(dut, m, write_reg(0x0A, 0x00)
                  + write_reg(0x03, 0x00) + packet(STD_CHUNKS))
    assert exp[-2:] == [(0x00, ST_RESULT)] * 2   # no hit -> zeros
    # winners were rule 3, rule 2, none: rule_1_hits stays 0
    exp = await check(dut, m, read_reg(0x1C) + read_reg(0x1D)
                  + read_reg(0x1E) + read_reg(0x1F) + read_reg(0x1B))
    assert exp == [(0, ST_READ_OK), (1, ST_READ_OK), (1, ST_READ_OK),
                   (1, ST_READ_OK), (3, ST_READ_OK)]


@cocotb.test()
async def early_eop_100_one_cycle_table_cleared(dut):
    """TIMING 100: exactly one cycle, the cycle after EOP; the table IS
    cleared (owner ruling) so the next packet starts empty; no rule
    counter moves (no error-occurrences counter exists)."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    exp = await check(dut, m, packet([(0x00, [0xAA])]) + idle(2))
    assert exp == [(0x00, ST_ACTIVE), (0x00, ST_ACTIVE),
                   (0x00, ST_EARLY_EOP), (0x00, ST_IDLE), (0x00, ST_IDLE)]
    # table was cleared: the next packet starts empty, so 5 more data
    # bytes do NOT fill it (a stale 1-byte slot0 would complete it and
    # yield 010 instead of this 100)
    exp = await check(dut, m, packet([(0x00, [0xBB]),
                                      (0x01, [0x11, 0x22]),
                                      (0x02, [0xDE, 0xAD])]))
    assert exp[-2] == (0x00, ST_ACTIVE)
    assert exp[-1] == (0x00, ST_EARLY_EOP)
    # both packets counted at start; no rule/no-rule counter moved
    exp = await check(dut, m, read_reg(0x1B) + read_reg(0x1F))
    assert exp == [(2, ST_READ_OK), (0, ST_READ_OK)]


@cocotb.test()
async def input_error_111_data_before_start(dut):
    """TIMING/I/O 111: entry, stickiness, frozen table, config ops
    swallowed, recovery only via rst_type=01, re-trigger."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    exp = await check(dut, m, [Cycle(ui_in=0x55, packet_status=PS_DATA)])
    assert exp == [(0x00, ST_INPUT_ERR)]
    # sticky: streaming and config ops are swallowed
    exp = await check(dut, m, packet([(0x00, [0xAA])], eop=False)
                    + [Cycle(cfg_mode=1, packet_status=CFG_READ, ui_in=0x1B)]
                    + [Cycle(packet_status=PS_EOP)])
    assert exp == [(0x00, ST_INPUT_ERR)] * 4
    # recovery only via rst_type=01; the swallowed bytes were not counted
    exp = await check(dut, m, soft_reset(RST_TABLE)
                    + read_reg(0x19) + read_reg(0x1B) + read_reg(0x1F))
    assert exp == [(0x00, ST_IDLE), (0, ST_READ_OK), (0, ST_READ_OK),
                   (0, ST_READ_OK)]
    # a fresh 111: other soft resets do NOT recover...
    exp = await check(dut, m, [Cycle(ui_in=0x55, packet_status=PS_DATA)])
    assert exp == [(0x00, ST_INPUT_ERR)]
    exp = await check(dut, m, soft_reset(RST_RULES) + soft_reset(RST_STATS))
    assert exp == [(0x00, ST_INPUT_ERR)] * 2
    # ...but rst_type=11 still cleared the (byte/packet) counters
    exp = await check(dut, m, soft_reset(RST_TABLE) + read_reg(0x19))
    assert exp == [(0x00, ST_IDLE), (0, ST_READ_OK)]
    # re-trigger rule: after the reset only 10 or 00 are legal
    exp = await check(dut, m, [Cycle(ui_in=0x55, packet_status=PS_DATA)])
    assert exp == [(0x00, ST_INPUT_ERR)]
    exp = await check(dut, m, soft_reset(RST_TABLE)
                  + [Cycle(packet_status=PS_EOP)])
    assert exp == [(0x00, ST_IDLE), (0x00, ST_INPUT_ERR)]
    exp = await check(dut, m, soft_reset(RST_TABLE) + idle(1)
                  + [Cycle(ui_in=0x00, packet_status=PS_META)])
    assert exp == [(0x00, ST_IDLE), (0x00, ST_IDLE), (0x00, ST_ACTIVE)]


@cocotb.test()
async def input_error_111_eop_before_start(dut):
    await start(dut)
    m = ref_model.PacketProcessorModel()
    exp = await check(dut, m, [Cycle(packet_status=PS_EOP)])
    assert exp == [(0x00, ST_INPUT_ERR)]
    exp = await check(dut, m, soft_reset(RST_TABLE)
                  + read_reg(0x1B) + read_reg(0x1F))
    assert exp == [(0x00, ST_IDLE), (0, ST_READ_OK),
                   (0, ST_READ_OK)]  # no packet started, no counter moved


@cocotb.test()
async def input_error_111_idle_mid_packet(dut):
    await start(dut)
    m = ref_model.PacketProcessorModel()
    exp = await check(dut, m, packet([(0x00, [0xAA])], eop=False) + [Cycle()])
    assert exp == [(0x00, ST_ACTIVE), (0x00, ST_ACTIVE),
                   (0x00, ST_INPUT_ERR)]
    exp = await check(dut, m, soft_reset(RST_TABLE)
                  + read_reg(0x1B) + read_reg(0x1F))
    assert exp == [(0x00, ST_IDLE), (1, ST_READ_OK),
                   (0, ST_READ_OK)]  # packet was started


@cocotb.test()
async def input_error_111_cfg_change_mid_packet(dut):
    await start(dut)
    m = ref_model.PacketProcessorModel()
    exp = await check(dut, m, packet([(0x00, [0xAA])], eop=False)
                    + [Cycle(cfg_mode=1, packet_status=CFG_READ, ui_in=0x00)])
    assert exp == [(0x00, ST_ACTIVE), (0x00, ST_ACTIVE),
                   (0x00, ST_INPUT_ERR)]
    exp = await check(dut, m, soft_reset(RST_TABLE)
                  + read_reg(0x1B) + read_reg(0x1F))
    assert exp == [(0x00, ST_IDLE), (1, ST_READ_OK),
                   (0, ST_READ_OK)]


@cocotb.test()
async def config_read_write_single_cycle(dut):
    """MODES: READ is single-cycle (110 + value); WRITE completes on the
    data cycle (011 + written value); address cycle gives no confirmation."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    exp = await check(dut, m, write_rule(2, 1, 0b101, 0b0110, 0x1122,
                                         0x5566, 0x99AA))
    assert exp[0] == (0x00, ST_IDLE)           # FLAGS address cycle: 000
    assert exp[1] == (0x6B, ST_WRITE_OK)       # FLAGS data cycle: 011/0x6B
    exp = await check(dut, m, read_reg(0x0A) + read_reg(0x0B)
                  + read_reg(0x10))
    assert exp == [(0x6B, ST_READ_OK), (0x11, ST_READ_OK),
                   (0xAA, ST_READ_OK)]
    # 110/011 are one-cycle states: the next cycle is back to 000
    exp = await check(dut, m, [Cycle(cfg_mode=1, packet_status=CFG_NONE)])
    assert exp == [(0x00, ST_IDLE)]


@cocotb.test()
async def config_write_rejections(dut):
    """MODES/I/O 101: all four rejection classes + 11-before-10; register
    unchanged; write_pending cleared by a rejected write (ruling 9); no
    counter moves."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    # duplicate type value (types default to 0,1,2)
    exp = await check(dut, m, write_reg(0x00, 0x01))
    assert exp == [(0x00, ST_IDLE), (0x00, ST_WRITE_ERR)]
    # counter addresses are read-only
    exp = await check(dut, m, write_reg(0x18, 0x55) + write_reg(0x1F, 0x55))
    assert exp[1] == (0x00, ST_WRITE_ERR) and exp[3] == (0x00, ST_WRITE_ERR)
    # FLAGS pattern XXXX_0001
    exp = await check(dut, m, write_reg(0x03, 0x01) + write_reg(0x03, 0xF1))
    assert exp[1] == (0x00, ST_WRITE_ERR) and exp[3] == (0x00, ST_WRITE_ERR)
    # 11 before 10
    exp = await check(dut, m, [Cycle(ui_in=0x99, cfg_mode=1,
                                     packet_status=CFG_WDATA)])
    assert exp == [(0x00, ST_WRITE_ERR)]
    # registers unchanged; legal flag 0xF0 works; same-value type rewrite
    exp = await check(dut, m, read_reg(0x00) + read_reg(0x03)
                  + write_reg(0x03, 0xF0) + write_reg(0x00, 0x00))
    assert exp == [(0x00, ST_READ_OK), (0x00, ST_READ_OK),
                   (0x00, ST_IDLE), (0xF0, ST_WRITE_OK),
                   (0x00, ST_IDLE), (0x00, ST_WRITE_OK)]
    # ruling 9: after the rejected write above, a plain 11 is again
    # data-before-address (pending was cleared by the rejection)
    exp = await check(dut, m, [Cycle(ui_in=0x99, cfg_mode=1,
                                     packet_status=CFG_WDATA)])
    assert exp == [(0x00, ST_WRITE_ERR)]
    # no counter moved by any of the rejections
    for addr in range(0x18, 0x20):
        exp = await check(dut, m, read_reg(addr))
        assert exp == [(0x00, ST_READ_OK)]


@cocotb.test()
async def config_pending_corners(dut):
    """MODES: 10->10 replace (no error); 01 cancels + READs; 00 keeps
    pending; packet-mode cycles never touch write_pending."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    exp = await check(dut, m, [Cycle(ui_in=0x04, cfg_mode=1, packet_status=CFG_WADDR),
                               Cycle(ui_in=0x05, cfg_mode=1, packet_status=CFG_WADDR),
                               Cycle(ui_in=0x77, cfg_mode=1, packet_status=CFG_WDATA)])
    assert exp == [(0x00, ST_IDLE), (0x00, ST_IDLE), (0x77, ST_WRITE_OK)]
    exp = await check(dut, m, read_reg(0x04) + read_reg(0x05))
    assert exp == [(0x00, ST_READ_OK), (0x77, ST_READ_OK)]
    # 01 cancels the pending write and performs the READ
    exp = await check(dut, m, [Cycle(ui_in=0x06, cfg_mode=1, packet_status=CFG_WADDR),
                               Cycle(ui_in=0x05, cfg_mode=1, packet_status=CFG_READ)])
    assert exp == [(0x00, ST_IDLE), (0x77, ST_READ_OK)]
    exp = await check(dut, m, read_reg(0x06))
    assert exp == [(0x00, ST_READ_OK)]
    # 00 keeps pending; packet-mode cycles don't disturb it
    exp = await check(dut, m, [Cycle(ui_in=0x07, cfg_mode=1, packet_status=CFG_WADDR)]
                    + [Cycle(cfg_mode=1, packet_status=CFG_NONE)]
                    + idle(2)
                    + [Cycle(ui_in=0x55, cfg_mode=1, packet_status=CFG_WDATA)])
    assert exp == [(0x00, ST_IDLE)] * 4 + [(0x55, ST_WRITE_OK)]
    exp = await check(dut, m, read_reg(0x07))
    assert exp == [(0x55, ST_READ_OK)]


@cocotb.test()
async def soft_reset_01_scope(dut):
    """rst_type=01: clears table + packet-active + input-error + result +
    out_state; priority over normal input; keeps rules/types/counters/
    write_pending."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    await check(dut, m, write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    exp = await check(dut, m, packet(STD_CHUNKS, eop=False))
    assert exp[-1] == (STD_HIT_OUT, ST_RESULT)     # result exists pre-EOP
    # 01 on the same cycle as a would-be input: reset wins, input ignored
    exp = await check(dut, m, [Cycle(ui_in=0x00, packet_status=PS_META,
                                     rst_type=RST_TABLE)])
    assert exp == [(0x00, ST_IDLE)]                # out_state cleared
    # full refill needed (table cleared); preserved rule hits again
    exp = await check(dut, m, packet(STD_CHUNKS))
    assert exp[-2:] == [(STD_HIT_OUT, ST_RESULT)] * 2
    exp = await check(dut, m, read_reg(0x1C) + read_reg(0x1B)
                  + read_reg(0x03) + read_reg(0x00))
    assert exp == [(1, ST_READ_OK), (2, ST_READ_OK),
                   (STD_FLAG, ST_READ_OK), (0x00, ST_READ_OK)]
    # write_pending survives rst_type=01 (ruling)
    exp = await check(dut, m, [Cycle(ui_in=0x05, cfg_mode=1, packet_status=CFG_WADDR)]
                    + soft_reset(RST_TABLE)
                    + [Cycle(ui_in=0x77, cfg_mode=1, packet_status=CFG_WDATA)])
    assert exp == [(0x00, ST_IDLE), (0x00, ST_IDLE), (0x77, ST_WRITE_OK)]


@cocotb.test()
async def soft_reset_10_scope_and_result_capture_corner(dut):
    """rst_type=10: rules -> 0, types -> 0,1,2, write_pending cleared
    (ruling); table/packet state/counters untouched; out_state reflects
    preserved state (ruling). THE RESULT-CAPTURE CORNER: the model
    captures the match ONCE at completion -- a rules reset afterwards does
    not change the 010 output or the EOP hit counting."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    await check(dut, m, write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    exp = await check(dut, m, packet(STD_CHUNKS, eop=False))
    assert exp[-1] == (STD_HIT_OUT, ST_RESULT)      # result captured
    # wipe the rules mid-packet: 010 keeps showing the CAPTURED result
    exp = await check(dut, m, soft_reset(RST_RULES))
    assert exp == [(STD_HIT_OUT, ST_RESULT)]        # ruling reflection
    exp = await check(dut, m, packet([(0x00, [])], eop=False))
    assert exp == [(STD_HIT_OUT, ST_RESULT)]        # still captured
    # EOP: hit counted for the captured rule 1
    exp = await check(dut, m, [Cycle(packet_status=PS_EOP)] + idle(1))
    assert exp == [(STD_HIT_OUT, ST_RESULT), (0x00, ST_IDLE)]
    exp = await check(dut, m, read_reg(0x1C) + read_reg(0x03)
                  + read_reg(0x00))
    assert exp == [(1, ST_READ_OK), (0x00, ST_READ_OK), (0x00, ST_READ_OK)]
    # write_pending cleared by rst_type=10 (owner ruling)
    exp = await check(dut, m, [Cycle(ui_in=0x05, cfg_mode=1, packet_status=CFG_WADDR)]
                    + soft_reset(RST_RULES)
                    + [Cycle(ui_in=0x77, cfg_mode=1, packet_status=CFG_WDATA)])
    assert exp == [(0x00, ST_IDLE), (0x00, ST_IDLE), (0x00, ST_WRITE_ERR)]


@cocotb.test()
async def soft_reset_11_scope(dut):
    """rst_type=11: statistics counters only; out_state reflects preserved
    state (ruling)."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    await check(dut, m, write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    exp = await check(dut, m, packet(STD_CHUNKS))
    assert exp[-1] == (STD_HIT_OUT, ST_RESULT)
    exp = await check(dut, m, soft_reset(RST_STATS))
    assert exp == [(0x00, ST_IDLE)]
    for addr in range(0x18, 0x20):
        exp = await check(dut, m, read_reg(addr))
        assert exp == [(0x00, ST_READ_OK)]
    # types/rules survive
    exp = await check(dut, m, read_reg(0x03))
    assert exp == [(STD_FLAG, ST_READ_OK)]
    # mid-packet rst_type=11: 001 reflected (ruling), counters cleared
    exp = await check(dut, m, packet([(0x00, [0xAA])], eop=False)
                  + soft_reset(RST_STATS))
    assert exp == [(0x00, ST_ACTIVE), (0x00, ST_ACTIVE), (0x00, ST_ACTIVE)]
    exp = await check(dut, m, [Cycle(rst_type=RST_TABLE)]  # clean up
                  + read_reg(0x19) + read_reg(0x1B))
    assert exp == [(0x00, ST_IDLE), (0x00, ST_READ_OK), (0x00, ST_READ_OK)]


@cocotb.test()
async def rst_n_async_at_arbitrary_cycles(dut):
    """RESET priority: rst_n clears everything, any time, even with ena=0
    (ruling)."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    await check(dut, m, set_type_values(0x10, 0x20, 0x30)
              + write_rule(1, 1, 0b111, 0b1010, *STD_VALUES)
              + packet([(0x10, [0xAA, 0xBB])], eop=False))
    # async reset in the middle of a packet
    exp = await check(dut, m, hard_reset(2))
    assert exp == [(0x00, ST_IDLE)] * 2
    for addr, want in enumerate([0x00, 0x01, 0x02]):
        exp = await check(dut, m, read_reg(addr))
        assert exp == [(want, ST_READ_OK)]
    exp = await check(dut, m, read_reg(0x1B) + read_reg(0x1F))
    assert exp == [(0, ST_READ_OK), (0, ST_READ_OK)]
    # hard reset works even while ena=0
    exp = await check(dut, m, packet([(0x00, [0xAA])], eop=False)
                  + [Cycle(ena=0, rst_n=0)])
    assert exp == [(0x00, ST_ACTIVE), (0x00, ST_ACTIVE), (0x00, ST_IDLE)]
    exp = await check(dut, m, read_reg(0x1B))
    assert exp == [(0, ST_READ_OK)]


@cocotb.test()
async def ena_gating(dut):
    """Ruling: ena=0 ignores ALL inputs (incl. rst_type, would-be errors),
    forces out_state=000/uo_out=0, holds state; resume works."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    await check(dut, m, write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    # a would-be input error is ignored while ena=0
    exp = await check(dut, m, [Cycle(ena=0, ui_in=0x55, packet_status=PS_DATA)])
    assert exp == [(0x00, ST_IDLE)]
    exp = await check(dut, m, packet([(0x00, [0xAA, 0xBB])], eop=False))
    assert exp == [(0x00, ST_ACTIVE)] * 3
    # mid-packet ena=0: outputs forced; idle-gap + soft reset ignored
    exp = await check(dut, m, [Cycle(ena=0), Cycle(ena=0),
                               Cycle(ena=0, rst_type=RST_TABLE)])
    assert exp == [(0x00, ST_IDLE)] * 3
    # resume mid-packet exactly where it stopped
    exp = await check(dut, m, packet([(0x01, [0x11, 0x22]),
                                      (0x02, [0xDE, 0xAD])]))
    assert exp[-2:] == [(STD_HIT_OUT, ST_RESULT)] * 2
    # ena=0 during a config write: the pending address survives the pause
    exp = await check(dut, m, [Cycle(ui_in=0x08, cfg_mode=1, packet_status=CFG_WADDR),
                               Cycle(ena=0, cfg_mode=1, packet_status=CFG_WDATA, ui_in=0x99),
                               Cycle(ui_in=0x66, cfg_mode=1, packet_status=CFG_WDATA)])
    assert exp == [(0x00, ST_IDLE), (0x00, ST_IDLE), (0x66, ST_WRITE_OK)]
    exp = await check(dut, m, read_reg(0x08))
    assert exp == [(0x66, ST_READ_OK)]
    # ena=0 during sticky 111: forced to 000, state held; 111 on resume
    m2 = ref_model.PacketProcessorModel()
    await hard_reset_dut(dut)
    exp = await check(dut, m2, [Cycle(ui_in=0x55, packet_status=PS_DATA),
                                Cycle(ena=0), Cycle(ena=0), idle(1)[0]])
    assert exp == [(0x00, ST_INPUT_ERR), (0x00, ST_IDLE), (0x00, ST_IDLE),
                   (0x00, ST_INPUT_ERR)]


@cocotb.test()
async def counter_rules(dut):
    """STATISTICS: bytes = metadata+data in packet mode (no EOP, no config,
    none during 111, dropped-but-valid counted); packets at start incl.
    later errors; hits once at EOP; error-after-result suppresses hit;
    counters read-only."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    # config traffic is not counted (14 write cycles + reads)
    await check(dut, m, write_rule(1, 1, 0b111, 0b1010, *STD_VALUES)
                  + read_reg(0x00) + read_reg(0x1C))
    exp = await check(dut, m, read_reg(0x19) + read_reg(0x1B))
    assert exp == [(0, ST_READ_OK), (0, ST_READ_OK)]
    # metadata+data count, EOP does not; hit counted once at EOP
    exp = await check(dut, m, packet(STD_CHUNKS, eop=False)
                    + packet([(0x00, []), (0x01, [])], eop=False)
                    + [Cycle(packet_status=PS_EOP)])
    assert exp[-1] == (STD_HIT_OUT, ST_RESULT)
    exp = await check(dut, m, read_reg(0x18) + read_reg(0x19)
                  + read_reg(0x1B) + read_reg(0x1C))
    assert exp == [(0, ST_READ_OK), (11, ST_READ_OK), (1, ST_READ_OK),
                   (1, ST_READ_OK)]
    # dropped-but-valid bytes count: unknown metadata + slot overflow.
    # Re-target rule 1's type-1 value at the overflow packet's slot0 first
    # (t1 bytes live at rule base+0x01..0x02); config cycles don't count.
    await check(dut, m, write_reg(0x04, 0x01) + write_reg(0x05, 0x02))
    exp = await check(dut, m, packet([(0x77, [0x99, 0x88]),
                                      (0x00, [1, 2, 3, 4]),
                                      (0x01, [0x11, 0x22]),
                                      (0x02, [0xDE, 0xAD])]))
    assert exp[-2:] == [(STD_HIT_OUT, ST_RESULT)] * 2
    exp = await check(dut, m, read_reg(0x19) + read_reg(0x1B)
                  + read_reg(0x1C))
    assert exp == [(25, ST_READ_OK), (2, ST_READ_OK), (2, ST_READ_OK)]
    # packet counted at start even when it later errors; no hit for it
    exp = await check(dut, m, packet([(0x00, [0xAA])], eop=False)
                  + [Cycle(packet_status=PS_IDLE)])  # idle gap -> 111
    assert exp[-1] == (0x00, ST_INPUT_ERR)
    exp = await check(dut, m, soft_reset(RST_TABLE) + read_reg(0x1B)
                  + read_reg(0x1C) + read_reg(0x1F))
    assert exp == [(0x00, ST_IDLE), (3, ST_READ_OK), (2, ST_READ_OK),
                   (0, ST_READ_OK)]
    # error AFTER a hit result suppresses that packet's hit count
    await check(dut, m, write_reg(0x04, 0xAA) + write_reg(0x05, 0xBB))
    exp = await check(dut, m, packet(STD_CHUNKS, eop=False) + [Cycle()])
    assert exp[-2:] == [(STD_HIT_OUT, ST_RESULT), (0x00, ST_INPUT_ERR)]
    exp = await check(dut, m, soft_reset(RST_TABLE) + read_reg(0x1C)
                  + read_reg(0x1F))
    assert exp == [(0x00, ST_IDLE), (2, ST_READ_OK), (0, ST_READ_OK)]
    # counters are read-only
    exp = await check(dut, m, write_reg(0x1C, 0x00))
    assert exp[1] == (0x00, ST_WRITE_ERR)
    exp = await check(dut, m, read_reg(0x1C))
    assert exp == [(2, ST_READ_OK)]


@cocotb.test()
async def counter_wrap_8bit(dut):
    """STATISTICS: an 8-bit counter (no_rule_hits) wraps modulo 256.
    (16-bit total_bytes wrap is covered at pp_stats unit level via
    deposit.)"""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    # no rules configured -> every completed packet increments no_rule_hits
    exp = await check(dut, m, packet(STD_CHUNKS) * 255)
    assert exp[-1] == (0x00, ST_RESULT)
    exp = await check(dut, m, read_reg(0x1F))
    assert exp == [(0xFF, ST_READ_OK)]
    exp = await check(dut, m, packet(STD_CHUNKS) + read_reg(0x1F))
    assert exp[-1] == (0x00, ST_READ_OK)  # wrapped


@cocotb.test()
async def back_to_back_packets_and_config_between(dut):
    """MODES: no idle gap needed between packets; config legal between
    packets."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    await check(dut, m, write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    exp = await check(dut, m, packet(STD_CHUNKS) + packet(STD_CHUNKS))
    assert exp == ([(0x00, ST_ACTIVE)] * 8
                   + [(STD_HIT_OUT, ST_RESULT)] * 2) * 2
    # config between packets, then a third packet
    exp = await check(dut, m, read_reg(0x1C) + write_reg(0x03, 0x00)
                  + packet(STD_CHUNKS))
    assert exp[0] == (2, ST_READ_OK)
    assert exp[-2:] == [(0x00, ST_RESULT)] * 2   # rule disabled -> no hit


@cocotb.test()
async def interleaved_zero_length_overflow_streams(dut):
    """MODES guidelines: partial/interleaved fills, repeated metadata,
    zero-length chunks, chunk longer than 2 bytes (overflow dropped)."""
    await start(dut)
    m = ref_model.PacketProcessorModel()
    await check(dut, m, write_rule(1, 1, 0b111, 0b1010, 0x0102,
                                   *STD_VALUES[1:]))
    exp = await check(dut, m, packet([
        (0x00, [0x01]),
        (0x01, [0x11]),
        (0x00, [0x02, 0x03, 0x04]),  # finishes 0102, 03/04 dropped
        (0x02, []),                  # zero-length chunk
        (0x01, [0x22]),
        (0x02, [0xDE, 0xAD]),
    ]))
    assert exp == [(0x00, ST_ACTIVE)] * 13 + [(STD_HIT_OUT, ST_RESULT)] * 2


# ---------------------------------------------------------------------------
# Random differential
# ---------------------------------------------------------------------------
@cocotb.test()
async def random_differential(dut):
    """Programmed classification, mixed traffic and legal readback per seed."""
    await start(dut)
    n_seeds = 50
    coverage = RegressionCoverage()
    for seed in range(n_seeds):
        case = build_case(seed, n_cycles=500)
        await hard_reset_dut(dut)
        m = ref_model.PacketProcessorModel()
        config_outputs = await check(dut, m, case.configuration, msg=f"seed {seed} setup")
        packet_outputs = await check(dut, m, case.packet, msg=f"seed {seed} packet")
        await check(dut, m, case.mixed, msg=f"seed {seed} mixed")
        sweep_outputs = await check(dut, m, case.sweep, msg=f"seed {seed} sweep")
        coverage.record(case, config_outputs, packet_outputs, sweep_outputs)
    # The top-level 1500-seed run additionally requires all masks/actions.
    coverage.assert_complete(n_seeds, full=False)
    dut._log.info("Chip regression coverage: %s", coverage.summary())
