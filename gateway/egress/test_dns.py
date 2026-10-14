"""Unit tests for the DNS limits. Needs mitmproxy importable (see test_stream.py)."""

import unittest

from credential_swap import DnsLimiter


class Limits(unittest.TestCase):
    def test_long_label_is_refused(self):
        lim = DnsLimiter()
        self.assertIsNone(lim.check("10.0.0.5", "www.example.com", 40, 0, 0))
        self.assertIn("longer than 40", lim.check("10.0.0.5", "a" * 41 + ".evil.example", 40, 0, 0))

    def test_distinct_names_per_minute_per_client(self):
        lim = DnsLimiter()
        for i in range(3):
            self.assertIsNone(lim.check("10.0.0.5", f"n{i}.example.com", 0, 3, 100))
        self.assertIsNone(lim.check("10.0.0.5", "n0.example.com", 0, 3, 110))      # a repeat is fine
        self.assertIsNotNone(lim.check("10.0.0.5", "n3.example.com", 0, 3, 110))   # a fourth new one isn't
        self.assertIsNone(lim.check("10.0.0.6", "n3.example.com", 0, 3, 110))      # other clients unaffected
        self.assertIsNone(lim.check("10.0.0.5", "n3.example.com", 0, 3, 171))      # a minute later it is

    def test_no_limits(self):
        self.assertIsNone(DnsLimiter().check("10.0.0.5", "x" * 60 + ".example.com", 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
