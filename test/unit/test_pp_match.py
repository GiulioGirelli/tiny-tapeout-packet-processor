# SPDX-License-Identifier: Apache-2.0
"""Unit bench for src/pp_match.sv (combinational 3-rule match engine, S4).

Directed tests + seeded random differential against a bench-local Python
reference written spec-first (description.md S4: hit = rule enabled AND
all ENABLED types equal; highest rule NUMBER wins; no hit -> zeros;
matching only when the table is full). Shared flag encoding comes from
test/ppctl.py. The DUT is purely combinational: no clock, values are
checked after a settle delay.
"""

import os
import random
import sys

import cocotb
from cocotb.triggers import Timer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ppctl import rule_flags  # shared flag-byte encoding

STD_WORDS = [0xAABB, 0x1122, 0xDEAD]  # S4: 3 types x 16 bits


def pack_table(words):
    return sum((w & 0xFFFF) << (16 * t) for t, w in enumerate(words))


def pack_flags(flags):
    return flags[0] | (flags[1] << 8) | (flags[2] << 16)


def pack_values(values):
    return sum((values[r][t] & 0xFFFF) << (16 * (3 * r + t))
               for r in range(3) for t in range(3))


class RefMatch:
    """Spec-first reference (S4): hit[r] = flags[r] bit0 AND, for each type
    t in 0..2, (flags[r] bit t+1 == 0) OR (table[t] == values[r][t]).
    Winner = highest rule number; win_types = enabled-type count - 1;
    match_en=0 -> zeros."""

    @staticmethod
    def evaluate(table_words, flags, values, match_en):
        hits = 0
        for r in range(3):
            f = flags[r]
            if not (f & 1):
                continue
            if all(not ((f >> (t + 1)) & 1) or table_words[t] == values[r][t]
                   for t in range(3)):
                hits |= 1 << r
        if not match_en:
            return 0, 0, 0, 0
        for r in (2, 1, 0):
            if (hits >> r) & 1:
                f = flags[r]
                n = bin((f >> 1) & 0x7).count("1")
                return hits, r + 1, (f >> 4) & 0xF, n - 1
        return hits, 0, 0, 0


async def check(dut, table_words, flags, values, match_en,
                exp_hits, exp_rule, exp_action, exp_types):
    """Drive the buses, settle, assert explicit expectations AND the
    spec-first reference."""
    dut.tbl.value = pack_table(table_words)
    dut.rule_flags.value = pack_flags(flags)
    dut.rule_values.value = pack_values(values)
    dut.match_en.value = match_en
    await Timer(1, unit="ns")
    got = (int(dut.hit_vector.value), int(dut.win_rule.value),
           int(dut.win_action.value), int(dut.win_types.value))
    assert got == (exp_hits, exp_rule, exp_action, exp_types), \
        f"got {got}, expected {(exp_hits, exp_rule, exp_action, exp_types)}"
    assert (exp_hits, exp_rule, exp_action, exp_types) == \
        RefMatch.evaluate(table_words, flags, values, match_en)


@cocotb.test()
async def single_hit_per_rule(dut):
    """Each rule alone hits the matching table."""
    for r in range(3):
        flags = [0, 0, 0]
        flags[r] = rule_flags(1, 0b111, r + 1)      # action = rule number
        values = [[0xDE00 + t for t in range(3)] for _ in range(3)]
        values[r] = list(STD_WORDS)
        await check(dut, STD_WORDS, flags, values, 1,
                    1 << r, r + 1, r + 1, 0b10)


@cocotb.test()
async def multi_hit_highest_rule_number_wins(dut):
    """All rules match -> rule 3 wins; rule 3 disabled -> rule 2; ..."""
    flags = [rule_flags(1, 0b111, 1), rule_flags(1, 0b111, 2),
             rule_flags(1, 0b111, 3)]
    values = [list(STD_WORDS), list(STD_WORDS), list(STD_WORDS)]
    await check(dut, STD_WORDS, flags, values, 1, 0b111, 3, 3, 0b10)
    flags[2] = 0x00  # disable rule 3
    await check(dut, STD_WORDS, flags, values, 1, 0b011, 2, 2, 0b10)
    flags[1] = 0x00  # disable rule 2
    await check(dut, STD_WORDS, flags, values, 1, 0b001, 1, 1, 0b10)


