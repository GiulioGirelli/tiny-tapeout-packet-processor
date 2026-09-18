"""Pytest self-tests for the packet-processor reference model (model.py),
configuration S4 (3 types, 16-bit entries, 5-bit/32-register map, 4-bit
actions, 8 statistics counters, no error-occurrences counter).

Every expectation in this file is hand-computed from description.md (S4)
incl. the CLARIFICATIONS section (owner rulings 09 Sep 2026, adapted to S4
on 10 Sep 2026). Expected values are literal so the tests double as a
readable, cycle-by-cycle statement of the spec's required behaviour.

The removed error-occurrences counter changes how errors are verified:
via out_state sequences (100/101/111) plus proof that the surviving
counters did NOT move (e.g. no_rule_hits unchanged), never via a counter.

Run (from the repo root, inside the toolchain container):
    python -m pytest test/test_model.py -q
"""

import random

import pytest

import generators
import ppctl
from ppctl import (Cycle, idle, hard_reset, soft_reset, read_reg, write_reg,
                   set_type_values, write_rule, packet,
                   PS_IDLE, PS_DATA, PS_META, PS_EOP,
                   CFG_NONE, CFG_READ, CFG_WADDR, CFG_WDATA,
                   RST_TABLE, RST_RULES, RST_STATS)
from model import (PacketProcessorModel,
                   ST_IDLE, ST_ACTIVE, ST_RESULT, ST_WRITE_OK, ST_EARLY_EOP,
                   ST_WRITE_ERR, ST_READ_OK, ST_INPUT_ERR)

# Standard collection-table contents used across the packet tests: with the
# default type registers 0x00..0x02 this packet fills all 3 slots (2 bytes
# each). 3 metadata + 6 data = 9 byte cycles, EOP on the 10th.
STD_VALUES = (0xAABB, 0x1122, 0xDEAD)
STD_CHUNKS = [(0x00, [0xAA, 0xBB]),
              (0x01, [0x11, 0x22]),
              (0x02, [0xDE, 0xAD])]
# Rule 1 with enable=1, mask=0b111, action=0b1010 -> flags 0xAF, and a hit
# on the standard table drives out = (0b1010<<4)|(0b10<<2)|0b01 = 0xA9.
STD_FLAG = 0xAF
STD_HIT_OUT = 0xA9

# Statistics addresses (S4 map)
A_TB_HI, A_TB_LO = 0x18, 0x19
A_TP_HI, A_TP_LO = 0x1A, 0x1B
A_R1, A_R2, A_R3, A_NR = 0x1C, 0x1D, 0x1E, 0x1F


# ---------------------------------------------------------------------------
# Reset state and register map
# ---------------------------------------------------------------------------
def test_reset_state_and_default_readback():
    m = PacketProcessorModel()
    assert m.run(idle(3)) == [(0x00, ST_IDLE)] * 3
    # ruling: type registers reset to 0x00,0x01,0x02 on hard reset
    for addr, want in enumerate([0x00, 0x01, 0x02]):
        assert m.run(read_reg(addr)) == [(want, ST_READ_OK)]
    # all 21 rule registers (0x03-0x17) are zero
    for addr in range(0x03, 0x18):
        assert m.run(read_reg(addr)) == [(0x00, ST_READ_OK)]
    # all 8 statistics registers (0x18-0x1F) are zero
    for addr in range(0x18, 0x20):
        assert m.run(read_reg(addr)) == [(0x00, ST_READ_OK)]


