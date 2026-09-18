"""Shared stimulus and coverage contract for RTL, GL and model regressions.

The controlled prefix starts from hard reset, programs custom types/rules,
then deliberately exercises one of the eight simultaneous-hit combinations.
The independent pin audit remains a separate oracle. This module does not
import the reference model or compute its cycle-by-cycle behavior.
"""
from collections import Counter
from dataclasses import dataclass
import random

import generators
from ppctl import Cycle, packet, read_reg, set_type_values, write_rule, RST_TABLE


@dataclass
class RegressionCase:
    configuration: list
    packet: list
    mixed: list
    sweep: list
    hit_mask: int
    type_mask: int
    action: int


def build_case(seed, n_cycles=60):
    rng = random.Random(seed)
    # Avoid collisions with reset defaults during sequential type writes.
    types = rng.sample(range(3, 256), 3)
    values = [rng.getrandbits(16) for _ in range(3)]
    hit_mask = seed % 8
    type_mask = 1 + (seed // 8) % 7
    action = (seed // 56) % 16
    configuration = set_type_values(*types)
    for r in range(3):
        rule_values = list(values)
        # Wildcarded fields deliberately differ, even for rules meant to hit.
        for t in range(3):
            if not (type_mask >> t) & 1:
                rule_values[t] ^= 0xFFFF
        if not (hit_mask >> r) & 1:
            t = rng.choice([t for t in range(3) if (type_mask >> t) & 1])
            rule_values[t] ^= 1 << rng.randrange(16)
        configuration += write_rule(r + 1, 1, type_mask, action, *rule_values)

    pending = []
    for meta, value in zip(types, values):
        # One-byte chunks exercise partial/interleaved collection. Overflow
        # follows the real low byte, never precedes it for the same type.
        pending.append([(meta, [value >> 8]),
                        (meta, [value & 255] +
                         [rng.randrange(256) for _ in range(rng.randrange(3))])])
    chunks = generators.interleave_chunks(rng, pending)
    chunks.insert(rng.randrange(len(chunks) + 1),
                  (0, [rng.randrange(256)]))  # irrelevant metadata
    return RegressionCase(
        configuration, packet(chunks),
        generators.random_sequence(rng, n_cycles, type_values=types),
        [Cycle(rst_type=RST_TABLE)] + [read_reg(a)[0] for a in range(32)],
        hit_mask, type_mask, action,
    )


class RegressionCoverage:
    """Assert directed intent in addition to the DUT/model comparisons."""
    def __init__(self):
        self.counts = Counter()

    def record(self, case, configuration_outputs, packet_outputs, sweep_outputs):
        assert len(configuration_outputs) == len(case.configuration)
        for c, out in zip(case.configuration, configuration_outputs):
            expected = (0, 0) if c.packet_status == 2 else (c.ui_in, 3)
            assert out == expected, f"classification setup did not commit: {c}, {out}"

        # Highest set bit is the winning rule. This expectation is calculated
        # from the requested hit combination, not the behavioral model.
        winner = case.hit_mask.bit_length()
        result = ((case.action << 4) | ((case.type_mask.bit_count() - 1) << 2)
                  | winner) if winner else 0
        assert len(packet_outputs) == len(case.packet)
        assert packet_outputs[0] == (0, 1)
        assert packet_outputs[-2:] == [(result, 2)] * 2
        seen_result = False
        for out in packet_outputs:
            if out[1] == 2:
                seen_result = True
                assert out == (result, 2)
            else:
                assert not seen_result and out == (0, 1)

        assert len(sweep_outputs) == 33
        assert sweep_outputs[0] == (0, 0)  # packet reset, preserves regs/stats
        assert all(state == 6 for _, state in sweep_outputs[1:]), \
            "a register sweep was blocked or did not complete a READ"
        self.counts['classifications'] += 1
        self.counts[f'winner_{winner}'] += 1
        self.counts[f'hit_mask_{case.hit_mask}'] += 1
        if winner:
            self.counts[f'type_mask_{case.type_mask}'] += 1
            self.counts[f'action_{case.action}'] += 1
        self.counts['sweep_success'] += 32

    def assert_complete(self, n_seeds, full=True):
        assert self.counts['classifications'] == n_seeds
        assert self.counts['sweep_success'] == n_seeds * 32
        for winner in range(4):
            assert self.counts[f'winner_{winner}'] > 0
        for mask in range(8):
            assert self.counts[f'hit_mask_{mask}'] > 0
        if full:
            for mask in range(1, 8):
                assert self.counts[f'type_mask_{mask}'] > 0
            for action in range(16):
                assert self.counts[f'action_{action}'] > 0

    def summary(self):
        return dict(sorted(self.counts.items()))
