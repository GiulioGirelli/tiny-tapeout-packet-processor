"""Pin-accurate, cycle-accurate Python reference model for the Tiny Tapeout
match-action packet processor, configuration S4 (REVISION S4, 10 Sep 2026):
3 types, 16-bit entries, 3 rules, 5-bit/32-register map, 4-bit actions,
8 statistics counters (no error-occurrences counter).

Authoritative sources (read together):
  - description.md               -- S4 functional specification (source of
                                    truth), incl. the CLARIFICATIONS section
                                    (owner rulings 09 Sep 2026, adapted to
                                    S4 on 10 Sep 2026)
  - S4_REIMPLEMENTATION_PLAN.md  -- the exact S4 delta

This is SHARED verification tooling: pure Python standard library only, no
cocotb imports. It is consumed by the pytest self-tests (test_model.py) now
and by the pin-level cocotb benches later.

Timing model (description.md TIMING): the circuit is synchronous with two
stages -- stage 1 processes the inputs at the clock edge (collection-table
fill / FSM updates), stage 2 registers the outputs. Therefore the outputs
caused by an input appear on the NEXT clock cycle. PacketProcessorModel.step
takes the pin values driven during one cycle and returns the registered
outputs valid immediately after that cycle's edge -- i.e. exactly what a
bench observes on the cycle following those inputs (uo_out, and
out_state = uio_out[7:5]).

Interpretations of spec-silent points are marked inline as "# INTERP #n:";
where an owner ruling confirmed or overrode one, the comment says so.
"""

from typing import NamedTuple

# ---------------------------------------------------------------------------
# out_state[2:0] encodings (description.md I/O FLAGS / OUT PINS)
# ---------------------------------------------------------------------------
ST_IDLE = 0b000        # nothing in output is valid
ST_ACTIVE = 0b001      # packet streaming in progress (no result yet)
ST_RESULT = 0b010      # classification result available on out[7:0]
ST_WRITE_OK = 0b011    # WRITE completed, out = written value
ST_EARLY_EOP = 0b100   # EOP arrived before the table was full (one cycle)
ST_WRITE_ERR = 0b101   # WRITE rejected (one cycle)
ST_READ_OK = 0b110     # READ completed, out = read value
ST_INPUT_ERR = 0b111   # input error, sticky until rst_type=01

# ---------------------------------------------------------------------------
# Register map (description.md ADDRESS MAP / MAP DETAILS): 5-bit, 32
# registers, exactly full. Address cycles use in[4:0]; in[7:5] are ignored.
# ---------------------------------------------------------------------------
NUM_TYPES = 3                               # S4: 3 relevant types
SLOT_BYTES = 2                              # S4: 16 bits (2 bytes) per type
TYPE_RESET_VALUES = (0x00, 0x01, 0x02)      # ruling: both resets
RULE_BASES = (0x03, 0x0A, 0x11)             # rule 1 / 2 / 3 base addresses
RULE_NUM_REGS = 7                           # 1 flag + 3x2 match bytes
ADDR_STATS_FIRST = 0x18                     # 0x18-0x1F: statistics (read-only)
ADDR_STATS_LAST = 0x1F
ADDR_MASK = 0x1F                            # 5-bit address space


class Result(NamedTuple):
    """One classification result (description.md OUT PINS, out_state=010)."""
    rule: int = 0      # winning rule number, 0 = no rule matched
    n_types: int = 0   # how many types the winning rule had to check (1-3)
    action: int = 0    # winning rule's 4-bit action (4'b0000 = NOP)


class _Rule:
    """One rule-table entry: flag byte + three 16-bit match values."""

    __slots__ = ("flags", "match")

    def __init__(self):
        self.flags = 0x00
        self.match = [0x0000, 0x0000, 0x0000]