def test_hard_reset_clears_everything():
    m = PacketProcessorModel()
    m.run(set_type_values(0x10, 0x20, 0x30))
    m.run(write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    m.run(packet([(0x10, [0xAA, 0xBB])], eop=False))
    assert m.run(hard_reset(2)) == [(0x00, ST_IDLE)] * 2
    for addr, want in enumerate([0x00, 0x01, 0x02]):
        assert m.run(read_reg(addr)) == [(want, ST_READ_OK)]
    assert m.run(read_reg(0x03)) == [(0x00, ST_READ_OK)]
    for addr in range(0x18, 0x20):
        assert m.run(read_reg(addr)) == [(0x00, ST_READ_OK)]
    # packet FSM is idle again: a data byte before metadata raises 111
    assert m.run([Cycle(ui_in=0x00, packet_status=PS_DATA)]) \
        == [(0x00, ST_INPUT_ERR)]


# ---------------------------------------------------------------------------
# Config mode: READ/WRITE, rejections, pending-WRITE corners
# ---------------------------------------------------------------------------
def test_rule_write_and_readback():
    m = PacketProcessorModel()
    values = (0x1122, 0x5566, 0x99AA)
    # rule 2 base 0x0A; flags = (0b0110<<4)|(0b101<<1)|1 = 0x6B
    expected_bytes = [0x6B, 0x11, 0x22, 0x55, 0x66, 0x99, 0xAA]
    outs = m.run(write_rule(2, 1, 0b101, 0b0110, *values))
    expected = []
    for data in expected_bytes:
        # address cycle: no confirmation; data cycle: 011 + written value
        expected += [(0x00, ST_IDLE), (data, ST_WRITE_OK)]
    assert outs == expected
    for addr, data in zip(range(0x0A, 0x11), expected_bytes):
        assert m.run(read_reg(addr)) == [(data, ST_READ_OK)]
    # rules 1 and 3 untouched
    assert m.run(read_reg(0x03)) == [(0x00, ST_READ_OK)]
    assert m.run(read_reg(0x11)) == [(0x00, ST_READ_OK)]


def test_write_rule_boundaries():
    m = PacketProcessorModel()
    # last rule register (rule 3 type3 LSB) is writable; the first
    # statistics address is the read-only boundary
    assert m.run(write_reg(0x17, 0x7E)) \
        == [(0x00, ST_IDLE), (0x7E, ST_WRITE_OK)]
    assert m.run(read_reg(0x17)) == [(0x7E, ST_READ_OK)]
    assert m.run(write_reg(0x18, 0x7E)) \
        == [(0x00, ST_IDLE), (0x00, ST_WRITE_ERR)]
    assert m.run(read_reg(0x18)) == [(0x00, ST_READ_OK)]


def test_write_rejections_leave_register_unchanged():
    m = PacketProcessorModel()
    # 1) duplicate type value: 0x01 already sits in type register 1
    assert m.run(write_reg(0x00, 0x01)) \
        == [(0x00, ST_IDLE), (0x00, ST_WRITE_ERR)]
    assert m.run(read_reg(0x00)) == [(0x00, ST_READ_OK)]
    # writing the value the SAME register already holds is legal (ruling)
    assert m.run(write_reg(0x00, 0x00)) \
        == [(0x00, ST_IDLE), (0x00, ST_WRITE_OK)]
    # 2) statistics registers are read-only (first and last counter address)
    assert m.run(write_reg(0x18, 0x55)) \
        == [(0x00, ST_IDLE), (0x00, ST_WRITE_ERR)]
    assert m.run(write_reg(0x1F, 0x55)) \
        == [(0x00, ST_IDLE), (0x00, ST_WRITE_ERR)]
    assert m.run(read_reg(0x18)) == [(0x00, ST_READ_OK)]
    # 3) flag pattern XXXX_0001 (rule enabled, no type checks): 0x01 / 0xF1
    assert m.run(write_reg(0x03, 0x01)) \
        == [(0x00, ST_IDLE), (0x00, ST_WRITE_ERR)]
    assert m.run(write_reg(0x03, 0xF1)) \
        == [(0x00, ST_IDLE), (0x00, ST_WRITE_ERR)]
    assert m.run(read_reg(0x03)) == [(0x00, ST_READ_OK)]
    # 0xF0 (disabled rule, no type checks) is a legal flag
    assert m.run(write_reg(0x03, 0xF0)) \
        == [(0x00, ST_IDLE), (0xF0, ST_WRITE_OK)]
    # 4) WRITE data (11) before any WRITE address (10)
    assert m.run([Cycle(ui_in=0x99, cfg_mode=1, packet_status=CFG_WDATA)]) \
        == [(0x00, ST_WRITE_ERR)]
    # no counter moved: the rejected writes touched nothing
    for addr in range(0x18, 0x20):
        assert m.run(read_reg(addr)) == [(0x00, ST_READ_OK)]


def test_counter_write_rejected_at_every_stats_address():
    m = PacketProcessorModel()
    for addr in range(0x18, 0x20):
        assert m.run(write_reg(addr, 0x55)) \
            == [(0x00, ST_IDLE), (0x00, ST_WRITE_ERR)]
        assert m.run(read_reg(addr)) == [(0x00, ST_READ_OK)]


def test_pending_write_corners():
    m = PacketProcessorModel()
    m.run(write_reg(0x06, 0x42))  # completed write, referenced below
    # 10 -> 10 replaces the pending address; the first one is wasted
    outs = m.run([Cycle(ui_in=0x04, cfg_mode=1, packet_status=CFG_WADDR),
                  Cycle(ui_in=0x05, cfg_mode=1, packet_status=CFG_WADDR),
                  Cycle(ui_in=0x77, cfg_mode=1, packet_status=CFG_WDATA)])
    assert outs == [(0x00, ST_IDLE), (0x00, ST_IDLE), (0x77, ST_WRITE_OK)]
    assert m.run(read_reg(0x04)) == [(0x00, ST_READ_OK)]  # never written
    assert m.run(read_reg(0x05)) == [(0x77, ST_READ_OK)]
    # 11 right after a completed write (no pending address) -> 101
    assert m.run([Cycle(ui_in=0x99, cfg_mode=1, packet_status=CFG_WDATA)]) \
        == [(0x00, ST_WRITE_ERR)]
    assert m.run(read_reg(0x06)) == [(0x42, ST_READ_OK)]  # not overwritten
    # 01 cancels the pending WRITE and performs the READ instead
    outs = m.run([Cycle(ui_in=0x07, cfg_mode=1, packet_status=CFG_WADDR),
                  Cycle(ui_in=0x06, cfg_mode=1, packet_status=CFG_READ)])
    assert outs == [(0x00, ST_IDLE), (0x42, ST_READ_OK)]
    assert m.run(read_reg(0x07)) == [(0x00, ST_READ_OK)]  # write cancelled
    # 00 keeps the pending address alive
    outs = m.run([Cycle(ui_in=0x08, cfg_mode=1, packet_status=CFG_WADDR)]
                 + [Cycle(cfg_mode=1, packet_status=CFG_NONE)] * 2
                 + [Cycle(ui_in=0x55, cfg_mode=1, packet_status=CFG_WDATA)])
    assert outs == [(0x00, ST_IDLE)] * 3 + [(0x55, ST_WRITE_OK)]
    assert m.run(read_reg(0x08)) == [(0x55, ST_READ_OK)]
    # packet-mode cycles do not disturb a pending write (ruling)
    outs = m.run([Cycle(ui_in=0x09, cfg_mode=1, packet_status=CFG_WADDR),
                  Cycle(packet_status=PS_IDLE),
                  Cycle(ui_in=0x66, cfg_mode=1, packet_status=CFG_WDATA)])
    assert outs == [(0x00, ST_IDLE), (0x00, ST_IDLE), (0x66, ST_WRITE_OK)]
    assert m.run(read_reg(0x09)) == [(0x66, ST_READ_OK)]


def test_rejected_write_clears_pending_owner_ruling_9():
    m = PacketProcessorModel()
    # duplicate type value: rejected -> 101, register unchanged
    assert m.run(write_reg(0x00, 0x01)) \
        == [(0x00, ST_IDLE), (0x00, ST_WRITE_ERR)]
    # the failed attempt COMPLETES the operation: pending is cleared, so a
    # bare 11 now is a fresh "data before address" error (owner ruling #9)
    assert m.run([Cycle(ui_in=0x99, cfg_mode=1, packet_status=CFG_WDATA)]) \
        == [(0x00, ST_WRITE_ERR)]
    assert m.run(read_reg(0x00)) == [(0x00, ST_READ_OK)]
    # a whole new WRITE must (and can) restart from packet_status=10
    assert m.run(write_reg(0x00, 0x05)) \
        == [(0x00, ST_IDLE), (0x05, ST_WRITE_OK)]


def test_address_cycles_ignore_upper_bits():
    """IN PINS: config addresses use in[4:0]; in[7:5] are ignored."""
    m = PacketProcessorModel()
    m.run(write_reg(0x04, 0x5A))
    # READ with in[7:5] set behaves as if only in[4:0] were present
    assert m.run([Cycle(ui_in=0xE0 | 0x04, cfg_mode=1, packet_status=CFG_READ)]) \
        == [(0x5A, ST_READ_OK)]
    # WRITE address with in[7:5] set latches in[4:0]
    assert m.run([Cycle(ui_in=0xA0 | 0x05, cfg_mode=1, packet_status=CFG_WADDR),
                  Cycle(ui_in=0x77, cfg_mode=1, packet_status=CFG_WDATA)]) \
        == [(0x00, ST_IDLE), (0x77, ST_WRITE_OK)]
    assert m.run(read_reg(0x05)) == [(0x77, ST_READ_OK)]


# ---------------------------------------------------------------------------
# Packet mode: matching, priority, result packing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rule_id,expected_out",
                         [(1, 0x19), (2, 0x2A), (3, 0x3B)])
