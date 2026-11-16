import json

from fastapi.testclient import TestClient

from apps.capability_adapter.app import create_app


class FakeRetriever:
    def __init__(self):
        self.calls = []

    def _items(self):
        return [
            {
                "title": "示例标题",
                "law_name": "中华人民共和国民法典",
                "article": "第十条",
                "document": "处理民事纠纷，应当依照法律。",
                "rerank_score": 0.75,
                "metadata": {
                    "document_title": "中华人民共和国民法典",
                    "citation_article": "第十条",
                    "page_number": 3,
                    "chunk_id": "chunk-1",
                },
            }
        ], {"total_s": 0.01}

    def search_and_rerank(self, query, top_k=8):
        self.calls.append(("law", query, top_k))
        return self._items()

    def search_cases(self, query, top_k=8):
        self.calls.append(("guiding_cases", query, top_k))
        return self._items()

    def search_reference(self, query, top_k=8):
        self.calls.append(("reference", query, top_k))
        return self._items()


def test_law_trace_reports_only_observable_primary_query():
    retriever = FakeRetriever()
    client = TestClient(create_app(retriever=retriever))

    response = client.post("/v1/search/law", json={"query": "民法典第十条", "limit": 3})

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "law"
    assert body["status"] == "success"
    assert body["executed_queries"] == [
        {"query": "民法典第十条", "kind": "primary", "status": "submitted"}
    ]
    assert body["coverage"]["internal_query_trace"] == "unavailable"
    assert body["coverage"]["trace_limitations"]
    assert retriever.calls == [("law", "民法典第十条", 3)]
    result = body["results"][0]
    assert set(result) == {
        "title",
        "source_label",
        "excerpt",
        "content",
        "hit_reason",
        "match_type",
        "rank",
        "score",
        "locator",
        "source_ref",
        "metadata",
    }
    assert result["rank"] == 1
    assert result["score"] == 0.75
    assert result["locator"] == {"article": "第十条", "page": 3}
    assert result["source_ref"] == {"chunk_id": "chunk-1"}


def test_guiding_case_and_reference_have_uniform_contract():
    retriever = FakeRetriever()
    client = TestClient(create_app(retriever=retriever))

    for route, source in (
        ("/v1/search/guiding-cases", "guiding_cases"),
        ("/v1/search/reference", "reference"),
    ):
        response = client.post(route, json={"query": "办案指引", "limit": 2})
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "source",
            "status",
            "query",
            "filters",
            "executed_queries",
            "coverage",
            "results",
            "error",
        }
        assert body["source"] == source
        assert body["coverage"]["status"] == "completed"
        assert body["executed_queries"][0]["query"] == "办案指引"
        assert body["results"][0]["content"]


def test_zero_hits_is_completed_not_failed():
    class EmptyRetriever(FakeRetriever):
        def _items(self):
            return [], {}

    client = TestClient(create_app(retriever=EmptyRetriever()))
    body = client.post("/v1/search/law", json={"query": "无结果"}).json()
    assert body["status"] == "success"
    assert body["coverage"]["status"] == "completed"
    assert body["results"] == []
    assert body["error"] is None


def test_retrieval_failure_is_structured_and_does_not_leak_exception_details():
    class BrokenRetriever:
        def search_and_rerank(self, query, top_k=8):
            raise RuntimeError("/private/database failed with token secret")

    client = TestClient(create_app(retriever=BrokenRetriever()))
    response = client.post("/v1/search/law", json={"query": "测试"})
    body = response.json()
    assert body["status"] == "failed"
    assert body["coverage"]["status"] == "failed"
    assert body["error"]["code"] == "retrieval_failed"
    assert "/private" not in json.dumps(body)
    assert "secret" not in json.dumps(body)
