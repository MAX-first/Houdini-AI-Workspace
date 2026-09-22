# -*- coding: utf-8 -*-
"""Shelf 一键启动的环境隔离与单实例测试。"""

import os
import socket
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from houdini_agent.launcher import shelf_start, workspace_control


class WorkspaceControlTests(unittest.TestCase):
    def setUp(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind((workspace_control.CONTROL_HOST, 0))
        self.original_port = workspace_control.CONTROL_PORT
        workspace_control.CONTROL_PORT = probe.getsockname()[1]
        probe.close()

    def tearDown(self):
        workspace_control.CONTROL_PORT = self.original_port

    def test_activation_and_duplicate_protection(self):
        activated = threading.Event()
        ports = []
        server = workspace_control.WorkspaceControlServer(lambda port: (ports.append(port), activated.set()))
        duplicate = workspace_control.WorkspaceControlServer()
        try:
            self.assertTrue(server.start())
            self.assertFalse(duplicate.start())
            self.assertTrue(workspace_control.request_activation(45172, timeout=1.0))
            self.assertTrue(activated.wait(1.0))
            self.assertEqual([45172], ports)
            status = workspace_control.request_status(timeout=1.0)
            self.assertFalse(status["ready"])
            server.update_status(ready=True, connected=True, session=True, houdini="22.0.368")
            status = workspace_control.request_status(timeout=1.0)
            self.assertEqual("22.0.368", status["houdini"])
            self.assertTrue(status["connected"])
        finally:
            duplicate.stop()
            server.stop()


class ShelfLauncherTests(unittest.TestCase):
    def test_bridge_is_reused_or_started_once(self):
        import houdini_agent.bridge.server as bridge_server

        class FakeSocket:
            @staticmethod
            def fileno():
                return 10

        class FakeThread:
            @staticmethod
            def is_alive():
                return True

        class FakeServer:
            socket = FakeSocket()
            server_address = ("127.0.0.1", 45172)

        server = FakeServer()
        with mock.patch.object(bridge_server, "_SERVER", server), \
                mock.patch.object(bridge_server, "_THREAD", FakeThread()), \
                mock.patch.object(bridge_server, "start_bridge") as start_bridge:
            self.assertIs(server, shelf_start._ensure_bridge())
            start_bridge.assert_not_called()

        with mock.patch.object(bridge_server, "_SERVER", None), \
                mock.patch.object(bridge_server, "_THREAD", None), \
                mock.patch.object(bridge_server, "start_bridge", return_value=server) as start_bridge:
            self.assertIs(server, shelf_start._ensure_bridge())
            start_bridge.assert_called_once_with(host="127.0.0.1", port=45172)

    def test_environment_removes_houdini_python_and_qt_paths(self):
        source = {
            "HFS": r"D:\software\Houdini22",
            "PATH": os.pathsep.join([
                r"D:\software\Houdini22\bin",
                r"C:\Windows\System32",
            ]),
            "PYTHONHOME": r"D:\software\Houdini22\python313",
            "PYTHONPATH": r"D:\software\Houdini22\houdini\python3.13libs",
            "QT_PLUGIN_PATH": r"D:\software\Houdini22\qt\plugins",
            "QT_QUICK_CONTROLS_STYLE": "houdini.pluto",
            "LOCALAPPDATA": r"C:\Users\dell\AppData\Local",
        }
        env = shelf_start.build_workspace_environment(45172, source)
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("QT_PLUGIN_PATH", env)
        self.assertNotIn("QT_QUICK_CONTROLS_STYLE", env)
        self.assertNotIn(r"D:\software\Houdini22\bin", env["PATH"])
        self.assertIn(r"C:\Windows\System32", env["PATH"])
        self.assertEqual("45172", env["HAGENT_BRIDGE_PORT"])
        self.assertEqual("1", env["HAGENT_TELEMETRY_OFF"])
        self.assertEqual(str(shelf_start.repo_root() / ".venv"), env["VIRTUAL_ENV"])

    def test_shelf_template_is_valid_xml(self):
        path = shelf_start.repo_root() / "tools" / "shelf" / "AIWorkspace.shelf"
        root = ET.parse(path).getroot()
        self.assertEqual("shelfDocument", root.tag)
        self.assertIsNotNone(root.find(".//tool[@name='start_ai_workspace']"))


if __name__ == "__main__":
    unittest.main()