def test_packet_hit_each_rule(rule_id, expected_out):
    # out = (action<<4)|((3-1)<<2)|rule_id with action == rule_id
    m = PacketProcessorModel()
    m.run(write_rule(rule_id, 1, 0b111, rule_id, *STD_VALUES))
    assert m.run(idle(2)) == [(0x00, ST_IDLE)] * 2
    outs = m.run(packet(STD_CHUNKS))
    # 001 from the cycle after the first metadata until the table completes
    # (9 byte cycles); 010 at stage 2 of the completing byte AND of EOP
    assert outs == [(0x00, ST_ACTIVE)] * 8 + [(expected_out, ST_RESULT)] * 2
    assert m.run(idle(1)) == [(0x00, ST_IDLE)]
    assert m.run(read_reg(A_TB_LO)) == [(9, ST_READ_OK)]  # 3 meta + 6 data
    assert m.run(read_reg(A_TP_LO)) == [(1, ST_READ_OK)]
    assert m.run(read_reg(0x1B + rule_id)) == [(1, ST_READ_OK)]  # 0x1C/1D/1E


def test_multi_hit_highest_rule_number_wins():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 1, *STD_VALUES))
    m.run(write_rule(2, 1, 0b111, 2, *STD_VALUES))
    m.run(write_rule(3, 1, 0b111, 3, *STD_VALUES))
    # all three rules hit -> rule 3 wins (highest number = highest priority)
    assert m.run(packet(STD_CHUNKS))[-2:] == [(0x3B, ST_RESULT)] * 2
    # rule 3 disabled -> rule 2 wins
    m.run(write_reg(0x11, 0x00))
    assert m.run(packet(STD_CHUNKS))[-2:] == [(0x2A, ST_RESULT)] * 2
    # rules 2 and 3 disabled -> rule 1 wins
    m.run(write_reg(0x0A, 0x00))
    assert m.run(packet(STD_CHUNKS))[-2:] == [(0x19, ST_RESULT)] * 2
    # one hit counted per packet, at its EOP
    assert m.run(read_reg(A_R1)) == [(1, ST_READ_OK)]
    assert m.run(read_reg(A_R2)) == [(1, ST_READ_OK)]
    assert m.run(read_reg(A_R3)) == [(1, ST_READ_OK)]
    assert m.run(read_reg(A_TP_LO)) == [(3, ST_READ_OK)]


def test_no_hit_outputs_zeros():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 0b1010, 0x0BAD, *STD_VALUES[1:]))
    outs = m.run(packet(STD_CHUNKS))
    # 010 held, out = zeros: rule number 0 and the NOP action 4'b0000
    assert outs[-2:] == [(0x00, ST_RESULT)] * 2
    assert m.run(read_reg(A_NR)) == [(1, ST_READ_OK)]  # no_rule_hits
    assert m.run(read_reg(A_R1)) == [(0, ST_READ_OK)]


@pytest.mark.parametrize("mask,expected_out",
                         [(0b001, 0x21), (0b011, 0x25), (0b111, 0x29)])
def test_type_count_encodings(mask, expected_out):
    # out[3:2] encodes the number of checked types: 00,01,10 = 1,2,3
    m = PacketProcessorModel()
    values = [STD_VALUES[t] if (mask >> t) & 1 else 0x5A00 + t
              for t in range(3)]
    m.run(write_rule(1, 1, mask, 0b010, *values))
    outs = m.run(packet(STD_CHUNKS))
    assert outs[-2:] == [(expected_out, ST_RESULT)] * 2


def test_result_packing_fields():
    # rule 3, action 0xF, mask 0b010 (type2 only, count 1 -> encoding 00):
    # out = (0xF<<4)|(0<<2)|3 = 0xF3
    m = PacketProcessorModel()
    m.run(write_rule(3, 1, 0b010, 0xF, 0x0000, STD_VALUES[1], 0x0000))
    outs = m.run(packet(STD_CHUNKS))
    assert outs[-2:] == [(0xF3, ST_RESULT)] * 2


