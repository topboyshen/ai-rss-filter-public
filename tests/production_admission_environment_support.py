"""Real sealed synthetic files for host/child environment tests; no Docker."""
from contextlib import contextmanager
import os

from test_private_release_contract import fixture as private_fixture, save, seal
from scripts import private_release_contract as private
from services import admission_fence as gate


@contextmanager
def sealed_inputs(directory, *, source_root=None):
    directory.mkdir(mode=0o700)
    generator = private_fixture.__wrapped__(directory)
    f = next(generator)
    try:
        if source_root is not None:
            f["root"] = source_root
            f["review_data"].update(private.source_identity(source_root))
        f["manifest_data"]["services"]["ai-filter-api"]["environment"]["AI_FILTER_API_KEY"] = "${AI_FILTER_API_KEY:?}"
        save(f["manifest"], f["manifest_data"])
        raw = f["environment"].read_bytes() + b"AI_FILTER_API_KEY=synthetic-child-secret-0123456789\n"
        save(f["environment"], raw)
        f["review_data"].update(input_manifest_sha256=private.sha(f["manifest"].read_bytes()),
                                environment_sha256=private.sha(raw))
        save(f["review"], f["review_data"])
        path = seal(f)
        contract, _manifest, digest = private.load(path, f["root"], require_fresh=False)
        permit = {"config": {"path": str(path.parent / "runtime.yaml"), "sha256": contract["files"]["runtime.yaml"],
                              "private_contract_path": str(path)},
                  "source": {"revision": contract["source_sha"], "tree": contract["source_tree"],
                             "manifest_sha256": contract["files"]["compose.json"], "private_contract_sha256": digest}}
        from core import config
        original_env, original_path = dict(os.environ), config.CONFIG_PATH
        try:
            os.environ.clear()
            os.environ.update(private.environment_values(raw))
            config.CONFIG_PATH = permit["config"]["path"]
            permit["config"]["effective_sha256"] = gate._hash(config.load_config())
        finally:
            os.environ.clear()
            os.environ.update(original_env)
            config.CONFIG_PATH = original_path
        yield f, permit
    finally:
        try:
            next(generator)
        except StopIteration:
            pass