class PacketProcessorModel:
    """Cycle-accurate behavioural model of description.md (S4).

    State after __init__ is identical to the state after a hard reset.
    """

    def __init__(self):
        self.hard_reset()

    # ------------------------------------------------------------------
    # Resets (description.md RESET; priority rst_n > rst_type > normal)
    # ------------------------------------------------------------------
    def hard_reset(self):
        """Asynchronous hard reset (rst_n asserted): every state cleared."""
        self.types = list(TYPE_RESET_VALUES)      # ruling: 0x00,0x01,0x02
        self.rules = [_Rule() for _ in range(3)]  # rules = all 0s
        self._clear_table()                       # collection table = empty
        self.pkt_active = False                   # packet FSM = IDLE
        self.result_valid = False                 # current result = none
        self.result = Result()
        self.error_sticky = False                 # input-error state cleared
        self.write_pending = False                # config FSM = IDLE
        self.write_addr = 0x00
        # statistics = all 0s (8 counters; no error-occurrences counter)
        self.total_bytes = 0                      # 16-bit
        self.total_packets = 0                    # 16-bit
        self.rule_hits = [0, 0, 0]                # 8-bit each
        self.no_rule_hits = 0                     # 8-bit

    def _soft_reset(self, code):
        """Synchronous soft reset, rst_type != 00 (description.md RESET)."""
        if code == 0b01:
            # Packet-processing reset: collection table + packet-active state
            # + input-error state + current result + out_state.
            # Does NOT touch type registers, rules, counters or the config
            # FSM (owner ruling: write_pending survives rst_type=01 and 11;
            # only rst_type=10, the hard reset or a config-FSM resolution
            # clears it).
            self._clear_table()
            self.pkt_active = False
            self.result_valid = False
            self.error_sticky = False
        elif code == 0b10:
            # Decision 2B: rule/type registers reset; collected bytes AND
            # the current matched/unmatched selection remain untouched.
            # Owner ruling: rst_type=10 ALSO clears write_pending.
            self.rules = [_Rule() for _ in range(3)]
            self.types = list(TYPE_RESET_VALUES)
            self.write_pending = False
        elif code == 0b11:
            # Statistics counters only.
            self.total_bytes = 0
            self.total_packets = 0
            self.rule_hits = [0, 0, 0]
            self.no_rule_hits = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------
    def step(self, ui_in: int, cfg_mode: int, packet_status: int,
             rst_type: int, ena: int = 1, rst_n: int = 1) -> tuple[int, int]:
        """Advance one clock cycle.

        The arguments are the pin values driven during this cycle (sampled at
        the clock edge). Returns (uo_out, out_state): the REGISTERED outputs
        valid immediately after that edge -- what a bench observes on the
        cycle following these inputs. rst_n=0 performs the asynchronous hard
        reset.
        """
        ui_in &= 0xFF
        cfg_mode &= 0x1
        packet_status &= 0x3
        rst_type &= 0x3

        # Priority 1: rst_n (description.md RESET priority list).
        # INTERP #6 / owner ruling: rst_n is checked before ena -- the
        # dedicated reset pin is asynchronous and is not masked by ena
        # ("Hard reset (rst_n) works also when ena=0", CLARIFICATIONS).
        if rst_n == 0:
            self.hard_reset()
            return (0x00, ST_IDLE)

        # Owner decision 3A: ena is sampled at this cycle's rising edge;
        # inputs ignored, outputs forced to 000/0, internal state held.
        if ena == 0:
            return (0x00, ST_IDLE)

        # Priority 2: rst_type. No ordinary input is processed on a cycle
        # where rst_type != 00.
        if rst_type != 0b00:
            self._soft_reset(rst_type)
            # Decision 1B: 01 clears packet/result/error state; 10/11
            # preserve ongoing state, while transient indications expire.
            return self._state_output()

        # Priority 3: normal operation.
        # Sticky input error (111): swallowed until rst_type=01.
        # INTERP #4 / owner ruling: while 111 is active ALL inputs (packet
        # and config mode) are swallowed -- the spec pins out_state at 111
        # "until the soft reset of the collection table is being triggered",
        # which leaves no cycle for a 110/011/001 output; streamed bytes
        # must not touch the table.
        if self.error_sticky:
            return (0x00, ST_INPUT_ERR)

        # cfg_mode changed during an active packet -> input error (111).
        if cfg_mode == 1 and self.pkt_active:
            return self._enter_input_error()

        if cfg_mode == 0:
            return self._packet_cycle(ui_in, packet_status)
        return self._config_cycle(ui_in, packet_status)

    def step_cycle(self, cycle) -> tuple[int, int]:
        """step() taking one stimulus record (any object with attributes
        ui_in, cfg_mode, packet_status, rst_type, ena, rst_n -- e.g.
        ppctl.Cycle)."""
        return self.step(cycle.ui_in, cycle.cfg_mode, cycle.packet_status,
                         cycle.rst_type, cycle.ena, cycle.rst_n)

    def run(self, cycles) -> list:
        """Run a stimulus list (e.g. from ppctl/generators), returning the
        list of (uo_out, out_state) tuples, one per cycle."""
        return [self.step_cycle(c) for c in cycles]

    # ------------------------------------------------------------------
    # Packet streaming mode (cfg_mode = 0)
    # ------------------------------------------------------------------
    def _packet_cycle(self, ui_in, ps):
        if ps == 0b00:
            # "not a valid byte of packet"; illegal inside a packet
            # (idle inserted mid-packet -> 111).
            if self.pkt_active:
                return self._enter_input_error()
            return (0x00, ST_IDLE)

        if ps == 0b10:
            # Metadata byte. Any metadata value starts a packet; the value
            # only selects which table slot (if any) following data fills.
            self._count_byte()
            if not self.pkt_active:
                self.pkt_active = True
                # total_packets counts at packet start, even if it later
                # errors (description.md STATISTICS).
                self.total_packets = (self.total_packets + 1) & 0xFFFF
            self.current_slot = self._slot_of(ui_in)
            return self._streaming_output()

        if ps == 0b01:
            # Data byte with respect to the latest metadata.
            if not self.pkt_active:
                # data byte before any metadata -> 111
                return self._enter_input_error()
            self._count_byte()
            slot = self.current_slot
            if slot is not None and len(self.table[slot]) < SLOT_BYTES:
                # Non-matching metadata selects no slot (byte dropped); a
                # full slot never overflows (extra bytes dropped). Bytes
                # append MSB-first (AA BB -> 16'hAABB).
                was_full = self._table_full()
                self.table[slot].append(ui_in)
                if not was_full and self._table_full():
                    # Rule matching triggers at stage 2 of the cycle that
                    # completes the table (description.md TIMING, 010).
                    self.result = self._match()
                    self.result_valid = True
            return self._streaming_output()

        # ps == 0b11: end of packet (a flag, never counted as a byte).
        if not self.pkt_active:
            # EOP before a packet was started -> 111
            return self._enter_input_error()
        if self.result_valid:
            # Table was full and a result was produced. Hit counters update
            # exactly once per packet, at EOP (description.md STATISTICS).
            if self.result.rule != 0:
                i = self.result.rule - 1
                self.rule_hits[i] = (self.rule_hits[i] + 1) & 0xFF
            else:
                self.no_rule_hits = (self.no_rule_hits + 1) & 0xFF
            out = self._encode(self.result)
            # "After one packet has filled the table and the packet has
            # finished processing ..., the table is cleared for the next
            # packet to come."
            self._clear_table()
            self.pkt_active = False
            self.result_valid = False
            # 010 is held "up until one cycle after receiving eop
            # (included)" -- the stage-2 output of this EOP cycle is still
            # 010 (this yields the spec's 2-cycle 010 case when the table
            # completes right before EOP).
            return (out, ST_RESULT)

        # EOP arrived before the collection table was full -> 100 for
        # exactly one cycle (the cycle after EOP). (No error-occurrences
        # counter exists in S4; the error shows only in out_state.)
        self.pkt_active = False
        # Owner ruling (CLARIFICATIONS): the collection table IS cleared at
        # an early-EOP (100) error too, so table clearing is uniform at
        # every packet end -- every new packet starts from an empty table.
        self._clear_table()
        return (0x00, ST_EARLY_EOP)

    def _streaming_output(self):
        """Registered output while a packet streams: 010 once a result
        exists (it replaces 001), else 001 (out=0 during 001 -- the optional
        cycle counter is not implemented)."""
        if self.result_valid:
            return (self._encode(self.result), ST_RESULT)
        return (0x00, ST_ACTIVE)

    def _enter_input_error(self):
        """Enter the sticky 111 input-error state (no counter in S4)."""
        self.error_sticky = True
        # Owner ruling: entering the error kills the packet -- clear
        # packet/result state so no later EOP can increment a hit counter
        # for it (STATISTICS: an error after a hit, before EOP, suppresses
        # the count).
        self.pkt_active = False
        self.result_valid = False
        self.current_slot = None
        # The collection table keeps its contents; bytes streamed while 111
        # is active do not touch it, and rst_type=01 is what clears it.
        return (0x00, ST_INPUT_ERR)

    # ------------------------------------------------------------------
    # Configuration mode (cfg_mode = 1)
    # ------------------------------------------------------------------
    def _config_cycle(self, ui_in, ps):
        if ps == 0b00:
            # "not a valid READ/WRITE operation"; a pending WRITE stays
            # pending (spec pending-WRITE table: 00 -> wait, keep pending).
            return (0x00, ST_IDLE)

        if ps == 0b01:
            # READ: single cycle; cancels any pending WRITE
            # (spec: 01 -> cancel pending WRITE + perform READ).
            # Address on in[4:0]; in[7:5] are ignored.
            self.write_pending = False
            return (self._read_reg(ui_in & ADDR_MASK), ST_READ_OK)

        if ps == 0b10:
            # WRITE address: latch it (write_pending=1). Two consecutive 10s
            # simply replace the pending address, no error.
            self.write_pending = True
            self.write_addr = ui_in & ADDR_MASK
            return (0x00, ST_IDLE)

        # ps == 0b11: WRITE data.
        if not self.write_pending:
            # data before address -> 101 for one cycle (no counter in S4)
            return (0x00, ST_WRITE_ERR)
        # Owner ruling: a rejected WRITE completes as a failed operation, so
        # write_pending clears either way, the register keeps its value, and
        # a new WRITE must restart from packet_status=10.
        self.write_pending = False
        if self._write_reg(self.write_addr, ui_in):
            return (ui_in, ST_WRITE_OK)
        return (0x00, ST_WRITE_ERR)

    def _write_reg(self, addr, data) -> bool:
        """Validate and perform a register write; False = rejected."""
        if addr >= ADDR_STATS_FIRST:
            return False  # counters are read-only (reset via rst_type=11)
        if addr < NUM_TYPES:
            # Type registers: the 3 values must be unique. Owner ruling:
            # rewriting a register with its own current value is legal --
            # uniqueness is checked only against the other two.
            for i in range(NUM_TYPES):
                if i != addr and self.types[i] == data:
                    return False
            self.types[addr] = data
            return True
        idx = addr - RULE_BASES[0]
        rule = self.rules[idx // RULE_NUM_REGS]
        off = idx % RULE_NUM_REGS
        if off == 0:
            # Flag register: reject XXXX_0001 -- rule enabled (bit0=1) with
            # no type checking enabled (bits[3:1]=000).
            if (data & 0x0F) == 0x01:
                return False
            rule.flags = data
            return True
        # Match-value byte: +0x01..+0x02 type1 MSB->LSB, +0x03.. type2,
        # +0x05..+0x06 type3.
        t = (off - 1) // SLOT_BYTES
        shift = 8 * ((SLOT_BYTES - 1) - ((off - 1) % SLOT_BYTES))
        rule.match[t] = (rule.match[t] & ~(0xFF << shift)) | (data << shift)
        return True

    def _read_reg(self, addr) -> int:
        if addr < NUM_TYPES:
            return self.types[addr]
        if addr < ADDR_STATS_FIRST:
            idx = addr - RULE_BASES[0]
            rule = self.rules[idx // RULE_NUM_REGS]
            off = idx % RULE_NUM_REGS
            if off == 0:
                return rule.flags
            t = (off - 1) // SLOT_BYTES
            shift = 8 * ((SLOT_BYTES - 1) - ((off - 1) % SLOT_BYTES))
            return (rule.match[t] >> shift) & 0xFF
        # Statistics (description.md MAP DETAILS; 0x18-0x1F, 8 counters)
        if addr == 0x18:
            return (self.total_bytes >> 8) & 0xFF
        if addr == 0x19:
            return self.total_bytes & 0xFF
        if addr == 0x1A:
            return (self.total_packets >> 8) & 0xFF
        if addr == 0x1B:
            return self.total_packets & 0xFF
        if addr == 0x1C:
            return self.rule_hits[0]
        if addr == 0x1D:
            return self.rule_hits[1]
        if addr == 0x1E:
            return self.rule_hits[2]
        return self.no_rule_hits  # 0x1F

    # ------------------------------------------------------------------
    # Collection table and rule matching
    # ------------------------------------------------------------------
    def _clear_table(self):
        self.table = [[], [], []]  # per-type bytes, MSB-first
        self.current_slot = None

    def _slot_of(self, metadata):
        """Table slot selected by a metadata byte, or None when the value
        matches none of the 3 type registers (data dropped until the next
        metadata)."""
        for i, t in enumerate(self.types):
            if t == metadata:
                return i
        return None

    def _table_full(self):
        return all(len(slot) == SLOT_BYTES for slot in self.table)

    def _slot_value(self, slot) -> int:
        """16-bit slot content, first byte at [15:8] (MSB-first)."""
        v = 0
        for b in self.table[slot]:
            v = (v << 8) | b
        return v

    def _match(self) -> Result:
        """3 parallel rules, highest rule NUMBER wins (3 > 2 > 1).

        Hit = rule enabled AND every ENABLED type equal between collected
        and stored values. No hit -> zeros (Result(0, 0, 0)).
        """
        values = [self._slot_value(t) for t in range(NUM_TYPES)]
        best = Result()
        for i, rule in enumerate(self.rules):
            flags = rule.flags
            if not (flags & 0x01):
                continue  # rule disabled
            n_types = 0
            hit = True
            for t in range(NUM_TYPES):
                if flags & (1 << (t + 1)):  # bits[3:1] = type1..3 enables
                    n_types += 1
                    if values[t] != rule.match[t]:
                        hit = False
                        break
            if n_types == 0:
                # enable=1 with mask=000 is rejected at WRITE time, so this
                # is unreachable; treated as no-hit defensively.
                continue
            if hit:
                # ascending order: a later (higher-numbered) hit overwrites
                best = Result(i + 1, n_types, (flags >> 4) & 0x0F)
        return best

    @staticmethod
    def _encode(result: Result) -> int:
        """out[7:0] for out_state=010 (description.md OUT PINS):
        out[1:0] rule number, out[3:2] types checked (00,01,10 = 1,2,3),
        out[7:4] action; all zero when no rule matched (rule 0, NOP)."""
        if result.rule == 0:
            return 0x00
        return ((result.action & 0xF) << 4) | ((result.n_types - 1) << 2) \
            | result.rule

    def _state_output(self):
        """Registered outputs reflecting preserved state (soft-reset cycles
        10/11, owner ruling; after a 01 reset the state is cleared -> 000)."""
        if self.error_sticky:
            return (0x00, ST_INPUT_ERR)
        if self.result_valid:
            return (self._encode(self.result), ST_RESULT)
        if self.pkt_active:
            return (0x00, ST_ACTIVE)
        return (0x00, ST_IDLE)

    def _count_byte(self):
        """total_bytes: accepted metadata AND data bytes in packet mode
        (never EOP, never config-mode bytes).

        INTERP #2 / owner ruling: bytes that trigger the sticky 111 error,
        and bytes streamed while 111 is active, are NOT counted -- they are
        rejected, not accepted into the packet stream ("any tentative to
        stream in other info will not impact the collection table" / "the
        byte triggering the error and any byte streamed while it is active
        do not count toward total_bytes", CLARIFICATIONS). Dropped-but-valid
        bytes (non-matching metadata, full slot) ARE accepted and counted.
        """
        self.total_bytes = (self.total_bytes + 1) & 0xFFFF
