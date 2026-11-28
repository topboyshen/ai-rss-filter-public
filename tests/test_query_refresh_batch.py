import json
import sqlite3

import pytest

from scripts import query_refresh_batch


def _database(tmp_path):
    path = tmp_path / "cache.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE news_processing_logs ("
        "processing_id TEXT, refresh_id TEXT, outcome TEXT, score_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE llm_call_logs ("
        "processing_id TEXT, backend_name TEXT, purpose TEXT, "
        "success INTEGER, error_type TEXT)"
    )
    conn.executemany(
        "INSERT INTO news_processing_logs VALUES (?, ?, ?, ?)",
        [
            ("p1", "batch-a", "scored", "llm"),
            ("p2", "batch-a", "filtered", "llm"),
            ("p3", "batch-b", "scored", "llm"),
        ],
    )
    conn.executemany(
        "INSERT INTO llm_call_logs VALUES (?, ?, ?, ?, ?)",
        [
            ("p1", "kimi", "stage1", 1, ""),
            ("p2", "qwen", "stage1", 0, "APITimeoutError"),
        ],
    )
    conn.commit()
    conn.close()
    return path


def test_refresh_batch_summary_joins_only_this_batch(tmp_path, capsys):
    path = _database(tmp_path)

    assert query_refresh_batch.main(["batch-a", "--db", str(path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["refresh_id"] == "batch-a"
    assert payload["processing"] == [
        {"outcome": "filtered", "score_path": "llm", "count": 1},
        {"outcome": "scored", "score_path": "llm", "count": 1},
    ]
    assert payload["model_calls"] == [
        {"backend": "kimi", "purpose": "stage1", "success": True,
         "error_type": "", "count": 1},
        {"backend": "qwen", "purpose": "stage1", "success": False,
         "error_type": "APITimeoutError", "count": 1},
    ]


@pytest.mark.parametrize("refresh_id", ["", "../batch", "a" * 65, "batch a"])
def test_refresh_batch_rejects_unsafe_ids(tmp_path, refresh_id):
    path = _database(tmp_path)

    with pytest.raises(SystemExit):
        query_refresh_batch.main([refresh_id, "--db", str(path)])


def test_refresh_batch_rejects_a_relative_database_path(tmp_path):
    with pytest.raises(SystemExit):
        query_refresh_batch.main(["batch-a", "--db", "cache.db"])
