// SPDX-License-Identifier: Apache-2.0
`default_nettype none

// Input interface and control FSMs (description.md S4 COMPONENTS / MODES /
// I/O FLAGS / RESET): decodes cfg_mode / packet_status / rst_type into
// datapath drives and statistic strobes.
//
// State (registers): pkt_active (a live packet is streaming), err_in
// (sticky 111 input error), write_pending + pending_addr (config WRITE
// address latched, in[4:0] -- in[7:5] are ignored per spec). All event
// strobes and datapath drives are combinational in the cycle of the input;
// the datapath registers them at the clock edge (two-stage TIMING).
//
// Gating (description.md RESET priority + owner rulings, CLARIFICATIONS):
//   rst_n (async) > rst_type > normal operation. On rst_type!=00 cycles NO
//   ordinary input is processed -- only that code's reset action fires
//   (01: table_clear + drop pkt_active/err_in; 10: rules_reset + clear
//   write_pending [ruling]; 11: stats_reset). ena=0 ignores everything,
//   including rst_type; err_in swallows all inputs until rst_type=01.
// S4 note: the error-occurrences counter is gone, so there is no
// error_event strobe; hit suppression stays on the control side (no
// hit_eop for an early EOP or a killed packet).
module pp_input (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        ena,
    input  logic [7:0]  ui_in,
    input  logic        cfg_mode,
    input  logic [1:0]  packet_status,
    input  logic [1:0]  rst_type,
    // state visibility (for pp_output)
    output logic        pkt_active,
    output logic        err_in,
    // one-cycle event strobes
    output logic        ev_start,      // first metadata of a packet accepted
    output logic        ev_eop_normal, // EOP with full table (clean end)
    output logic        ev_eop_early,  // EOP before the table was full (100)
    output logic        ev_err_input,  // input error committed (111)
    output logic        ev_read,       // READ address presented
    output logic        ev_write_ok,   // WRITE committed and accepted
    output logic        ev_write_err,  // WRITE rejected / data before address
    // pp_core drives
    output logic        meta_valid,
    output logic [7:0]  meta_byte,
    output logic        data_valid,
    output logic [7:0]  data_byte,
    output logic        table_clear,   // rst_type=01 or any packet end (ruling)
    output logic        rules_reset,   // rst_type=10
    output logic        wr_en,
    output logic [4:0]  wr_addr,
    output logic [7:0]  wr_data,
    input  logic        wr_accept,     // combinational, from pp_rules
    input  logic        core_full,     // registered table full, from pp_core
    output logic [4:0]  rd_addr,
    // pp_stats strobes
    output logic        stats_reset,   // rst_type=11
    output logic        byte_accepted,
    output logic        packet_started,
    output logic        hit_eop
);

    logic        write_pending;
    logic [4:0]  pending_addr;

    // ----------------------------------------------------------------------
    // Combinational decode: events, datapath drives, stats strobes
    // ----------------------------------------------------------------------
    always_comb begin
        ev_start       = 1'b0;
        ev_eop_normal  = 1'b0;
        ev_eop_early   = 1'b0;
        ev_err_input   = 1'b0;
        ev_read        = 1'b0;
        ev_write_ok    = 1'b0;
        ev_write_err   = 1'b0;
        meta_valid     = 1'b0;
        data_valid     = 1'b0;
        wr_en          = 1'b0;
        byte_accepted  = 1'b0;
        packet_started = 1'b0;
        hit_eop        = 1'b0;

        if (ena && (rst_type == 2'b00) && !err_in) begin
            if (cfg_mode == 1'b0) begin
                // ------------------------------------------------ packet mode
                case (packet_status)
                    2'b10: begin
                        // metadata: always accepted; starts a packet when idle
                        meta_valid    = 1'b1;
                        byte_accepted = 1'b1;
                        if (!pkt_active) begin
                            ev_start       = 1'b1;
                            packet_started = 1'b1;
                        end
                    end
                    2'b01: begin
                        // data: legal only inside a packet
                        if (pkt_active) begin
                            data_valid    = 1'b1;
                            byte_accepted = 1'b1;
                        end else begin
                            ev_err_input = 1'b1;  // data before first metadata
                        end
                    end
                    2'b11: begin
                        // end of packet (a flag, never a counted byte)
                        if (!pkt_active) begin
                            ev_err_input = 1'b1;  // EOP before a packet started
                        end else if (core_full) begin
                            ev_eop_normal = 1'b1;
                            hit_eop       = 1'b1;  // hit counters update at EOP
                        end else begin
                            ev_eop_early = 1'b1;  // table not full -> 100
                        end
                    end
                    default: begin  // 2'b00
                        if (pkt_active) begin
                            ev_err_input = 1'b1;  // idle inserted mid-packet
                        end
                    end
                endcase
            end else begin
                // ------------------------------------------------ config mode
                if (pkt_active) begin
                    ev_err_input = 1'b1;  // cfg_mode changed mid-packet
                end else begin
                    case (packet_status)
                        2'b01: begin
                            // READ: single cycle; cancels any pending WRITE
                            ev_read = 1'b1;
                        end
                        2'b10: begin
                            // WRITE address: latched below (replace = no error)
                        end
                        2'b11: begin
                            // WRITE data
                            if (write_pending) begin
                                wr_en = 1'b1;
                                if (wr_accept) begin
                                    ev_write_ok = 1'b1;
                                end else begin
                                    ev_write_err = 1'b1;  // rejected WRITE
                                end
                            end else begin
                                ev_write_err = 1'b1;  // data before address
                            end
                        end
                        default: ;  // 2'b00: nothing (pending kept)
                    endcase
                end
            end
        end
    end

    // Byte taps. Config addresses use in[4:0]; in[7:5] are ignored (spec).
    assign meta_byte = ui_in;
    assign data_byte = ui_in;
    assign wr_data   = ui_in;
    assign wr_addr   = pending_addr;
    assign rd_addr   = ui_in[4:0];

    // Soft-reset strobes (ena-gated; rst_n stays purely asynchronous).
    // Ruling: the table also clears at EVERY packet end, normal or early.
    assign table_clear = (ena && (rst_type == 2'b01))
                       | ev_eop_normal | ev_eop_early;
    assign rules_reset = ena && (rst_type == 2'b10);
    assign stats_reset = ena && (rst_type == 2'b11);

    // ----------------------------------------------------------------------
    // State registers
    // ----------------------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pkt_active    <= 1'b0;
            err_in        <= 1'b0;
            write_pending <= 1'b0;
            pending_addr  <= 5'h00;
        end else if (ena) begin
            if (rst_type == 2'b01) begin
                // packet-processing reset: kills the packet and the sticky
                // error; write_pending survives (owner ruling)
                pkt_active <= 1'b0;
                err_in     <= 1'b0;
            end else if (rst_type == 2'b10) begin
                // owner ruling: rst_type=10 clears write_pending
                write_pending <= 1'b0;
            end else if (rst_type == 2'b00) begin
                // packet FSM
                if (ev_err_input) begin
                    // the packet is dead (owner ruling): no later EOP can
                    // count a hit for it
                    err_in     <= 1'b1;
                    pkt_active <= 1'b0;
                end else begin
                    if (ev_start) pkt_active <= 1'b1;
                    if (ev_eop_normal || ev_eop_early) pkt_active <= 1'b0;
                end
                // config FSM (only when no live packet, never during 111)
                if (!err_in && (cfg_mode == 1'b1) && !pkt_active) begin
                    case (packet_status)
                        2'b01: write_pending <= 1'b0;  // READ cancels pending
                        2'b10: begin
                            write_pending <= 1'b1;     // latch / replace
                            pending_addr  <= ui_in[4:0];
                        end
                        2'b11: begin
                            // WRITE attempt completes (accepted or rejected --
                            // owner ruling: a rejected WRITE clears pending)
                            if (write_pending) write_pending <= 1'b0;
                        end
                        default: ;  // 2'b00: keep pending
                    endcase
                end
            end
            // rst_type == 2'b11: statistics reset only, no state change here
        end
        // ena == 0: hold everything (ruling)
    end

endmodule
