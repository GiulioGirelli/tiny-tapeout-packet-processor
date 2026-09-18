// SPDX-License-Identifier: Apache-2.0
`default_nettype none

// Statistics counters (description.md S4 STATISTICS / MAP DETAILS): 8
// counters, no error-occurrences counter.
//
//   total_bytes[15:0]    +1 per accepted metadata/data byte in packet mode
//                        (never EOP, never config-mode traffic)
//   total_packets[15:0]  +1 when a packet starts (first metadata accepted),
//                        even if the packet later errors
//   rule_hits[3]x[7:0]   winning rule's counter, +1 at EOP (hit_eop strobe)
//   no_rule_hits[7:0]    +1 at EOP when no rule hit
//
// All counters wrap modulo their width. They are read-only for the config
// WRITE path and are cleared only by stats_reset (rst_type=11) or the hard
// reset. Each strobe counts independently, so simultaneous strobes compose
// (e.g. byte_accepted + packet_started on a first-metadata cycle; the
// control logic never raises hit_eop for an errored packet -- an error
// suppresses the hit count by withholding hit_eop).
module pp_stats (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        stats_reset,    // sync: rst_type=11 -> all counters 0
    input  logic        byte_accepted,  // metadata or data byte accepted
    input  logic        packet_started, // first metadata of a packet accepted
    input  logic        hit_eop,        // EOP of a cleanly matched packet
    input  logic  [1:0] hit_rule,       // winning rule 1..3, 0 = no hit
    input  logic  [4:0] rd_addr,
    output logic  [7:0] rd_data         // combinational; 0 outside 0x18..0x1F
);

    logic [15:0] total_bytes;
    logic [15:0] total_packets;
    logic [7:0]  rule_hits [3];
    logic [7:0]  no_rule_hits;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            total_bytes   <= 16'h0000;
            total_packets <= 16'h0000;
            for (int i = 0; i < 3; i++) rule_hits[i] <= 8'h00;
            no_rule_hits  <= 8'h00;
        end else if (stats_reset) begin
            total_bytes   <= 16'h0000;
            total_packets <= 16'h0000;
            for (int i = 0; i < 3; i++) rule_hits[i] <= 8'h00;
            no_rule_hits  <= 8'h00;
        end else begin
            if (byte_accepted)  total_bytes   <= total_bytes + 16'd1;
            if (packet_started) total_packets <= total_packets + 16'd1;
            if (hit_eop) begin
                case (hit_rule)
                    2'd1:     rule_hits[0] <= rule_hits[0] + 8'd1;
                    2'd2:     rule_hits[1] <= rule_hits[1] + 8'd1;
                    2'd3:     rule_hits[2] <= rule_hits[2] + 8'd1;
                    default:  no_rule_hits <= no_rule_hits + 8'd1;
                endcase
            end
        end
    end

    // Read map (description.md S4 MAP DETAILS); 0 outside the statistics
    // region.
    always_comb begin
        case (rd_addr)
            5'h18:   rd_data = total_bytes[15:8];
            5'h19:   rd_data = total_bytes[7:0];
            5'h1A:   rd_data = total_packets[15:8];
            5'h1B:   rd_data = total_packets[7:0];
            5'h1C:   rd_data = rule_hits[0];
            5'h1D:   rd_data = rule_hits[1];
            5'h1E:   rd_data = rule_hits[2];
            5'h1F:   rd_data = no_rule_hits;
            default: rd_data = 8'h00;
        endcase
    end

endmodule
