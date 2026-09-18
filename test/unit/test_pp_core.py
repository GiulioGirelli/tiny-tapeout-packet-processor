# SPDX-License-Identifier: Apache-2.0
"""Unit bench for src/pp_core.sv (collection + rules + combinational match,
S4).

Directed tests + seeded random differential. References:
- test/model.py (the pin-accurate reference model, S4) for config
  write/read outcomes and for the 010 result byte on completing cycles;
- ShadowCore, a bench-local spec-first shadow of the datapath
  (description.md S4 collection table / rules / rule matching), for
  per-cycle combinational full_next / hit_vector / win_* checks.

ppctl Cycle records are translated into core pin drives the way the
control FSMs (pp_input) gate them: bytes are only accepted into an active
packet, EOP (and rst_type=01) clears the table (owner ruling: uniform
clearing at every packet end), config writes commit single-cycle through
the wr port, rst_type=10 pulses rules_reset (and drops a pending write
address), and sticky-111 periods drive idle. Statistics (rst_type=11) and
ena live outside the core.
"""

import os
import random
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import generators
import model as ref_model
import ppctl
from ppctl import (Cycle, PS_IDLE, PS_DATA, PS_META, PS_EOP,
                   CFG_READ, CFG_WADDR, CFG_WDATA,
                   write_reg, write_rule, set_type_values, packet, read_reg)

RULE_BASES = (0x03, 0x0A, 0x11)
ST_RESULT = 0b010
ST_READ_OK = 0b110
ST_WRITE_OK = 0b011

STD_VALUES = (0xAABB, 0x1122, 0xDEAD)
STD_CHUNKS = [(0x00, [0xAA, 0xBB]),
              (0x01, [0x11, 0x22]),
              (0x02, [0xDE, 0xAD])]
STD_HIT_OUT = 0xA9  # (0b1010<<4)|(0b10<<2)|0b01


class ShadowCore:
    """Spec-first shadow of the S4 core datapath: 3x16-bit collection table
    (metadata selects a slot among the 3 type registers, data appends
    MSB-first, max 2 bytes per slot), 24-byte register file (types
    0x00-0x02 unique, rules 0x03-0x17, statistics read-only), and the
    combinational 3-rule match (highest id wins, only when the table is
    effectively full)."""

    def __init__(self):
        self.regs = [0, 1, 2] + [0] * 21
        self.slots = [[], [], []]
        self.sel = None

    # -- committed-state mutators (applied at the clock edge) --
    def table_clear(self):
        self.slots = [[], [], []]
        self.sel = None

    def rules_reset(self):
        self.regs = [0, 1, 2] + [0] * 21

    def meta(self, byte):
        self.sel = self.regs.index(byte) if byte in self.regs[:3] else None

    def data(self, byte):
        if self.sel is not None and len(self.slots[self.sel]) < 2:
            self.slots[self.sel].append(byte)

    def accept(self, addr, data):
        if addr >= 0x18:
            return False
        if addr < 0x03:
            return not any(self.regs[i] == data for i in range(3)
                           if i != addr)
        if addr in RULE_BASES and (data & 0x0F) == 0x01:
            return False
        return True

    def write(self, addr, data):
        if self.accept(addr, data):
            self.regs[addr] = data

    # -- combinational views --
    def read(self, addr):
        return self.regs[addr] if addr < 0x18 else 0

    def table_full(self):
        return all(len(s) == 2 for s in self.slots)

    def peek_next(self, clear=0, data=None):
        """Expected (full_next, hit_vector, win_rule, win_action, win_types)
        for the given in-flight inputs, sampled before the clock edge."""
        slots = [list(s) for s in self.slots]
        if clear:
            slots = [[], [], []]
        elif data is not None and self.sel is not None \
                and len(slots[self.sel]) < 2:
            slots[self.sel].append(data)
        if not all(len(s) == 2 for s in slots):
            return 0, 0, 0, 0, 0
        words = []
        for s in slots:
            w = 0
            for b in s:
                w = (w << 8) | b
            words.append(w)
        hits = 0
        for r in range(3):
            f = self.regs[RULE_BASES[r]]
            if not (f & 1):
                continue
            ok = True
            for t in range(3):
                if (f >> (t + 1)) & 1:
                    w = (self.regs[RULE_BASES[r] + 1 + 2 * t] << 8) \
                        | self.regs[RULE_BASES[r] + 2 + 2 * t]
                    if words[t] != w:
                        ok = False
                        break
            if ok:
                hits |= 1 << r
        for r in (2, 1, 0):
            if (hits >> r) & 1:
                f = self.regs[RULE_BASES[r]]
                n = bin((f >> 1) & 0x7).count("1")
                return 1, hits, r + 1, (f >> 4) & 0xF, n - 1
        return 1, hits, 0, 0, 0


