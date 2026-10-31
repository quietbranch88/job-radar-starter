"""Portable checks run by both unittest and pytest; all fixtures are isolated."""

import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("operator_core_checker", ROOT / "scripts" / "check_operator_core.py")
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class OperatorCoreParityTests(unittest.TestCase):
    def clone(self, root, layout):
        source = CHECKER._core(ROOT)
        target = root / layout
        target.mkdir(parents=True)
        for name in CHECKER.MODULES:
            shutil.copyfile(source / name, target / name)
        (root / "contracts").mkdir()
        shutil.copyfile(ROOT / "contracts" / "operator-core.json",
                        root / "contracts" / "operator-core.json")
        return target

    def test_current_modules_match_reviewed_contract(self):
        report = CHECKER.check(ROOT)
        self.assertTrue(report["ok"], report)
        self.assertFalse(report["peer_checked"])

    def test_peer_layout_matches_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            peer = Path(directory) / "peer"
            self.clone(peer, Path("job-scan") / "operator_state")
            report = CHECKER.check(ROOT, peer)
            self.assertTrue(report["ok"], report)
            self.assertTrue(report["peer_checked"])
            self.assertTrue(report["modules"]["local_state.py"]["matches_peer"])

    def test_local_state_mutation_is_detected_without_changing_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            peer = Path(directory) / "peer"
            target = self.clone(peer, Path("job-scan") / "operator_state")
            path = target / "local_state.py"
            original = path.read_text(encoding="utf-8")
            guard = "local state contains duplicate fields"
            self.assertIn(guard, original)
            self.assertTrue(CHECKER.check(ROOT, peer)["ok"])
            path.write_text(original.replace(guard, "local state contains repeated fields", 1), encoding="utf-8")
            report = CHECKER.check(ROOT, peer)
            self.assertFalse(report["ok"])
            self.assertFalse(report["modules"]["local_state.py"]["matches_peer"])
            path.write_text(original, encoding="utf-8")
            self.assertTrue(CHECKER.check(ROOT, peer)["ok"])


if __name__ == "__main__":
    unittest.main()
