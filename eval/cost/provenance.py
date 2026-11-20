"""Standard-library provenance helpers; importing this never loads the gateway."""
from contextlib import contextmanager
import hashlib
from importlib import metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import tempfile
import zipfile


_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "node_modules", "results", "artifacts", "logs", "dist", "build",
}
_COMMIT = re.compile(r"[0-9a-f]{40,64}", re.I)
_SNAPSHOT = re.compile(r"(?:^|/)snapshots/([0-9a-f]{40,64})(?:/|$)", re.I)


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def git_output(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@contextmanager
def archived_baseline(repo, ref="2007299"):
    """Export a resolved commit to a temporary directory, without changing Git state."""
    revision = git_output(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    archive = subprocess.check_output(["git", "-C", str(repo), "-c", "core.autocrlf=false",
                                       "-c", "core.eol=lf", "archive", "--format=zip", revision])
    with tempfile.TemporaryDirectory(prefix="gateway-baseline-") as folder:
        root = Path(folder).resolve()
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            for member in zipped.infolist():
                relative = PurePosixPath(member.filename)
                if relative.is_absolute() or ".." in relative.parts or "\\" in member.filename:
                    raise ValueError(f"Unsafe archived path: {member.filename}")
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"Symlink is unsupported in benchmark archive: {member.filename}")
                if not (root / member.filename).resolve().is_relative_to(root):
                    raise ValueError(f"Archived path escapes its directory: {member.filename}")
            zipped.extractall(root)
        yield root, revision


def code_provenance(root, revision=None):
    """Hash executed code/config files, including uncommitted working-tree changes."""
    root = Path(root).resolve()
    files = [root / "main.py", root / "requirements.txt"]
    for folder in ("scheduler", "smart_ai_gateway", "demo"):
        path = root / folder
        if path.exists():
            # Prune before descent: .venv may contain thousands of Python files.
            for current, directories, names in os.walk(path, followlinks=False):
                directories[:] = [name for name in directories
                                  if name.lower() not in _EXCLUDED_DIRS
                                  and not (Path(current) / name / "pyvenv.cfg").is_file()
                                  and not (Path(current) / name).is_symlink()]
                files.extend(Path(current) / name for name in names
                             if Path(name).suffix in (".py", ".config"))
    hashes = {p.relative_to(root).as_posix(): sha256_file(p) for p in sorted(set(files)) if p.exists()}
    if revision is None:
        try:
            revision = git_output(root, "rev-parse", "HEAD")
        except subprocess.CalledProcessError:
            revision = None
    return {"git_commit": revision, "files_sha256": hashes, "content_sha256": digest(hashes)}


def loaded_model_provenance(cache):
    """Read resolved Hugging Face commits from already-loaded objects, without I/O.

    A model name or a mutable revision such as 'main' is not a resolved commit.
    Some local/custom models expose no revision; report that limitation rather
    than pretending that the configured name identifies their weights.
    """
    result = {}
    for role, attribute, setting in (("embedding", "embedding_model", "embedding_model"),
                                     ("verifier", "verifier", "verifier_model")):
        model = getattr(cache, attribute, None)
        revisions, visited = set(), set()
        pending = [(model, 0)] if model is not None else []
        while pending:
            node, depth = pending.pop()
            if id(node) in visited:
                continue
            visited.add(id(node))
            config = getattr(node, "config", None)
            kwargs = getattr(node, "init_kwargs", {})
            candidates = [getattr(node, "_commit_hash", None),
                          getattr(config, "_commit_hash", None),
                          getattr(node, "name_or_path", None),
                          getattr(config, "_name_or_path", None)]
            if isinstance(kwargs, dict):
                candidates.extend(kwargs.get(key) for key in ("_commit_hash", "revision", "name_or_path"))
            for value in candidates:
                if not isinstance(value, str):
                    continue
                if _COMMIT.fullmatch(value):
                    revisions.add(value.lower())
                match = _SNAPSHOT.search(value.replace("\\", "/"))
                if match:
                    revisions.add(match.group(1).lower())
            # SentenceTransformer keeps its transformer in _modules; a
            # CrossEncoder exposes model directly. Never traverse weight trees.
            if depth == 0:
                modules = getattr(node, "_modules", None)
                if isinstance(modules, dict):
                    pending.extend((child, 1) for child in modules.values())
            if depth < 2:
                for name in ("model", "auto_model", "tokenizer"):
                    child = getattr(node, name, None)
                    if child is not None:
                        pending.append((child, depth + 1))
        result[role] = {
            "configured_name": getattr(getattr(cache, "_cfg", None), setting, None),
            "resolved_revisions": sorted(revisions),
            "revision_status": "resolved" if revisions else ("unavailable" if model is not None else "not_loaded"),
        }
    return result


def runtime_versions():
    versions = {"python": platform.python_version(), "platform": platform.platform()}
    for name in ("fastapi", "starlette", "httpx", "qdrant-client", "sentence-transformers", "torch", "numpy"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions
