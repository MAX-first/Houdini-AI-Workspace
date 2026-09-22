# -*- coding: utf-8 -*-
"""从 Houdini Shelf 启动 Bridge 与独立 AI Workspace。"""

import os
import subprocess
import time
from pathlib import Path

from .workspace_control import request_activation, request_status


_launch_pending = False
_ENV_REMOVE = {
    "HFS", "HB", "HH", "HHC", "HT", "HSITE", "HOUDINI_PATH", "HOUDINI_PACKAGE_DIR",
    "QT_API", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH",
    "QML_IMPORT_PATH", "QT_QUICK_CONTROLS_STYLE", "PYSIDE_DESIGNER_PLUGINS", "VIRTUAL_ENV",
    "__PYVENV_LAUNCHER__",
}


def repo_root():
    """返回项目根目录。"""
    return Path(__file__).resolve().parents[2]


def workspace_log_path():
    """返回外部程序日志路径。"""
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "HoudiniAgent"
    return base / "workspace.log"


def build_workspace_environment(bridge_port, source=None):
    """构建不继承 Houdini Python 与 Qt 路径的子进程环境。"""
    env = dict(source or os.environ)
    hfs = os.path.normcase(os.path.normpath(env.get("HFS", "")))
    for key in list(env):
        if key in _ENV_REMOVE or key.upper().startswith("PYTHON"):
            env.pop(key, None)

    clean_path = []
    for entry in env.get("PATH", "").split(os.pathsep):
        normalized = os.path.normcase(os.path.normpath(entry)) if entry else ""
        if not entry or (hfs and (normalized == hfs or normalized.startswith(hfs + os.sep))):
            continue
        clean_path.append(entry)

    root = repo_root()
    venv = root / ".venv"
    env["PATH"] = os.pathsep.join([str(venv / "Scripts")] + clean_path)
    env["VIRTUAL_ENV"] = str(venv)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HAGENT_BRIDGE_PORT"] = str(int(bridge_port))
    env["HAGENT_TELEMETRY_OFF"] = "1"
    env["DCC_AI_TELEMETRY_OFF"] = "1"
    env["HAGENT_SAFE_EXTERNAL"] = "1"
    return env


def _ensure_bridge():
    """复用当前 Bridge，失效时在当前 Houdini 中重新启动。"""
    import houdini_agent.bridge.server as bridge_server

    server = getattr(bridge_server, "_SERVER", None)
    thread = getattr(bridge_server, "_THREAD", None)
    socket_ok = bool(server is not None and getattr(server, "socket", None)
                     and server.socket.fileno() >= 0)
    if server is not None and thread is not None and thread.is_alive() and socket_ok:
        return server

    if server is not None:
        try:
            server.server_close()
        except Exception:
            pass
    bridge_server._SERVER = None
    bridge_server._THREAD = None
    server = bridge_server.start_bridge(host="127.0.0.1", port=45172)
    if server is None:
        raise RuntimeError("Bridge 启动失败，请查看 %LOCALAPPDATA%\\HoudiniAgent\\bridge.log")
    return server


def _allow_foreground_activation():
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass


def _python_executable():
    root = repo_root()
    name = "pythonw.exe" if os.name == "nt" else "python"
    executable = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / name
    if not executable.is_file():
        raise RuntimeError("未找到独立 Python：%s" % executable)
    return executable


def _spawn_workspace(bridge_port):
    root = repo_root()
    log_path = workspace_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    stream = log_path.open("a", encoding="utf-8")
    try:
        return subprocess.Popen(
            [str(_python_executable()), "-m", "houdini_agent.launcher.workspace_app"],
            cwd=str(root),
            env=build_workspace_environment(bridge_port),
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=flags,
        )
    finally:
        stream.close()


def _set_status(hou, message, error=False):
    try:
        severity = hou.severityType.Error if error else hou.severityType.Message
        hou.ui.setStatusMessage(message, severity=severity)
    except Exception:
        pass


def _watch_launch(hou, process, bridge_port):
    deadline = time.monotonic() + 15.0
    next_check = [0.0]

    def finish(message, error=False):
        global _launch_pending
        _launch_pending = False
        try:
            hou.ui.removeEventLoopCallback(check)
        except Exception:
            pass
        _set_status(hou, message, error)
        if error:
            hou.ui.displayMessage(message, severity=hou.severityType.Error)

    def check():
        now = time.monotonic()
        if now < next_check[0]:
            return
        next_check[0] = now + 0.25
        status = request_status(timeout=0.08)
        if status and status.get("qml_status") == "Error":
            finish("AI Workspace 的 QML 加载失败。日志：%s" % workspace_log_path(), True)
        elif (status and status.get("ready") and status.get("qml_status") == "Ready"
                and status.get("qml_root") and status.get("rendered")
                and status.get("connected") and status.get("session")):
            _allow_foreground_activation()
            request_activation(bridge_port, timeout=0.08)
            finish("AI Workspace 已启动并连接当前 Houdini")
        elif process.poll() is not None:
            message = "AI Workspace 启动失败，退出码 %s。日志：%s" % (process.returncode, workspace_log_path())
            finish(message, True)
        elif now >= deadline:
            finish("AI Workspace 启动超时。日志：%s" % workspace_log_path(), True)

    hou.ui.addEventLoopCallback(check)


def start():
    """启动或激活 AI Workspace。"""
    global _launch_pending
    import hou

    try:
        server = _ensure_bridge()
        bridge_port = int(server.server_address[1])
        _allow_foreground_activation()
        if request_activation(bridge_port):
            _set_status(hou, "AI Workspace 已在运行，已切换到前台")
            return {"success": True, "action": "activated", "bridge_port": bridge_port}
        if _launch_pending:
            _set_status(hou, "AI Workspace 正在启动，请稍候")
            return {"success": True, "action": "pending", "bridge_port": bridge_port}

        _launch_pending = True
        process = _spawn_workspace(bridge_port)
        _set_status(hou, "正在启动 AI Workspace…")
        _watch_launch(hou, process, bridge_port)
        return {"success": True, "action": "started", "pid": process.pid, "bridge_port": bridge_port}
    except Exception as exc:
        _launch_pending = False
        message = "AI Workspace 启动失败：%s" % exc
        _set_status(hou, message, True)
        hou.ui.displayMessage(message, severity=hou.severityType.Error)
        return {"success": False, "error": str(exc)}
