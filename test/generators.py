"""Seeded random stimulus generators for the Tiny Tapeout packet processor,
configuration S4 (3 types, 16-bit entries, 5-bit/32-register map, 4-bit
actions, 8 statistics counters).

SHARED verification tooling: pure Python standard library only, no cocotb
imports. Used by the randomized differential cocotb benches (and smoke-tested
by test_model.py).

These generators produce STIMULUS ONLY. They never compute expected outputs:
the reference model (test/model.py) is the single source of truth that turns
stimulus into expectations. Every function takes an explicit random.Random
so callers control seeding:

    rng = random.Random(seed)
    cycles = generators.random_sequence(rng, 1000)
"""

from ppctl import (Cycle, idle, write_reg, packet,
                   PS_IDLE, PS_DATA, PS_META, PS_EOP,
                   CFG_NONE, CFG_READ, CFG_WADDR, CFG_WDATA,
                   RST_TABLE, RST_RULES, RST_STATS)

RULE_BASES = (0x03, 0x0A, 0x11)


# ---------------------------------------------------------------------------
# Configuration content
# ---------------------------------------------------------------------------
def random_type_values(rng) -> list:
    """3 unique random type values (spec: the 3 type registers must hold
    unique values)."""
    return rng.sample(range(256), 3)


def random_rule(rng, enabled: bool = None) -> dict:
    """A random *writable* S4 rule.

    The rejected flag pattern XXXX_0001 (enable=1 with an empty type mask)
    is never produced: when the rule is enabled the mask is 1..7. Pass
    enabled=True/False to force the enable bit. Feed the result to
    ppctl.write_rule(rule_id, r["enable"], r["types_mask"], r["action"],
    *r["values"]).
    """
    if enabled is None:
        enabled = rng.random() < 0.75
    types_mask = rng.randint(1, 7) if enabled else rng.randint(0, 7)
    return {"enable": int(enabled),
            "types_mask": types_mask,
            "action": rng.randint(0, 15),
            "values": [rng.getrandbits(16) for _ in range(3)]}


# ---------------------------------------------------------------------------
# Packet streams
# ---------------------------------------------------------------------------
def random_packet(rng, type_values, max_chunks: int = 6,
                  max_chunk_bytes: int = 6, eop: bool = True,
                  match_prob: float = 0.8) -> list:
    """Structurally valid packet: 1..max_chunks chunks of 0..max_chunk_bytes
    random data bytes each.

    Covers the spec's streaming corners: zero-length chunks, chunks with
    more than 2 data bytes (slot overflow -> dropped), repeated metadata,
    and metadata not among the type values (probability 1-match_prob ->
    data dropped until the next metadata). Whether the collection table
    fills (and whether EOP is early) is deliberately left to chance.
    """
    cycles = []
    for _ in range(rng.randint(1, max_chunks)):
        if rng.random() < match_prob:
            meta = rng.choice(type_values)
        else:
            meta = rng.randint(0, 255)
        cycles.append(Cycle(ui_in=meta, packet_status=PS_META))
        for _ in range(rng.randint(0, max_chunk_bytes)):
            cycles.append(Cycle(ui_in=rng.randint(0, 255),
                                packet_status=PS_DATA))
    if eop:
        cycles.append(Cycle(packet_status=PS_EOP))
    return cycles


def random_full_packet(rng, type_values, max_extra: int = 4,
                       eop: bool = True) -> list:
    """Packet guaranteed to fill the collection table: each of the 3 type
    values receives at least 2 data bytes (plus up to max_extra overflow
    bytes, which are dropped). Per-type bytes are split into 1-2 chunks and
    the chunks of all types are interleaved randomly (per-type byte order is
    preserved), so rule matching triggers on the completing byte.
    """
    pending = []
    for meta in type_values:
        n_bytes = 2 + rng.randint(0, max_extra)
        data = [rng.randint(0, 255) for _ in range(n_bytes)]
        cut = rng.randint(1, n_bytes - 1) if rng.random() < 0.5 else n_bytes
        pieces = [(meta, data[:cut])]
        if cut < n_bytes:
            pieces.append((meta, data[cut:]))
        pending.append(pieces)
    return packet(interleave_chunks(rng, pending), eop=eop)


