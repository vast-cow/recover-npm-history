# npm History Recovery

Rebuild a Git history from the published releases of an npm package using `recover_npm_history.py`.

Each release becomes a commit containing the files from its npm tarball. This reconstructs release snapshots; the package's original source commits and unpublished files cannot be recovered from npm releases alone.

## Requirements

- Python 3.9 or later
- Git available on `PATH`
- Network access to `https://registry.npmjs.org` and the package tarball URLs

The script uses Python's standard library. Node.js, npm, and additional Python packages are not required. Private registry authentication is not implemented.

## Usage

Save `recover_npm_history.py` locally, then run:

```bash
python3 recover_npm_history.py \
    --package dummy-package \
    --output ./output \
    --git-user-name "Your Name" \
    --git-user-email "you@example.com"
```

Replace `dummy-package` with the actual npm package name. Scoped names such as `@your-scope/your-package` are supported.

Use a new or empty output directory for the first run. Keep the script outside that directory: release imports replace its working-tree contents.

To see all options:

```bash
python3 recover_npm_history.py --help
```

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--package` | `dummy-package` | npm package to recover; replace the placeholder with your target package. |
| `--output` | `./output` | Destination Git repository directory. |
| `--git-user-name` | `npm Registry Recovery` | Repository-local Git user name. |
| `--git-user-email` | `npm-recovery@localhost` | Repository-local Git user email. |
| `--delay` | `0.2` | Delay in seconds between release downloads; must be non-negative. |

## Generated history

The script reads registry metadata and sorts releases by publication timestamp, using the version string to break ties. New repositories use the `main` branch.

1. An empty root commit named `Initial commit` is created with both its author and committer dates set to **one day (24 hours) before the earliest available npm release**.
2. Each release produces a commit named `npm release <version>`, even if its contents match the preceding release.
3. Each release commit uses the npm publication timestamp for both Git dates, at Git's second-level precision. The commit body records the package name and publication time.

Commit identities use the configured repository-local name and email. No Git tags are created, and nothing is pushed to a remote.

Inspect the resulting history:

```bash
cd ./output
git log --oneline --reverse
git log --reverse --format=fuller
```

## Resuming

Run the same command again with the same package and output directory. The script skips committed releases when the existing commit sequence matches the beginning of the current npm release sequence.

The repository must start with `Initial commit`, followed only by the expected release commits, and its working tree must be clean. Existing commits, including the Initial commit's date, are not rewritten.

If an interruption leaves uncommitted files, inspect and resolve those changes before retrying. An interruption before the Initial commit is created may require starting with a new output directory. If registry metadata changes so that the existing history no longer matches, use a new output directory to rebuild it.

## Integrity and extraction

- Tarballs are checked against supported npm integrity metadata, with SHA-1 `shasum` as a fallback when no supported integrity entry is available.
- Missing verification metadata or a checksum mismatch stops recovery.
- Extraction rejects absolute paths, path traversal, `.git` targets, symbolic links, hard links, unsupported entry types, and duplicate file entries.
- Tarballs are validated and extracted to a temporary directory before the repository's working tree is replaced.
- Package installation scripts are never executed.

Only releases still available through registry metadata and downloadable tarballs can be recovered. Missing timestamps, missing tarball URLs, download failures, and unsupported archive entries stop the run rather than silently skipping a release.
