"""Codex App Server 协议与模型配置的离线测试。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from houdini_agent.codex import (AppServerClient, AppServerError, AppServerTimeout,
                                 ExecutionModelSettings, parse_model_catalog, resolve_execution_model)


FAKE_SERVER = r'''
import json
import sys
import time

initialized = False
for line in sys.stdin:
    message = json.loads(line)
    method = message["method"]
    if method == "initialized":
        initialized = True
        continue
    if method == "initialize":
        result = {"userAgent": "fake"}
    elif not initialized:
        result = None
    elif method == "account/read":
        result = {"account": {"type": "chatgpt", "email": "private@example.com", "planType": "plus"}}
    elif method == "model/list":
        cursor = message["params"].get("cursor")
        result = {"data": [{"id": "model-b", "model": "model-b", "displayName": "Model B",
                            "defaultReasoningEffort": "high", "isDefault": False,
                            "supportedReasoningEfforts": [{"reasoningEffort": "high", "description": "Deep"}],
                            "inputModalities": ["text"]}], "nextCursor": None} if cursor else {
            "data": [{"id": "model-a", "model": "model-a", "displayName": "Model A",
                      "defaultReasoningEffort": "low", "isDefault": True,
                      "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "Fast"},
                                                    {"reasoningEffort": "high", "description": "Deep"}]}],
            "nextCursor": "second"}
    elif method == "slow/read":
        time.sleep(0.3)
        result = {}
    else:
        result = None
    if result is None:
        response = {"id": message["id"], "error": {"code": -1, "message": "unsupported"}}
    else:
        response = {"id": message["id"], "result": result}
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()
'''


def fake_popen(command, **kwargs):
    """用本地 Python 子进程模拟 stdio，不连接网络或真实账户。"""
    if command[1:] != ["app-server", "--stdio"]:
        raise AssertionError("启动参数不符合预期")
    return subprocess.Popen([sys.executable, "-u", "-c", FAKE_SERVER], **kwargs)


class AppServerTests(unittest.TestCase):
    def test_handshake_account_models_and_shutdown(self):
        client = AppServerClient(executable="fake-codex", popen_factory=fake_popen)
        with client:
            process = client._process
            pid = client.process_id
            self.assertEqual(pid, client.start().process_id)
            self.assertEqual({"authenticated": True, "chatgpt_connected": True,
                              "account_type": "chatgpt", "plan_type": "plus"},
                             client.account_status())
            self.assertNotIn("email", client.account_status())
            self.assertEqual(["model-a", "model-b"], [item["id"] for item in client.list_models()])
        self.assertIsNotNone(process.poll())
        self.assertIsNone(client.process_id)

    def test_missing_cli_and_uninitialized_request(self):
        client = AppServerClient()
        with self.assertRaises(AppServerError):
            client.account_status()
        with mock.patch("houdini_agent.codex.app_server.find_codex_cli", return_value=None):
            with self.assertRaisesRegex(AppServerError, "未找到 Codex CLI"):
                client.start()

    def test_timeout_is_bounded(self):
        with AppServerClient(executable="fake-codex", timeout=0.05, popen_factory=fake_popen) as client:
            with self.assertRaises(AppServerTimeout):
                client._request("slow/read", {})


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.catalog = parse_model_catalog([
            {"id": "model-a", "model": "model-a", "displayName": "Model A", "isDefault": True,
             "defaultReasoningEffort": "low", "supportedReasoningEfforts": [
                 {"reasoningEffort": "low", "description": "Fast"},
                 {"reasoningEffort": "high", "description": "Deep"}]},
            {"id": "model-b", "model": "model-b", "displayName": "Model B", "hidden": True},
        ])

    def test_catalog_and_default_resolution(self):
        self.assertEqual(1, len(self.catalog))
        self.assertEqual(("text", "image"), self.catalog[0].input_modalities)
        model, effort = resolve_execution_model(self.catalog)
        self.assertEqual(("model-a", "low"), (model.id, effort))
        self.assertEqual("high", resolve_execution_model(self.catalog, "model-a", "high")[1])
        self.assertEqual("low", resolve_execution_model(self.catalog, "removed-model", "invalid")[1])

    def test_selection_is_separate_and_validated(self):
        with TemporaryDirectory() as directory:
            settings = ExecutionModelSettings(Path(directory) / "codex_execution.json")
            self.assertEqual({"model_id": None, "reasoning_effort": None}, settings.read())
            settings.save(self.catalog, "model-a", "high")
            self.assertEqual({"model_id": "model-a", "reasoning_effort": "high"}, settings.read())
            with self.assertRaises(ValueError):
                settings.save(self.catalog, "model-b")
            with self.assertRaises(ValueError):
                settings.save(self.catalog, "model-a", "ultra")
            self.assertEqual("model-a", json.loads(settings.path.read_text(encoding="utf-8"))["model_id"])


if __name__ == "__main__":
    unittest.main()
