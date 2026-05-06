import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "overlays"
    / "agenticseek"
    / "sources"
    / "architecture_map.py"
)
SPEC = importlib.util.spec_from_file_location("architecture_map", MODULE_PATH)
arch = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(arch)


class ArchitectureMapTests(unittest.TestCase):
    def test_python_absolute_import_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            (root / "app" / "__init__.py").write_text("", encoding="utf-8")
            (root / "app" / "util.py").write_text("X = 1\n", encoding="utf-8")
            (root / "app" / "main.py").write_text("import app.util\n", encoding="utf-8")
            edges = arch.build_dependency_edges(root)
            self.assertTrue(any(b.endswith("app/util") or b == "app/util" for _, b in edges))

    def test_mermaid_contains_flowchart(self):
        diagram = arch.edges_to_mermaid_flowchart({("app/a", "app/b")})
        self.assertIn("flowchart LR", diagram)
        self.assertIn("-->", diagram)


if __name__ == "__main__":
    unittest.main()
