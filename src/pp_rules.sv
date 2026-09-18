// SPDX-License-Identifier: Apache-2.0
`default_nettype none

// Type registers (3x 8-bit) + rule table (3 rules x 7 registers): the
// writable part of the 5-bit address space (description.md S4 ADDRESS MAP /
// MAP DETAILS / Configuration mode).
//
//   0x00-0x02   type value registers (must hold 3 unique values)
//   0x03-0x09   rule 1: +0x00 FLAGS, +0x01..+0x02 type1 MSB->LSB,
//               +0x03..+0x04 type2, +0x05..+0x06 type3
//   0x0A-0x10   rule 2 (same layout)
//   0x11-0x17   rule 3 (same layout)
//   0x18-0x1F   statistics (pp_stats) -- read-only here, read as 0
//
// rules_reset (rst_type=10) zeroes the rule table and restores the type
// registers to 0x00,0x01,0x02 (owner ruling, CLARIFICATIONS).
module pp_rules (
    input  logic         clk,
    input  logic         rst_n,
    input  logic         rules_reset,   // sync: rst_type=10
    input  logic         wr_en,         // a WRITE commits this cycle
    input  logic  [4:0]  wr_addr,
    input  logic  [7:0]  wr_data,
    output logic         wr_accept,     // combinational validation
    input  logic  [4:0]  rd_addr,
    output logic  [7:0]  rd_data,       // combinational; 0 for addr >= 0x18
    output logic  [23:0] type_values,   // type_values[8*i +: 8] = type reg i
    output logic  [23:0] rule_flags,    // rule_flags[8*r +: 8] = rule r+1 flags
    output logic [143:0] rule_values    // rule_values[16*(3r+t) +: 16]
);

    // Byte register file for exactly the 24 writable registers 0x00-0x17
    // (no dummy registers): 3 type registers + 3 rules x 7. The statistics
    // region 0x18-0x1F can never be written (wr_accept=0) and reads as 0.
    logic [7:0] regs [24];

    logic is_flags_addr;
    assign is_flags_addr = (wr_addr == 5'h03) || (wr_addr == 5'h0A)
                        || (wr_addr == 5'h11);

    // WRITE validation (description.md Configuration mode; owner ruling:
    // rewriting a type register with its own current value is legal --
    // uniqueness is checked only against the other two). Combinational,
    // uses the current register state; a rejected WRITE leaves every
    // register unchanged.
    always_comb begin
        wr_accept = 1'b1;
        if (wr_addr >= 5'h18) begin
            // statistics registers are read-only (rst_type=11 resets them)
            wr_accept = 1'b0;
        end else if (wr_addr < 5'h03) begin
            // the 3 type values must be unique
            if ((wr_addr != 5'h00) && (regs[0] == wr_data)) wr_accept = 1'b0;
            if ((wr_addr != 5'h01) && (regs[1] == wr_data)) wr_accept = 1'b0;
            if ((wr_addr != 5'h02) && (regs[2] == wr_data)) wr_accept = 1'b0;
        end else if (is_flags_addr && ((wr_data & 8'h0F) == 8'h01)) begin
            // FLAGS pattern XXXX_0001: rule enabled, no type checks enabled
            wr_accept = 1'b0;
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            // hard reset: type registers 0,1,2 (ruling), rest 0
            for (int i = 0; i < 24; i++) regs[i] <= 8'h00;
            regs[5'h01] <= 8'h01;
            regs[5'h02] <= 8'h02;
        end else if (rules_reset) begin
            // rst_type=10: same type-register defaults, rules zeroed;
            // priority over a WRITE on the same cycle
            for (int i = 0; i < 24; i++) regs[i] <= 8'h00;
            regs[5'h01] <= 8'h01;
            regs[5'h02] <= 8'h02;
        end else if (wr_en && wr_accept) begin
            regs[wr_addr] <= wr_data;
        end
    end

    // Combinational read; the statistics region reads as 0 here.
    assign rd_data = (rd_addr < 5'h18) ? regs[rd_addr] : 8'h00;

    // Packed outputs.
    assign type_values = {regs[5'h02], regs[5'h01], regs[5'h00]};
    assign rule_flags  = {regs[5'h11], regs[5'h0A], regs[5'h03]};

    for (genvar r = 0; r < 3; r++) begin : gen_rules
        for (genvar t = 0; t < 3; t++) begin : gen_types
            // rule r+1 base = 0x03 + r*7; type t+1 16-bit value MSB-first
            // at base+1+2t, base+2+2t
            assign rule_values[16*(3*r + t) +: 16] =
                {regs[5'h03 + r*7 + t*2 + 1],
                 regs[5'h03 + r*7 + t*2 + 2]};
        end
    end

endmodule
