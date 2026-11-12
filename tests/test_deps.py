"""第三方依赖检查模块单测：不安装、不调用真实 GUI，只验证逻辑与解析。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin import deps  # noqa: E402


class DepsTests(unittest.TestCase):
    def test_python_dep_present(self):
        d = deps.python_dep()
        self.assertTrue(d["present"])
        self.assertTrue(d["required"] and d["critical"])

    def test_try_version_extracts_semver_only(self):
        from unittest.mock import patch

        class _P:
            stdout = "cua-driver 0.14.1\n"
            stderr = ""
            returncode = 0

        with patch.object(deps.subprocess, "run", return_value=_P()):
            ver = deps._try_version("/fake/cua-driver", "--version", timeout=1)
        self.assertEqual(ver, "0.14.1")  # 只取版本号，不带程序名前缀

    def test_evaluate_returns_ok_flag(self):
        cfg = {
            "desktop": {"cua_driver_bin": "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"},
            "growth": {"enabled": True, "browser_app": "auto",
                       "cua_driver_bin": "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"},
        }
        report = deps.evaluate(cfg)
        self.assertIn("ok", report)
        self.assertIn("dependencies", report)
        names = [d["name"] for d in report["dependencies"]]
        # CuaDriver 是默认必需；agent-desktop 是可选第二引擎（engine=agent 时才必需）
        self.assertEqual(names, ["Python", "CuaDriver", "agent-desktop", "浏览器（任意已装）"])
        self.assertNotIn("agent-browser", " ".join(names))
        ad = next(d for d in report["dependencies"] if d["name"] == "agent-desktop")
        self.assertFalse(ad["required"])  # auto/ax 模式下可选，不阻断
        self.assertFalse(ad["critical"])

    def test_agent_desktop_becomes_required_when_engine_agent(self):
        """显式 engine=agent 时 agent-desktop 变必需；缺失则整体 not ok。"""
        from unittest.mock import patch

        cfg = {"desktop": {"engine": "agent", "agent_desktop_bin": "/nonexistent/ad"},
               "growth": {"enabled": True,
                          "cua_driver_bin": "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"}}
        with patch.object(deps, "_which", return_value=None):
            report = deps.evaluate(cfg)
        ad = next(d for d in report["dependencies"] if d["name"] == "agent-desktop")
        self.assertTrue(ad["required"])
        self.assertFalse(ad["present"])
        self.assertIn("agent-desktop", report["required_missing"])

    def test_cua_driver_is_the_only_required_external(self):
        """CuaDriver 是唯一必需外部工具：缺它则整体 not ok。"""
        from unittest.mock import patch

        cfg = {"desktop": {"cua_driver_bin": "/nonexistent/cua-driver"},
               "growth": {"enabled": True}}
        # 屏蔽 PATH 回退（本机 cua-driver 就在 PATH 上，会误判 present）
        with patch.object(deps, "_which", return_value=None):
            report = deps.evaluate(cfg)
        cua = next(d for d in report["dependencies"] if d["name"] == "CuaDriver")
        self.assertTrue(cua["required"])
        self.assertFalse(cua["present"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["required_missing"], ["CuaDriver"])
        # 浏览器为可选信息项，缺失不阻塞
        browser = next(d for d in report["dependencies"] if "浏览器" in d["name"])
        self.assertFalse(browser["required"])

    def test_browser_dep_is_informational(self):
        d = deps.browser_dep({"growth": {"browser_app": "auto"}})
        self.assertFalse(d["required"])
        self.assertFalse(d["critical"])


if __name__ == "__main__":
    unittest.main()
