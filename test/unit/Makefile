# Makefile — per-module cocotb unit benches (Icarus), mirrors ../Makefile.
# Usage: make -B DUT=pp_collection | pp_rules | pp_stats
#        make regress          # runs all three benches

# defaults
SIM ?= icarus
FST ?= -fst # Use more efficient FST format
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../../src

DUT ?= pp_collection

# Per-DUT source lists (pp_core bundles the whole datapath).
SRCS_pp_collection = $(SRC_DIR)/pp_collection.sv
SRCS_pp_rules      = $(SRC_DIR)/pp_rules.sv
SRCS_pp_stats      = $(SRC_DIR)/pp_stats.sv
SRCS_pp_match      = $(SRC_DIR)/pp_match.sv
SRCS_pp_core       = $(SRC_DIR)/pp_core.sv $(SRC_DIR)/pp_collection.sv \
                     $(SRC_DIR)/pp_rules.sv $(SRC_DIR)/pp_match.sv
SRCS_pp_chip       = $(SRC_DIR)/pp_chip.sv $(SRC_DIR)/pp_input.sv \
                     $(SRC_DIR)/pp_output.sv $(SRC_DIR)/pp_core.sv \
                     $(SRC_DIR)/pp_collection.sv $(SRC_DIR)/pp_rules.sv \
                     $(SRC_DIR)/pp_match.sv $(SRC_DIR)/pp_stats.sv

SIM_BUILD = sim_build/$(DUT)
VERILOG_SOURCES = $(SRCS_$(DUT))

# SystemVerilog-2012 for always_ff/always_comb/logic
COMPILE_ARGS += -g2012

TOPLEVEL = $(DUT)

# List test modules to run, separated by commas and without the .py suffix:
COCOTB_TEST_MODULES = test_$(DUT)

# include cocotb's make rules to take care of the simulator setup
include $(shell cocotb-config --makefiles)/Makefile.sim

DUTS = pp_collection pp_rules pp_stats pp_match pp_core pp_chip

regress:
	@for dut in $(DUTS); do \
		echo "=== unit bench: $$dut ==="; \
		$(MAKE) -B DUT=$$dut || exit 1; \
	done

.PHONY: regress
