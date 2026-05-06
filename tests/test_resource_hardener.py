"""Tests for local_ai_stack ResourceHardener compose override generation."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from local_ai_stack.resource_hardener import (
    build_override_document,
    compute_resources,
    ensure_compose_resource_override,
    render_override_yaml,
)


class ResourceHardenerTests(unittest.TestCase):
    def test_compute_resources_scales_with_ram(self) -> None:
        lo = compute_resources(4096, 4)
        hi = compute_resources(65536, 32)
        self.assertLess(lo[0].mem_limit_mib, hi[0].mem_limit_mib)
        self.assertLess(lo[1].mem_limit_mib, hi[1].mem_limit_mib)

    def test_override_yaml_mem_limit_and_cpu_reservations(self) -> None:
        searx, n8n = compute_resources(32768, 12)
        doc = build_override_document(searx, n8n)
        self.assertIn("mem_limit", doc["services"]["searxng"])
        self.assertIn("mem_limit", doc["services"]["n8n"])
        res_s = doc["services"]["searxng"]["deploy"]["resources"]["reservations"]
        res_n = doc["services"]["n8n"]["deploy"]["resources"]["reservations"]
        self.assertIn("cpus", res_s)
        self.assertIn("memory", res_s)
        self.assertIn("cpus", res_n)
        self.assertIn("memory", res_n)
        lim_s = doc["services"]["searxng"]["deploy"]["resources"]["limits"]
        self.assertIn("cpus", lim_s)
        text = render_override_yaml(doc, ram_mib=32768, logical_cpus=12)
        body = text[text.find("services:") :]
        parsed = yaml.safe_load(body)
        self.assertTrue(str(parsed["services"]["searxng"]["mem_limit"]).endswith("m"))
        self.assertIn("searxng", parsed["services"])

    def test_ensure_respects_disable_flag(self) -> None:
        with mock.patch.dict(os.environ, {"OCTO_RESOURCE_HARDENER": "0"}):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                p = ensure_compose_resource_override(root)
                self.assertIsNone(p)
                out = root / "deploy" / "local-ai" / "docker-compose.override.yaml"
                self.assertFalse(out.is_file())

    def test_ensure_writes_file(self) -> None:
        with mock.patch.dict(os.environ, {"OCTO_RESOURCE_HARDENER": "1"}, clear=False):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                p = ensure_compose_resource_override(root)
                self.assertIsNotNone(p)
                assert p is not None
                self.assertTrue(p.is_file())
                self.assertIn("searxng", p.read_text(encoding="utf-8"))
