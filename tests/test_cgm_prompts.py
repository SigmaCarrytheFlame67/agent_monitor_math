"""Bounds, provenance and tool-freedom of the CGM prompt pack."""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor.cgm import prompts
from agent_monitor.runners import math_harness_runner as runner

ROOT = Path(__file__).resolve().parents[1]
CGM_DIR = ROOT / "agent_monitor" / "cgm"
ARCHIVE_SHA256 = "d12630af90d13688a950c8c8ea14c2cf5ac6a52afbc4e69d4c876dc3944cb825"


class PromptPackBoundsTests(unittest.TestCase):
    def test_every_system_prompt_fits_the_runner_bound(self) -> None:
        # The runner rejects any system prompt above this bound before a call.
        self.assertEqual(runner.MAX_SYSTEM_PROMPT_BYTES, 12_000)
        pack = prompts.all_system_prompts()
        self.assertEqual(
            set(pack),
            {"strategist", "verifier", "curator"}
            | {f"worker:{name}" for name in prompts.TECHNIQUE_PROCEDURES},
        )
        for role, text in pack.items():
            with self.subTest(role=role):
                self.assertLessEqual(len(text.encode("utf-8")), 12_000)
                self.assertGreater(len(text), 1_000)

    def test_worker_prompt_covers_every_plan_technique(self) -> None:
        self.assertEqual(set(prompts.TECHNIQUE_PROCEDURES), set(prompts.PLAN_TECHNIQUES))
        for technique in prompts.PLAN_TECHNIQUES:
            with self.subTest(technique=technique):
                text = prompts.worker_system(technique)
                self.assertTrue(text.startswith(prompts.WORKER_SYSTEM))
                self.assertIn(prompts.TECHNIQUE_PROCEDURES[technique], text)
        self.assertEqual(prompts.worker_system("unknown"), prompts.worker_system("plain"))

    def test_prompts_name_no_tools_files_or_network(self) -> None:
        forbidden = (
            "gm_add", "gm_search", "gm_get", "fact_submit", "fact_search",
            "fact_neighbors", "graph_link", "graph_frontier", "graph_annotate",
            "search_arxiv", "TASK.md", "local_memory", "download", "mcp",
            "web search", "websearch", "webfetch", "curl", "wget", "$verify",
            "$direct", "$construct", "fact_graph/", "results/", "problem.md",
        )
        for role, text in prompts.all_system_prompts().items():
            lowered = text.lower()
            for token in forbidden:
                with self.subTest(role=role, token=token):
                    self.assertNotIn(token.lower(), lowered)
        self.assertNotIn("tool", prompts.FORMAT_REMINDER.replace("no tool calls", ""))

    def test_verbatim_upstream_contracts_are_embedded(self) -> None:
        self.assertTrue(
            prompts.STRATEGIST_SYSTEM.startswith(
                "You are a senior research-mathematics strategy advisor, consulted by an "
                "automated proof-search orchestrator."
            )
        )
        self.assertIn("recorded verbatim as the swarm's master_guidance.", prompts.STRATEGIST_SYSTEM)
        contract = (
            "{\n"
            '  "verification_report": {\n'
            '    "summary": "string",\n'
            '    "critical_errors": [\n'
            '      {"location": "string", "issue": "string"}\n'
            "    ],\n"
            '    "gaps": [\n'
            '      {"location": "string", "issue": "string"}\n'
            "    ]\n"
            "  },\n"
            '  "verdict": "correct",\n'
            '  "repair_hints": ""\n'
            "}"
        )
        self.assertIn(contract, prompts.VERIFIER_SYSTEM)
        for rule in (
            'Return `"correct"` if and only if both `critical_errors` and `gaps` are empty.',
            "Hard Prohibition P1",
            "Hard Prohibition P3",
            "Hard Prohibition P5",
            "Hard Prohibition P6",
            "P3 (chain)",
            "Never claim that you checked a",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, prompts.VERIFIER_SYSTEM)
        self.assertNotIn("search_arxiv_theorems", prompts.VERIFIER_SYSTEM)
        for marker in ("=== LEMMA L1 ===", "=== THEOREM ===", "=== STATUS ===", "=== END ==="):
            self.assertIn(marker, prompts.WORKER_SYSTEM)

    def test_prompt_pack_hash_is_stable_and_sensitive(self) -> None:
        first = prompts.prompt_pack_sha256()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(first, prompts.prompt_pack_sha256())
        with patch.object(prompts, "VERIFIER_SYSTEM", prompts.VERIFIER_SYSTEM + " "):
            self.assertNotEqual(first, prompts.prompt_pack_sha256())
        pack = {
            "version": prompts.PROMPT_PACK_VERSION,
            "prompts": prompts.all_system_prompts(),
            "format_reminder": prompts.FORMAT_REMINDER,
        }
        self.assertEqual(
            first,
            hashlib.sha256(
                json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )


class PromptLicensingTests(unittest.TestCase):
    def test_apache_license_copy_and_change_notices_ship_with_the_prompts(self) -> None:
        license_text = (CGM_DIR / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Apache License", license_text)
        self.assertIn("Version 2.0, January 2004", license_text)
        self.assertIn("Copyright 2026 FrenzyMath", license_text)
        notice = (CGM_DIR / "NOTICE.md").read_text(encoding="utf-8")
        self.assertIn(ARCHIVE_SHA256, notice)
        self.assertIn("Changes", notice)
        module_doc = prompts.__doc__ or ""
        self.assertIn("ADAPTED WORK NOTICE", module_doc)
        self.assertIn(ARCHIVE_SHA256, module_doc)
        self.assertIn("Copyright 2026 FrenzyMath", module_doc)
        source = Path(prompts.__file__).read_text(encoding="utf-8")
        self.assertGreaterEqual(len(re.findall(r"Adapted from", source)), 4)

    def test_attribution_lists_the_archive_license_and_changes(self) -> None:
        attribution = (ROOT / "ATTRIBUTION.md").read_text(encoding="utf-8")
        self.assertIn("continual-graph-memory-main.zip", attribution)
        self.assertIn(ARCHIVE_SHA256, attribution)
        self.assertIn("Apache-2.0, Copyright 2026 FrenzyMath", attribution)
        self.assertIn("agent_monitor/cgm/LICENSE", attribution)
        self.assertIn("results/*", attribution)

    def test_license_assets_are_packaged(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"cgm/LICENSE"', pyproject)
        self.assertIn('"cgm/NOTICE.md"', pyproject)


if __name__ == "__main__":
    unittest.main()
