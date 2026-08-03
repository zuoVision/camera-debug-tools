import re
import unittest

from camera_debug import apply_parser_transforms


class BitParserTests(unittest.TestCase):
    def test_extracts_bit_from_hex_value(self):
        self.assertEqual(apply_parser_transforms("0xdb", {"bit": 3}), 1)
        self.assertEqual(apply_parser_transforms("0xd3", {"bit": 3}), 0)

    def test_extracts_bit_from_decimal_value(self):
        self.assertEqual(apply_parser_transforms("219", {"bit": 3}), 1)
        self.assertEqual(apply_parser_transforms("003", {"bit": 1}), 1)
        self.assertEqual(apply_parser_transforms(219.0, {"bit": 3}), 1)

    def test_rejects_invalid_bit_index(self):
        for bit in (-1, 64, True, "3"):
            with self.subTest(bit=bit), self.assertRaises(ValueError):
                apply_parser_transforms("0xdb", {"bit": bit})

    def test_real_bmc_output(self):
        parser = {
            "pattern": r"\[0x[0-9a-fA-F]+\]\s*=\s*(0x[0-9a-fA-F]+)",
            "group": 1,
            "bit": 3,
            "map": {"0": "LOST", "1": "LOCKED"},
        }
        cases = (
            ("[csi_i2c_ops_unit_test:297] INFO:   [0x1a] = 0xdb", 3, "LOCKED"),
            ("[csi_i2c_ops_unit_test:297] INFO:   [0x1dc] = 0x81", 0, "LOCKED"),
        )
        for output, bit, expected in cases:
            with self.subTest(output=output):
                parser["bit"] = bit
                match = re.search(parser["pattern"], output)
                self.assertIsNotNone(match)
                value = apply_parser_transforms(match.group(parser["group"]), parser)
                self.assertEqual(parser["map"][str(value)], expected)


if __name__ == "__main__":
    unittest.main()
