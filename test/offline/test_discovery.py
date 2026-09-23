import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('discovery', ROOT / 'files/host/discover.py')
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)


class DiscoveryTests(unittest.TestCase):
    def test_nvidia_domain_width(self):
        self.assertEqual(discovery.normalize_bdf('00000000:AB:00.0'), '0000:ab:00.0')
        self.assertEqual(discovery.normalize_bdf('0000:ab:00.0'), '0000:ab:00.0')

    def test_reject_bad_address(self):
        for bad in ['00:00.0', '../device', '0000:01:00.9', '0000:01:00.0junk']:
            with self.assertRaises(ValueError):
                discovery.normalize_bdf(bad)

    def test_incomplete_gpu_inventory_is_not_success(self):
        with patch.object(discovery, 'command', return_value=''), patch.object(discovery, 'PCI', Path('/nonexistent')):
            with self.assertRaisesRegex(RuntimeError, 'eight host-owned'):
                discovery.inventory()
