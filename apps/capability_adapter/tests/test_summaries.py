import json

import httpx
from fastapi.testclient import TestClient

from apps.capability_adapter.app import create_app
from apps.capability_adapter.summarizer import SummaryGenerator, SummarySettings


PAYLOAD = {
    "question": "合同是否有效",
    "provider": "builtin-extractive",
    "sources": [
        {
            "handle": "S1",
            "title": "民法典第十条",
            "text": "处理民事纠纷，应当依照法律。法律没有规定的，可以适用习惯。",
            "locator": {"article": "第十条"},
        },
        {
            "handle": "S2",
            "title": "案件材料第一段",
            "text": "双方于2024年签订合同。",
            "locator": {"line_start": 1},
        },
    ],
}


def test_builtin_extractive_uses_only_sources_and_never_calls_a_model():
    def unexpected_request(_request):
        raise AssertionError("规则生成器不应发起外部请求")

    generator = SummaryGenerator(
        SummarySettings(provider="builtin-extractive"),
        transport=httpx.MockTransport(unexpected_request),
    )
    client = TestClient(create_app(summary_generator=generator))

    response = client.post("/v1/summaries", json=PAYLOAD)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "builtin-extractive"
    assert body["actual_model"] is None
    assert body["used_handles"] == ["S1", "S2"]
    assert "处理民事纠纷，应当依照法律。" in body["body"]
    assert "双方于2024年签订合同。" in body["body"]
    assert "[[S1]]" in body["body"] and "[[S2]]" in body["body"]
    assert "未使用模型" in body["body"]
    assert body["warnings"]
    capabilities = client.get("/v1/capabilities").json()["summary"]
    assert capabilities["default_provider"] == "builtin-extractive"
    assert capabilities["providers"][0]["status"] == "available"


