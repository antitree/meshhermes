#!/usr/bin/env python3
"""Bump the version in the three files that declare it, atomically.

The version lives in `pyproject.toml`, `plugin.yaml` and `__init__.py`, and
`TestVersionConsistency` asserts the three agree.  Nothing keeps them in
sync on its own, so a release that edits two of the three leaves `main`
red.  This script is the single writer: both release workflows call it, and
it either updates all three or writes nothing at all.

    python scripts/bump_version.py patch          # 1.0.6 -> 1.0.7
    python scripts/bump_version.py minor          # 1.0.7 -> 1.1.0
    python scripts/bump_version.py patch --dry-run
    python scripts/bump_version.py --current      # print and exit

The `patch` bump is what fires automatically after a PR merges; `minor` is
the manually-triggered release.  There is deliberately no `major` bump —
2.0.0 is a decision, not a button.

**No `tomllib`.**  The test matrix starts at Python 3.10, which does not
ship it (see the same workaround in `.github/workflows/tests.yml`), and
pulling in `tomli` for one version line is not worth a dependency.  The
rewrite is a targeted regex substitution on each file rather than a
parse-and-serialise round trip: `plugin.yaml` carries long explanatory
comments that `yaml.safe_dump` would silently discard, and `pyproject.toml`
would lose its comments the same way.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import re
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The version must be a plain three-part release number.  Pre-release and
#: build suffixes (1.0.7rc1, 1.0.7+dirty) are rejected rather than guessed
#: at: `TestVersionConsistency` requires `\d+\.\d+\.\d+`, so accepting one
#: here would only push the failure into CI.
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

VALID_PARTS = ("patch", "minor")


class VersionFile(NamedTuple):
    """One file that declares the version, and how to find it in there."""

    path: str
    #: Must have exactly one capturing group: the version itself.  The
    #: replacement rebuilds the line from the match, so the surrounding
    #: syntax (quoting, indentation, YAML vs TOML) is preserved verbatim.
    pattern: re.Pattern
    label: str


VERSION_FILES: Tuple[VersionFile, ...] = (
    VersionFile(
        "pyproject.toml",
        # Anchored to a line start so the `requires-python` line and any
        # dependency pin cannot match.
        re.compile(r'(?m)^(version\s*=\s*")(\d+\.\d+\.\d+)(")'),
        "pyproject.toml [project] version",
    ),
    VersionFile(
        "plugin.yaml",
        # Unquoted YAML scalar, at the top level (no leading indentation) —
        # the `requires_env:` entries are indented, so they cannot match.
        re.compile(r"(?m)^(version:\s*)(\d+\.\d+\.\d+)(\s*)$"),
        "plugin.yaml version",
    ),
    VersionFile(
        "__init__.py",
        re.compile(r'(?m)^(__version__\s*=\s*")(\d+\.\d+\.\d+)(")'),
        "__init__.py __version__",
    ),
)


class BumpError(RuntimeError):
    """A version could not be read, parsed, or rewritten.

    Raised rather than exiting so the unit tests can assert on the failure
    modes; `main()` turns it into a non-zero exit with the message.
    """


def parse_version(text: str) -> Tuple[int, int, int]:
    """Parse `major.minor.patch`, rejecting anything else.

    Leading zeros are rejected on purpose: `1.0.07` would round-trip to
    `1.0.8` and silently renumber the release.
    """
    match = SEMVER_RE.match(text.strip())
    if not match:
        raise BumpError(f"not a semver triple: {text!r}")
    parts = match.groups()
    for part in parts:
        if len(part) > 1 and part[0] == "0":
            raise BumpError(f"version part has a leading zero: {text!r}")
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def format_version(version: Tuple[int, int, int]) -> str:
    return "{}.{}.{}".format(*version)


def next_version(current: str, part: str) -> str:
    """Compute the next version.

    `patch` increments the third position (1.0.6 -> 1.0.7, and 1.0.9 ->
    1.0.10 rather than rolling into the minor).  `minor` increments the
    second and zeroes the third (1.0.7 -> 1.1.0).
    """
    if part not in VALID_PARTS:
        raise BumpError(f"unknown bump part {part!r}; expected one of {VALID_PARTS}")
    major, minor, patch = parse_version(current)
    if part == "patch":
        return format_version((major, minor, patch + 1))
    return format_version((major, minor + 1, 0))


def read_versions(root: Path = REPO_ROOT) -> Dict[str, str]:
    """Return {path: version} for each declaring file.

    Raises if a file is missing or holds no recognisable version, rather
    than defaulting — guessing a version here would tag the wrong release.
    """
    found: Dict[str, str] = {}
    for spec in VERSION_FILES:
        target = root / spec.path
        if not target.exists():
            raise BumpError(f"missing {spec.path}")
        matches = spec.pattern.findall(target.read_text())
        if not matches:
            raise BumpError(f"no version found in {spec.path} ({spec.label})")
        if len(matches) > 1:
            # Two version lines means the pattern is matching something it
            # should not; rewriting both would corrupt the file.
            raise BumpError(f"{spec.path} has {len(matches)} version lines; expected 1")
        found[spec.path] = matches[0][1]
    return found


def current_version(root: Path = REPO_ROOT) -> str:
    """The repo's single current version, asserting the three agree.

    Refusing to bump from an inconsistent state is deliberate: the bump
    would pick one file's number and quietly overwrite the others, hiding
    the drift that `TestVersionConsistency` exists to surface.
    """
    versions = read_versions(root)
    distinct = set(versions.values())
    if len(distinct) != 1:
        raise BumpError(f"version mismatch across files: {versions}")
    value = distinct.pop()
    parse_version(value)  # reject a malformed-but-consistent version
    return value


def _rewrite(text: str, spec: VersionFile, new: str) -> str:
    replaced, count = spec.pattern.subn(
        lambda m: f"{m.group(1)}{new}{m.group(3)}", text
    )
    if count != 1:
        raise BumpError(f"expected 1 rewrite in {spec.path}, made {count}")
    return replaced


def apply_version(new: str, root: Path = REPO_ROOT, dry_run: bool = False) -> List[str]:
    """Write `new` into all three files, or none of them.

    Every rewrite is computed and validated in memory first; only once all
    three have succeeded does anything touch the disk.  A malformed pattern
    or an unexpected file shape therefore fails with the tree unchanged,
    instead of leaving `main` with two files bumped and one not.
    """
    parse_version(new)
    staged: List[Tuple[Path, str]] = []
    for spec in VERSION_FILES:
        target = root / spec.path
        if not target.exists():
            raise BumpError(f"missing {spec.path}")
        staged.append((target, _rewrite(target.read_text(), spec, new)))

    if dry_run:
        return [spec.path for spec in VERSION_FILES]

    for target, content in staged:
        target.write_text(content)

    # Read back rather than trusting the writes: this is the invariant the
    # whole script exists to protect, and a partial write here is exactly
    # the failure that would turn main red.
    after = read_versions(root)
    if set(after.values()) != {new}:
        raise BumpError(f"post-write verification failed: {after}")
    return [spec.path for spec in VERSION_FILES]


# --------------------------------------------------------------------------
# CHANGELOG
# --------------------------------------------------------------------------

#: `## [1.0.6] - 2026-08-22`
_SECTION_RE_TEMPLATE = r"(?m)^## \[{version}\][^\n]*\n(.*?)(?=^## \[|\Z)"


def extract_changelog_section(changelog: str, version: str) -> str:
    """Return the body of this version's CHANGELOG section, or "".

    Used by the release workflow for the GitHub Release notes.  An absent
    or whitespace-only section returns the empty string so the caller can
    substitute something meaningful rather than publishing a blank release.
    """
    pattern = re.compile(_SECTION_RE_TEMPLATE.format(version=re.escape(version)), re.S)
    match = pattern.search(changelog)
    if not match:
        return ""
    return match.group(1).strip()


def add_changelog_entry(version: str, root: Path = REPO_ROOT, dry_run: bool = False) -> bool:
    """Insert a stub `## [version]` section under `## [Unreleased]`.

    This is not bookkeeping for its own sake: `test_changelog_documents_the
    _current_version` asserts `## [<current version>]` appears in the file,
    so a bump that skipped the changelog would leave `main` failing its own
    suite.  The entry is deliberately a stub pointing at the compare view —
    an automated bump has no way to know what the merged PR actually
    changed, and inventing prose would be worse than linking the diff.

    Returns False when the section already exists (a hand-written entry for
    this version wins; it is better than the stub).
    """
    path = root / "CHANGELOG.md"
    if not path.exists():
        raise BumpError("missing CHANGELOG.md")
    text = path.read_text()

    if re.search(r"(?m)^## \[%s\]" % re.escape(version), text):
        return False

    previous = _latest_released_version(text)
    today = _datetime.date.today().isoformat()
    repo = "https://github.com/antitree/meshhermes"
    entry = (
        f"## [{version}] - {today}\n\n"
        f"Automated patch release. See the "
        f"[full diff]({repo}/compare/v{previous}...v{version}) "
        f"for the changes this rolls up.\n\n"
    )

    anchor = re.search(r"(?m)^## \[Unreleased\][^\n]*\n+", text)
    if not anchor:
        raise BumpError("CHANGELOG.md has no '## [Unreleased]' heading to insert under")
    text = text[: anchor.end()] + entry + text[anchor.end() :]

    text = _update_link_refs(text, version, previous, repo)

    if not dry_run:
        path.write_text(text)
    return True


def _latest_released_version(changelog: str) -> str:
    """The newest `## [x.y.z]` heading, for the compare-link base."""
    match = re.search(r"(?m)^## \[(\d+\.\d+\.\d+)\]", changelog)
    if not match:
        raise BumpError("CHANGELOG.md has no released version heading")
    return match.group(1)


def _update_link_refs(text: str, version: str, previous: str, repo: str) -> str:
    """Repoint `[Unreleased]` and add this version's compare link.

    The changelog keeps link-reference definitions at the bottom; leaving
    them stale would make the new heading an unresolved link.
    """
    unreleased = f"[Unreleased]: {repo}/compare/v{version}...HEAD"
    text, count = re.subn(
        r"(?m)^\[Unreleased\]:[^\n]*$", unreleased, text, count=1
    )
    new_ref = f"[{version}]: {repo}/compare/v{previous}...v{version}"
    if count:
        # Slot the new definition directly after [Unreleased], keeping the
        # list in descending version order like the rest of the file.
        text = text.replace(unreleased, f"{unreleased}\n{new_ref}", 1)
    else:
        text = text.rstrip("\n") + f"\n{new_ref}\n"
    return text


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bump the version across pyproject.toml, plugin.yaml and __init__.py."
    )
    parser.add_argument(
        "part",
        nargs="?",
        choices=VALID_PARTS,
        help="patch: 1.0.6 -> 1.0.7 (automatic, after a PR merges). "
        "minor: 1.0.7 -> 1.1.0 (manual release).",
    )
    parser.add_argument(
        "--current",
        action="store_true",
        help="print the current version and exit without changing anything",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would change without writing",
    )
    parser.add_argument(
        "--no-changelog",
        action="store_true",
        help="skip inserting the CHANGELOG stub entry",
    )
    parser.add_argument(
        "--root",
        default=str(REPO_ROOT),
        help="repository root (default: the checkout this script lives in)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()

    try:
        current = current_version(root)
        if args.current:
            print(current)
            return 0
        if not args.part:
            parser.error("a bump part is required unless --current is given")

        new = next_version(current, args.part)
        apply_version(new, root, dry_run=args.dry_run)
        changed_changelog = (
            False if args.no_changelog else add_changelog_entry(new, root, dry_run=args.dry_run)
        )
    except BumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    prefix = "would bump" if args.dry_run else "bumped"
    print(f"{prefix} {current} -> {new} ({args.part})")
    for spec in VERSION_FILES:
        print(f"  {spec.path}")
    if changed_changelog:
        print("  CHANGELOG.md")

    # Machine-readable output for the workflows: `$GITHUB_OUTPUT` is fed
    # from these rather than re-parsing the human lines above.
    print(f"old_version={current}")
    print(f"new_version={new}")
    print(f"tag=v{new}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