def test_flag_action_nop_is_legal():
    # action 4'b0000 (NOP) is a legal programmed action for an enabled rule
    m = PacketProcessorModel()
    outs = m.run(write_rule(1, 1, 0b111, 0b0000, *STD_VALUES))
    assert outs[1] == (0x0F, ST_WRITE_OK)  # flags = enable|mask, action 0
    outs = m.run(packet(STD_CHUNKS))
    assert outs[-2:] == [(0x09, ST_RESULT)] * 2  # (0<<4)|(2<<2)|1


# ---------------------------------------------------------------------------
# Collection-table behaviour
# ---------------------------------------------------------------------------
def test_interleaved_partial_fills_and_repeated_metadata():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    outs = m.run(packet([
        (0x00, [0xAA]),
        (0x01, [0x11]),
        (0x00, [0xBB]),           # slot0 = AABB
        (0x02, [0xDE]),
        (0x01, [0x22]),           # slot1 = 1122
        (0x02, [0xAD]),           # slot2 = DEAD -> table full
    ]))
    assert outs == [(0x00, ST_ACTIVE)] * 11 + [(STD_HIT_OUT, ST_RESULT)] * 2
    assert m.run(read_reg(A_TB_LO)) == [(12, ST_READ_OK)]  # 6 meta + 6 data


def test_zero_length_chunks():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    outs = m.run(packet([
        (0x00, []),                     # zero-length chunk
        (0x00, [0xAA, 0xBB]),
        (0x01, []),                     # zero-length chunk
        (0x01, [0x11, 0x22]),
        (0x02, [0xDE, 0xAD]),
    ]))
    assert outs == [(0x00, ST_ACTIVE)] * 10 + [(STD_HIT_OUT, ST_RESULT)] * 2


def test_slot_overflow_bytes_dropped():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 0b1010, 0x0102, *STD_VALUES[1:]))
    outs = m.run(packet([
        (0x00, [0x01, 0x02, 0x03, 0x04]),  # 2 overflow bytes dropped
        (0x01, [0x11, 0x22]),
        (0x02, [0xDE, 0xAD]),
    ]))
    # slot0 keeps its first 2 bytes MSB-first (0x0102) -> the rule hits
    assert outs == [(0x00, ST_ACTIVE)] * 10 + [(STD_HIT_OUT, ST_RESULT)] * 2
    # overflow bytes are accepted traffic and count towards total_bytes
    assert m.run(read_reg(A_TB_LO)) == [(11, ST_READ_OK)]


def test_non_matching_metadata_data_dropped():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    outs = m.run(packet([
        (0x77, [0x99, 0x88]),           # unknown type: its data is dropped
        (0x00, [0xAA, 0xBB]),
        (0x01, [0x11, 0x22]),
        (0x02, [0xDE, 0xAD]),
    ]))
    assert outs == [(0x00, ST_ACTIVE)] * 11 + [(STD_HIT_OUT, ST_RESULT)] * 2
    assert m.run(read_reg(A_TB_LO)) == [(12, ST_READ_OK)]  # dropped count too


def test_table_cleared_after_packet_processed():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    assert m.run(packet(STD_CHUNKS))[-2:] == [(STD_HIT_OUT, ST_RESULT)] * 2
    # re-target rule 1 at different data; a stale (still full) table would
    # never complete again and the EOP would raise 100 instead of 010
    m.run(write_rule(1, 1, 0b111, 0b0100, 0x0102, 0x0506, 0x090A))
    outs = m.run(packet([(0x00, [0x01, 0x02]),
                         (0x01, [0x05, 0x06]),
                         (0x02, [0x09, 0x0A])]))
    assert outs == [(0x00, ST_ACTIVE)] * 8 + [(0x49, ST_RESULT)] * 2


def test_back_to_back_packets_no_idle_gap():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    outs = m.run(packet(STD_CHUNKS) + packet(STD_CHUNKS))
    assert outs == ([(0x00, ST_ACTIVE)] * 8
                    + [(STD_HIT_OUT, ST_RESULT)] * 2) * 2
    assert m.run(read_reg(A_TP_LO)) == [(2, ST_READ_OK)]
    assert m.run(read_reg(A_R1)) == [(2, ST_READ_OK)]
    assert m.run(read_reg(A_TB_LO)) == [(18, ST_READ_OK)]


# ---------------------------------------------------------------------------
# Errors: early EOP (100) and sticky input error (111) -- no error counter
# ---------------------------------------------------------------------------
def test_early_eop_100_one_cycle_and_counters():
    m = PacketProcessorModel()
    outs = m.run(packet([(0x00, [0xAA])]))
    assert outs == [(0x00, ST_ACTIVE), (0x00, ST_ACTIVE),
                    (0x00, ST_EARLY_EOP)]
    # 100 is set for exactly one cycle (the cycle after EOP)
    assert m.run(idle(2)) == [(0x00, ST_IDLE)] * 2
    # the packet and its 2 accepted bytes were counted...
    assert m.run(read_reg(A_TP_LO)) == [(1, ST_READ_OK)]
    assert m.run(read_reg(A_TB_LO)) == [(2, ST_READ_OK)]
    # ...but no rule counter moved (no hit was ever computed, and there is
    # no error-occurrences counter to absorb the event)
    assert m.run(read_reg(A_NR)) == [(0, ST_READ_OK)]
    assert m.run(read_reg(A_R1)) == [(0, ST_READ_OK)]


