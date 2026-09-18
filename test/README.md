# S4 verification

Activate the development-container environment first:

```sh
source /ttsetup/venv/bin/activate
```

Run these commands from the repository root:

```sh
python -m pytest test/test_model.py -q
python test/audit_random_coverage.py
make -C test/unit regress
make -C test -B
```

The default top-level suite runs **both `test` and `audit_s4`**, through public
pins only. The same default suite runs at gate level. `audit_s4` includes an
independent classification oracle, exhaustive flag validation, natural counter
wraps and the accepted 1B/2B/3A reset/enable behavior. These are required tests,
not expected failures or optional probes.

Every seed of the top-level random regression now:

1. Programs custom metadata types and all three rules from a known hard reset.
2. Streams a deliberately matching or nonmatching packet, preserving each
   type's byte order while interleaving chunks.
3. Applies 60 cycles of adversarial mixed traffic, including illegal inputs,
   configuration writes and resets. This part is intentionally unconstrained;
   only the controlled prefix promises classification coverage.
4. Uses packet reset (`rst_type=01`) to clear active/error state while retaining
   configuration and statistics, then requires all 32 READs to succeed.

`regression.py` asserts the intended classification result independently of the
cycle model, accepted setup writes, all four winners (including no hit), all
seven masks, all sixteen actions, all eight intended simultaneous-hit
combinations, and **48,000 successful sweep READs across 1,500 seeds**.
A DUT/model agreement on a blocked READ is a failure. The coverage script
checks these same assertions without running RTL; it is not a substitute for
simulation. The chip unit bench uses the same structure with 50 seeds and
500 mixed cycles per seed; exhaustive mask/action coverage is required in the
larger top-level run.

For a focused run of the accepted behavior tests:

```sh
make -C test -B COCOTB_TEST_MODULES=audit_s4 \
  COCOTB_TEST_FILTER='soft_reset_transients|type_reset_preserves|synchronous_enable'
```

For gate-level verification, use Icarus v13, set `PDK_ROOT` to the installed
IHP PDK root, and copy the netlist from the intended final hardening build to
`test/gate_level_netlist.v`. Then run:

```sh
make -C test -B GATES=yes GL_TEST=1
```

Never reuse a netlist from before an RTL change. Functional GL simulation
checks cell connectivity/behavior; sign-off timing comes from STA, not these
unannotated simulations. No tests in either top-level module require RTL
hierarchy visibility.

Simulation writes `results.xml` and, unless disabled with `WAVES=0`, `tb.fst`.
Keep these generated files out of commits. If old build directories are owned
by a different container user, correct their ownership or use a writable
copy of the test directory with the same sources; do not mistake an artifact
permission failure for a design failure.