def test_openai_compatible_uses_independent_configuration_and_actual_model():
    calls = []

    def model_response(request):
        calls.append(request)
        content = json.dumps(
            {
                "body": "依据所选法条，合同效力需结合事实核对 [[S1]]",
                "used_handles": ["S1"],
                "warnings": ["仅供人工复核"],
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json={
                "model": "actual-model-v2",
                "choices": [{"message": {"role": "assistant", "content": content}}],
            },
        )

    settings = SummarySettings(
        provider="openai-compatible",
        openai_base_url="https://model.example/v1",
        openai_model="configured-model",
        openai_api_key="secret-key",
    )
    generator = SummaryGenerator(settings, transport=httpx.MockTransport(model_response))
    client = TestClient(create_app(summary_generator=generator))
    payload = {**PAYLOAD, "provider": "openai-compatible"}

    response = client.post("/v1/summaries", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "openai-compatible"
    assert body["actual_model"] == "actual-model-v2"
    assert body["used_handles"] == ["S1"]
    assert len(calls) == 1
    assert calls[0].url == "https://model.example/v1/chat/completions"
    assert calls[0].headers["Authorization"] == "Bearer secret-key"
    request_body = json.loads(calls[0].content)
    assert request_body["model"] == "configured-model"
    assert "secret-key" not in response.text


def test_openai_configuration_failure_does_not_fall_back_to_extractive():
    def unexpected_request(_request):
        raise AssertionError("配置不完整时不应请求模型")

    generator = SummaryGenerator(
        SummarySettings(
            provider="openai-compatible",
            openai_base_url="",
            openai_model="",
            openai_api_key="",
        ),
        transport=httpx.MockTransport(unexpected_request),
    )
    client = TestClient(create_app(summary_generator=generator))

    response = client.post(
        "/v1/summaries", json={**PAYLOAD, "provider": "openai-compatible"}
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "summary_configuration_error"
    assert "未生成摘要" in detail["message"]
    assert "规则摘录草稿" not in response.text


def test_model_service_failure_and_unknown_citations_are_structured_errors():
    failing = SummaryGenerator(
        SummarySettings(
            provider="openai-compatible",
            openai_base_url="https://model.example/v1",
            openai_model="configured-model",
            openai_api_key="secret-key",
            fallback_to_extractive=False,
        ),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(500, text="secret-key internal failure")
        ),
    )
    client = TestClient(create_app(summary_generator=failing))
    response = client.post(
        "/v1/summaries", json={**PAYLOAD, "provider": "openai-compatible"}
    )
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "summary_generation_failed"
    assert "secret-key" not in response.text
    assert "规则摘录草稿" not in response.text

    def unknown_citation(_request):
        content = json.dumps(
            {"body": "伪造引用 [[S99]]", "used_handles": ["S99"], "warnings": []}
        )
        return httpx.Response(
            200,
            json={"model": "actual-model", "choices": [{"message": {"content": content}}]},
        )

    invalid = SummaryGenerator(
        SummarySettings(
            provider="openai-compatible",
            openai_base_url="https://model.example/v1",
            openai_model="configured-model",
            openai_api_key="secret-key",
        ),
        transport=httpx.MockTransport(unknown_citation),
    )
    invalid_client = TestClient(create_app(summary_generator=invalid))
    rejected = invalid_client.post(
        "/v1/summaries", json={**PAYLOAD, "provider": "openai-compatible"}
    )
    assert rejected.status_code == 502
    assert rejected.json()["detail"]["code"] == "summary_generation_failed"


def test_connection_failure_falls_back_to_extractive():
    """模型不可达时自动降级为规则摘录，响应带 degraded 标记。"""
    def connect_error(_request):
        raise httpx.ConnectError("connection refused")

    settings = SummarySettings(
        provider="openai-compatible",
        openai_base_url="https://unreachable.example/v1",
        openai_model="test-model",
        openai_api_key="key",
        fallback_to_extractive=True,
    )
    generator = SummaryGenerator(settings, transport=httpx.MockTransport(connect_error))
    client = TestClient(create_app(summary_generator=generator))

    response = client.post("/v1/summaries", json={**PAYLOAD, "provider": "openai-compatible"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["degraded"] is True
    assert body["provider"] == "builtin-extractive"
    assert body["actual_model"] is None
    assert any("降级" in w for w in body["warnings"])
    assert "[[S1]]" in body["body"]


def test_connection_failure_no_fallback_returns_503():
    """fallback_to_extractive=False 时连接失败返回 503。"""
    def connect_error(_request):
        raise httpx.ConnectError("connection refused")

    settings = SummarySettings(
        provider="openai-compatible",
        openai_base_url="https://unreachable.example/v1",
        openai_model="test-model",
        openai_api_key="key",
        fallback_to_extractive=False,
    )
    generator = SummaryGenerator(settings, transport=httpx.MockTransport(connect_error))
    client = TestClient(create_app(summary_generator=generator))

    response = client.post("/v1/summaries", json={**PAYLOAD, "provider": "openai-compatible"})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "summary_model_unavailable"


def test_5xx_falls_back_to_extractive():
    """模型返回 5xx 时降级为规则摘录。"""
    def server_error(_request):
        return httpx.Response(503, text="model overloaded")

    settings = SummarySettings(
        provider="openai-compatible",
        openai_base_url="https://model.example/v1",
        openai_model="test-model",
        openai_api_key="key",
        fallback_to_extractive=True,
    )
    generator = SummaryGenerator(settings, transport=httpx.MockTransport(server_error))
    client = TestClient(create_app(summary_generator=generator))

    response = client.post("/v1/summaries", json={**PAYLOAD, "provider": "openai-compatible"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["degraded"] is True
    assert any("503" in w for w in body["warnings"])


def test_4xx_does_not_fallback():
    """模型返回 4xx 时不降级，返回 502。"""
    def client_error(_request):
        return httpx.Response(401, text="unauthorized")

    settings = SummarySettings(
        provider="openai-compatible",
        openai_base_url="https://model.example/v1",
        openai_model="test-model",
        openai_api_key="wrong-key",
        fallback_to_extractive=True,
    )
    generator = SummaryGenerator(settings, transport=httpx.MockTransport(client_error))
    client = TestClient(create_app(summary_generator=generator))

    response = client.post("/v1/summaries", json={**PAYLOAD, "provider": "openai-compatible"})

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "summary_generation_failed"


def test_summary_route_uses_internal_token(tmp_path, monkeypatch):
    monkeypatch.setenv("SLP_CAPABILITY_ADAPTER_TOKEN", "internal-secret")
    monkeypatch.setenv("SLP_WORKBENCH_MATERIAL_ROOT", str(tmp_path))
    monkeypatch.setenv("SLP_SUMMARY_PROVIDER", "builtin-extractive")
    client = TestClient(create_app())

    assert client.post("/v1/summaries", json=PAYLOAD).status_code == 401
    authorized = client.post(
        "/v1/summaries",
        json=PAYLOAD,
        headers={"X-SLP-Internal-Token": "internal-secret"},
    )
    assert authorized.status_code == 200
    assert "internal-secret" not in authorized.text
