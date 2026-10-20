"""Regression tests for the CAN pump mapping (do not import the live server)."""

import ast
import unittest
from pathlib import Path


def read_direct_states():
    """Read the declarative map without starting the CAN workers or HTTP API."""
    source = Path(__file__).resolve().parents[1] / "wps_can_bridge.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    for stmt in module.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DIRECT_STATES"
            for target in stmt.targets
        ):
            return ast.literal_eval(stmt.value)
    raise AssertionError("DIRECT_STATES assignment not found")


class PumpMappingTests(unittest.TestCase):
    def test_external_g1_heating_circuit(self):
        self.assertEqual(
            read_direct_states()[0x00028270], "heating_circuit_pump_on"
        )

    def test_internal_g2_heat_carrier(self):
        self.assertEqual(
            read_direct_states()[0x0002C270], "heating_carrier_pump_on"
        )

    def test_both_state_mappings_remain_distinct(self):
        states = read_direct_states()
        self.assertNotEqual(states[0x00028270], states[0x0002C270])
        self.assertEqual(states[0x00048270], "compressor_on")


if __name__ == "__main__":
    unittest.main()