@cocotb.test()
async def disabled_rules_never_hit(dut):
    """A rule with enable=0 cannot hit even with equal values."""
    flags = [0x00, 0x00, 0x00]  # all disabled
    values = [list(STD_WORDS)] * 3
    await check(dut, STD_WORDS, flags, values, 1, 0b000, 0, 0, 0)
    # enable bit set on rule 2 only, mismatching values on rules 1/3 anyway
    flags[1] = rule_flags(1, 0b111, 0b1111)
    await check(dut, STD_WORDS, flags, values, 1, 0b010, 2, 0b1111, 0b10)


@cocotb.test()
async def type_enable_subsets_and_encodings(dut):
    """Each subset of 1-3 enabled types hits; win_types = count-1."""
    for mask, enc in [(0b001, 0b00), (0b011, 0b01), (0b111, 0b10)]:
        flags = [rule_flags(1, mask, 0b0010), 0, 0]
        values = [[STD_WORDS[t] if (mask >> t) & 1 else 0x5A00 + t
                   for t in range(3)]] + [[0] * 3] * 2
        # enabled types match, disabled types deliberately mismatch
        await check(dut, STD_WORDS, flags, values, 1, 0b001, 1, 0b0010, enc)


@cocotb.test()
async def wildcarded_types(dut):
    """Only the enabled type is compared; the rest are wildcards."""
    flags = [rule_flags(1, 0b010, 0b1010), 0, 0]   # check type 2 only
    values = [[0x0000, STD_WORDS[1], 0x0000]] + [[0] * 3] * 2
    await check(dut, STD_WORDS, flags, values, 1, 0b001, 1, 0b1010, 0b00)
    # type 2 mismatch kills the hit even though wildcards match by default
    bad = list(STD_WORDS)
    bad[1] ^= 0xFF
    await check(dut, bad, flags, values, 1, 0b000, 0, 0, 0)


@cocotb.test()
async def no_hit_outputs_zeros(dut):
    """Enabled type mismatch on every rule -> all outputs zero."""
    flags = [rule_flags(1, 0b111, 1), rule_flags(1, 0b011, 2),
             rule_flags(1, 0b100, 3)]
    values = [[0x0BAD] * 3, [0x0BAD] * 3, [0x0BAD] * 3]
    await check(dut, STD_WORDS, flags, values, 1, 0b000, 0, 0, 0)


@cocotb.test()
async def match_en_zero_forces_zeros(dut):
    """Even with perfectly matching contents, match_en=0 -> all zeros."""
    flags = [rule_flags(1, 0b111, 1), rule_flags(1, 0b111, 2),
             rule_flags(1, 0b111, 3)]
    values = [list(STD_WORDS)] * 3
    await check(dut, STD_WORDS, flags, values, 0, 0b000, 0, 0, 0)


@cocotb.test()
async def random_differential(dut):
    """Seeded random tables/rules vs the spec-first reference."""
    rng = random.Random(0xCAFE)
    for _ in range(500):
        table_words = [rng.getrandbits(16) for _ in range(3)]
        flags = []
        for _ in range(3):
            f = rng.randint(0, 255)
            if (f & 0x0F) == 0x01:
                # enable=1 with empty mask is rejected at WRITE time in the
                # full design; keep the stimulus to writable flags
                f |= 0x02
            flags.append(f)
        values = [[rng.getrandbits(16) for _ in range(3)] for _ in range(3)]
        # bias towards hits: sometimes copy table slices into rules
        if rng.random() < 0.5:
            r = rng.randrange(3)
            values[r] = list(table_words)
            if rng.random() < 0.5:
                t = rng.randrange(3)
                values[r][t] ^= 1 << rng.randrange(16)
        match_en = rng.randint(0, 1)
        exp = RefMatch.evaluate(table_words, flags, values, match_en)
        await check(dut, table_words, flags, values, match_en, *exp)