def interleave_chunks(rng, per_type_chunks):
    """Shuffle across types while retaining each type's chunk order."""
    pending = [list(chunks) for chunks in per_type_chunks if chunks]
    result = []
    while pending:
        i = rng.randrange(len(pending))
        result.append(pending[i].pop(0))
        if not pending[i]:
            pending.pop(i)
    return result


def random_invalid_packet(rng, type_values) -> list:
    """One of the spec's malformed streams.

    "data_first"/"eop_first"/"idle_gap"/"cfg_flip" raise the sticky 111
    input error; "early_eop" raises the one-cycle 100 error (guaranteed: at
    most 2 chunks x 2 data bytes can never fill the 6-byte table);
    "truncated" simply stops mid-packet (no EOP) -- legal so far, it leaves
    the packet FSM active for whatever stimulus follows.
    """
    kind = rng.choice(["data_first", "eop_first", "idle_gap",
                       "cfg_flip", "early_eop", "truncated"])
    if kind == "data_first":
        # data byte before any metadata -> 111
        return [Cycle(ui_in=rng.randint(0, 255), packet_status=PS_DATA)]
    if kind == "eop_first":
        # EOP before a packet was started -> 111
        return [Cycle(packet_status=PS_EOP)]
    if kind == "idle_gap":
        # idle (00) inserted mid-packet -> 111
        return (random_packet(rng, type_values, max_chunks=2,
                              max_chunk_bytes=2, eop=False)
                + [Cycle(packet_status=PS_IDLE)]
                + random_packet(rng, type_values, max_chunks=2,
                                max_chunk_bytes=2, eop=True))
    if kind == "cfg_flip":
        # cfg_mode changed during an active packet -> 111
        return (random_packet(rng, type_values, max_chunks=2,
                              max_chunk_bytes=2, eop=False)
                + [Cycle(ui_in=rng.randint(0, 0x1F), cfg_mode=1,
                         packet_status=CFG_READ)])
    if kind == "early_eop":
        # table cannot be full -> EOP raises 100
        return random_packet(rng, type_values, max_chunks=2,
                             max_chunk_bytes=2, eop=True)
    # truncated: packet left open (no EOP)
    return random_packet(rng, type_values, max_chunks=3, max_chunk_bytes=3,
                         eop=False)


# ---------------------------------------------------------------------------
# Config-operation sequences
# ---------------------------------------------------------------------------
def random_config_ops(rng, n_ops: int, known_types=None) -> list:
    """Random config-mode sequence of n_ops operations.

    Mixes single-cycle READs, accepted WRITEs, every rejected-WRITE class
    (counter addresses 0x18-0x1F, flag pattern XXXX_0001, duplicate type
    values across the 3 registers) and the pending-WRITE corners from the
    spec (10->10 replace, 00 keeps pending, 01 cancels + READs, 11 with no
    pending address). `known_types`, when given, is the caller's current 3
    type values and is used to build guaranteed-duplicate type writes.
    """
    cycles = []
    for _ in range(n_ops):
        kind = rng.choices(
            ["read", "write_data", "write_flags", "write_type",
             "write_counter", "write_bad_flags", "write_dup_type",
             "corner_10_10", "corner_gap", "corner_cancel_read",
             "corner_11_alone", "gap"],
            weights=[20, 18, 10, 8, 5, 5, 5, 6, 6, 6, 5, 4])[0]

        if kind == "read":
            cycles.append(Cycle(ui_in=rng.randint(0, 0xFF), cfg_mode=1,
                                packet_status=CFG_READ))  # in[7:5] ignored
        elif kind == "write_data":
            # rule match-value registers are always writable
            base = rng.choice(RULE_BASES)
            cycles += write_reg(base + rng.randint(1, 6),
                                rng.randint(0, 255))
        elif kind == "write_flags":
            # a legal flag byte (never XXXX_0001)
            flags = rng.randint(0, 255)
            if (flags & 0x0F) == 0x01:
                flags |= 0x02
            cycles += write_reg(rng.choice(RULE_BASES), flags)
        elif kind == "write_type":
            cycles += write_reg(rng.randint(0x00, 0x02), rng.randint(0, 255))
        elif kind == "write_counter":
            # always rejected: counters are read-only
            cycles += write_reg(rng.randint(0x18, 0x1F), rng.randint(0, 255))
        elif kind == "write_bad_flags":
            # always rejected: enable=1 with empty type mask (XXXX_0001)
            cycles += write_reg(rng.choice(RULE_BASES),
                                (rng.randint(0, 15) << 4) | 0x01)
        elif kind == "write_dup_type":
            if known_types is not None:
                dst, src = rng.sample(range(3), 2)
                cycles += write_reg(dst, known_types[src])
            else:
                cycles += write_reg(rng.randint(0x00, 0x02),
                                    rng.randint(0, 255))
        elif kind == "corner_10_10":
            # two consecutive address cycles: the first one is wasted
            cycles.append(Cycle(ui_in=rng.randint(0, 0x1F), cfg_mode=1,
                                packet_status=CFG_WADDR))
            cycles.append(Cycle(ui_in=rng.randint(0, 0x1F), cfg_mode=1,
                                packet_status=CFG_WADDR))
            cycles.append(Cycle(ui_in=rng.randint(0, 255), cfg_mode=1,
                                packet_status=CFG_WDATA))
        elif kind == "corner_gap":
            # 00 keeps the pending address
            cycles.append(Cycle(ui_in=rng.randint(0, 0x1F), cfg_mode=1,
                                packet_status=CFG_WADDR))
            for _ in range(rng.randint(1, 3)):
                cycles.append(Cycle(cfg_mode=1, packet_status=CFG_NONE))
            cycles.append(Cycle(ui_in=rng.randint(0, 255), cfg_mode=1,
                                packet_status=CFG_WDATA))
        elif kind == "corner_cancel_read":
            # 01 cancels the pending WRITE and performs a READ
            cycles.append(Cycle(ui_in=rng.randint(0, 0x1F), cfg_mode=1,
                                packet_status=CFG_WADDR))
            cycles.append(Cycle(ui_in=rng.randint(0, 0x1F), cfg_mode=1,
                                packet_status=CFG_READ))
        elif kind == "corner_11_alone":
            # data with no (known) pending address
            cycles.append(Cycle(ui_in=rng.randint(0, 255), cfg_mode=1,
                                packet_status=CFG_WDATA))
        else:  # gap
            for _ in range(rng.randint(1, 2)):
                cycles.append(Cycle(cfg_mode=1, packet_status=CFG_NONE))
    return cycles


