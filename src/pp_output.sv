// SPDX-License-Identifier: Apache-2.0
`default_nettype none

// Output interface (description.md S4 COMPONENTS / OUT PINS / TIMING): the
// registered out_state[2:0] / uo_out[7:0] and the per-packet result
// capture.
//
// Result capture: when the table effectively completes (full_next) during
// a live, error-free packet and no result exists yet, win_* are captured
// into result_* -- ONCE per packet. Both the 010 output and the EOP hit
// counting (hit_rule, exported to pp_stats) use the captured values even
// if the rules are reset mid-packet afterwards (matches test/model.py,
// which stores the match at completion and never recomputes). result_valid
// clears on a normal EOP, an input error, rst_type=01, or rst_n.
//
// out_state/uo_out are registers updated at the clock edge, so the output
// caused by an input is visible on the NEXT cycle (two-stage TIMING).
// State-derived levels (001 packet-active, 010 result) are computed from
// the NEXT state of the FSMs -- the same edge that creates the state also
// registers the level (test/model.py returns 001 for the first-metadata
// cycle and 010 for the completing-byte cycle).
//
// Next-state priority:
//   1. !ena          -> 000 / 0   (forced outputs; all other state holds)
//   2. rst_type==01  -> 000 / 0   (packet-processing reset)
//   3. rst_type 10/11 -> reflect preserved state (owner ruling:
//                        err_in->111, result_valid->010/captured,
//                        pkt_active->001, else 000 -- identical to
//                        test/model.py's _state_output())
//   4. ev_err_input || err_in -> 111 / 0 (sticky until rst_type=01)
//   5. ev_eop_early  -> 100 / 0   (one cycle)
//   6. ev_read       -> 110 / rd_data
//   7. ev_write_ok   -> 011 / wr_data
//   8. ev_write_err  -> 101 / 0   (one cycle)
//   9. ev_eop_normal -> 010 / captured result (held one cycle past EOP)
//  10. result_valid_next -> 010 / result (live win_* on the completing
//                         cycle, captured registers afterwards)
//  11. pkt_active_next   -> 001 / 0
//  12. else          -> 000 / 0
module pp_output (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       ena,
    input  logic [1:0] rst_type,
    input  logic       pkt_active,
    input  logic       err_in,
    input  logic       ev_start,
    input  logic       ev_eop_normal,
    input  logic       ev_eop_early,
    input  logic       ev_err_input,
    input  logic       ev_read,
    input  logic       ev_write_ok,
    input  logic       ev_write_err,
    input  logic       full_next,      // table effectively full (this cycle)
    input  logic [1:0] win_rule,
    input  logic [3:0] win_action,     // S4: 4-bit action
    input  logic [1:0] win_types,
    input  logic [7:0] rd_data,        // muxed read value (for 110)
    input  logic [7:0] wr_data,        // written value echo (for 011)
    output logic [2:0] out_state,
    output logic [7:0] uo_out,
    output logic [1:0] result_rule     // captured winner, to pp_stats.hit_rule
);

    logic       result_valid;
    logic [1:0] result_types;
    logic [3:0] result_action;

    // The completing byte is in flight this cycle and no result exists yet.
    logic capture_now;
    assign capture_now = full_next && pkt_active && !err_in && !result_valid;

    // Next-state view of result_valid / pkt_active (normal operation only;
    // reset/ena cycles are handled by dedicated branches below).
    logic result_valid_next;
    assign result_valid_next = (result_valid || capture_now)
                            && !(ev_eop_normal || ev_err_input);

    logic pkt_active_next;
    assign pkt_active_next = ev_start
                          || (pkt_active && !(ev_eop_normal || ev_eop_early
                                              || ev_err_input));

    // out[7:0] for out_state=010 (description.md S4 OUT PINS):
    // out[1:0] rule number, out[3:2] types checked, out[7:4] action.
    logic [7:0] packed_result;
    assign packed_result = {result_action, result_types, result_rule};

    // On the completing cycle the captured registers are only being loaded
    // at this same edge, so the live win_* drive the output; afterwards the
    // captured registers do.
    logic [7:0] result_out;
    assign result_out = capture_now ? {win_action, win_types, win_rule}
                                    : packed_result;

    // ----------------------------------------------------------------------
    // Result capture (once per packet)
    // ----------------------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            result_valid  <= 1'b0;
            result_rule   <= 2'd0;
            result_types  <= 2'd0;
            result_action <= 4'd0;
        end else if (ena) begin
            if ((rst_type == 2'b01) || ev_err_input || ev_eop_normal) begin
                result_valid <= 1'b0;
            end else if (capture_now) begin
                result_valid  <= 1'b1;
                result_rule   <= win_rule;
                result_types  <= win_types;
                result_action <= win_action;
            end
        end
    end

    // ----------------------------------------------------------------------
    // Registered outputs
    // ----------------------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            out_state <= 3'b000;
            uo_out    <= 8'h00;
        end else if (!ena) begin
            // ruling: outputs forced while ena=0; all other state holds
            out_state <= 3'b000;
            uo_out    <= 8'h00;
        end else if (rst_type == 2'b01) begin
            out_state <= 3'b000;
            uo_out    <= 8'h00;
        end else if (rst_type != 2'b00) begin
            // rst_type 10/11: no ordinary input is processed; the output
            // reflects the preserved state (owner ruling)
            if (err_in) begin
                out_state <= 3'b111;
                uo_out    <= 8'h00;
            end else if (result_valid) begin
                out_state <= 3'b010;
                uo_out    <= packed_result;
            end else if (pkt_active) begin
                out_state <= 3'b001;
                uo_out    <= 8'h00;
            end else begin
                out_state <= 3'b000;
                uo_out    <= 8'h00;
            end
        end else begin
            if (ev_err_input || err_in) begin
                out_state <= 3'b111;
                uo_out    <= 8'h00;
            end else if (ev_eop_early) begin
                out_state <= 3'b100;
                uo_out    <= 8'h00;
            end else if (ev_read) begin
                out_state <= 3'b110;
                uo_out    <= rd_data;
            end else if (ev_write_ok) begin
                out_state <= 3'b011;
                uo_out    <= wr_data;
            end else if (ev_write_err) begin
                out_state <= 3'b101;
                uo_out    <= 8'h00;
            end else if (ev_eop_normal) begin
                // 010 held "up until one cycle after receiving eop
                // (included)" -- the stage-2 output of the EOP cycle
                out_state <= 3'b010;
                uo_out    <= result_out;
            end else if (result_valid_next) begin
                out_state <= 3'b010;
                uo_out    <= result_out;
            end else if (pkt_active_next) begin
                out_state <= 3'b001;
                uo_out    <= 8'h00;
            end else begin
                out_state <= 3'b000;
                uo_out    <= 8'h00;
            end
        end
    end

endmodule
