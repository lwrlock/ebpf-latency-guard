import unittest
from elg.analyzer import bucket_histogram_from_samples, classify_verdict, compute_percentiles


class LatencyGuardTests(unittest.TestCase):
    def test_verdict_classification(self):
        self.assertEqual(classify_verdict(1.2, 0.5, 0.2, 0.0, 500), "HEALTHY")
        self.assertEqual(classify_verdict(24.5, 1.0, 0.2, 0.0, 500), "CPU_RUNQ_CONTENTION")
        self.assertEqual(classify_verdict(1.2, 0.5, 6.0, 0.0, 500), "NVME_IO_STALL")
        self.assertEqual(classify_verdict(1.2, 0.5, 0.2, 3.5, 500), "MEMORY_RECLAIM_STALL")
        self.assertEqual(classify_verdict(1.2, 0.5, 0.2, 0.0, 15000), "IRQ_STORM")

    def test_histogram_and_percentiles(self):
        delays = [0.2, 0.8, 1.4, 3.2, 7.5, 22.0]
        hist = dict(bucket_histogram_from_samples(delays))
        self.assertEqual(hist["<0.5ms"], 1)
        self.assertEqual(hist["0.5-2ms"], 2)
        self.assertEqual(hist["15ms+"], 1)
        pcts = compute_percentiles(delays)
        self.assertGreaterEqual(pcts["p99"], pcts["p95"])
        self.assertGreaterEqual(pcts["p95"], pcts["p50"])


if __name__ == "__main__":
    unittest.main()
