import re
import sqlite3

import pytest
import zstandard
from fastapi.testclient import TestClient

from apps.capability_adapter.app import create_app
from apps.capability_adapter.judgments import JudgmentEngine


CJK_RE = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+)")


def seg(text):
    return CJK_RE.sub(lambda match: " ".join(match.group(1)), text or "")


DDL = """
CREATE TABLE docs (
    id INTEGER PRIMARY KEY,
    ah TEXT, ajmc TEXT, fy TEXT, ssdq TEXT, ajlx TEXT, slcx TEXT,
    cprq TEXT, dsr TEXT, ay TEXT, fl TEXT, nr_zst BLOB, nr_len INTEGER, md5 TEXT
);
CREATE VIRTUAL TABLE docs_fts USING fts5(
    ah, fy, ssdq, ajlx, slcx, dsr, ay, fl, nr,
    content='', contentless_delete=0, tokenize='unicode61'
);
CREATE TABLE progress (k TEXT PRIMARY KEY, v TEXT);
INSERT INTO progress VALUES ('done', '1');
"""


def make_db(root, year, rows):
    path = root / "judgments_{}.db".format(year)
    connection = sqlite3.connect(path)
    connection.executescript(DDL)
    compressor = zstandard.ZstdCompressor()
    for row in rows:
        text = row["text"]
        values = (
            row["id"],
            row.get("case_number", "{}-1".format(row["id"])),
            row.get("title", "示例民事判决书"),
            row.get("court", "北京市测试人民法院"),
            row.get("region", "北京市"),
            row.get("case_type", "民事案件"),
            row.get("procedure", "一审"),
            row.get("date", "{}-01-01".format(year)),
            row.get("parties", "甲、乙"),
            row.get("cause", "民间借贷纠纷"),
            row.get("laws", "民法典"),
            compressor.compress(text.encode("utf-8")),
            len(text),
            "md5-{}".format(row["id"]),
        )
        connection.execute(
            "INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
        )
        fts_values = (
            row["id"],
            seg(values[1]),
            seg(values[3]),
            seg(values[4]),
            seg(values[5]),
            seg(values[6]),
            seg(values[8]),
            seg(values[9]),
            seg(values[10]),
            seg(text),
        )
        connection.execute(
            "INSERT INTO docs_fts(rowid,ah,fy,ssdq,ajlx,slcx,dsr,ay,fl,nr) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            fts_values,
        )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def judgment_client(tmp_path, monkeypatch):
    root = tmp_path / "judgments"
    root.mkdir()
    make_db(
        root,
        2020,
        [
            {"id": 1, "text": "民间借贷 利息 约定有效", "court": "北京市第一人民法院"},
            {
                "id": 2,
                "text": "买卖合同 违约金 利息",
                "court": "上海市第二人民法院",
                "cause": "买卖合同纠纷",
            },
        ],
    )
    make_db(
        root,
        2021,
        [
            {"id": 1, "text": "民间借贷 利息 司法解释", "court": "北京市第三人民法院"},
        ],
    )
    monkeypatch.setenv("SLP_JUDGMENT_DB_DIR", str(root))
    monkeypatch.setenv("SLP_JUDGMENT_SEARCH_WORKERS", "2")
    return TestClient(create_app()), root


def test_and_search_filters_and_full_source_read(judgment_client):
    client, _ = judgment_client
    response = client.post(
        "/v1/search/judgments",
        json={
            "query": "民间借贷 利息",
            "year_from": 2020,
            "year_to": 2021,
            "court": "北京市",
            "cause": "民间借贷",
            "limit": 10,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert len(body["results"]) == 2
    assert all(item["match_type"] == "and" for item in body["results"])
    assert all(item["metadata"]["court"].startswith("北京市") for item in body["results"])
    assert body["coverage"]["completed"] == 2
    assert body["coverage"]["empty"] == 0
    assert body["coverage"]["failed"] == 0
    assert body["coverage"]["timeout"] == 0
    assert body["executed_queries"]

    first = body["results"][0]
    source = client.post("/v1/judgments/source", json=first["source_ref"])
    assert source.status_code == 200
    full = source.json()
    assert full["status"] == "success"
    assert "民间借贷" in full["content"]
    assert full["source_ref"] == first["source_ref"]
    assert full["metadata"]["case_number"]


def test_and_zero_hits_falls_back_to_or_with_partial_match(judgment_client):
    client, _ = judgment_client
    body = client.post(
        "/v1/search/judgments",
        json={"query": "民间借贷 不存在词", "year_from": 2020, "year_to": 2020},
    ).json()

    assert body["results"]
    assert all(item["match_type"] == "or" for item in body["results"])
    assert all(item["partial_match"] for item in body["results"])
    database = body["coverage"]["databases"][0]
    assert database["match_type"] == "or"
    assert database["partial_match"] is True
    assert [item["mode"] for item in body["executed_queries"]] == ["and", "or"]


def test_successful_zero_hits_is_empty_coverage(judgment_client):
    client, _ = judgment_client
    body = client.post(
        "/v1/search/judgments",
        json={"query": "完全不存在", "year_from": 2021, "year_to": 2021},
    ).json()
    assert body["status"] == "success"
    assert body["results"] == []
    assert body["coverage"]["status"] == "completed"
    assert body["coverage"]["empty"] == 1


def test_one_broken_database_does_not_block_other_years(judgment_client):
    client, root = judgment_client
    broken = root / "judgments_2019.db"
    connection = sqlite3.connect(broken)
    connection.execute("CREATE TABLE progress (k TEXT PRIMARY KEY, v TEXT)")
    connection.execute("INSERT INTO progress VALUES ('done', '1')")
    connection.commit()
    connection.close()

    body = client.post(
        "/v1/search/judgments",
        json={"query": "民间借贷", "year_from": 2019, "year_to": 2021, "limit": 10},
    ).json()

    assert body["status"] == "partial"
    assert body["results"]
    assert body["coverage"]["failed"] == 1
    failed = next(item for item in body["coverage"]["databases"] if item["year"] == 2019)
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "database_failed"
    assert str(broken) not in response_text(body)


def response_text(body):
    import json

    return json.dumps(body, ensure_ascii=False)


def test_timeout_has_per_database_coverage_and_no_fake_executed_query(tmp_path):
    root = tmp_path / "judgments"
    root.mkdir()
    make_db(root, 2020, [{"id": 1, "text": "民间借贷 利息"}])
    engine = JudgmentEngine(root, deadline_s=0, workers=1)

    body = engine.search("民间借贷")

    assert body["status"] == "failed"
    assert body["results"] == []
    assert body["coverage"]["timeout"] == 1
    assert body["coverage"]["databases"][0]["status"] == "timeout"
    assert body["executed_queries"] == []
    assert body["error"]["code"] == "all_databases_unavailable"


def test_source_rejects_nonexistent_and_non_strict_references(judgment_client):
    client, _ = judgment_client
    assert client.post("/v1/judgments/source", json={"year": 1999, "rowid": 1}).status_code == 404
    for year in ("../../2020", "2020", 20201):
        response = client.post("/v1/judgments/source", json={"year": year, "rowid": 1})
        assert response.status_code == 422
    assert client.post(
        "/v1/judgments/source", json={"year": 2020, "rowid": "1 OR 1=1"}
    ).status_code == 422
    assert client.post("/v1/judgments/source", json={"year": 2020, "rowid": 0}).status_code == 422
