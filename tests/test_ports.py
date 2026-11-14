"""CDP 端口发现 / 归属校验 / 协商。

背景（2026-11-14 读 `app.asar` + 两台 5.11.3 实测）：
  - 客户端只在启动时读一次 WORKBUDDY_REMOTE_DEBUGGING_PORT（`/^\\d+$/`），
    `appendSwitch('remote-debugging-port', n)`，**端口被占没有回退**。
  - Chromium 把真实端口写进 `<userData>/DevToolsActivePort`，所以文件是
    「当前听在哪」的事实，环境变量只是「下次启动的意图」。
  - 另一台机器（9223）用户目录下还有 AdsPower 写的同名文件（59680），
    所以端口文件必须按路径白名单读，绝不能全局 find。

这些用例守住三件事：归属不能误判、陈旧值要能降级、协商不误改文件。
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin import launchd, ports  # noqa: E402


class _FakeCDP(BaseHTTPRequestHandler):
    """极简 /json/version，用来区分 owned / foreign。"""

    ua = "WorkBuddy/5.11.3 Chrome/140"

    def do_GET(self):  # noqa: N802
        if self.path == "/json/list":
            body = b"[]"
        elif self.path == "/json/version":
            body = json.dumps({"Browser": "Chrome/140.0.0.0",
                               "User-Agent": self.ua}).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静默
        pass


def _serve(ua: str) -> tuple[HTTPServer, int]:
    handler = type("H", (_FakeCDP,), {"ua": ua})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _cfg(**growth) -> dict:
    g = {"cdp_port": 9222}
    g.update(growth)
    return {"growth": g, "state_db": "~/.local/share/buddy-web-checkin/state.sqlite3"}


class ProbeTests(unittest.TestCase):
    def test_workbuddy_ua_is_owned(self):
        srv, port = _serve("WorkBuddy/5.11.3 Chrome/140")
        try:
            self.assertEqual(ports.probe(port)["state"], "owned")
            self.assertEqual(ports.owner_state(port)[0], "owned")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_chrome_ua_is_foreign(self):
        srv, port = _serve("Mozilla/5.0 Chrome/140 Safari/537.36")
        try:
            self.assertEqual(ports.probe(port)["state"], "foreign")
            self.assertEqual(ports.owner_state(port)[0], "foreign")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_closed_port(self):
        with patch.object(ports, "listener", return_value=None):
            self.assertEqual(ports.probe(9)["state"], "closed")
            self.assertEqual(ports.owner_state(9)[0], "closed")

    def test_empty_ua_falls_back_to_lsof_exe(self):
        """UA 缺失时只看可执行路径 —— 「进程名叫 Electron」不算证据。"""
        srv, port = _serve("")
        fake = {"port": port, "pid": 1, "cmd": "Electron",
                "exe": "/Applications/WorkBuddy.app/Contents/MacOS/Electron",
                "owned": True}
        try:
            with patch.object(ports, "listener", return_value=fake):
                self.assertEqual(ports.owner_state(port)[0], "owned")
                fake["exe"] = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                fake["owned"] = False
                self.assertEqual(ports.owner_state(port)[0], "foreign")
        finally:
            srv.shutdown()
            srv.server_close()


class ActivePortFileTests(unittest.TestCase):
    def test_reads_port_and_ws(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "DevToolsActivePort"
            path.write_text("9223\n/devtools/browser/abc\n", encoding="utf-8")
            with patch.object(ports, "active_port_paths", lambda: [path]):
                info = ports.read_active_port()
            self.assertEqual(info["port"], 9223)
            self.assertEqual(info["ws"], "/devtools/browser/abc")

    def test_missing_or_garbage_returns_none(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "DevToolsActivePort"
            for content in ("", "not-a-port\n", "9223\n"):
                path.write_text(content, encoding="utf-8")
                with patch.object(ports, "active_port_paths", lambda: [path]):
                    got = ports.read_active_port()
                if content.startswith("not-a-port") or content == "":
                    self.assertIsNone(got)
                else:  # 只有端口行也算有效
                    self.assertEqual(got["port"], 9223)

    def test_only_workbuddy_paths_are_read(self):
        """AdsPower 的 DevToolsActivePort 不能被当成客户端的。"""
        paths = [str(p) for p in ports.active_port_paths()]
        self.assertTrue(any(".workbuddy" in p for p in paths))
        self.assertFalse(any("adspower" in p.lower() for p in paths))


class EnvPortTests(unittest.TestCase):
    def test_parses_domain_environment(self):
        out = ("gui/501 = {\n\tenvironment = {\n"
               "\t\tWORKBUDDY_REMOTE_DEBUGGING_PORT => 9223\n"
               "\t\tSSH_AUTH_SOCK => /tmp/x\n\t}\n}\n")
        with patch.object(ports.subprocess, "run",
                          lambda *a, **k: type("P", (), {"stdout": out, "returncode": 0})()):
            self.assertEqual(ports.env_port(), 9223)

    def test_missing_returns_none(self):
        with patch.object(ports.subprocess, "run",
                          lambda *a, **k: type("P", (), {"stdout": "", "returncode": 0})()):
            self.assertIsNone(ports.env_port())


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cdp_port.json"
        self._patch = patch.object(ports, "cache_path", lambda cfg=None: self.cache)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_active_file_wins_and_is_cached(self):
        res = ports.Resolution(port=1, ok=True, source="active_file")
        with patch.object(ports, "read_active_port", lambda: {"port": 9333} | {}), \
             patch.object(ports, "env_port", lambda: 9223), \
             patch.object(ports, "probe", lambda p, timeout=0.5: {"port": p, "state": "owned", "ua": "WorkBuddy", "browser": "", "detail": "ok"}), \
             patch.object(ports, "owner_state", lambda p, prb=None: ("owned", "ok")):
            res = ports.resolve(_cfg())
        self.assertTrue(res.ok)
        self.assertEqual((res.port, res.source), (9333, "active_file"))
        self.assertEqual(json.loads(self.cache.read_text())["port"], 9333)

    def test_stale_file_falls_back_to_env(self):
        """文件里的端口已经关掉（客户端重启过）：必须降级到环境变量。"""
        def fake_probe(port, timeout=0.5):
            return {"port": port, "state": "owned" if port == 9223 else "closed",
                    "ua": "", "browser": "", "detail": ""}

        def fake_owner(port, prb=None):
            return ("owned" if port == 9223 else "closed",
                    "ok" if port == 9223 else "no listener")

        with patch.object(ports, "read_active_port", lambda: {"port": 9222}), \
             patch.object(ports, "env_port", lambda: 9223), \
             patch.object(ports, "probe", fake_probe), \
             patch.object(ports, "owner_state", fake_owner), \
             patch.object(ports, "listener", lambda p: None):
            res = ports.resolve(_cfg())
        self.assertTrue(res.ok)
        self.assertEqual((res.port, res.source), (9223, "env"))

    def test_nothing_found_reports_preferred_port(self):
        with patch.object(ports, "read_active_port", lambda: None), \
             patch.object(ports, "env_port", lambda: 9223), \
             patch.object(ports, "probe", lambda p, timeout=0.5: {"port": p, "state": "closed", "ua": "", "browser": "", "detail": ""}), \
             patch.object(ports, "owner_state", lambda p, prb=None: ("closed", "no listener")), \
             patch.object(ports, "listener", lambda p: None), \
             patch.object(ports, "client_pids", lambda: [1]):
            res = ports.resolve(_cfg())
        self.assertFalse(res.ok)
        self.assertEqual(res.source, "none")
        self.assertEqual(res.port, 9223)  # 下次启动最可能用的那个


class NegotiateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        patch.object(ports, "cache_path",
                     lambda cfg=None: Path(self.tmp.name) / "c.json").start()

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    def test_keep_when_owned(self):
        with patch.object(ports, "resolve",
                          lambda cfg=None, timeout=0.5: ports.Resolution(9223, True, "env", "ok")):
            plan = ports.negotiate(_cfg())
        self.assertEqual(plan["action"], "keep")
        self.assertFalse(plan["needs_restart"])

    def test_client_not_running(self):
        res = ports.Resolution(9222, False, "none", facts={"client_pids": []})
        with patch.object(ports, "resolve", lambda cfg=None, timeout=0.5: res):
            plan = ports.negotiate(_cfg())
        self.assertEqual(plan["action"], "client_not_running")

    def test_switch_when_port_taken_by_foreign(self):
        res = ports.Resolution(9222, False, "none", facts={"client_pids": [1], "env": 9222})
        with patch.object(ports, "resolve", lambda cfg=None, timeout=0.5: res), \
             patch.object(ports, "_conflict_of", lambda cfg, r: "Google Chrome (pid 42)"), \
             patch.object(ports, "find_free", lambda cfg=None: 9240):
            plan = ports.negotiate(_cfg())
        self.assertEqual(plan["action"], "switch")
        self.assertEqual(plan["port"], 9240)
        self.assertTrue(plan["needs_restart"])

    def test_no_free_port(self):
        res = ports.Resolution(9222, False, "none", facts={"client_pids": [1]})
        with patch.object(ports, "resolve", lambda cfg=None, timeout=0.5: res), \
             patch.object(ports, "_conflict_of", lambda cfg, r: "Chrome"), \
             patch.object(ports, "find_free", lambda cfg=None: None):
            plan = ports.negotiate(_cfg())
        self.assertEqual(plan["action"], "no_free_port")

    def test_restart_required_when_client_silent(self):
        res = ports.Resolution(9222, False, "none", facts={"client_pids": [1]})
        with patch.object(ports, "resolve", lambda cfg=None, timeout=0.5: res), \
             patch.object(ports, "_conflict_of", lambda cfg, r: ""), \
             patch.object(ports, "find_free", lambda cfg=None: 9241):
            plan = ports.negotiate(_cfg())
        self.assertEqual(plan["action"], "restart_required")
        self.assertEqual(plan["port"], 9241)


class ApplyPortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "config.toml"
        self.cfg_path.write_text(
            "# 头部注释\n[growth]\n# 端口说明：别删\ncdp_port = 9222   # 回退值\n"
            "auto_makeup = true\n", encoding="utf-8")
        patch.object(ports, "cache_path",
                     lambda cfg=None: Path(self.tmp.name) / "c.json").start()

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    def test_config_rewrite_keeps_comments(self):
        calls: list[list[str]] = []
        with patch.object(ports.subprocess, "run",
                          lambda cmd, **k: calls.append(cmd) or type("P", (), {"returncode": 0, "stderr": "", "stdout": ""})()), \
             patch.object(launchd, "install_cdp_env", lambda port, **k: {"ok": True, "port": port}):
            done = ports.apply_port(_cfg(), 9242, config_path=self.cfg_path)
        text = self.cfg_path.read_text(encoding="utf-8")
        self.assertIn("cdp_port = 9242   # 回退值", text)
        self.assertIn("# 端口说明：别删", text)
        self.assertIn("auto_makeup = true", text)
        self.assertTrue(done["setenv"])
        self.assertTrue(done["config"])
        self.assertIn("launchctl setenv WORKBUDDY_REMOTE_DEBUGGING_PORT 9242", " ".join(calls[0]))

    def test_setenv_failure_is_reported_not_raised(self):
        with patch.object(ports.subprocess, "run",
                          lambda cmd, **k: type("P", (), {"returncode": 1, "stderr": "denied", "stdout": ""})()), \
             patch.object(launchd, "install_cdp_env", lambda port, **k: {"ok": False, "error": "boom"}):
            done = ports.apply_port(_cfg(), 9243, config_path=self.cfg_path)
        self.assertFalse(done["setenv"])
        self.assertIn("denied", done["setenv_error"])
        self.assertFalse(done["plist"]["ok"])


class LaunchdRenderTests(unittest.TestCase):
    def test_cdp_env_plist_carries_port(self):
        text = launchd.render_cdp_env(9242)
        self.assertIn("com.local.workbuddy-cdp-env", text)
        self.assertIn("WORKBUDDY_REMOTE_DEBUGGING_PORT 9242", text)
        self.assertIn("<key>RunAtLoad</key>", text)

    def test_daemon_plist_shape(self):
        import plistlib

        text = launchd.render_daemon("/tmp/repo", "/tmp/daemon.log")
        data = plistlib.loads(text.encode())
        self.assertEqual(data["Label"], "com.local.buddy-web-checkin")
        self.assertEqual(data["ProgramArguments"][1:], ["/tmp/repo/run.sh", "daemon"])
        self.assertEqual(data["EnvironmentVariables"], {"BUDDY_FREE_MODEL": "1"})
        self.assertTrue(data["KeepAlive"])


class SelfHealTests(unittest.TestCase):
    """端口对不上时，GrowthBrowser 应自己换到客户端真实端口（不重启客户端）。"""

    def setUp(self):
        from buddy_checkin.adapters.growth_browser import GrowthBrowser

        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "DevToolsActivePort"
        self.srv, self.port = _serve("WorkBuddy/5.11.3 Chrome/140")
        self.path.write_text(f"{self.port}\n/devtools/browser/x\n", encoding="utf-8")
        patch.object(ports, "active_port_paths", lambda: [self.path]).start()
        patch.object(ports, "cache_path",
                     lambda cfg=None: Path(self.tmp.name) / "c.json").start()
        self.gb = GrowthBrowser(_cfg(cdp_port=9, cdp_port_source="config"))

    def tearDown(self):
        patch.stopall()
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def test_targets_heals_to_real_port(self):
        self.assertEqual(self.gb.port, 9)
        self.gb.targets()  # 9 上没有服务 → 触发一次重探
        self.assertEqual(self.gb.port, self.port)
        self.assertEqual(self.gb.port_source, "active_file")
        self.assertIsNone(self.gb.block)


if __name__ == "__main__":
    unittest.main()
