"""Reviewed control directories within an unchanged legacy data bind.

This host-only helper reads metadata. It never provisions directories or treats
their existence as an inventory, migration, or activation authorization.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat


KEY = "control_subpaths"
KINDS = {"state", "freeze"}
FIELDS = {"source", "target", "anchor_source", "anchor_target", "directories"}


def require(condition):
    if not condition:
        raise ValueError("invalid reviewed control subpath")


def absolute(value):
    require(isinstance(value, str) and value.startswith("/") and not value.startswith("//") and value == os.path.normpath(value)
            and not any(char in value for char in "\x00\r\n,$"))
    return Path(value)


def directory_chain(anchor, source):
    """Open every component without following links; bind all descendants."""
    anchor, source = absolute(anchor), absolute(source)
    require(source != anchor and source.is_relative_to(anchor))
    require(1 <= len(source.relative_to(anchor).parts) <= 8)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    records = []
    current = Path("/")
    try:
        for component in source.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            current /= component
            if current == anchor or current.is_relative_to(anchor):
                info = os.fstat(descriptor)
                mode = stat.S_IMODE(info.st_mode)
                require(info.st_uid == os.geteuid() and not mode & 0o022)
                require(current == anchor or mode == 0o700)
                require(not records or info.st_dev == records[0]["device"])
                records.append({"path": str(current), "device": info.st_dev, "inode": info.st_ino,
                                "uid": info.st_uid, "gid": info.st_gid, "mode": mode})
    finally:
        os.close(descriptor)
    require(len(records) == len(source.relative_to(anchor).parts) + 1)
    return records


def capture(source, target, anchor_source, anchor_target="/app/data"):
    """Produce explicit review input from existing private directories only."""
    source, target = absolute(str(source)), absolute(str(target))
    anchor = absolute(str(anchor_source))
    require(anchor_target == "/app/data" and source != anchor and source.is_relative_to(anchor))
    require(target == Path(anchor_target) / source.relative_to(anchor))
    return {"source": str(source), "target": str(target), "anchor_source": str(anchor),
            "anchor_target": anchor_target, "directories": directory_chain(str(anchor), str(source))}


def check_record(value):
    require(isinstance(value, dict) and set(value) == FIELDS)
    records = value["directories"]
    require(isinstance(records, list) and 2 <= len(records) <= 9)
    for record in records:
        require(isinstance(record, dict) and set(record) == {"path", "device", "inode", "uid", "gid", "mode"}
                and isinstance(record["path"], str)
                and all(type(record[key]) is int and record[key] >= 0 for key in ("device", "inode", "uid", "gid", "mode")))
    actual = capture(value["source"], value["target"], value["anchor_source"], value["anchor_target"])
    require(value == actual)


def _covers(parent, child):
    return absolute(child).is_relative_to(absolute(parent))


def check_mounts(mounts, value):
    """Require the unchanged anchor and reject any intervening/shadow mount."""
    anchor = [mount for mount in mounts if mount["target"] == value["anchor_target"]]
    require(len(anchor) == 1 and anchor[0]["type"] == "bind" and not anchor[0].get("read_only", False)
            and anchor[0]["source"] == value["anchor_source"])
    for mount in mounts:
        if mount is anchor[0]:
            continue
        require(not _covers(mount["target"], value["target"])
                and not _covers(value["target"], mount["target"]))


def validate(manifest, review, app_services):
    """Optional narrow mode; normal private topology checks remain unchanged."""
    if KEY not in review:
        return
    mappings = review[KEY]
    require(isinstance(mappings, dict) and set(mappings) == set(app_services) and len(mappings) == 2)
    roles = set()
    shared = None
    for name, values in mappings.items():
        service = manifest["services"][name]
        env = service.get("environment", {})
        role = env.get("AI_FILTER_ROLE")
        require(role in {"web", "worker"} and role not in roles
                and env.get("AI_FILTER_PRODUCTION_STARTUP_OPERATION")
                and env.get("AI_FILTER_PRODUCTION_STARTUP_ROOT")
                and env.get("AI_FILTER_ADMISSION_MODE") == "enforced")
        roles.add(role)
        require(isinstance(values, dict) and set(values) == KINDS)
        require(shared is None or values == shared)
        shared = values
        for value in values.values():
            check_record(value)
            check_mounts(service.get("volumes", []), value)
            for old_name in review["service_mapping"][name]:
                for old in review["existing_topology"][old_name]:
                    check_mounts(old["mounts"], value)
        state, freeze = values["state"], values["freeze"]
        require(state["anchor_source"] == freeze["anchor_source"]
                and not _covers(state["source"], freeze["source"])
                and not _covers(freeze["source"], state["source"])
                and env.get("AI_FILTER_ADMISSION_ROOT") == freeze["target"])


def verify_actual(mappings, kind, source, target, mounts):
    """Only a reviewed state/freeze path may resolve through its parent bind."""
    require(kind in KINDS and isinstance(mappings, dict) and set(mappings) == KINDS)
    value = mappings[kind]
    check_record(value)
    require(str(source) == value["source"] and str(target) == value["target"])
    check_mounts(mounts, value)
