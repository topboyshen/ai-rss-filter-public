"""Disposable real Git sources copied from read-only mounted public code."""
from pathlib import Path
import subprocess


def isolated_git_source(destination, origin, *, files=None):
    destination, origin = Path(destination), Path(origin)
    destination.mkdir(mode=0o700)
    if files is None:
        directories = ("scripts", "services", "core", "db", "utils", "workers", "routes", "modules")
        files = {str(path.relative_to(origin)) for directory in directories
                 for path in (origin / directory).rglob("*.py") if "__pycache__" not in path.parts}
        files.update(("app.py", "worker_main.py", "sse_engine.py", "logging_bootstrap.py", "scoring_provenance.py",
                      "scoring_explanation.py", "gunicorn.conf.py", "config.yaml", "docker-compose.yml"))
        files.update(str(path.relative_to(origin)) for path in (origin / "ops/miniflux").iterdir()
                     if path.is_file() and path.suffix in {".json", ".patch"})
    for name in sorted(files):
        relative = Path(name)
        assert not relative.is_absolute() and ".." not in relative.parts and ".git" not in relative.parts
        source, target = origin / relative, destination / relative
        assert source.is_file() and not source.is_symlink()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    (destination / ".gitignore").write_text("__pycache__/\n*.pyc\nsecrets/\ndata/\nlogs/\n")
    for args in (("init", "-q"), ("add", "."),
                 ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                  "commit", "-qm", "isolated public source fixture")):
        subprocess.run(["git", "-C", str(destination), *args], check=True, capture_output=True, timeout=10)
    return destination
