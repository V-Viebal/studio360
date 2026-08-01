"""Contract smoke for the Studio360 -> LLM360 Gemini Veo bridge."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from lib.video_backends.base import VideoGenerationRequest
from lib.video_backends.gemini import GeminiVideoBackend


class _GeminiBridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _record(self, body: bytes = b"") -> None:
        self.server.events.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "api_key": self.headers.get("x-goog-api-key"),
                "body": body,
            }
        )

    def _write(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._record(body)
        response = json.dumps({"name": "operations/opaque-operation", "done": False}).encode()
        self._write(200, response, "application/json")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self._record()
        if self.path == "/v1beta/operations/opaque-operation":
            response = json.dumps(
                {
                    "name": "operations/opaque-operation",
                    "done": True,
                    "response": {
                        "generateVideoResponse": {"generatedSamples": [{"video": {"uri": "files/opaque-file"}}]}
                    },
                }
            ).encode()
            self._write(200, response, "application/json")
            return
        if self.path == "/v1beta/files/opaque-file:download?alt=media":
            self._write(200, b"fake-veo-video", "video/mp4")
            return
        self._write(404, b"not found", "text/plain")

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def gemini_bridge_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GeminiBridgeHandler)
    server.events = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


async def test_llm360_bridge_matches_google_genai_veo_protocol(gemini_bridge_server, tmp_path) -> None:
    base_url = f"http://127.0.0.1:{gemini_bridge_server.server_port}"
    backend = GeminiVideoBackend(
        backend_type="llm360",
        api_key="studio360-gateway-key",
        base_url=base_url,
        video_model="veo-3.1-fast-generate-preview",
    )
    output = tmp_path / "veo.mp4"

    result = await backend.generate(
        VideoGenerationRequest(
            prompt="A cinematic sunrise above Bangkok",
            output_path=output,
            duration_seconds=8,
        )
    )

    assert output.read_bytes() == b"fake-veo-video"
    assert result.video_uri == "files/opaque-file"
    assert result.model == "veo-3.1-fast-generate-preview"

    events = gemini_bridge_server.events
    assert [event["path"] for event in events] == [
        "/v1beta/models/veo-3.1-fast-generate-preview:predictLongRunning",
        "/v1beta/operations/opaque-operation",
        "/v1beta/files/opaque-file:download?alt=media",
    ]
    assert all(event["api_key"] == "studio360-gateway-key" for event in events)
    submit_payload = json.loads(events[0]["body"])
    assert submit_payload["instances"][0]["prompt"] == "A cinematic sunrise above Bangkok"
    assert submit_payload["parameters"]["durationSeconds"] == 8


def test_llm360_provider_is_video_only_and_defaults_to_veo_fast() -> None:
    """The LLM360 gateway must not advertise unsupported Gemini media routes."""
    from lib.backend_assembly.specs import PROVIDER_SPEC_REGISTRY
    from lib.config.registry import PROVIDER_REGISTRY

    provider = PROVIDER_REGISTRY["gemini-llm360"]
    fast_model = provider.models["veo-3.1-fast-generate-preview"]

    assert fast_model.default is True
    assert fast_model.media_type == "video"
    assert ("gemini-llm360", "video") in PROVIDER_SPEC_REGISTRY
    assert ("gemini-llm360", "image") not in PROVIDER_SPEC_REGISTRY


def test_llm360_connection_probe_normalizes_google_base_url(monkeypatch) -> None:
    from types import SimpleNamespace

    from server.routers import providers

    captured: dict[str, Any] = {}

    class _Models:
        def list(self):
            return [SimpleNamespace(name="models/veo-3.1-fast-generate-preview")]

    class _Client:
        def __init__(self, **kwargs: Any):
            captured.update(kwargs)
            self.models = _Models()

    monkeypatch.setattr("google.genai.Client", _Client)
    result = providers._test_gemini_aistudio(
        {
            "api_key": "studio360-gateway-key",
            "base_url": "https://api-llm360.hmz.one/v1beta",
        },
        lambda key, **_kwargs: key,
    )

    assert captured["api_key"] == "studio360-gateway-key"
    assert captured["http_options"] == {"base_url": "https://api-llm360.hmz.one/"}
    assert result.available_models == ["veo-3.1-fast-generate-preview"]
