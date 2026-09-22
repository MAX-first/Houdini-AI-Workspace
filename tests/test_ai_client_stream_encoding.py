"""验证 Windows GBK 输出流不会中断流式响应。"""

import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from houdini_agent.launcher.workspace_app import _configure_output_streams
from houdini_agent.utils.ai_client_streaming import AIClientStreamingMixin


class FakeResponse:
    status_code = 200
    encoding = "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def iter_content(self, **_kwargs):
        yield b'data: {"choices":[{"delta":{"reasoning_content":"test"}}]}\n'
        yield b'data: [DONE]\n'


class FakeSession:
    def __init__(self):
        self.posts = 0

    def post(self, *_args, **_kwargs):
        self.posts += 1
        return FakeResponse()


class FakeClient(AIClientStreamingMixin):
    def __init__(self):
        self._http_session = FakeSession()
        self._max_retries = 1
        self._chunk_timeout = 1
        self._stop_event = threading.Event()

    def _get_api_key(self, _provider):
        return "unit-test-key"

    def _get_api_url(self, _provider, _model):
        return "http://127.0.0.1/not-requested"

    def _is_anthropic_protocol(self, _provider, _model):
        return False

    def is_glm47(self, _model):
        return False


class StreamEncodingTests(unittest.TestCase):
    def test_gbk_stdout_reproduces_error_after_http_response(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            output = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": folder}), mock.patch("sys.stdout", output):
                chunks = list(client.chat_stream([{"role": "user", "content": "hello"}], provider="deepseek"))
            self.assertEqual(1, client._http_session.posts)
            self.assertEqual("error", chunks[0]["type"])
            self.assertIn("position 12", chunks[0]["error"])
            trace = (Path(folder) / "HoudiniAgent" / "ai_client_error.log").read_text(encoding="utf-8")
            self.assertIn("phase=response_stream", trace)
            self.assertIn("ai_client_streaming.py", trace)
            self.assertIn("_process_sse_line", trace)
            self.assertIn("UnicodeEncodeError", trace)
            self.assertNotIn("unit-test-key", trace)
            self.assertNotIn("hello", trace)

    def test_utf8_output_preserves_streamed_reasoning(self):
        client = FakeClient()
        output = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        with mock.patch("sys.stdout", output):
            _configure_output_streams()
            chunks = list(client.chat_stream([{"role": "user", "content": "hello"}], provider="deepseek"))
            self.assertEqual("utf-8", output.encoding)
        self.assertEqual(1, client._http_session.posts)
        self.assertEqual(["thinking", "done"], [chunk["type"] for chunk in chunks])


if __name__ == "__main__":
    unittest.main()