def test_early_eop_clears_table_owner_ruling_q1b():
    # Owner ruling (CLARIFICATIONS): the collection table IS cleared at an
    # early-EOP (100) error; every new packet starts from an empty table.
    m = PacketProcessorModel()
    # rule 1 matches only if slot0 is built purely from the SECOND packet's
    # bytes: a stale [0xAA] leftover would make it 0xAABB, not 0xBBCC
    m.run(write_rule(1, 1, 0b111, 0b1010, 0xBBCC, *STD_VALUES[1:]))
    assert m.run(packet([(0x00, [0xAA])]))[-1] == (0x00, ST_EARLY_EOP)
    outs = m.run(packet([(0x00, [0xBB, 0xCC]),
                         (0x01, [0x11, 0x22]),
                         (0x02, [0xDE, 0xAD])]))
    assert outs == [(0x00, ST_ACTIVE)] * 8 + [(STD_HIT_OUT, ST_RESULT)] * 2
    assert m.run(read_reg(A_TP_LO)) == [(2, ST_READ_OK)]
    assert m.run(read_reg(A_R1)) == [(1, ST_READ_OK)]
    assert m.run(read_reg(A_NR)) == [(0, ST_READ_OK)]


def test_input_error_data_before_metadata_sticky_and_recovery():
    m = PacketProcessorModel()
    assert m.run([Cycle(ui_in=0x55, packet_status=PS_DATA)]) \
        == [(0x00, ST_INPUT_ERR)]
    # sticky: further streaming is swallowed; the table stays frozen
    assert m.run(packet([(0x00, [0xAA, 0xBB])], eop=False)) \
        == [(0x00, ST_INPUT_ERR)] * 3
    assert m.run([Cycle(packet_status=PS_EOP)]) == [(0x00, ST_INPUT_ERR)]
    # only rst_type=01 recovers
    assert m.run(soft_reset(RST_TABLE)) == [(0x00, ST_IDLE)]
    # the offending byte and the swallowed bytes were not counted (ruling)
    assert m.run(read_reg(A_TB_LO)) == [(0, ST_READ_OK)]
    assert m.run(read_reg(A_TP_LO)) == [(0, ST_READ_OK)]
    assert m.run(read_reg(A_NR)) == [(0, ST_READ_OK)]
    # after the reset, 00 and 10 are legal again
    assert m.run(idle(1)) == [(0x00, ST_IDLE)]
    assert m.run([Cycle(ui_in=0x00, packet_status=PS_META)]) \
        == [(0x00, ST_ACTIVE)]


def test_input_error_retrigger_after_reset():
    m = PacketProcessorModel()
    m.run([Cycle(ui_in=0x55, packet_status=PS_DATA)])      # enters 111
    m.run(soft_reset(RST_TABLE))
    # the cycle after the reset must be 10 or 00, otherwise 111 re-triggers
    assert m.run([Cycle(ui_in=0x55, packet_status=PS_DATA)]) \
        == [(0x00, ST_INPUT_ERR)]
    m.run(soft_reset(RST_TABLE))
    assert m.run([Cycle(packet_status=PS_EOP)]) == [(0x00, ST_INPUT_ERR)]
    m.run(soft_reset(RST_TABLE))
    assert m.run(idle(1)) == [(0x00, ST_IDLE)]             # 00 is fine
    assert m.run([Cycle(ui_in=0x00, packet_status=PS_META)]) \
        == [(0x00, ST_ACTIVE)]
    # three error entries happened, and no counter absorbed any of them;
    # total_bytes only saw the final accepted metadata byte
    assert m.run(soft_reset(RST_TABLE)) == [(0x00, ST_IDLE)]
    assert m.run(read_reg(A_NR)) == [(0, ST_READ_OK)]
    assert m.run(read_reg(A_TB_LO)) == [(1, ST_READ_OK)]


def test_input_error_idle_mid_packet():
    m = PacketProcessorModel()
    outs = m.run(packet([(0x00, [0xAA])], eop=False) + [Cycle()])
    assert outs == [(0x00, ST_ACTIVE), (0x00, ST_ACTIVE),
                    (0x00, ST_INPUT_ERR)]
    assert m.run([Cycle(ui_in=0x00, packet_status=PS_META)]) \
        == [(0x00, ST_INPUT_ERR)]  # sticky
    assert m.run(soft_reset(RST_TABLE)) == [(0x00, ST_IDLE)]
    assert m.run(read_reg(A_TP_LO)) == [(1, ST_READ_OK)]  # packet was started


def test_input_error_cfg_mode_change_mid_packet():
    m = PacketProcessorModel()
    outs = m.run(packet([(0x00, [0xAA])], eop=False)
                 + [Cycle(cfg_mode=1, packet_status=CFG_READ, ui_in=0x00)])
    assert outs == [(0x00, ST_ACTIVE), (0x00, ST_ACTIVE),
                    (0x00, ST_INPUT_ERR)]
    # config operations are swallowed while 111 is sticky (ruling)
    assert m.run([Cycle(cfg_mode=1, packet_status=CFG_READ, ui_in=0x1B)]) \
        == [(0x00, ST_INPUT_ERR)]
    assert m.run(soft_reset(RST_TABLE)) == [(0x00, ST_IDLE)]
    assert m.run(read_reg(A_TP_LO)) == [(1, ST_READ_OK)]


# ---------------------------------------------------------------------------
# Soft resets: exact scope of each rst_type code
# ---------------------------------------------------------------------------
def test_rst_table_scope():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    outs = m.run(packet(STD_CHUNKS, eop=False))  # table full, result ready
    assert outs[-1] == (STD_HIT_OUT, ST_RESULT)
    # 01 clears: collection table, packet-active state, current result,
    # out_state (here pre-EOP, so no hit is ever counted for this packet)
    assert m.run(soft_reset(RST_TABLE)) == [(0x00, ST_IDLE)]
    # a full refill is needed -> the table was really cleared; the
    # preserved rule hits again
    outs = m.run(packet(STD_CHUNKS))
    assert outs == [(0x00, ST_ACTIVE)] * 8 + [(STD_HIT_OUT, ST_RESULT)] * 2
    # untouched by 01: rules, type registers, counters
    assert m.run(read_reg(A_R1)) == [(1, ST_READ_OK)]     # only packet 2
    assert m.run(read_reg(A_TP_LO)) == [(2, ST_READ_OK)]  # both started
    assert m.run(read_reg(0x00)) == [(0x00, ST_READ_OK)]
    assert m.run(read_reg(0x03)) == [(STD_FLAG, ST_READ_OK)]


