// SPDX-License-Identifier: Apache-2.0
`default_nettype none

// 3x16-bit collection table (description.md S4 COMPONENTS / Collection
// table).
//
// A metadata byte selects the slot whose type register equals it (the
// selection is registered together with a matched flag; a non-matching
// metadata clears the flag so following data bytes are dropped until the
// next metadata). Data bytes append MSB-first into the selected slot
// (1st byte -> bits [15:8], 2nd -> [7:0]); a slot holding 2 bytes never
// overflows -- further bytes are dropped. Partial, interleaved fills and
// repeated metadata are legal. `clear` empties slots, counts and the
// selection (driven for rst_type=01 and at packet end, incl. the early-EOP
// 100 error per the owner ruling, CLARIFICATIONS).
//
// collected_next / full_next expose the table's EFFECTIVE next state:
// current contents with the in-flight data byte applied to the selected
// matched slot (if it holds <2 bytes); `clear` has priority and yields the
// emptied table (full_next=0). The match engine samples these
// combinationally so its result is valid at stage 2 of the very cycle
// whose final byte completes the table (description.md TIMING).
module pp_collection (
    input  logic         clk,
    input  logic         rst_n,
    input  logic         clear,          // sync clear: rst_type=01 OR packet end
    input  logic         meta_valid,     // accepted metadata byte this cycle
    input  logic  [7:0]  meta_byte,
    input  logic         data_valid,     // accepted data byte this cycle
    input  logic  [7:0]  data_byte,
    input  logic  [23:0] type_values,    // type_values[8*i +: 8] = type reg i
    output logic [47:0]  collected,      // collected[16*i +: 16] = slot i
    output logic [47:0]  collected_next, // slots with in-flight byte applied
    output logic         full,           // all 3 slots hold 2 bytes (comb)
    output logic         full_next       // full computed from post-byte counts
);

    // Per-slot data register and fill count (0..2).
    logic [15:0] slot_data [3];
    logic [1:0]  slot_cnt  [3];

    // Registered metadata selection.
    logic [1:0] sel_slot;
    logic       sel_matched;

    // Combinational match of the current metadata byte against the 3 type
    // registers (type values are unique, so priority here is don't-care).
    logic [1:0] match_slot;
    logic       match_found;

    always_comb begin
        match_found = 1'b0;
        match_slot  = 2'd0;
        if (meta_byte == type_values[7:0]) begin
            match_found = 1'b1;
            match_slot  = 2'd0;
        end else if (meta_byte == type_values[15:8]) begin
            match_found = 1'b1;
            match_slot  = 2'd1;
        end else if (meta_byte == type_values[23:16]) begin
            match_found = 1'b1;
            match_slot  = 2'd2;
        end
    end

    // Effective next state of the slots/counts (single source for both the
    // registers and the next-state outputs): the in-flight data byte is
    // applied to the selected matched slot unless it already holds 2 bytes
    // (full slots do not overflow or replace, they just stop filling);
    // `clear` has priority and empties everything.
    logic [15:0] slot_next [3];
    logic [1:0]  cnt_next  [3];

    always_comb begin
        for (int i = 0; i < 3; i++) begin
            slot_next[i] = slot_data[i];
            cnt_next[i]  = slot_cnt[i];
        end
        if (clear) begin
            for (int i = 0; i < 3; i++) begin
                slot_next[i] = 16'h0;
                cnt_next[i]  = 2'd0;
            end
        end else if (data_valid && sel_matched
                     && (slot_cnt[sel_slot] != 2'd2)) begin
            slot_next[sel_slot] = {slot_data[sel_slot][7:0], data_byte};
            cnt_next[sel_slot]  = slot_cnt[sel_slot] + 2'd1;
        end
    end

    // Full when every slot holds 2 bytes (description.md: "It flags when it
    // is completely full").
    assign full = (slot_cnt[0] == 2'd2) && (slot_cnt[1] == 2'd2)
               && (slot_cnt[2] == 2'd2);

    assign full_next = (cnt_next[0] == 2'd2) && (cnt_next[1] == 2'd2)
                    && (cnt_next[2] == 2'd2);

    for (genvar g = 0; g < 3; g++) begin : gen_collected
        assign collected[16*g +: 16]      = slot_data[g];
        assign collected_next[16*g +: 16] = slot_next[g];
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (int i = 0; i < 3; i++) begin
                slot_data[i] <= 16'h0;
                slot_cnt[i]  <= 2'd0;
            end
            sel_slot    <= 2'd0;
            sel_matched <= 1'b0;
        end else begin
            for (int i = 0; i < 3; i++) begin
                slot_data[i] <= slot_next[i];
                slot_cnt[i]  <= cnt_next[i];
            end
            if (clear) begin
                sel_slot    <= 2'd0;
                sel_matched <= 1'b0;
            end else if (meta_valid) begin
                sel_slot    <= match_slot;
                sel_matched <= match_found;
            end
        end
    end

endmodule
