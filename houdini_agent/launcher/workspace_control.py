# -*- coding: utf-8 -*-
"""AI Workspace 单实例控制通道。"""

import json
import socket
import threading


CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 45193
_PROTOCOL = "houdini-ai-workspace-v1"


def _request(action, bridge_port=None, timeout=0.25):
    payload = {"protocol": _PROTOCOL, "action": action, "bridge_port": bridge_port}
    raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=float(timeout)) as sock:
            sock.settimeout(float(timeout))
            sock.sendall(raw)
            data = b""
            while not data.endswith(b"\n") and len(data) < 4096:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        result = json.loads(data.decode("utf-8", "replace")) if data else {}
        valid = result.get("protocol") == _PROTOCOL and result.get("status") == "ok"
        return result if valid else None
    except Exception:
        return None


def request_activation(bridge_port=None, timeout=0.25):
    """请求现有窗口切到前台，并切换到指定 Bridge 端口。"""
    return _request("activate", bridge_port, timeout) is not None


def request_status(timeout=0.25):
    """读取独立程序当前就绪与 Bridge 连接状态。"""
    return _request("status", timeout=timeout)


class WorkspaceControlServer:
    """监听 Shelf 激活请求，进程退出后端口自动释放。"""

    def __init__(self, on_activate=None):
        self._on_activate = on_activate
        self._pending_port = None
        self._socket = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status = {
            "ready": False,
            "qml_status": "",
            "qml_root": False,
            "rendered": False,
            "connected": False,
            "session": False,
            "houdini": "",
        }

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((CONTROL_HOST, CONTROL_PORT))
            sock.listen(4)
            sock.settimeout(0.5)
        except OSError:
            sock.close()
            return False
        self._socket = sock
        self._thread = threading.Thread(target=self._serve, name="HAgentWorkspaceControl", daemon=True)
        self._thread.start()
        return True

    def set_activation_handler(self, handler):
        with self._lock:
            self._on_activate = handler
            pending = self._pending_port
            self._pending_port = None
        if pending is not None:
            handler(pending)

    def update_status(self, **values):
        with self._lock:
            for key in self._status:
                if key in values:
                    self._status[key] = values[key]

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                self._handle(conn)

    def _handle(self, conn):
        try:
            conn.settimeout(0.5)
            data = b""
            while not data.endswith(b"\n") and len(data) < 4096:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            request = json.loads(data.decode("utf-8", "replace"))
            action = request.get("action")
            if request.get("protocol") != _PROTOCOL or action not in ("activate", "status"):
                raise ValueError("invalid request")
            port = request.get("bridge_port")
            port = int(port) if port else 0
            if action == "activate":
                with self._lock:
                    handler = self._on_activate
                    if handler is None:
                        self._pending_port = port
                if handler is not None:
                    handler(port)
            with self._lock:
                status = dict(self._status)
            response = {"protocol": _PROTOCOL, "status": "ok", **status}
        except Exception as exc:
            response = {"protocol": _PROTOCOL, "status": "error", "error": str(exc)}
        try:
            conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass

    def stop(self):
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
