"""通过本机 stdio JSONL 与 Codex App Server 通信。"""

import json
import queue
import shutil
import subprocess
import threading


class AppServerError(RuntimeError):
    """App Server 启动或通信失败。"""


class AppServerTimeout(AppServerError):
    """App Server 请求超时。"""


def find_codex_cli():
    """查找本机 Codex CLI，不安装或修改它。"""
    return shutil.which("codex")


class AppServerClient:
    """管理本客户端创建的 App Server 子进程。"""

    def __init__(self, executable=None, timeout=10.0, popen_factory=None):
        self.executable = executable
        self.timeout = float(timeout)
        self._popen = popen_factory or subprocess.Popen
        self._process = None
        self._reader = None
        self._pending = {}
        self._next_id = 1
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._initialized = False
        self._closed = False

    @property
    def process_id(self):
        """返回本客户端创建的子进程 ID。"""
        return self._process.pid if self._process is not None else None

    def start(self):
        """启动子进程并完成一次初始化握手。"""
        if self._closed:
            raise AppServerError("客户端已关闭，不能再次启动")
        if self._initialized:
            return self
        executable = self.executable or find_codex_cli()
        if not executable:
            raise AppServerError("未找到 Codex CLI，请先检查本机安装")
        try:
            self._process = self._popen(
                [executable, "app-server", "--stdio"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            )
            self._reader = threading.Thread(target=self._read_stdout, name="CodexAppServerReader", daemon=True)
            self._reader.start()
            self._request("initialize", {"clientInfo": {
                "name": "houdini_ai_workspace", "title": "Houdini AI Workspace", "version": "0.1.0",
            }})
            self._send({"method": "initialized", "params": {}})
            self._initialized = True
            return self
        except Exception:
            self.close()
            raise

    def _send(self, message):
        """按 JSONL 协议向子进程写入一条消息。"""
        payload = (json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            if self._process is None or self._process.poll() is not None:
                raise AppServerError("Codex App Server 未运行")
            try:
                self._process.stdin.write(payload)
                self._process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise AppServerError("无法写入 Codex App Server") from exc

    def _request(self, method, params):
        """发送请求并在限定时间内等待匹配的响应。"""
        response_queue = queue.Queue(maxsize=1)
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = response_queue
        try:
            self._send({"method": method, "id": request_id, "params": params})
            try:
                response = response_queue.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise AppServerTimeout(f"Codex App Server 请求超时：{method}") from exc
            if isinstance(response, Exception):
                raise response
            if "error" in response:
                error = response["error"]
                code = error.get("code") if isinstance(error, dict) else None
                raise AppServerError(f"Codex App Server 请求失败：{method}（{code}）")
            if "result" not in response:
                raise AppServerError(f"Codex App Server 响应缺少结果：{method}")
            return response["result"]
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _read_stdout(self):
        """持续读取响应，避免通知与请求响应交错时阻塞。"""
        try:
            for line in self._process.stdout:
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict) or "id" not in message:
                    continue
                with self._lock:
                    waiter = self._pending.get(message["id"])
                if waiter is not None:
                    try:
                        waiter.put_nowait(message)
                    except queue.Full:
                        pass
        finally:
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for waiter in pending:
                try:
                    waiter.put_nowait(AppServerError("Codex App Server 通信已结束"))
                except queue.Full:
                    pass

    def account_status(self):
        """只读取认证类型和套餐，不返回账户邮箱或令牌。"""
        self._require_initialized()
        result = self._request("account/read", {"refreshToken": False})
        account = result.get("account") if isinstance(result, dict) else None
        account = account if isinstance(account, dict) else {}
        return {"authenticated": bool(account), "chatgpt_connected": account.get("type") == "chatgpt",
                "account_type": account.get("type"), "plan_type": account.get("planType")}

    def list_models(self):
        """分页读取当前 App Server 返回的可见模型。"""
        self._require_initialized()
        models, cursor, seen = [], None, set()
        while True:
            params = {"limit": 20, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            result = self._request("model/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise AppServerError("Codex 模型列表格式无效")
            models.extend(entry for entry in result["data"] if isinstance(entry, dict) and not entry.get("hidden"))
            cursor = result.get("nextCursor")
            if not cursor:
                return models
            if cursor in seen:
                raise AppServerError("Codex 模型列表分页游标重复")
            seen.add(cursor)

    def _require_initialized(self):
        """拒绝在初始化前发送业务请求。"""
        if not self._initialized or self._closed:
            raise AppServerError("请先初始化 Codex App Server")

    def close(self):
        """仅关闭本客户端启动的子进程。"""
        if self._closed:
            return
        self._closed = True
        self._initialized = False
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if self._reader is not None and self._reader is not threading.current_thread():
                self._reader.join(timeout=1)
            self._process = None

    def __enter__(self):
        """启动并返回客户端。"""
        return self.start()

    def __exit__(self, _exc_type, _exc, _traceback):
        """退出上下文时关闭子进程。"""
        self.close()
