"""第三方依赖检查模块单测：不安装、不调用真实 GUI，只验证逻辑与解析。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin import deps  # noqa: E402
from buddy_checkin.adapters import growth_browser  # noqa: E402

BASE_CFG = {
    "desktop": {},
    "growth": {"enabled": True, "cdp_port": 9222, "agent_browser_bin": ""},
}


def _offline_targets(self) -> bool:
    """让 CDP 探测在单测里恒定返回不可达，避免依赖本机客户端状态。"""
    return False


class DepsTests(unittest.TestCase):
    def setUp(self):
        self._patches = [
            patch("buddy_checkin.ports.client_version", return_value="5.11.3"),
            patch("buddy_checkin.ports.client_app", return_value=Path("/Applications/WorkBuddy.app")),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_python_dep_present(self):
        d = deps.python_dep()
        self.assertTrue(d["present"])
        self.assertTrue(d["required"] and d["critical"])

    def test_try_version_extracts_semver_only(self):
        class _P:
            stdout = "agent-browser 0.42.0\n"
            stderr = ""
            returncode = 0

        with patch.object(deps.subprocess, "run", return_value=_P()):
            ver = deps._try_version("/fake/agent-browser", "--version", timeout=1)
        self.assertEqual(ver, "0.42.0")  # 只取版本号，不带程序名前缀

    def test_dependency_names_reflect_cdp_chain(self):
        """依赖清单必须体现 CDP 链路，且不再包含已移除的 CuaDriver。"""
        with patch.object(growth_browser.GrowthBrowser, "available", _offline_targets):
            report = deps.evaluate(BASE_CFG)
        names = [d["name"] for d in report["dependencies"]]
        self.assertEqual(
            names,
            ["WorkBuddy 客户端", "Python", "node", "agent-browser", "WorkBuddy CDP", "agent-desktop"],
        )
        self.assertNotIn("CuaDriver", names)
        self.assertNotIn("浏览器（任意已装）", names)

    def test_old_client_blocks(self):
        """5.9.x 是网页版活动中心：必须判红，并给出升级方式（含官方直链）。"""
        with patch("buddy_checkin.ports.client_version", return_value="5.9.3.42398134-d4692421"), \
             patch.object(growth_browser.GrowthBrowser, "available", _offline_targets):
            report = deps.evaluate(BASE_CFG)
        client = next(d for d in report["dependencies"] if d["name"] == "WorkBuddy 客户端")
        self.assertFalse(client["present"])
        self.assertIn("5.11.0", client["install"])
        self.assertIn("download.codebuddy.cn", client["install"])
        self.assertIn("WorkBuddy 客户端", report["required_missing"])
        self.assertFalse(report["ok"])

    def test_old_node_blocks(self):
        """node ≥22 是硬要求（全局 WebSocket），20 上要判缺失。"""
        with patch.object(deps, "_try_version", return_value="20.11.1"):
            d = deps.node_dep()
        self.assertFalse(d["present"])
        self.assertIn("22", d["install"])

    def test_new_node_passes(self):
        with patch.object(deps, "_try_version", return_value="22.22.2"):
            d = deps.node_dep()
        self.assertTrue(d["present"])

    def test_agent_browser_and_cdp_are_required(self):
        with patch.object(growth_browser.GrowthBrowser, "available", _offline_targets), \
             patch.object(deps, "_which", return_value=None), \
             patch.object(growth_browser.GrowthBrowser, "_resolve_agent_browser", return_value=None):
            report = deps.evaluate(BASE_CFG)
        ab = next(d for d in report["dependencies"] if d["name"] == "agent-browser")
        cdp = next(d for d in report["dependencies"] if d["name"] == "WorkBuddy CDP")
        self.assertTrue(ab["required"] and ab["critical"])
        self.assertFalse(ab["present"])
        self.assertTrue(cdp["required"] and cdp["critical"])
        self.assertFalse(cdp["present"])
        self.assertFalse(report["ok"])
        self.assertIn("agent-browser", report["required_missing"])
        self.assertIn("WorkBuddy CDP", report["required_missing"])

    def test_cdp_present_when_port_alive(self):
        with patch.object(growth_browser.GrowthBrowser, "available", return_value=True):
            report = deps.evaluate(BASE_CFG)
        cdp = next(d for d in report["dependencies"] if d["name"] == "WorkBuddy CDP")
        self.assertTrue(cdp["present"])

    def test_agent_desktop_stays_optional(self):
        """agent-desktop 只作 AX 兜底，任何配置下都不阻断主流程。"""
        with patch.object(growth_browser.GrowthBrowser, "available", return_value=True), \
             patch.object(deps, "_which", return_value=None):
            report = deps.evaluate({"desktop": {"engine": "agent"}, "growth": {"enabled": True}})
        ad = next(d for d in report["dependencies"] if d["name"] == "agent-desktop")
        self.assertFalse(ad["required"])
        self.assertFalse(ad["critical"])
        self.assertNotIn("agent-desktop", report["required_missing"])


if __name__ == "__main__":
    unittest.main()