# ---------------------------------------------------------------------------
# Resets and mixed streams
# ---------------------------------------------------------------------------
def random_soft_reset(rng, max_len: int = 1) -> list:
    """1..max_len cycles asserting a random rst_type code (01/10/11)."""
    return [Cycle(rst_type=rng.choice([RST_TABLE, RST_RULES, RST_STATS]))
            for _ in range(rng.randint(1, max_len))]


def random_sequence(rng, n_cycles: int, type_values=None) -> list:
    """Mixed-mode stimulus of exactly n_cycles: valid and invalid packet
    streams, config-operation bursts, soft-reset injection, idle runs and
    rare ena=0 cycles (all other pins wiggled -- they must be ignored).

    The sequence may end mid-anything (mid-packet, pending write, ...); any
    prefix of legal stimulus is legal stimulus, and the model tracks the
    resulting state. This is adversarial fuzzing, not a guarantee of a
    classification: config writes/resets may change the actual type values
    during the sequence. regression.build_case adds a separately programmed
    classification scenario and a legal readback sweep to every seed.
    """
    tv = list(type_values) if type_values is not None else [0, 1, 2]
    cycles = []
    while len(cycles) < n_cycles:
        kind = rng.choices(
            ["packet", "full_packet", "invalid", "config", "reset",
             "idle", "ena_off"],
            weights=[30, 15, 12, 25, 5, 10, 3])[0]
        if kind == "packet":
            cycles += random_packet(rng, tv)
        elif kind == "full_packet":
            cycles += random_full_packet(rng, tv)
        elif kind == "invalid":
            cycles += random_invalid_packet(rng, tv)
        elif kind == "config":
            cycles += random_config_ops(rng, rng.randint(1, 6), tv)
        elif kind == "reset":
            cycles += random_soft_reset(rng, max_len=2)
        elif kind == "idle":
            cycles += idle(rng.randint(1, 4))
        else:  # ena_off: inputs are ignored (CLARIFICATIONS), outputs forced 0
            cycles.append(Cycle(ui_in=rng.randint(0, 255),
                                cfg_mode=rng.randint(0, 1),
                                packet_status=rng.randint(0, 3),
                                rst_type=rng.randint(0, 3),
                                ena=0))
    return cycles[:n_cycles]