def test_rst_table_preserves_write_pending():
    # owner ruling: rst_type=01 does not clear the config FSM / write_pending.
    m = PacketProcessorModel()
    assert m.run([Cycle(ui_in=0x05, cfg_mode=1, packet_status=CFG_WADDR)]) \
        == [(0x00, ST_IDLE)]
    assert m.run(soft_reset(RST_TABLE)) == [(0x00, ST_IDLE)]
    assert m.run([Cycle(ui_in=0x77, cfg_mode=1, packet_status=CFG_WDATA)]) \
        == [(0x77, ST_WRITE_OK)]
    assert m.run(read_reg(0x05)) == [(0x77, ST_READ_OK)]


def test_rst_stats_preserves_write_pending():
    # owner ruling: write_pending also survives rst_type=11.
    m = PacketProcessorModel()
    assert m.run([Cycle(ui_in=0x06, cfg_mode=1, packet_status=CFG_WADDR)]) \
        == [(0x00, ST_IDLE)]
    assert m.run(soft_reset(RST_STATS)) == [(0x00, ST_IDLE)]
    assert m.run([Cycle(ui_in=0x88, cfg_mode=1, packet_status=CFG_WDATA)]) \
        == [(0x88, ST_WRITE_OK)]
    assert m.run(read_reg(0x06)) == [(0x88, ST_READ_OK)]


def test_rst_rules_clears_write_pending():
    # owner ruling: rst_type=10 clears write_pending.
    m = PacketProcessorModel()
    assert m.run([Cycle(ui_in=0x05, cfg_mode=1, packet_status=CFG_WADDR)]) \
        == [(0x00, ST_IDLE)]
    assert m.run(soft_reset(RST_RULES)) == [(0x00, ST_IDLE)]
    # pending is gone: the data byte now arrives before any address -> 101
    assert m.run([Cycle(ui_in=0x77, cfg_mode=1, packet_status=CFG_WDATA)]) \
        == [(0x00, ST_WRITE_ERR)]
    assert m.run(read_reg(0x05)) == [(0x00, ST_READ_OK)]  # never written


def test_rst_rules_scope():
    m = PacketProcessorModel()
    m.run(set_type_values(0x10, 0x20, 0x30))
    m.run(write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    assert m.run(packet([(0x10, [0xAA])]))[-1] == (0x00, ST_EARLY_EOP)
    assert m.run(soft_reset(RST_RULES)) == [(0x00, ST_IDLE)]
    # type registers back to the ruling defaults, rule table zeroed
    for addr, want in enumerate([0x00, 0x01, 0x02]):
        assert m.run(read_reg(addr)) == [(want, ST_READ_OK)]
    assert m.run(read_reg(0x03)) == [(0x00, ST_READ_OK)]
    # counters are not touched by rst_type=10
    assert m.run(read_reg(A_TP_LO)) == [(1, ST_READ_OK)]
    assert m.run(read_reg(A_TB_LO)) == [(2, ST_READ_OK)]


def test_rst_rules_mid_packet_preserves_table_and_out_state():
    m = PacketProcessorModel()
    outs = m.run(packet([(0x00, [0xAA, 0xBB]),
                         (0x01, [0x11])], eop=False))
    assert outs == [(0x00, ST_ACTIVE)] * 5
    # rst_type=10 does not touch packet processing: 001 is preserved
    # during the reset cycle (owner ruling)
    assert m.run(soft_reset(RST_RULES)) == [(0x00, ST_ACTIVE)]
    outs = m.run(packet([(0x01, [0x22]),
                         (0x02, [0xDE, 0xAD])]))
    # the preserved table completes, but the rules are gone -> 010 / zeros
    assert outs == [(0x00, ST_ACTIVE)] * 4 + [(0x00, ST_RESULT)] * 2
    assert m.run(read_reg(A_TB_LO)) == [(10, ST_READ_OK)]  # untouched
    assert m.run(read_reg(A_NR)) == [(1, ST_READ_OK)]


def test_rst_stats_scope():
    m = PacketProcessorModel()
    m.run(set_type_values(0x10, 0x20, 0x30))
    m.run(write_rule(2, 1, 0b111, 0b1010, *STD_VALUES))
    outs = m.run(packet([(0x10, [0xAA, 0xBB]),
                         (0x20, [0x11, 0x22]),
                         (0x30, [0xDE, 0xAD])]))
    assert outs[-2:] == [(0xAA, ST_RESULT)] * 2  # rule 2: (0xA<<4)|(2<<2)|2
    assert m.run(soft_reset(RST_STATS)) == [(0x00, ST_IDLE)]
    for addr in range(0x18, 0x20):
        assert m.run(read_reg(addr)) == [(0x00, ST_READ_OK)]
    # types and rules survive rst_type=11
    for addr, want in enumerate([0x10, 0x20, 0x30]):
        assert m.run(read_reg(addr)) == [(want, ST_READ_OK)]
    assert m.run(read_reg(0x0A)) == [(STD_FLAG, ST_READ_OK)]


def test_rst_stats_mid_packet_preserves_out_state():
    m = PacketProcessorModel()
    assert m.run([Cycle(ui_in=0x00, packet_status=PS_META)]) \
        == [(0x00, ST_ACTIVE)]
    assert m.run(soft_reset(RST_STATS)) == [(0x00, ST_ACTIVE)]  # ruling
    assert m.run(soft_reset(RST_TABLE)) == [(0x00, ST_IDLE)]


# ---------------------------------------------------------------------------
# Statistics rules
# ---------------------------------------------------------------------------
def test_counters_bytes_packets_config_excluded():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))  # 14 config cycles
    for _ in range(3):
        m.run(read_reg(0x00))
    # config-mode traffic is not counted
    assert m.run(read_reg(A_TB_LO)) == [(0, ST_READ_OK)]
    assert m.run(read_reg(A_TP_LO)) == [(0, ST_READ_OK)]
    # metadata and data bytes count; the EOP flag does not
    assert m.run(packet(STD_CHUNKS))[-1] == (STD_HIT_OUT, ST_RESULT)
    assert m.run(read_reg(A_TB_HI)) == [(0x00, ST_READ_OK)]
    assert m.run(read_reg(A_TB_LO)) == [(9, ST_READ_OK)]
    assert m.run(read_reg(A_TP_LO)) == [(1, ST_READ_OK)]
    # a packet is counted when it starts, even if it ends in an error
    m.run(packet([(0x77, [0x99, 0x88])]))  # unknown metadata, early EOP
    assert m.run(read_reg(A_TB_LO)) == [(12, ST_READ_OK)]
    assert m.run(read_reg(A_TP_LO)) == [(2, ST_READ_OK)]
    # the first packet hit rule 1; the second never completed a match
    assert m.run(read_reg(A_R1)) == [(1, ST_READ_OK)]
    assert m.run(read_reg(A_NR)) == [(0, ST_READ_OK)]