async def tick(dut):
    await RisingEdge(dut.clk)
    await Timer(1, unit="ns")


async def reset(dut):
    dut.table_clear.value = 0
    dut.rules_reset.value = 0
    dut.meta_valid.value = 0
    dut.meta_byte.value = 0
    dut.data_valid.value = 0
    dut.data_byte.value = 0
    dut.wr_en.value = 0
    dut.wr_addr.value = 0
    dut.wr_data.value = 0
    dut.rd_addr.value = 0
    dut.rst_n.value = 0
    await tick(dut)
    await tick(dut)
    dut.rst_n.value = 1
    await tick(dut)


class CoreDriver:
    """Translates ppctl Cycle records into pp_core pin drives, replicating
    the acceptance gating the control FSMs apply, and checks the core's
    combinational outputs against the shadow every cycle."""

    def __init__(self, dut, shadow):
        self.dut = dut
        self.shadow = shadow
        self.pkt_active = False
        self.err = False
        self.pending = None

    async def cycle(self, c, m_out=None):
        assert c.rst_n == 1, "use reset() for hard resets"
        d = self.dut
        d.table_clear.value = 0
        d.rules_reset.value = 0
        d.meta_valid.value = 0
        d.data_valid.value = 0
        d.wr_en.value = 0
        clear_now = False
        data_inflight = None
        meta_now = None
        wr_commit = None  # (addr, data)

        if c.ena == 0:
            pass  # idle: every input ignored (ruling)
        elif c.rst_type == 0b01:
            d.table_clear.value = 1
            clear_now = True
            self.pkt_active = False
            self.err = False
        elif c.rst_type == 0b10:
            d.rules_reset.value = 1
            self.pending = None  # owner ruling: 10 clears pending
        elif c.rst_type == 0b11:
            pass  # statistics live outside the core
        elif self.err:
            pass  # sticky 111: the stream is swallowed
        elif c.cfg_mode == 1 and self.pkt_active:
            self.err = True  # cfg_mode changed mid-packet -> 111
        elif c.cfg_mode == 0:
            if c.packet_status == PS_META:
                d.meta_valid.value = 1
                d.meta_byte.value = c.ui_in
                meta_now = c.ui_in
                self.pkt_active = True
            elif c.packet_status == PS_DATA:
                if self.pkt_active:
                    d.data_valid.value = 1
                    d.data_byte.value = c.ui_in
                    data_inflight = c.ui_in
                else:
                    self.err = True  # data before first metadata -> 111
            elif c.packet_status == PS_EOP:
                if self.pkt_active:
                    # packet end always clears the table (owner ruling)
                    d.table_clear.value = 1
                    clear_now = True
                    self.pkt_active = False
                else:
                    self.err = True  # EOP before a packet started -> 111
            else:  # PS_IDLE
                if self.pkt_active:
                    self.err = True  # idle inserted mid-packet -> 111
        else:  # config mode
            if c.packet_status == CFG_WADDR:
                self.pending = c.ui_in & 0x1F
            elif c.packet_status == CFG_WDATA:
                if self.pending is not None:
                    wr_commit = (self.pending, c.ui_in)
                    d.wr_en.value = 1
                    d.wr_addr.value = self.pending
                    d.wr_data.value = c.ui_in
                    self.pending = None
                # else: 101 data-before-address, no write commits
            elif c.packet_status == CFG_READ:
                self.pending = None  # READ cancels a pending write
                d.rd_addr.value = c.ui_in & 0x1F
            # CFG_NONE: nothing

        # ---- pre-edge combinational checks against the shadow ----
        await Timer(1, unit="ns")
        exp = self.shadow.peek_next(clear=1 if clear_now else 0,
                                    data=data_inflight)
        got = (int(d.full_next.value), int(d.hit_vector.value),
               int(d.win_rule.value), int(d.win_action.value),
               int(d.win_types.value))
        assert got == exp, f"comb {got} != shadow {exp}"
        assert int(d.full.value) == int(self.shadow.table_full())

        # completing cycle: table becomes effectively full right now and was
        # not full before -> the model must show 010 with the same result
        completing = (exp[0] == 1 and not clear_now
                      and not self.shadow.table_full())
        if completing and m_out is not None:
            assert m_out[1] == ST_RESULT, f"model shows {m_out} on completion"
            dec_rule = m_out[0] & 0x3          # out[1:0]
            dec_types = (m_out[0] >> 2) & 0x3  # out[3:2]
            dec_action = (m_out[0] >> 4) & 0xF  # out[7:4]
            assert (exp[2], exp[3], exp[4]) == (dec_rule, dec_action,
                                                dec_types)

        # READ cycle: combinational rd_data must equal the model's value
        # (rules part only -- statistics live outside the core)
        if (m_out is not None and m_out[1] == ST_READ_OK
                and (c.ui_in & 0x1F) < 0x18):
            assert int(d.rd_data.value) == m_out[0]

        # WRITE commit cycle: combinational wr_accept vs the shadow
        if wr_commit is not None:
            assert int(d.wr_accept.value) == int(self.shadow.accept(*wr_commit))

        await tick(d)

        # ---- commit the shadow (clear has priority over a fill) ----
        if clear_now:
            self.shadow.table_clear()
        elif data_inflight is not None:
            self.shadow.data(data_inflight)
        if meta_now is not None:
            self.shadow.meta(meta_now)
        if c.ena and c.rst_type == 0b10:
            self.shadow.rules_reset()
        if wr_commit is not None:
            self.shadow.write(*wr_commit)
        return exp

    async def config_write(self, addr, data):
        """Single-cycle write commit through the wr port."""
        d = self.dut
        d.wr_en.value = 1
        d.wr_addr.value = addr
        d.wr_data.value = data
        await Timer(1, unit="ns")
        self.last_accept = bool(int(d.wr_accept.value))
        assert self.last_accept == self.shadow.accept(addr, data)
        await tick(d)
        d.wr_en.value = 0
        self.shadow.write(addr, data)

    async def read(self, addr, expected):
        self.dut.rd_addr.value = addr
        await Timer(1, unit="ns")
        assert int(self.dut.rd_data.value) == expected
        assert expected == self.shadow.read(addr)


