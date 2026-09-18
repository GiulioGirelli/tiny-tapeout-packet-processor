// SPDX-FileCopyrightText: © 2026 Giulio Girelli
// SPDX-License-Identifier: Apache-2.0
//
// Tiny Tapeout wrapper for the configurable match-action packet processor
// (description.md I/O PINS). Thin shell around pp_chip: the whole design
// (input FSMs, collection table, rule table, match engine, statistics,
// output stage) lives in pp_chip and its submodules; this file only maps
// the standard Tiny Tapeout pins and drives the constant uio_oe. Safe-idle
// behaviour (ena=0 -> out_state=000 / out=0x00, inputs ignored) and the
// asynchronous hard reset are implemented inside pp_chip, not duplicated
// here.

`default_nettype none

module tt_um_GiulioGirelli_packet_processor (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  // uio[0]   = cfg_mode           --> INPUT
  // uio[2:1] = packet_status[1:0] --> INPUT
  // uio[4:3] = rst_type[1:0]      --> INPUT
  // uio[7:5] = out_state[2:0]     --> OUTPUT
  wire [2:0] out_state;

  pp_chip chip (
      .clk           (clk),
      .rst_n         (rst_n),
      .ena           (ena),
      .ui_in         (ui_in),
      .cfg_mode      (uio_in[0]),
      .packet_status (uio_in[2:1]),
      .rst_type      (uio_in[4:3]),
      .uo_out        (uo_out),
      .out_state     (out_state)
  );

  assign uio_out = {out_state, 5'b00000};
  assign uio_oe  = 8'b1110_0000;  // only out_state[2:0] = uio[7:5] is output

  // uio_in[7:5] are outputs from the chip's perspective; their input path
  // is unused.
  wire _unused = &{1'b0, uio_in[7:5]};

endmodule