def test_hit_counted_once_at_eop_not_per_cycle():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    stim = packet(STD_CHUNKS, eop=False)
    stim += packet([(0x00, []), (0x01, []), (0x02, [])], eop=False)
    stim += [Cycle(packet_status=PS_EOP)]
    outs = m.run(stim)
    # 010 held for 5 cycles: completing byte, 3 streamed metadatas, EOP
    assert outs == [(0x00, ST_ACTIVE)] * 8 + [(STD_HIT_OUT, ST_RESULT)] * 5
    assert m.run(read_reg(A_R1)) == [(1, ST_READ_OK)]  # counted exactly once
    assert m.run(read_reg(A_TB_LO)) == [(12, ST_READ_OK)]  # +3 metadata


def test_error_after_result_suppresses_hit_count():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    # result computed, then an idle gap errors before EOP
    outs = m.run(packet(STD_CHUNKS, eop=False) + [Cycle()])
    assert outs == [(0x00, ST_ACTIVE)] * 8 + [(STD_HIT_OUT, ST_RESULT),
                                              (0x00, ST_INPUT_ERR)]
    assert m.run(soft_reset(RST_TABLE)) == [(0x00, ST_IDLE)]
    assert m.run(read_reg(A_R1)) == [(0, ST_READ_OK)]   # hit NOT counted
    assert m.run(read_reg(A_NR)) == [(0, ST_READ_OK)]   # nor the no-rule
    assert m.run(read_reg(A_TP_LO)) == [(1, ST_READ_OK)]


def test_no_rule_counter_wraps_modulo_256():
    m = PacketProcessorModel()
    # no rules -> every completed packet increments no_rule_hits
    assert m.run(packet(STD_CHUNKS) * 255)[-1] == (0x00, ST_RESULT)
    assert m.run(read_reg(A_NR)) == [(0xFF, ST_READ_OK)]
    m.run(packet(STD_CHUNKS))
    assert m.run(read_reg(A_NR)) == [(0x00, ST_READ_OK)]  # wrapped


def test_total_bytes_wraps_modulo_2pow16():
    m = PacketProcessorModel()
    # meta + 65534 data bytes (slot overflow drops them, but they count)
    m.run(packet([(0x00, [i & 0xFF for i in range(65534)])]))
    assert m.run(read_reg(A_TB_HI)) == [(0xFF, ST_READ_OK)]  # 65535
    assert m.run(read_reg(A_TB_LO)) == [(0xFF, ST_READ_OK)]
    m.run(packet([(0x00, [])]))  # one more accepted metadata -> 65536
    assert m.run(read_reg(A_TB_HI)) == [(0x00, ST_READ_OK)]  # wrapped
    assert m.run(read_reg(A_TB_LO)) == [(0x00, ST_READ_OK)]


