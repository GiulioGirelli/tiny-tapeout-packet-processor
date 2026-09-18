// SPDX-License-Identifier: Apache-2.0
`default_nettype none

// Datapath core (description.md S4 MAIN IDEA): byte stream -> metadata
// selector -> 3x16-bit collection table -> 3 configurable parallel rules
// -> priority selection. Wires pp_collection's effective next state
// (collected_next / full_next) into the combinational match engine so the
// match result is valid at stage 2 of the completing byte's cycle
// (description.md TIMING). pp_stats is kept outside the core: its strobes
// come from the control FSMs (pp_input).
module pp_core (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        table_clear,   // rst_type=01 or packet end
    input  logic        rules_reset,   // rst_type=10
    input  logic        meta_valid,
    input  logic [7:0]  meta_byte,
    input  logic        data_valid,
    input  logic [7:0]  data_byte,
    input  logic        wr_en,
    input  logic [4:0]  wr_addr,
    input  logic [7:0]  wr_data,
    output logic        wr_accept,
    input  logic [4:0]  rd_addr,
    output logic [7:0]  rd_data,       // rules part only (stats at top level)
    output logic        full,          // registered full (monitoring)
    output logic        full_next,     // effective full (match trigger)
    output logic [2:0]  hit_vector,
    output logic [1:0]  win_rule,
    output logic [3:0]  win_action,    // S4: 4-bit action
    output logic [1:0]  win_types
);

    logic [23:0]  type_values;
    logic [23:0]  rule_flags;
    logic [143:0] rule_values;
    logic [47:0]  collected;
    logic [47:0]  collected_next;

    pp_collection u_collection (
        .clk            (clk),
        .rst_n          (rst_n),
        .clear          (table_clear),
        .meta_valid     (meta_valid),
        .meta_byte      (meta_byte),
        .data_valid     (data_valid),
        .data_byte      (data_byte),
        .type_values    (type_values),
        .collected      (collected),
        .collected_next (collected_next),
        .full           (full),
        .full_next      (full_next)
    );

    pp_rules u_rules (
        .clk            (clk),
        .rst_n          (rst_n),
        .rules_reset    (rules_reset),
        .wr_en          (wr_en),
        .wr_addr        (wr_addr),
        .wr_data        (wr_data),
        .wr_accept      (wr_accept),
        .rd_addr        (rd_addr),
        .rd_data        (rd_data),
        .type_values    (type_values),
        .rule_flags     (rule_flags),
        .rule_values    (rule_values)
    );

    pp_match u_match (
        .tbl            (collected_next),
        .match_en       (full_next),
        .rule_flags     (rule_flags),
        .rule_values    (rule_values),
        .hit_vector     (hit_vector),
        .win_rule       (win_rule),
        .win_action     (win_action),
        .win_types      (win_types)
    );

    // The registered contents are only consumed through the next-state path
    // at core level; `full` is exposed for monitoring.
    logic _unused;
    assign _unused = &{1'b0, collected};

endmodule
