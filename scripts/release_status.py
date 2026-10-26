#!/usr/bin/env python3
"""Read-only release status: aggregate existing evidence, never infer.

It only summarises files that already exist (release state, gate
attestation, history, release lock).  It cannot recover release output
that was already lost with a disconnected session, and it never starts,
retries, or modifies a release.

Usage:
  scripts/release_status.py [--registry DIR] [--attest-dir DIR]
                            [--candidate-image TAG] [--candidate-commit SHA]

Without --candidate-* it reports current production only and does not
guess which release may be running.
"""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path

ATT_MAGIC = "ai-filter-release-test-attestation-v1"
HISTORY_FIELDS = ("ts", "kind", "tag", "digest", "status")


def read_release_state(registry: Path) -> dict:
    """current/previous state files; short or unreadable means unknown."""
    out = {}
    for name in ("current", "previous"):
        path = registry / f"{name}.state"
        try:
            lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
        except OSError:
            out[name] = {"status": "unknown", "reason": "状态文件缺失或不可读"}
            continue
        if len(lines) < 2:
            out[name] = {"status": "unknown", "reason": "状态文件不完整"}
        else:
            out[name] = {"status": "known", "tag": lines[0], "digest": lines[1]}
    return out


def read_attestation(attest_dir: Path, commit: str | None) -> dict:
    """File existence alone is not a pass; content and commit binding decide."""
    if not commit:
        return {"status": "not_requested"}
    state = attest_dir / f"{commit}.state"
    log = attest_dir / f"{commit}.log"
    if not state.is_file():
        return {"status": "no_evidence", "log": str(log) if log.is_file() else None}
    try:
        lines = [line.strip() for line in state.read_text().splitlines() if line.strip()]
    except OSError:
        return {"status": "unknown", "reason": "状态文件不可读"}
    if not lines or lines[0] != ATT_MAGIC:
        return {"status": "unknown", "reason": "状态文件格式不符"}
    if len(lines) < 2 or not lines[1].startswith(commit):
        return {"status": "unknown", "reason": "状态未绑定到指定提交"}
    result = lines[-1]
    if result == "passed":
        return {"status": "passed", "log": str(log) if log.is_file() else None}
    if result == "failed":
        return {"status": "failed", "log": str(log) if log.is_file() else None}
    return {"status": "unknown", "reason": f"未识别的结果 {result!r}"}


def read_history(registry: Path, tag: str | None) -> dict:
    """Stage rows for one tag; a missing row is never called running."""
    path = registry / "history.tsv"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"status": "unknown", "reason": "发布历史不可读"}
    rows = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= len(HISTORY_FIELDS):
            rows.append(dict(zip(HISTORY_FIELDS, parts)))
    if not tag:
        return {"status": "known", "total": len(rows)}
    stages = {}
    for row in rows:
        if row["tag"] == tag:
            stages[row["kind"]] = row
    return {"status": "known", "stages": stages, "completed": "deploy" in stages}


def read_lock(registry: Path) -> dict:
    """Lock occupancy only; never report the holder's command line."""
    path = registry / "release.lock"
    if not path.exists():
        return {"status": "free", "reason": "无锁文件"}
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return {"status": "unknown", "reason": "锁文件不可读"}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "holder": "unknown"}
        except OSError:
            return {"status": "unknown", "reason": "无法判定锁状态"}
        fcntl.flock(fd, fcntl.LOCK_UN)
        return {"status": "free"}
    finally:
        os.close(fd)


def compare_versions(state: dict, stage: dict | None) -> str:
    if not stage:
        return "unknown"
    digest = str(state.get("digest") or "")
    if not digest or state.get("status") != "known":
        return "unknown"
    return "same" if digest == stage["digest"] else "different"


def collect(registry: Path, attest_dir: Path, image: str | None, commit: str | None) -> dict:
    state = read_release_state(registry)
    image = image or (state["current"].get("tag") if state["current"].get("status") == "known" else None)
    history = read_history(registry, image)
    stage = (history.get("stages") or {}).get("deploy") if image else None
    return {
        "release_state": state,
        "gate": read_attestation(attest_dir, commit),
        "history": history,
        "lock": read_lock(registry),
        "version_match": compare_versions(state["current"], stage),
        "candidate_image": image,
        "candidate_commit": commit,
    }