# ---------------------------------------------------------------------------
# ena global gate (CLARIFICATIONS)
# ---------------------------------------------------------------------------
def test_ena_gates_inputs_and_forces_outputs():
    m = PacketProcessorModel()
    m.run(write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    # a would-be input error is ignored while ena=0
    assert m.run([Cycle(ena=0, ui_in=0x55, packet_status=PS_DATA)]) \
        == [(0x00, ST_IDLE)]
    outs = m.run(packet([(0x00, [0xAA, 0xBB])], eop=False))
    assert outs == [(0x00, ST_ACTIVE)] * 3
    # mid-packet ena=0: outputs forced to 000/0x00, ALL inputs ignored --
    # even an idle gap or a soft reset that would normally fire
    assert m.run([Cycle(ena=0), Cycle(ena=0),
                  Cycle(ena=0, rst_type=RST_TABLE)]) == [(0x00, ST_IDLE)] * 3
    # on resume the packet continues exactly where it stopped
    outs = m.run(packet([(0x01, [0x11, 0x22]),
                         (0x02, [0xDE, 0xAD])]))
    assert outs == [(0x00, ST_ACTIVE)] * 5 + [(STD_HIT_OUT, ST_RESULT)] * 2
    assert m.run(read_reg(A_TB_LO)) == [(9, ST_READ_OK)]  # ena=0 ignored
    assert m.run(read_reg(A_R1)) == [(1, ST_READ_OK)]
    assert m.run(read_reg(A_NR)) == [(0, ST_READ_OK)]     # no errors happened
    # rst_n is asynchronous and NOT gated by ena (ruling)
    assert m.run([Cycle(ena=0, rst_n=0)]) == [(0x00, ST_IDLE)]
    assert m.run(read_reg(A_TP_LO)) == [(0, ST_READ_OK)]


# ---------------------------------------------------------------------------
# Generators: seeded, reproducible, model-compatible (stimulus only)
# ---------------------------------------------------------------------------
def test_generators_are_seeded_and_model_compatible():
    for seed in range(25):
        stim_a = generators.random_sequence(random.Random(seed), 200)
        stim_b = generators.random_sequence(random.Random(seed), 200)
        assert stim_a == stim_b  # same seed -> identical stimulus
        assert len(stim_a) == 200
        outs = PacketProcessorModel().run(stim_a)  # must never crash
        assert len(outs) == 200
        for out, state in outs:
            assert 0 <= out <= 0xFF
            assert 0 <= state <= 0b111


def test_generator_full_packet_fills_table():
    # with the model's default type values, a generated "full" packet must
    # always complete the collection table (010 appears, never 100)
    for seed in range(25):
        m = PacketProcessorModel()
        stim = generators.random_full_packet(random.Random(seed), [0, 1, 2])
        outs = m.run(stim)
        assert (0x00, ST_RESULT) in outs  # no rules -> 010 with zeros
        assert ST_EARLY_EOP not in [state for _, state in outs]


def test_generator_random_rule_is_writable():
    for seed in range(50):
        rule = generators.random_rule(random.Random(seed))
        flags = ppctl.rule_flags(rule["enable"], rule["types_mask"],
                                 rule["action"])
        assert (flags & 0x0F) != 0x01  # never the rejected XXXX_0001


# Verification fixes after the 18 September audit.
def test_interleaving_preserves_order_within_each_type():
    chunks = [[(t, [10 * t + i]) for i in range(4)] for t in range(3)]
    for seed in range(25):
        result = generators.interleave_chunks(random.Random(seed), chunks)
        assert len(result) == 12
        for t in range(3):
            assert [chunk for chunk in result if chunk[0] == t] == chunks[t]


def test_production_random_regression_coverage():
    from regression import build_case, RegressionCoverage
    coverage = RegressionCoverage()
    for seed in range(1500):
        case = build_case(seed)
        if seed < 10:
            assert case == build_case(seed)
        m = PacketProcessorModel()
        config_outputs = m.run(case.configuration)
        packet_outputs = m.run(case.packet)
        m.run(case.mixed)
        sweep_outputs = m.run(case.sweep)
        coverage.record(case, config_outputs, packet_outputs, sweep_outputs)
    coverage.assert_complete(1500)


@pytest.mark.parametrize('bad_section', ['configuration', 'packet', 'sweep'])
def test_coverage_rejects_missing_setup_classification_or_read(bad_section):
    from regression import build_case, RegressionCoverage
    case = build_case(1)  # rule 1 must hit
    m = PacketProcessorModel()
    outputs = {'configuration': m.run(case.configuration),
               'packet': m.run(case.packet)}
    m.run(case.mixed)
    outputs['sweep'] = m.run(case.sweep)
    if bad_section == 'configuration':
        outputs['configuration'][1] = (0, ST_WRITE_ERR)
    elif bad_section == 'packet':
        outputs['packet'] = [(0, ST_ACTIVE)] * len(case.packet)
    else:
        outputs['sweep'][1] = (0, ST_INPUT_ERR)
    with pytest.raises(AssertionError):
        RegressionCoverage().record(case, outputs['configuration'],
                                    outputs['packet'], outputs['sweep'])


@pytest.mark.parametrize('sticky', [False, True])
def test_regression_sweep_recovers_without_erasing_configuration_or_counters(sticky):
    from regression import build_case
    m = PacketProcessorModel()
    m.run(write_reg(4, 0xA5))
    m.run(packet([(0, [0xAA])], eop=False))
    if sticky:
        m.run(idle())
    outputs = m.run(build_case(0).sweep)
    assert outputs[0] == (0, ST_IDLE)
    assert all(state == ST_READ_OK for _, state in outputs[1:])
    assert outputs[1 + 4] == (0xA5, ST_READ_OK)
    assert outputs[1 + 0x19] == (2, ST_READ_OK)
    assert outputs[1 + 0x1B] == (1, ST_READ_OK)


@pytest.mark.parametrize('code', [RST_RULES, RST_STATS])
def test_reset_1b_read_pulse_expires(code):
    m = PacketProcessorModel()
    assert m.run(read_reg(1)) == [(1, ST_READ_OK)]
    assert m.run(soft_reset(code, 2)) == [(0, ST_IDLE)] * 2


@pytest.mark.parametrize('matched', [False, True])
def test_reset_2b_retains_matched_and_unmatched_metadata_selection(matched):
    m = PacketProcessorModel()
    m.run(write_reg(0, 0x80))
    m.run(packet([(0x80 if matched else 0, [0xAA])], eop=False))
    assert m.run(soft_reset(RST_RULES)) == [(0, ST_ACTIVE)]
    m.run([Cycle(ui_in=0xBB, packet_status=PS_DATA)])
    outputs = m.run(packet([(1, [0, 0]), (2, [0, 0])], eop=False))
    assert outputs[-1] == (0, ST_RESULT if matched else ST_ACTIVE)
    if not matched:
        outputs = m.run(packet([(0, [0xCC, 0xDD])], eop=False))
        assert outputs[-1] == (0, ST_RESULT)