# ---------------------------------------------------------------------------
# Directed tests
# ---------------------------------------------------------------------------
@cocotb.test()
async def config_writes_and_readbacks_vs_model(dut):
    """Config writes (accepted and rejected) + readbacks, model-checked."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    m = ref_model.PacketProcessorModel()
    shadow = ShadowCore()
    driver = CoreDriver(dut, shadow)
    ops = (set_type_values(0x10, 0x20, 0x30)
           + write_rule(1, 1, 0b111, 0b1010, *STD_VALUES)
           + write_rule(3, 1, 0b101, 0b1111, 0x0BAD, 0x1122, 0x5566)
           + write_reg(0x00, 0x20)   # rejected: duplicate of type register 1
           + write_reg(0x18, 0x55)   # rejected: statistics are read-only
           + write_reg(0x0A, 0x01))  # rejected: FLAGS pattern XXXX_0001
    outs = m.run(ops)
    assert len(ops) % 2 == 0
    for i in range(0, len(ops), 2):
        await driver.config_write(ops[i].ui_in, ops[i + 1].ui_in)
        # model outcome of the same 2-cycle op: 011 accepted / 101 rejected
        assert driver.last_accept == (outs[i + 1][1] == ST_WRITE_OK)
    for addr in range(0x18):
        m_read = m.run(read_reg(addr))
        assert m_read[0][1] == ST_READ_OK
        await driver.read(addr, m_read[0][0])


@cocotb.test()
async def completing_cycle_combinational_match(dut):
    """On the exact cycle of the final fill byte, full_next=1 and win_* already
    hold the result the model shows as 010 on that same cycle."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    m = ref_model.PacketProcessorModel()
    shadow = ShadowCore()
    driver = CoreDriver(dut, shadow)
    ops = write_rule(1, 1, 0b111, 0b1010, *STD_VALUES)
    m.run(ops)
    for i in range(0, len(ops), 2):
        await driver.config_write(ops[i].ui_in, ops[i + 1].ui_in)

    seq = packet(STD_CHUNKS)
    outs = m.run(seq)
    exps = []
    for i, c in enumerate(seq):
        exps.append(await driver.cycle(c, m_out=outs[i]))
    fulls = [e[0] for e in exps]
    # 9 byte-stream cycles (idx 0-8) + EOP (idx 9)
    assert fulls[:8] == [0] * 8, "full_next early!"
    assert fulls[8] == 1, "full_next must rise on the final fill byte"
    assert fulls[9] == 0, "EOP cycle clears the table"
    # completing cycle (idx 8): rule 1, action 0b1010, 3 types (enc 10)
    assert exps[8][1] == 0b001                       # hit_vector
    assert exps[8][2:] == (1, 0b1010, 0b10)
    assert outs[8] == (STD_HIT_OUT, ST_RESULT)       # model's 010 byte
    # EOP cycle (idx 9): the model HOLDS 010 while the core's live match
    # is already cleared -- pp_output bridges this with a result register;
    # the bench documents the phase relationship here.
    assert outs[9] == (STD_HIT_OUT, ST_RESULT)
    assert exps[9][0] == 0 and exps[9][2] == 0


