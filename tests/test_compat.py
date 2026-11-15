"""客户端兼容性红线：版本解析、放行判定、升级指引里的硬事实。

这些用例守住的是「小白照文档做能不能一次成功」：
  - 4 段构建号 / v 前缀 / 预览版后缀都要能正确解析
  - 5.9.x（网页版活动中心）必须被拦，5.11+ 必须放行
  - 读不到版本不能误判成「过期」（否则装在自定义路径的机器被冤枉）
  - 升级指引里必须同时给出官方入口和可用直链
"""
from __future__ import annotations

import platform
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin import compat  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_plain_semver(self):
        self.assertEqual(compat.parse("5.11.3"), (5, 11, 3))

    def test_four_segment_build(self):
        """客户端真实版本串带构建号+哈希：5.11.3.43806934-ecace0f7。"""
        self.assertEqual(compat.parse("5.11.3.43806934-ecace0f7"), (5, 11, 3))
        self.assertEqual(compat.build_no("5.11.3.43806934-ecace0f7"), "43806934")

    def test_v_prefix_and_two_segments(self):
        self.assertEqual(compat.parse("v5.12"), (5, 12, 0))

    def test_preview_suffix(self):
        self.assertEqual(compat.parse("5.12.0-preview.1"), (5, 12, 0))
        self.assertEqual(compat.channel("5.12.0-preview.1"), "preview")
        self.assertEqual(compat.channel("5.11.3"), "stable")

    def test_garbage_is_none(self):
        for raw in (None, "", "unknown", "   ", "WorkBuddy"):
            self.assertIsNone(compat.parse(raw), raw)


class CheckTests(unittest.TestCase):
    def test_5093_is_blocked(self):
        """5.9.x 是网页版活动中心，必须拦，且要说清原因。"""
        r = compat.check("5.9.3.42398134-d4692421")
        self.assertFalse(r["ok"])
        self.assertEqual(r["level"], "too_old")
        self.assertIn("网页版活动中心", r["message"])

    def test_510_and_old_minors_blocked(self):
        for ver in ("5.8.3", "5.10.9", "4.24.8"):
            self.assertFalse(compat.check(ver)["ok"], ver)

    def test_min_version_passes(self):
        for ver in ("5.11.0", "5.11.3", "5.11.3.43806934-ecace0f7"):
            r = compat.check(ver)
            self.assertTrue(r["ok"], ver)
            self.assertEqual(r["level"], "ok", ver)

    def test_newer_than_tested_warns_but_passes(self):
        r = compat.check("5.12.1")
        self.assertTrue(r["ok"])
        self.assertEqual(r["level"], "newer_than_tested")
        self.assertIn("inspect", r["message"])

    def test_unknown_does_not_block(self):
        """装在 ~/Applications 或自定义路径时读不到版本，不能当成过期。"""
        r = compat.check(None)
        self.assertTrue(r["ok"])
        self.assertEqual(r["level"], "unknown")
        self.assertIn("WORKBUDDY_APP", r["action"])

    def test_upgrade_text_has_official_entry_and_direct_links(self):
        txt = compat.upgrade_text("5.9.3", arch="darwin-arm64")
        self.assertIn("检查更新", txt)          # 官方内升级入口
        self.assertIn("download.codebuddy.cn", txt)  # 官方 CDN 直链
        self.assertIn("5.11.3", txt)
        self.assertIn("历史版本下载", txt)       # 提醒官网只到 5.9.3 的坑
        self.assertIn("应用程序", txt)          # 必须装进 /Applications，否则不能自动更新

    def test_recommended_matches_host_arch(self):
        rec = compat.recommended()
        expect = "darwin-arm64" if platform.machine() in ("arm64", "aarch64") else "darwin-x64"
        self.assertEqual(rec["arch"], expect)
        self.assertTrue(rec["url"].endswith(".dmg"))

    def test_arch_override_via_env(self):
        with patch.dict("os.environ", {"WORKBUDDY_ARCH": "darwin-x64"}):
            self.assertEqual(compat.recommended()["arch"], "darwin-x64")

    def test_one_line_marks(self):
        self.assertTrue(compat.one_line("5.11.3").startswith("✓"))
        self.assertTrue(compat.one_line("5.9.3").startswith("✗"))
        self.assertTrue(compat.one_line(None).startswith("?"))


if __name__ == "__main__":
    unittest.main()
