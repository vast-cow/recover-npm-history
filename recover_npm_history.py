#!/usr/bin/env python3
"""
Recover npm package release history into a Git repository.

Features:
- Download all available versions from the remote npm registry
- Sort releases by npm publication timestamp
- Create an empty Initial commit
- Create one Git commit per npm release
- Preserve npm publication timestamps as Git commit dates
- Configure repository-local Git user.name and user.email
- Verify downloaded tarballs using npm integrity metadata
- Resume interrupted recovery
- Never execute package installation scripts
- Never create Git tags

Requires:
    Python 3.9+
    Git

Example:
    python3 recover_npm_history.py \
        --package dummy-package \
        --output ./output \
        --git-user-name "Your Name" \
        --git-user-email "you@example.com"
"""

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_PACKAGE = "dummy-package"
REGISTRY = "https://registry.npmjs.org"
USER_AGENT = "npm-history-recovery/1.0"

COMMIT_PREFIX = "npm release "
INITIAL_COMMIT = "Initial commit"


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------

def download(url, retries=4):
    """Download bytes with exponential backoff."""

    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, */*",
                },
            )

            with urllib.request.urlopen(
                request,
                timeout=120,
            ) as response:
                return response.read()

        except (
            urllib.error.URLError,
            TimeoutError,
        ) as exc:
            if attempt == retries - 1:
                raise RuntimeError(
                    f"Download failed: {url}"
                ) from exc

            delay = 2 ** attempt
            print(f"  Retry in {delay}s: {exc}")
            time.sleep(delay)


# ------------------------------------------------------------
# Git
# ------------------------------------------------------------

def git(*args, cwd=None, env=None):
    """Execute a Git command."""

    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed:\n"
            f"{result.stderr}"
        )

    return result.stdout.strip()


def init_repo(repo, user_name, user_email, first_release):
    """Initialize and configure a Git repository."""

    repo.mkdir(parents=True, exist_ok=True)

    is_new = not (repo / ".git").exists()

    if is_new:
        if any(repo.iterdir()):
            raise RuntimeError(
                f"Output directory is not empty: {repo}"
            )

        git("init", cwd=repo)
        git(
            "symbolic-ref",
            "HEAD",
            "refs/heads/main",
            cwd=repo,
        )

    else:
        try:
            history = git(
                "log",
                "--format=%s",
                cwd=repo,
            )
        except RuntimeError:
            history = ""

        if history:
            subjects = history.splitlines()

            if subjects[-1] != INITIAL_COMMIT:
                raise RuntimeError(
                    "Repository does not have the expected "
                    "Initial commit."
                )

            if not all(
                subject.startswith(COMMIT_PREFIX)
                for subject in subjects[:-1]
            ):
                raise RuntimeError(
                    "Repository contains unrelated commits."
                )

    # Configure repository-local Git identity.
    git(
        "config",
        "--local",
        "user.name",
        user_name,
        cwd=repo,
    )

    git(
        "config",
        "--local",
        "user.email",
        user_email,
        cwd=repo,
    )

    print(f"Git user.name:  {user_name}")
    print(f"Git user.email: {user_email}")

    # Create an empty root commit before importing releases.
    if is_new:
        initial_date = first_release - timedelta(days=1)
        date = initial_date.strftime("%Y-%m-%dT%H:%M:%S%z")
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date

        for key in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        ):
            env.pop(key, None)

        git(
            "commit",
            "--allow-empty",
            "-m",
            INITIAL_COMMIT,
            cwd=repo,
            env=env,
        )

        print(
            "Created empty Initial commit dated "
            f"{initial_date.isoformat()}"
        )


def get_committed_versions(repo):
    """Return committed npm versions in chronological order."""

    try:
        output = git(
            "log",
            "--reverse",
            "--format=%s",
            cwd=repo,
        )
    except RuntimeError:
        return []

    subjects = output.splitlines()

    if not subjects or subjects[0] != INITIAL_COMMIT:
        raise RuntimeError(
            "Missing Initial commit"
        )

    versions = []

    for subject in subjects[1:]:
        if not subject.startswith(COMMIT_PREFIX):
            raise RuntimeError(
                f"Unexpected commit: {subject}"
            )

        versions.append(
            subject[len(COMMIT_PREFIX):]
        )

    return versions


def ensure_clean_worktree(repo):
    """Prevent accidental destruction of user changes."""

    status = git(
        "status",
        "--porcelain",
        "--untracked-files=all",
        cwd=repo,
    )

    if status:
        raise RuntimeError(
            "Git working tree is not clean. "
            "Commit or remove local changes before continuing."
        )


def clear_worktree(repo):
    """Remove all files except Git metadata."""

    for item in repo.iterdir():
        if item.name == ".git":
            continue

        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()


def commit_release(repo, version, published, package):
    """Create a Git commit for one npm release."""

    git("add", "-A", "-f", cwd=repo)

    env = os.environ.copy()

    # Preserve npm publication time.
    date = published.strftime("%Y-%m-%dT%H:%M:%S%z")

    env["GIT_AUTHOR_DATE"] = date
    env["GIT_COMMITTER_DATE"] = date

    # Honor repository-local Git identity.
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        env.pop(key, None)

    git(
        "commit",
        "--allow-empty",
        "-m",
        f"{COMMIT_PREFIX}{version}",
        "-m",
        (
            f"Package: {package}\n"
            f"Published: {published.isoformat()}"
        ),
        cwd=repo,
        env=env,
    )


# ------------------------------------------------------------
# npm metadata
# ------------------------------------------------------------

def fetch_metadata(package):
    """Fetch complete npm package metadata."""

    encoded = urllib.parse.quote(package, safe="")
    url = f"{REGISTRY}/{encoded}"

    print(f"Fetching npm metadata: {url}")

    return json.loads(download(url))


def parse_date(value):
    """Parse npm publication timestamp."""

    if not value:
        raise ValueError(
            "Missing npm publication timestamp"
        )

    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


def get_releases(metadata):
    """Return all releases sorted by publication time."""

    versions = metadata.get("versions", {})
    timestamps = metadata.get("time", {})

    releases = []

    for version, info in versions.items():
        published = timestamps.get(version)

        if not published:
            raise RuntimeError(
                f"Missing publication time for {version}"
            )

        dist = info.get("dist", {})
        tarball = dist.get("tarball")

        if not tarball:
            raise RuntimeError(
                f"Missing tarball URL for {version}"
            )

        releases.append({
            "version": version,
            "published": parse_date(published),
            "tarball": tarball,
            "integrity": dist.get("integrity"),
            "shasum": dist.get("shasum"),
        })

    releases.sort(
        key=lambda r: (
            r["published"],
            r["version"],
        )
    )

    return releases


# ------------------------------------------------------------
# Integrity verification
# ------------------------------------------------------------

def verify_tarball(data, release):
    """Verify tarball against npm integrity metadata."""

    integrity = release.get("integrity")
    shasum = release.get("shasum")

    if integrity:
        supported = {
            "sha512": hashlib.sha512,
            "sha384": hashlib.sha384,
            "sha256": hashlib.sha256,
            "sha1": hashlib.sha1,
        }

        for token in integrity.split():
            algorithm, separator, encoded = token.partition("-")

            if not separator or algorithm not in supported:
                continue

            encoded = encoded.split("?", 1)[0]

            expected = base64.b64decode(
                encoded,
                validate=True,
            )

            actual = supported[algorithm](data).digest()

            if actual != expected:
                raise ValueError(
                    f"Integrity mismatch: {release['version']}"
                )

            return

    if shasum:
        actual = hashlib.sha1(data).hexdigest()

        if actual.lower() != shasum.lower():
            raise ValueError(
                f"SHA1 mismatch: {release['version']}"
            )

        return

    raise RuntimeError(
        f"No integrity metadata for {release['version']}"
    )


# ------------------------------------------------------------
# Safe tarball extraction
# ------------------------------------------------------------

def safe_extract(data, destination):
    """
    Extract npm package tarball without executing code.

    Reject:
    - Absolute paths
    - Path traversal
    - Symlinks and hardlinks
    - Device files
    - Files targeting .git
    - Duplicate file paths
    """

    destination = Path(destination).resolve()
    seen = set()

    with tarfile.open(
        fileobj=io.BytesIO(data),
        mode="r:gz",
    ) as archive:

        for member in archive.getmembers():
            raw_name = member.name

            if raw_name.startswith("/"):
                raise ValueError(
                    f"Absolute path: {raw_name}"
                )

            parts = Path(raw_name).parts

            if not parts or parts[0] != "package":
                raise ValueError(
                    f"Unexpected npm path: {raw_name}"
                )

            relative = Path(*parts[1:])

            if not relative.parts:
                continue

            if any(
                part in ("..", ".git")
                for part in relative.parts
            ):
                raise ValueError(
                    f"Unsafe path: {raw_name}"
                )

            target = destination / relative

            if not target.resolve().is_relative_to(destination):
                raise ValueError(
                    f"Path traversal: {raw_name}"
                )

            if relative in seen and not member.isdir():
                raise ValueError(
                    f"Duplicate tar entry: {raw_name}"
                )

            seen.add(relative)

            if member.isdir():
                target.mkdir(
                    parents=True,
                    exist_ok=True,
                )

            elif member.isfile():
                target.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                source = archive.extractfile(member)

                if source is None:
                    raise ValueError(
                        f"Cannot extract: {raw_name}"
                    )

                with source, open(target, "wb") as output:
                    shutil.copyfileobj(source, output)

                mode = (
                    0o755
                    if member.mode & 0o111
                    else 0o644
                )

                os.chmod(target, mode)

            else:
                raise ValueError(
                    f"Unsupported tar entry: {raw_name}"
                )


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Recover all npm releases into a Git repository"
        )
    )

    parser.add_argument(
        "--package",
        default=DEFAULT_PACKAGE,
        help="npm package name",
    )

    parser.add_argument(
        "--output",
        default="./output",
        help="Output Git repository directory",
    )

    parser.add_argument(
        "--git-user-name",
        default="npm Registry Recovery",
        help="Repository-local Git user.name",
    )

    parser.add_argument(
        "--git-user-email",
        default="npm-recovery@localhost",
        help="Repository-local Git user.email",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Delay between downloads in seconds",
    )

    args = parser.parse_args()

    if args.delay < 0:
        parser.error("--delay must be non-negative")

    repo = Path(args.output).resolve()

    # Fetch remote metadata before initializing repository.
    metadata = fetch_metadata(args.package)
    releases = get_releases(metadata)

    print()
    print(f"Package:  {args.package}")
    print(f"Releases: {len(releases)}")
    print(f"Output:   {repo}")
    print()

    if not releases:
        raise RuntimeError(
            "No npm releases found"
        )

    init_repo(
        repo,
        args.git_user_name,
        args.git_user_email,
        releases[0]["published"],
    )

    completed = get_committed_versions(repo)

    expected_prefix = [
        release["version"]
        for release in releases[:len(completed)]
    ]

    if completed != expected_prefix:
        raise RuntimeError(
            "Existing Git history does not match "
            "the npm release sequence. "
            "Use a new output directory."
        )

    ensure_clean_worktree(repo)

    if completed:
        print(
            f"Resuming after {len(completed)} releases"
        )

    total = len(releases)

    for index, release in enumerate(
        releases[len(completed):],
        start=len(completed) + 1,
    ):
        version = release["version"]

        print(
            f"[{index}/{total}] "
            f"Downloading {version}"
        )

        data = download(release["tarball"])

        verify_tarball(data, release)

        # Validate and extract before modifying repository.
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)

            safe_extract(data, temp_path)

            clear_worktree(repo)

            for item in temp_path.iterdir():
                shutil.move(
                    str(item),
                    str(repo / item.name),
                )

        commit_release(
            repo,
            version,
            release["published"],
            args.package,
        )

        print(f"  Committed npm release {version}")

        if index < total:
            time.sleep(args.delay)

    print()
    print("=" * 60)
    print("Recovery completed")
    print("=" * 60)
    print(f"Repository: {repo}")
    print(f"Releases:   {total}")
    print()
    print("Inspect history:")
    print(f"  cd {repo}")
    print("  git log --oneline --reverse")
    print()
    print("No Git tags were created.")


if __name__ == "__main__":
    main()
