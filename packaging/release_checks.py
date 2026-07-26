from __future__ import annotations

import argparse
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=check,
        capture_output=True,
        text=True,
    )


def release_version(root: Path) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    locked = [
        package["version"]
        for package in tomllib.loads((root / "uv.lock").read_text())["package"]
        if package["name"] == "sqlalchemy-monetdb-adbc"
    ]
    if locked != [project]:
        raise ValueError(f"pyproject.toml and uv.lock versions disagree: project={project}, locked={locked}")
    return project


def verify_provenance(root: Path, tag: str, commit: str, main_ref: str) -> None:
    version = release_version(root)
    expected_tag = f"v{version}"
    if tag != expected_tag:
        raise ValueError(f"release tag {tag!r} does not match version {version!r}; expected {expected_tag!r}")
    tag_commit = _git(root, "rev-parse", f"{tag}^{{commit}}").stdout.strip()
    workflow_commit = _git(root, "rev-parse", f"{commit}^{{commit}}").stdout.strip()
    if tag_commit != workflow_commit:
        raise ValueError(f"tag {tag} peels to {tag_commit}, not workflow commit {workflow_commit}")
    if _git(root, "merge-base", "--is-ancestor", tag_commit, main_ref, check=False).returncode != 0:
        raise ValueError(f"release commit {tag_commit} is not reachable from protected {main_ref}")


def verify_artifacts(path: Path, version: str) -> None:
    prefix = f"sqlalchemy_monetdb_adbc-{version}"
    expected = {f"{prefix}-py3-none-any.whl", f"{prefix}.tar.gz"}
    artifacts = [artifact for artifact in path.iterdir() if not artifact.name.startswith(".")]
    found = {artifact.name for artifact in artifacts}
    if any(not artifact.is_file() for artifact in artifacts) or found != expected:
        raise ValueError(f"release artifact set mismatch: found={sorted(found)}, expected={sorted(expected)}")
    package = "sqlalchemy_monetdb_adbc"
    source_files = {
        f"{package}/{source.name}"
        for pattern in ("*.py", "py.typed")
        for source in (path.parent / package).glob(pattern)
    }
    with zipfile.ZipFile(path / f"{prefix}-py3-none-any.whl") as archive:
        wheel_files = set(archive.namelist())
    missing = source_files - wheel_files
    if missing:
        raise ValueError(f"wheel is missing package files: {sorted(missing)}")
    with tarfile.open(path / f"{prefix}.tar.gz") as archive:
        members = archive.getnames()
    forbidden = {"MONETDB_ISSUE.md", "REVIEW.md", "benchmarks"}
    leaked = [member for member in members if len(parts := Path(member).parts) > 1 and parts[1] in forbidden]
    if leaked:
        raise ValueError(f"sdist contains private or local-only files: {leaked}")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    provenance = subparsers.add_parser("provenance")
    provenance.add_argument("--root", type=Path, default=Path.cwd())
    provenance.add_argument("--tag", required=True)
    provenance.add_argument("--commit", required=True)
    provenance.add_argument("--main-ref", default="origin/main")
    artifacts = subparsers.add_parser("artifacts")
    artifacts.add_argument("path", type=Path)
    artifacts.add_argument("--version", required=True)
    args = parser.parse_args()
    if args.command == "provenance":
        verify_provenance(args.root, args.tag, args.commit, args.main_ref)
    else:
        verify_artifacts(args.path, args.version)


if __name__ == "__main__":
    main()
