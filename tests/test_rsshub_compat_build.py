"""Offline build and exact compiled-handler regressions; no Docker or live requests."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "deploy" / "rsshub-compat"
PATCH = CONTEXT / "patch.mjs"
FIXTURES = json.loads((CONTEXT / "fixtures" / "candidate-modules.json").read_text())
NEWS = "wallstreetcn-news-BKjmaYn2.mjs"
LIVE = "wallstreetcn-live-Dn6YC-py.mjs"
CLS = "cls-telegraph-DFOzEo9d.mjs"
YICAI = "yicai-latest-CkhpqUUe.mjs"
ERROR = "not-found-CaYyolkS.mjs"
AFTER = {
    NEWS: "81465413a6be812cfa16a45002bf39abf1c5ae8cb484611d7bd113da66c6fde1",
    LIVE: "cac09bd189c96257c08b3feb3aabfb3c83004920ee8262867564ca16dec8289a",
    CLS: "c69222247c422099080f835c222a9a7f58053ef238b23a88df597b79ce25c9ce",
    YICAI: "5a5ea6b1acaae4e10f4ee98100d067680267a170f45394c780ede203f526f45d",
}


def _node(*args, payload=None):
    node = shutil.which("node")
    assert node, "RSSHub compatibility regressions require Node.js (also available on CI runners)"
    return subprocess.run(
        [node, *args],
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_compose_pins_the_validated_derived_artifact_without_runtime_patch_or_resource_relaxation():
    service = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["rsshub"]
    assert service["image"] == (
        "ghcr.io/topboyshen/ai-rss-filter-rsshub-compat@sha256:"
        "75e83ea300ba883de3d2d0877071c4bd54c46eabbd82c6a4cba30526ef795610"
    )
    assert service["mem_limit"] == "768m" and service["pids_limit"] == 256
    assert not any(key in service for key in ("entrypoint", "command", "volumes", "build"))
    assert service["environment"] == {"NODE_ENV": "production", "CACHE_TYPE": "memory", "TZ": "Asia/Shanghai"}
    assert service["networks"] == ["ai-net"]
    assert service["ports"] == ["172.17.0.1:1200:1200", "127.0.0.1:1200:1200"]
    assert "com.centurylinklabs.watchtower.enable=false" in service["labels"]


def _evaluate(script, payload=None):
    result = _node("--input-type=module", "-e", script, payload=payload)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _handler(name, **kwargs):
    result = _node(
        "--experimental-vm-modules",
        str(CONTEXT / "fixtures" / "offline-handler.mjs"),
        payload={"name": name, **kwargs},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _tree(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    for name, source in FIXTURES.items():
        (dist / name).write_text(source)
    return dist


def _patch_tree(dist, manifest):
    return _evaluate(
        f"import{{patchTree}}from{json.dumps(PATCH.as_uri())};"
        "try{console.log(JSON.stringify({ok:true,manifest:patchTree("
        f"{json.dumps(str(dist))},{json.dumps(str(manifest))},{json.dumps('a' * 64)}"
        ")}));}catch(e){console.log(JSON.stringify({ok:false,error:e.message}));}"
    )


def test_exact_modules_and_manifest(tmp_path):
    dist = _tree(tmp_path)
    manifest_path = tmp_path / "receipt.json"
    result = _patch_tree(dist, manifest_path)
    assert result["ok"]
    manifest = json.loads(manifest_path.read_text())
    assert manifest == result["manifest"]
    assert manifest["schema_version"] == 1
    assert manifest["compat_version"] == "v2-yicai-empty-cache"
    assert manifest["context_sha256"] == "a" * 64
    assert manifest["base_image"].endswith(
        "e7cd1bd8838b91f89c2ed56b0889f3754edc6aa1f1f5dac002e2e97e1079bd54"
    )
    assert manifest["base_revision"] == "f23ada8a636fbbb5f92d8e91558cf4e8ffaca66a"
    assert {Path(module["path"]).name for module in manifest["modules"]} == set(AFTER)
    for module in manifest["modules"]:
        name = Path(module["path"]).name
        assert module["after_sha256"] == AFTER[name]
        assert hashlib.sha256((dist / name).read_bytes()).hexdigest() == AFTER[name]
        assert module["before_sha256"] == hashlib.sha256(FIXTURES[name].encode()).hexdigest()
    assert (dist / ERROR).read_text() == FIXTURES[ERROR]
    assert manifest["dependencies"] == [{
        "path": f"/app/dist/{ERROR}",
        "sha256": hashlib.sha256(FIXTURES[ERROR].encode()).hexdigest(),
    }]


@pytest.mark.parametrize("name", [NEWS, LIVE, CLS, YICAI, ERROR])
@pytest.mark.parametrize("mode", ["changed", "missing", "symlink"])
def test_any_input_drift_fails_before_module_write(tmp_path, name, mode):
    dist = _tree(tmp_path)
    path = dist / name
    if mode == "changed":
        path.write_text(FIXTURES[name] + "\n")
    elif mode == "missing":
        path.unlink()
    else:
        original = tmp_path / "linked-source"
        original.write_text(FIXTURES[name])
        path.unlink()
        path.symlink_to(original)
    before = {item.name: item.read_bytes() for item in dist.iterdir() if item.is_file()}
    result = _patch_tree(dist, tmp_path / "receipt.json")
    assert not result["ok"]
    assert not (tmp_path / "receipt.json").exists()
    assert before == {item.name: item.read_bytes() for item in dist.iterdir() if item.is_file()}


def test_already_patched_is_not_silently_accepted(tmp_path):
    dist = _tree(tmp_path)
    assert _patch_tree(dist, tmp_path / "first.json")["ok"]
    assert not _patch_tree(dist, tmp_path / "second.json")["ok"]
    assert not (tmp_path / "second.json").exists()


@pytest.mark.parametrize("source,before", [("abc", "x"), ("abcabc", "abc"), ("abc", "")])
def test_replacement_requires_one_match(source, before):
    result = _evaluate(
        f"import{{replaceExactlyOnce}}from{json.dumps(PATCH.as_uri())};"
        "try{replaceExactlyOnce("
        f"{json.dumps(source)},{json.dumps(before)},'new');console.log(false);"
        "}catch{console.log(true)}"
    )
    assert result is True


def test_context_hash_checks_contents_and_rejects_symlinks(tmp_path):
    context = tmp_path / "context"
    context.mkdir()
    (context / "b").write_text("two")
    (context / "a").write_text("one")
    result = _node(str(PATCH), "--context-digest", str(context))
    assert result.returncode == 0
    expected = "".join(
        f"{name}\0{hashlib.sha256(content.encode()).hexdigest()}\n"
        for name, content in [("a", "one"), ("b", "two")]
    )
    assert result.stdout.strip() == hashlib.sha256(expected.encode()).hexdigest()
    (context / "a").write_text("changed")
    assert _node(str(PATCH), "--context-digest", str(context)).stdout != result.stdout
    (context / "link").symlink_to(context / "a")
    assert _node(str(PATCH), "--context-digest", str(context)).returncode != 0


def test_cli_wrong_context_identity_fails_without_writes(tmp_path):
    dist = _tree(tmp_path)
    result = _node(
        str(PATCH), "--dist", str(dist), "--manifest", str(tmp_path / "receipt.json"),
        "--context-root", str(CONTEXT), "--context-sha256", "0" * 64,
    )
    assert result.returncode == 1
    assert "context SHA256 mismatch" in result.stderr
    assert not (tmp_path / "receipt.json").exists()
    assert (dist / NEWS).read_text() == FIXTURES[NEWS]


def test_build_cli_validates_exact_copied_context_and_writes_receipt(tmp_path):
    copied = tmp_path / "context"
    shutil.copytree(CONTEXT, copied)
    script = copied / "patch.mjs"
    digest = _node(str(script), "--context-digest", str(copied))
    assert digest.returncode == 0
    dist = _tree(tmp_path)
    manifest = tmp_path / "receipt.json"
    result = _node(
        str(script), "--dist", str(dist), "--manifest", str(manifest),
        "--context-root", str(copied), "--context-sha256", digest.stdout.strip(),
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt == json.loads(manifest.read_text())
    assert receipt["context_sha256"] == digest.stdout.strip()
    assert hashlib.sha256((dist / NEWS).read_bytes()).hexdigest() == AFTER[NEWS]


def test_build_cli_rejects_changed_context_after_identity_is_chosen(tmp_path):
    copied = tmp_path / "context"
    shutil.copytree(CONTEXT, copied)
    script = copied / "patch.mjs"
    digest = _node(str(script), "--context-digest", str(copied)).stdout.strip()
    (copied / "README.md").write_text("changed context")
    dist = _tree(tmp_path)
    result = _node(
        str(script), "--dist", str(dist), "--manifest", str(tmp_path / "receipt.json"),
        "--context-root", str(copied), "--context-sha256", digest,
    )
    assert result.returncode == 1
    assert "context SHA256 mismatch" in result.stderr
    assert (dist / NEWS).read_text() == FIXTURES[NEWS]


@pytest.mark.parametrize("category", [None, "global"])
def test_default_and_global_restore_mixed_channel(category):
    args = {} if category is None else {"category": category}
    result = _handler(NEWS, **args)
    assert result["ok"]
    assert "channel=global-channel&accept=article&limit=25" in result["calls"][0]
    assert result["result"]["title"] == "华尔街见闻 - 资讯 - 最新"


@pytest.mark.parametrize("category", [
    "shares", "bonds", "commodities", "forex", "finance", "enterprise",
    "asset-manage", "tmt", "ai", "estate", "car", "medicine",
])
def test_other_supported_categories_keep_candidate_channel(category):
    result = _handler(NEWS, category=category)
    assert result["ok"]
    assert f"channel={category}&accept=article" in result["calls"][0]


@pytest.mark.parametrize("category", ["china", "unknown", "", "GLOBAL", "toString", "__proto__"])
def test_retired_or_unknown_category_fails_before_upstream(category):
    result = _handler(NEWS, category=category)
    assert not result["ok"]
    assert result["calls"] == []
    assert result["error_name"] == "NotFoundError"
    assert "Unsupported Wallstreetcn news category" in result["error_message"]


def test_mixed_items_keep_candidate_details_dates_and_filtering():
    rows = [
        {"resource_type": kind, "resource": {
            "id": identity, "uri": f"https://wallstreetcn.com/{path}/{identity}",
            "display_time": timestamp,
        }}
        for kind, path, identity, timestamp in [
            ("article", "articles", "11", 1792100000),
            ("live", "livenews", "22", 1792100100),
            ("ad", "ads", "33", 1792100200),
            ("article", "articles", "44", 1792100300),
        ]
    ]
    result = _handler(NEWS, items=rows, details={
        "11": {"title": "文章", "content": "正文", "source_name": "甲"},
        "22": {"content_text": "快讯", "content": "快讯正文", "content_more": "补充"},
    })
    assert result["ok"]
    items = result["result"]["item"]
    assert [item["guid"] for item in items] == ["11", "22"]
    assert [item["title"] for item in items] == ["文章", "快讯"]
    assert [item["description"] for item in items] == ["正文", "快讯正文补充"]
    assert items[0]["pubDate"] != items[1]["pubDate"]
    assert any("/lives/22" in url for url in result["calls"])
    assert not any("/33" in url for url in result["calls"])


def test_wallstreetcn_live_uses_the_current_api_host():
    result = _handler(
        LIVE,
        category="a-stock",
        items=[{
            "score": 1,
            "uri": "https://wallstreetcn.com/livenews/1",
            "title": "标题",
            "content_text": "正文",
            "display_time": 1792100000,
        }],
    )
    assert result["ok"], result
    assert result["calls"][0].startswith(
        "https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=a-stock-channel"
    )
    assert result["result"]["title"] == "华尔街见闻 - 实时快讯 - A股"


@pytest.mark.parametrize("title,content", [("标题", "正文"), ("", "没有标题的正文")])
def test_bare_cls_restores_empty_link_and_serializer_title_fallback(title, content):
    result = _handler(CLS, items=[{
        "id": 123, "title": title, "content": content, "ctime": 1792100000,
    }])
    assert result["ok"]
    item = result["result"]["item"][0]
    assert item["title"] == (title or content)
    assert not item.get("link")
    assert "guid" not in item  # The unchanged real RSS serializer chooses the normalized title.
    assert item["description"] == content
    assert item["pubDate"].endswith("Z")
    assert result["calls"][0]["url"].endswith("/api/cache")


def test_bare_cls_preserves_real_upstream_shareurl():
    result = _handler(CLS, items=[{
        "id": 123, "title": "真实文章", "content": "正文", "ctime": 1792100000,
        "shareurl": "https://www.cls.cn/detail/real-link",
    }])
    assert result["ok"]
    assert result["result"]["item"][0]["link"] == "https://www.cls.cn/detail/real-link"


@pytest.mark.parametrize("category", ["watch", "announcement", "red", "remind", "fund", "hk", "hk_us"])
@pytest.mark.parametrize("shareurl", ["", "https://www.cls.cn/detail/shared"])
def test_categorized_cls_links_and_request_are_unchanged(category, shareurl):
    args = {"category": category, "items": [{
        "id": 123, "title": "分类标题", "content": "正文", "ctime": 1792100000,
        "shareurl": shareurl,
    }]}
    before = _handler(CLS, unpatched=True, **args)
    after = _handler(CLS, **args)
    assert before == after
    assert after["result"]["item"][0]["link"] == (shareurl or "https://www.cls.cn/detail/123")


def test_yicai_latest_rejects_empty_items_before_returning_a_cacheable_feed():
    before = _handler(YICAI, unpatched=True, items=[])
    after = _handler(YICAI, items=[])
    assert before["ok"] and before["result"]["item"] == []
    assert not after["ok"] and after["error_name"] == "Error"
    assert after["error_message"] == "this route is empty, please check the original site or create an issue"
    assert after["calls"] == before["calls"] == ["https://www.yicai.com/api/ajax/getlatest?page=1&pagesize=30"]


def test_yicai_latest_preserves_items_limits_and_upstream_errors():
    items = [{"title": "Example article", "link": "https://example.invalid/item/1", "description": "Body"}]
    result = _handler(YICAI, limit=1, items=items)
    assert result == _handler(YICAI, unpatched=True, limit=1, items=items)
    assert result["ok"] and result["result"]["item"] == items
    assert result["calls"] == ["https://www.yicai.com/api/ajax/getlatest?page=1&pagesize=1"]
    error = _handler(YICAI, upstreamError="Invalid feed response")
    assert error == _handler(YICAI, unpatched=True, upstreamError="Invalid feed response")
    assert not error["ok"] and error["error_name"] == "TypeError"


def test_dockerfile_keeps_runtime_and_only_uses_pinned_base():
    text = (CONTEXT / "Dockerfile").read_text()
    instructions = [line.strip().split()[0].upper() for line in text.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    assert instructions.count("FROM") == 1
    assert not {"CMD", "ENTRYPOINT", "USER", "VOLUME", "EXPOSE"}.intersection(instructions)
    assert "FROM diygod/rsshub@sha256:e7cd1bd8838b91f89c2ed56b0889f3754edc6aa1f1f5dac002e2e97e1079bd54" in text
    assert "--context-sha256" in text
    assert "io.ai-filter.rsshub-compat.context-sha256" in text
    assert "git" not in text.lower()
