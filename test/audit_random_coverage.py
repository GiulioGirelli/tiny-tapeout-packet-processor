"""Assert the production 1500-seed differential stimulus coverage.

Run from the repository root: python test/audit_random_coverage.py
This does not simulate RTL; it measures stimulus using the existing model.
"""
import json

from model import PacketProcessorModel
from regression import build_case, RegressionCoverage


def main():
    coverage = RegressionCoverage()
    for seed in range(1500):
        model = PacketProcessorModel()
        case = build_case(seed)
        config_outputs = model.run(case.configuration)
        packet_outputs = model.run(case.packet)
        model.run(case.mixed)
        sweep_outputs = model.run(case.sweep)
        coverage.record(case, config_outputs, packet_outputs, sweep_outputs)
    coverage.assert_complete(1500)
    print(json.dumps(coverage.summary(), indent=2))


if __name__ == "__main__":
    main()
