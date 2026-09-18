// SPDX-License-Identifier: Apache-2.0
`default_nettype none

// Chip-level composition of the packet processor (description.md S4 MAIN
// IDEA / HARDWARE): pp_input (control FSMs + strobes) drives pp_core
// (collection table + rule table + match engine) and pp_stats (8 counters,
// no error-occurrences counter); pp_output registers out_state/uo_out.
// Exposes exactly the chip's pin functions -- the Tiny Tapeout wrapper is a
// thin shell around this module.
module pp_chip (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       ena,
    input  logic [7:0] ui_in,
    input  logic       cfg_mode,
    input  logic [1:0] packet_status,
    input  logic [1:0] rst_type,
    output logic [7:0] uo_out,
    output logic [2:0] out_state
);

    // pp_input -> state visibility / events
    logic       pkt_active;
    logic       err_in;
    logic       ev_start;
    logic       ev_eop_normal;
    logic       ev_eop_early;
    logic       ev_err_input;
    logic       ev_read;
    logic       ev_write_ok;
    logic       ev_write_err;
    // pp_input -> pp_core drives
    logic       meta_valid;
    logic [7:0] meta_byte;
    logic       data_valid;
    logic [7:0] data_byte;
    logic       table_clear;
    logic       rules_reset;
    logic       wr_en;
    logic [4:0] wr_addr;
    logic [7:0] wr_data;
    logic       wr_accept;
    logic       core_full;
    logic [4:0] rd_addr;
    // pp_input -> pp_stats strobes
    logic       stats_reset;
    logic       byte_accepted;
    logic       packet_started;
    logic       hit_eop;
    // core <-> output
    logic       full_next;
    logic [2:0] hit_vector;
    logic [1:0] win_rule;
    logic [3:0] win_action;
    logic [1:0] win_types;
    logic [1:0] result_rule;
    // read path mux
    logic [7:0] core_rd_data;
    logic [7:0] stats_rd_data;
    logic [7:0] rd_data;

    pp_input u_input (
        .clk            (clk),
        .rst_n          (rst_n),
        .ena            (ena),
        .ui_in          (ui_in),
        .cfg_mode       (cfg_mode),
        .packet_status  (packet_status),
        .rst_type       (rst_type),
        .pkt_active     (pkt_active),
        .err_in         (err_in),
        .ev_start       (ev_start),
        .ev_eop_normal  (ev_eop_normal),
        .ev_eop_early   (ev_eop_early),
        .ev_err_input   (ev_err_input),
        .ev_read        (ev_read),
        .ev_write_ok    (ev_write_ok),
        .ev_write_err   (ev_write_err),
        .meta_valid     (meta_valid),
        .meta_byte      (meta_byte),
        .data_valid     (data_valid),
        .data_byte      (data_byte),
        .table_clear    (table_clear),
        .rules_reset    (rules_reset),
        .wr_en          (wr_en),
        .wr_addr        (wr_addr),
        .wr_data        (wr_data),
        .wr_accept      (wr_accept),
        .core_full      (core_full),
        .rd_addr        (rd_addr),
        .stats_reset    (stats_reset),
        .byte_accepted  (byte_accepted),
        .packet_started (packet_started),
        .hit_eop        (hit_eop)
    );

    pp_core u_core (
        .clk            (clk),
        .rst_n          (rst_n),
        .table_clear    (table_clear),
        .rules_reset    (rules_reset),
        .meta_valid     (meta_valid),
        .meta_byte      (meta_byte),
        .data_valid     (data_valid),
        .data_byte      (data_byte),
        .wr_en          (wr_en),
        .wr_addr        (wr_addr),
        .wr_data        (wr_data),
        .wr_accept      (wr_accept),
        .rd_addr        (rd_addr),
        .rd_data        (core_rd_data),
        .full           (core_full),
        .full_next      (full_next),
        .hit_vector     (hit_vector),
        .win_rule       (win_rule),
        .win_action     (win_action),
        .win_types      (win_types)
    );

    pp_stats u_stats (
        .clk            (clk),
        .rst_n          (rst_n),
        .stats_reset    (stats_reset),
        .byte_accepted  (byte_accepted),
        .packet_started (packet_started),
        .hit_eop        (hit_eop),
        .hit_rule       (result_rule),
        .rd_addr        (rd_addr),
        .rd_data        (stats_rd_data)
    );

    pp_output u_output (
        .clk            (clk),
        .rst_n          (rst_n),
        .ena            (ena),
        .rst_type       (rst_type),
        .pkt_active     (pkt_active),
        .err_in         (err_in),
        .ev_start       (ev_start),
        .ev_eop_normal  (ev_eop_normal),
        .ev_eop_early   (ev_eop_early),
        .ev_err_input   (ev_err_input),
        .ev_read        (ev_read),
        .ev_write_ok    (ev_write_ok),
        .ev_write_err   (ev_write_err),
        .full_next      (full_next),
        .win_rule       (win_rule),
        .win_action     (win_action),
        .win_types      (win_types),
        .rd_data        (rd_data),
        .wr_data        (wr_data),
        .out_state      (out_state),
        .uo_out         (uo_out),
        .result_rule    (result_rule)
    );

    // Read mux: statistics registers come from pp_stats; the rule/type
    // space from pp_core (which already reads 0 for addr >= 0x18).
    assign rd_data = (rd_addr >= 5'h18) ? stats_rd_data : core_rd_data;

    // The match hit vector has no chip-level consumer.
    logic _unused;
    assign _unused = &{1'b0, hit_vector};

endmodule
