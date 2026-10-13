"""Unit tests for the streaming scrubber. Needs mitmproxy importable:
python -m unittest discover -s gateway/egress -p 'test_*.py'"""

import unittest

from credential_swap import stream_scrubber

SECRET, MASK = b"sk-real-secret-1234", b"sk-pac-placeholder"


def run(chunks, pairs=((SECRET, MASK, "test"),)):
    hits = []
    scrub = stream_scrubber(list(pairs), hits.append)
    out = [scrub(c) for c in chunks] + [scrub(b"")]
    return out, hits


class StreamScrubber(unittest.TestCase):
    def test_passes_chunks_through_without_delay(self):
        out, hits = run([b"data: {\"a\": 1}\n\n", b"data: {\"b\": 2}\n\n"])
        self.assertEqual(out, [b"data: {\"a\": 1}\n\n", b"data: {\"b\": 2}\n\n", b""])
        self.assertEqual(hits, [])

    def test_secret_inside_one_chunk(self):
        out, hits = run([b"x " + SECRET + b" y"])
        self.assertEqual(b"".join(out), b"x " + MASK + b" y")
        self.assertEqual(hits, ["test"])

    def test_secret_split_across_chunks(self):
        out, hits = run([b"token=sk-real-", b"secret-1234&more"])
        self.assertEqual(out[0], b"token=")  # the possible start of the secret is held back
        self.assertEqual(b"".join(out), b"token=" + MASK + b"&more")
        self.assertEqual(hits, ["test"])

    def test_held_back_tail_is_flushed_at_the_end(self):
        out, _ = run([b"ends with sk-re"])
        self.assertEqual(b"".join(out), b"ends with sk-re")


if __name__ == "__main__":
    unittest.main()
