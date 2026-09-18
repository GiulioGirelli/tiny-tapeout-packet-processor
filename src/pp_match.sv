// SPDX-License-Identifier: Apache-2.0
`default_nettype none

// Combinational 3-rule match engine (description.md S4 HIGH LEVEL
// FUNCTIONING / Rule table): a rule is hit iff it is enabled (flags bit0)
// and every ENABLED type (flags bits[3:1]) matches between collected and
// stored 16-bit values; a disabled type check is a wildcard. If several
// rules hit, the highest rule NUMBER wins (3 > 2 > 1); no hit -> zeros.
// Rule matching can only be performed when the collection table is full:
// with match_en=0 every output is 0.
//
// Fully combinational so the result is valid at stage 2 of the cycle whose
// final byte completes the table (two-stage TIMING): pp_core feeds it
// pp_collection's collected_next / full_next.
module pp_match (
    input  logic [47:0]  tbl,          // effective slot contents (collected_next);
                                       // ("table" is an SV keyword)
    input  logic         match_en,     // table effectively full (full_next)
    input  logic [23:0]  rule_flags,   // rule_flags[8*r +: 8] = rule r+1 flags
    input  logic [143:0] rule_values,  // rule_values[16*(3*r + t) +: 16] = rule r+1 type t+1 value
    output logic [2:0]   hit_vector,   // hit_vector[r] = rule r+1 hit
    output logic [1:0]   win_rule,     // winning rule number 1..3, 0 = none
    output logic [3:0]   win_action,   // winner's action bits (flags[7:4])
    output logic [1:0]   win_types     // winner's type count: 1,2,3 -> 00,01,10
);

    logic [2:0] hits;

    for (genvar r = 0; r < 3; r++) begin : gen_hits
        logic [2:0] type_ok;
        for (genvar t = 0; t < 3; t++) begin : gen_cmp
            assign type_ok[t] = ~rule_flags[8*r + t + 1]
                | (tbl[16*t +: 16] == rule_values[16*(3*r + t) +: 16]);
        end
        assign hits[r] = rule_flags[8*r] & (&type_ok);
    end

    // Winning rule's flag byte (priority: highest rule number).
    logic [7:0] win_flags;
    assign win_flags = hits[2] ? rule_flags[23:16]
                     : hits[1] ? rule_flags[15:8]
                     : hits[0] ? rule_flags[7:0]
                     : 8'h00;

    // Number of enabled type checks of the winner (1..3 for a legal hit --
    // enable=1 with an empty mask is rejected at WRITE time in pp_rules).
    logic [1:0] n_types;
    assign n_types = {1'b0, win_flags[1]} + {1'b0, win_flags[2]}
                   + {1'b0, win_flags[3]};

    // win_flags[0] (enable) is implied by a hit.
    logic _unused;
    assign _unused = &{1'b0, win_flags[0]};

    always_comb begin
        if (!match_en) begin
            hit_vector = 3'b000;
            win_rule   = 2'd0;
            win_action = 4'd0;
            win_types  = 2'd0;
        end else begin
            hit_vector = hits;
            // priority: highest rule number wins
            if (hits[2])      win_rule = 2'd3;
            else if (hits[1]) win_rule = 2'd2;
            else if (hits[0]) win_rule = 2'd1;
            else              win_rule = 2'd0;
            if (win_rule != 2'd0) begin
                win_action = win_flags[7:4];
                // 1,2,3 types -> 00,01,10 (count minus one)
                win_types  = n_types - 2'd1;
            end else begin
                win_action = 4'd0;
                win_types  = 2'd0;
            end
        end
    end

endmodule