@cocotb.test()
async def hit_priority_and_no_hit_through_core(dut):
    """Multi-hit priority and the no-hit case through the full core."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    m = ref_model.PacketProcessorModel()
    shadow = ShadowCore()
    driver = CoreDriver(dut, shadow)
    ops = (write_rule(1, 1, 0b111, 1, *STD_VALUES)
           + write_rule(2, 1, 0b111, 2, *STD_VALUES)
           + write_rule(3, 1, 0b111, 3, *STD_VALUES))
    m.run(ops)
    for i in range(0, len(ops), 2):
        await driver.config_write(ops[i].ui_in, ops[i + 1].ui_in)

    async def stream_and_get_completion():
        seq = packet(STD_CHUNKS)
        outs = m.run(seq)
        completing = None
        for i, c in enumerate(seq):
            exp = await driver.cycle(c, m_out=outs[i])
            if exp[0] == 1 and completing is None:
                completing = exp
        return completing

    # all three rules hit -> highest number wins
    exp = await stream_and_get_completion()
    assert exp[1] == 0b111 and exp[2:] == (3, 3, 0b10)
    # disable rule 3 -> rule 2 wins
    await driver.config_write(0x11, 0x00)
    m.run(write_reg(0x11, 0x00))
    exp = await stream_and_get_completion()
    assert exp[1] == 0b011 and exp[2:] == (2, 2, 0b10)
    # no rule matches anymore -> full table, zero result
    await driver.config_write(0x0A, 0x00)
    m.run(write_reg(0x0A, 0x00))
    await driver.config_write(0x03, 0x00)
    m.run(write_reg(0x03, 0x00))
    exp = await stream_and_get_completion()
    assert exp[0] == 1 and exp[1] == 0b000 and exp[2:] == (0, 0, 0)


@cocotb.test()
async def table_clear_scope_through_core(dut):
    """table_clear empties the table: a fresh packet must refill all 6
    bytes and then hits with its own values."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    m = ref_model.PacketProcessorModel()
    shadow = ShadowCore()
    driver = CoreDriver(dut, shadow)
    ops = write_rule(1, 1, 0b111, 0b1010, *STD_VALUES)
    m.run(ops)
    for i in range(0, len(ops), 2):
        await driver.config_write(ops[i].ui_in, ops[i + 1].ui_in)
    # partial fill, then rst_type=01
    seq = packet([(0x00, [0xAA])], eop=False) + [Cycle(rst_type=0b01)]
    outs = m.run(seq)
    for i, c in enumerate(seq):
        await driver.cycle(c, m_out=outs[i])
    assert int(dut.full.value) == 0
    # a full packet now completes with exactly its own 6 data bytes
    seq = packet(STD_CHUNKS)
    outs = m.run(seq)
    exps = [await driver.cycle(c, m_out=outs[i]) for i, c in enumerate(seq)]
    assert exps[8][0] == 1 and exps[8][2:] == (1, 0b1010, 0b10)


@cocotb.test()
async def rules_reset_scope_through_core(dut):
    """rules_reset restores default types and zeroes the rules; the table
    keeps working (full_next still rises, but no rule can hit)."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    m = ref_model.PacketProcessorModel()
    shadow = ShadowCore()
    driver = CoreDriver(dut, shadow)
    ops = (set_type_values(0x10, 0x20, 0x30)
           + write_rule(1, 1, 0b111, 0b1010, *STD_VALUES))
    m.run(ops)
    for i in range(0, len(ops), 2):
        await driver.config_write(ops[i].ui_in, ops[i + 1].ui_in)
    seq = [Cycle(rst_type=0b10)]
    outs = m.run(seq)
    for i, c in enumerate(seq):
        await driver.cycle(c, m_out=outs[i])
    # types are back to 0,1,2: the standard packet fills the table...
    seq = packet(STD_CHUNKS)
    outs = m.run(seq)
    exps = [await driver.cycle(c, m_out=outs[i]) for i, c in enumerate(seq)]
    # ...but with the rules zeroed the completing cycle shows no hit
    assert exps[8][0] == 1 and exps[8][1] == 0b000
    assert exps[8][2:] == (0, 0, 0)
    assert outs[8] == (0x00, ST_RESULT)


@cocotb.test()
async def random_differential(dut):
    """Seeded random mixed streams: per-cycle shadow checks for every
    combinational output, model checks on completing and READ cycles."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    for seed in range(8):
        await reset(dut)
        rng = random.Random(0xC0DE + seed)
        # packets target the reset-default type values; random config ops
        # (incl. type-register rewrites) are tracked by model and shadow
        seq = generators.random_sequence(rng, 300, type_values=[0, 1, 2])
        m = ref_model.PacketProcessorModel()
        outs = m.run(seq)
        shadow = ShadowCore()
        driver = CoreDriver(dut, shadow)
        for i, c in enumerate(seq):
            await driver.cycle(c, m_out=outs[i])