def _fmt(data: dict) -> str:
    lines = []
    if not data["candidate_commit"] and not data["candidate_image"]:
        lines.append("未指定候选镜像/提交：以下仅为当前生产，未判断正在发布谁。")
    cur, prev = data["release_state"]["current"], data["release_state"]["previous"]
    lines.append(f"当前生产: {cur.get('tag', '未知')} {cur.get('digest', '')[:19]} ({cur['status']})")
    lines.append(f"上一版本: {prev.get('tag', '未知')} ({prev['status']})")
    lock = data["lock"]
    lines.append(f"发布锁: {lock['status']}" + ("（占用者未知，不显示命令行）" if lock["status"] == "busy" else ""))
    gate = data["gate"]
    mapping = {"passed": "通过", "failed": "失败", "no_evidence": "没有完成证据",
               "unknown": f"未知（{gate.get('reason', '')}）", "not_requested": "未指定提交，未判断"}
    lines.append(f"gate: {mapping.get(gate['status'], gate['status'])}" + (f" 日志={gate['log']}" if gate.get("log") else ""))
    history = data["history"]
    if history["status"] != "known":
        lines.append(f"发布阶段: 未知（{history.get('reason', '')}）")
    elif not data["candidate_image"]:
        lines.append(f"发布阶段: 未指定候选版本（历史共 {history['total']} 行）")
    elif not history["stages"]:
        lines.append("发布阶段: 尚无完成记录（未启动、执行中或异常退出都可能没有记录）")
    else:
        for kind in ("build", "deploy"):
            row = history["stages"].get(kind)
            if row:
                lines.append(f"  {kind}: {row['status']} @ {row['ts']}")
        if "deploy" not in history["stages"]:
            lines.append("  尚无 deploy 完成记录")
    lines.append(f"版本一致性: {data['version_match']}")
    lines.append("限制: 只汇总现有证据，不能恢复已丢失的发布输出。")
    return "\n".join(lines)


def _default_registry() -> Path:
    """Registry tied to script location, not invocation CWD."""
    return Path(__file__).resolve().parents[1] / "data" / "releases"


def _resolve_registry(raw: str | None) -> Path:
    """Explicit --registry keeps its original semantics (relative to CWD if
    relative, absolute if absolute); only the default is script-anchored."""
    if raw is None:
        return _default_registry()
    # Explicit relative keeps CWD-relative semantics only for lookup;
    # for display, absolute-resolution makes the report unambiguous.
    return Path(raw).resolve()


def _registry_diag(registry: Path) -> dict:
    out = {"resolved": str(registry)}
    if not registry.exists():
        out["status"] = "missing"
        out["reason"] = "registry directory does not exist"
        return out
    if not registry.is_dir():
        out["status"] = "missing"
        out["reason"] = "registry path is not a directory"
        return out
    for name in ("current.state", "previous.state"):
        p = registry / name
        if not p.exists():
            out[f"{name}.missing"] = True
            continue
        try:
            text = p.read_text()
        except OSError as e:
            out[f"{name}.unreadable"] = str(e)
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            out[f"{name}.incomplete"] = f"{len(lines)} non-empty lines, need >= 2"
    out.setdefault("status", "known")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only release status")
    parser.add_argument("--registry", default=None)
    parser.add_argument("--attest-dir", default="/run/ai-filter-release-gate/attestations")
    parser.add_argument("--candidate-image")
    parser.add_argument("--candidate-commit")
    args = parser.parse_args(argv)
    registry = _resolve_registry(args.registry)
    diag = _registry_diag(registry)
    print(_fmt(collect(registry, Path(args.attest_dir),
                       args.candidate_image, args.candidate_commit)))
    print(f"registry: {diag['resolved']}")
    if diag["status"] != "known":
        print(f"registry状态: {diag['status']}({diag.get('reason', '')})")
    for k, v in sorted(diag.items()):
        if k in ("resolved", "status", "reason"):
            continue
        print(f"registry-detail: {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
