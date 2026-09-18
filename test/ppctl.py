"""Stimulus builders for the Tiny Tapeout packet processor, configuration
S4 (REVISION S4, 10 Sep 2026: 3 types, 16-bit entries, 5-bit/32-register
map, 4-bit actions, 8 statistics counters).

SHARED verification tooling: pure Python standard library only, no cocotb
imports. Used by the model self-tests (test_model.py) now and by the
pin-level cocotb benches later.

Every builder returns a list of per-cycle stimulus records (Cycle). Feed a
record list to PacketProcessorModel.run() (test/model.py) or drive the DUT
pins with one record per clock cycle; the model/bench then observes the
registered outputs on the following cycle (description.md TIMING).

All encodings trace to description.md I/O FLAGS / IN PINS / ADDRESS MAP.
"""

from typing import NamedTuple


class Cycle(NamedTuple):
    """Pin values driven during one clock cycle (sampled at the edge).

    Defaults describe a benign idle cycle: packet mode, no byte, no reset,
    chip enabled, out of reset.
    """
    ui_in: int = 0x00         # in[7:0]  -- data/address byte
    cfg_mode: int = 0         # uio[0]   -- 0 = packet streaming, 1 = config
    packet_status: int = 0    # uio[2:1] -- meaning depends on cfg_mode
    rst_type: int = 0         # uio[4:3] -- 00 none, 01 table, 10 rules, 11 stats
    ena: int = 1              # global gate (CLARIFICATIONS)
    rst_n: int = 1            # asynchronous hard reset, active low


# ---------------------------------------------------------------------------
# packet_status[1:0] encodings (description.md I/O FLAGS)
# ---------------------------------------------------------------------------
# Packet streaming mode (cfg_mode = 0)
PS_IDLE = 0b00   # not a valid byte of packet
PS_DATA = 0b01   # data byte w.r.t. the latest metadata
PS_META = 0b10   # metadata byte
PS_EOP = 0b11    # end of packet (flag only, no data)

# Configuration mode (cfg_mode = 1)
CFG_NONE = 0b00  # not a valid READ/WRITE operation
CFG_READ = 0b01  # address for READ (in[4:0]; in[7:5] ignored)
CFG_WADDR = 0b10 # address for WRITE (write becomes pending)
CFG_WDATA = 0b11 # data to write

# ---------------------------------------------------------------------------
# rst_type[1:0] encodings (description.md I/O FLAGS / RESET)
# ---------------------------------------------------------------------------
RST_NONE = 0b00
RST_TABLE = 0b01  # collection table + packet-active + input-error + result
RST_RULES = 0b10  # rule table + type registers (-> 0,1,2, ruling)
RST_STATS = 0b11  # statistics counters only

# ---------------------------------------------------------------------------
# Register map (description.md ADDRESS MAP / MAP DETAILS): 5-bit, 32
# registers, exactly full
# ---------------------------------------------------------------------------
ADDR_TYPE = (0x00, 0x01, 0x02)             # type value registers
RULE_BASE = (0x03, 0x0A, 0x11)             # rule 1/2/3 base address (7 regs each)
ADDR_TOTAL_BYTES_HI = 0x18
ADDR_TOTAL_BYTES_LO = 0x19
ADDR_TOTAL_PACKETS_HI = 0x1A
ADDR_TOTAL_PACKETS_LO = 0x1B
ADDR_RULE1_HITS = 0x1C
ADDR_RULE2_HITS = 0x1D
ADDR_RULE3_HITS = 0x1E
ADDR_NO_RULE_HITS = 0x1F


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def idle(n: int = 1) -> list:
    """n idle cycles (packet mode, no byte)."""
    return [Cycle() for _ in range(n)]


def hard_reset(n: int = 1) -> list:
    """n cycles with rst_n asserted (asynchronous hard reset)."""
    return [Cycle(rst_n=0) for _ in range(n)]


def soft_reset(code: int, n: int = 1) -> list:
    """n cycles asserting rst_type=code (RST_TABLE / RST_RULES / RST_STATS).

    rst_type has priority over normal processing and no ordinary input is
    processed on such a cycle (description.md RESET).
    """
    return [Cycle(rst_type=code) for _ in range(n)]


def read_reg(addr: int) -> list:
    """Single-cycle READ: returns the addressed value + out_state=110 on the
    next cycle. Also cancels any pending WRITE (spec pending table: 01).
    Address on in[4:0]; in[7:5] are ignored."""
    return [Cycle(ui_in=addr & 0x1F, cfg_mode=1, packet_status=CFG_READ)]


def write_reg(addr: int, data: int) -> list:
    """Two-cycle WRITE: latch address (ps=10), then data (ps=11).
    out_state=011 + written value on success, 101 on rejection."""
    return [Cycle(ui_in=addr & 0x1F, cfg_mode=1, packet_status=CFG_WADDR),
            Cycle(ui_in=data & 0xFF, cfg_mode=1, packet_status=CFG_WDATA)]


def set_type_values(v0: int, v1: int, v2: int) -> list:
    """Write the 3 type registers (must be unique values, spec WRITE rules)."""
    cycles = []
    for addr, value in zip(ADDR_TYPE, (v0, v1, v2)):
        cycles += write_reg(addr, value)
    return cycles


def rule_flags(enable: int, types_mask: int, action: int) -> int:
    """Flag byte layout (description.md REGISTERS):
    bit0 = rule enable, bits[3:1] = type1..3 check enables (types_mask bit i
    enables type i+1), bits[7:4] = action (4'b0000 = NOP)."""
    return ((action & 0xF) << 4) | ((types_mask & 0x7) << 1) | (enable & 0x1)


def write_rule(rule_id: int, enable: int, types_mask: int, action: int,
               t1: int, t2: int, t3: int) -> list:
    """Write all 7 registers of rule `rule_id` (1..3):
    flags, then the three 16-bit match values MSB-first (MAP DETAILS:
    base+0x01..0x02 type1, +0x03..0x04 type2, +0x05..0x06 type3)."""
    base = RULE_BASE[rule_id - 1]
    cycles = write_reg(base, rule_flags(enable, types_mask, action))
    for i, value in enumerate((t1, t2, t3)):
        for b in range(2):
            cycles += write_reg(base + 1 + 2 * i + b,
                                (value >> (8 * (1 - b))) & 0xFF)
    return cycles


def packet(chunks, eop: bool = True) -> list:
    """Packet-streaming cycles for a list of chunks.

    Each chunk is (metadata_byte, data_bytes) where data_bytes is any
    iterable of byte values (possibly empty -> zero-length chunk, allowed by
    the spec). A metadata byte opens the chunk; the data bytes follow it;
    the same metadata may repeat within a packet. Appends one EOP cycle
    unless eop=False (useful for mid-packet error/reset scenarios).
    """
    cycles = []
    for meta, data_bytes in chunks:
        cycles.append(Cycle(ui_in=meta & 0xFF, packet_status=PS_META))
        for d in data_bytes:
            cycles.append(Cycle(ui_in=d & 0xFF, packet_status=PS_DATA))
    if eop:
        # EOP holds no relevant data in the input, it is just a flag
        cycles.append(Cycle(packet_status=PS_EOP))
    return cycles
