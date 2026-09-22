# -*- coding: utf-8 -*-
"""安全启动独立 AI Workspace。"""

import ctypes
import os
import sys
import time
import traceback
from pathlib import Path

from .workspace_control import WorkspaceControlServer, request_activation


# Houdini 22 的私有 Qt Quick Controls 样式不属于独立 PySide6 环境。
_INHERITED_QT_QUICK_STYLE = os.environ.pop("QT_QUICK_CONTROLS_STYLE", None)


def _log_path():
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "HoudiniAgent"
    base.mkdir(parents=True, exist_ok=True)
    return base / "workspace.log"


def _log(message):
    try:
        with _log_path().open("a", encoding="utf-8") as stream:
            stream.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(message).rstrip() + "\n")
    except Exception:
        pass


def _configure_output_streams():
    """让重定向到 UTF-8 日志的 Python 输出流使用 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")


def _install_qt_message_handler(q_install_message_handler):
    """将 Qt、QML 与场景图消息写入启动日志。"""
    def handler(message_type, context, message):
        category = getattr(context, "category", "") or "Qt"
        _log("[%s/%s] %s" % (message_type, category, message))

    q_install_message_handler(handler)
    return handler


def _log_runtime(app, pyside_version, qt_version, q_library_info):
    """记录不含密钥的 Python、Qt 与启动环境信息。"""
    _log("=== AI Workspace 启动诊断 ===")
    _log("PID=%s PPID=%s cwd=%s" % (os.getpid(), os.getppid(), os.getcwd()))
    _log("Python=%s executable=%s" % (sys.version.replace("\n", " "), sys.executable))
    _log("prefix=%s base_prefix=%s" % (sys.prefix, sys.base_prefix))
    _log("PySide6=%s Qt=%s" % (pyside_version, qt_version))
    _log("stdout=%s stderr=%s" % (
        getattr(sys.stdout, "encoding", None), getattr(sys.stderr, "encoding", None),
    ))
    _log("Qt libraryPaths=%s" % list(app.libraryPaths()))
    for name in ("PrefixPath", "PluginsPath", "QmlImportsPath"):
        location = getattr(q_library_info, name, None)
        if location is not None:
            try:
                _log("Qt %s=%s" % (name, q_library_info.path(location)))
            except Exception as exc:
                _log("Qt %s 读取失败：%s" % (name, exc))
    keys = (
        "HAGENT_BRIDGE_PORT", "HAGENT_SAFE_EXTERNAL", "HAGENT_TELEMETRY_OFF", "DCC_AI_TELEMETRY_OFF",
        "VIRTUAL_ENV", "HFS", "PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
        "QML_IMPORT_PATH", "QML2_IMPORT_PATH", "QT_OPENGL", "QT_QUICK_BACKEND", "QSG_RHI_BACKEND",
    )
    _log("环境=" + repr({key: os.environ.get(key) for key in keys if key in os.environ}))


def _qml_state(view):
    """读取 QQuickWidget 的加载、根组件与渲染状态。"""
    status = view.status()
    status_name = getattr(status, "name", str(status))
    root = view.rootObject()
    errors = [error.toString() for error in view.errors()]
    return status_name, root, errors


def _log_qml_state(stage, win, view, inspect_render=False):
    """记录 QML 根组件和窗口渲染状态。"""
    status_name, root, errors = _qml_state(view)
    handle = win.windowHandle()
    root_size = (float(root.width()), float(root.height())) if root is not None else None
    _log(
        "QML[%s] status=%s root=%s errors=%s winVisible=%s exposed=%s winSize=%sx%s "
        "viewVisible=%s viewSize=%sx%s rootSize=%s"
        % (
            stage, status_name, type(root).__name__ if root is not None else None, errors, win.isVisible(),
            bool(handle and handle.isExposed()), win.width(), win.height(), view.isVisible(), view.width(), view.height(),
            root_size,
        )
    )
    rendered = False
    if inspect_render:
        pixmap = view.grab()
        colors = set()
        if not pixmap.isNull():
            image = pixmap.toImage()
            for x in range(0, max(1, image.width()), max(1, image.width() // 12)):
                for y in range(0, max(1, image.height()), max(1, image.height() // 12)):
                    colors.add(image.pixelColor(x, y).name())
        rendered = not pixmap.isNull() and len(colors) > 1
        _log("渲染[%s] sampledColors=%s rendered=%s" % (stage, sorted(colors)[:20], rendered))
    return status_name, root is not None, rendered


def _activate_window(win):
    """恢复并激活现有窗口。"""
    try:
        from PySide6.QtCore import Qt
        state = win.windowState() & ~Qt.WindowMinimized
        win.setWindowState(state | Qt.WindowActive)
    except Exception:
        pass
    win.show()
    win.raise_()
    win.activateWindow()
    if os.name == "nt":
        try:
            hwnd = int(win.winId())
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass


def main():
    _configure_output_streams()
    bridge_port = int(os.environ.get("HAGENT_BRIDGE_PORT") or 0) or None
    if request_activation(bridge_port):
        return 0

    control = WorkspaceControlServer()
    if not control.start():
        if request_activation(bridge_port, timeout=0.5):
            return 0
        _log("AI Workspace 控制端口被占用，无法启动单实例服务")
        return 2

    os.environ["HAGENT_TELEMETRY_OFF"] = "1"
    os.environ["DCC_AI_TELEMETRY_OFF"] = "1"

    try:
        import PySide6
        from PySide6.QtCore import QLibraryInfo, QObject, Signal, Slot, QTimer, qInstallMessageHandler, qVersion
        from PySide6.QtWidgets import QApplication
        from houdini_agent.ui_qml import external_app

        qt_message_handler = _install_qt_message_handler(qInstallMessageHandler)
        app = QApplication.instance() or QApplication([])
        _log_runtime(app, PySide6.__version__, qVersion(), QLibraryInfo)
        if _INHERITED_QT_QUICK_STYLE:
            _log("已隔离 Houdini Qt Quick Controls 样式：%s" % _INHERITED_QT_QUICK_STYLE)
        win = external_app.show_tool(
            install_packages=False,
            allow_houdini_launch=False,
            check_updates=False,
        )
        view = win.centralWidget()
        _log_qml_state("show_tool 返回", win, view)

        class ActivationRelay(QObject):
            requested = Signal(int)

            @Slot(int)
            def activate(self, port):
                if port:
                    os.environ["HAGENT_BRIDGE_PORT"] = str(port)
                    coord = getattr(win, "_coordinator", None)
                    if coord is not None:
                        coord.connected = False
                        coord._misses = 0
                        coord.poll.setInterval(1500)
                        coord._poll_tick()
                _activate_window(win)

        relay = ActivationRelay(win)
        relay.requested.connect(relay.activate)
        control.set_activation_handler(relay.requested.emit)
        control.update_status(ready=True)

        def inspect_qml(stage, inspect_render=False):
            status_name, has_root, rendered = _log_qml_state(stage, win, view, inspect_render)
            control.update_status(qml_status=status_name, qml_root=has_root)
            if inspect_render:
                control.update_status(rendered=rendered)

        view.statusChanged.connect(lambda _status: inspect_qml("statusChanged"))
        QTimer.singleShot(0, lambda: inspect_qml("事件循环开始"))
        QTimer.singleShot(500, lambda: inspect_qml("500ms"))
        QTimer.singleShot(1500, lambda: inspect_qml("1500ms", True))
        QTimer.singleShot(5000, lambda: inspect_qml("5000ms", True))

        def publish_status():
            coord = getattr(win, "_coordinator", None)
            controller = getattr(win, "_controller", None)
            info = getattr(coord, "bridge_info", {}) if coord is not None else {}
            control.update_status(
                ready=True,
                connected=bool(getattr(coord, "connected", False)),
                session=getattr(controller, "_session", None) is not None,
                houdini=str((info or {}).get("houdini") or ""),
            )

        status_timer = QTimer(win)
        status_timer.timeout.connect(publish_status)
        status_timer.start(250)
        publish_status()
        app._hagent_workspace_control = control
        app._hagent_workspace_relay = relay
        app._hagent_workspace_status_timer = status_timer
        app._hagent_workspace_qt_message_handler = qt_message_handler
        result = app.exec() if hasattr(app, "exec") else app.exec_()
    except Exception:
        _log(traceback.format_exc())
        result = 1
    finally:
        control.stop()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
