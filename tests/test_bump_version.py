"""Tests for `scripts/bump_version.py`.

The bump script is the single writer for a version declared in three files
that `TestVersionConsistency` requires to agree.  Its failure mode is not a
crash but a *partial* write — two files bumped, one not — which turns `main`
red after the commit has already landed.  These tests drive the script's
logic directly against a temporary copy of the three files rather than
through the workflow, which cannot run here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import bump_version as bv  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures: a miniature repo with the same shapes as the real files
# --------------------------------------------------------------------------

PYPROJECT = """\
[build-system]
requires = ["setuptools>=68"]

[project]
name = "meshhermes"
version = "1.0.6"
requires-python = ">=3.10"
dependencies = [
    "meshtastic>=2.3.0",
]
"""

PLUGIN_YAML = """\
# A comment that must survive the rewrite.
name: meshtastic
kind: platform
version: 1.0.6
requires_env:
  - name: MESHTASTIC_TRANSPORT
    description: "Not a version line."
"""

INIT_PY = '''\
"""Docstring."""

__all__ = ["register"]
__version__ = "1.0.6"
'''

CHANGELOG = """\
# Changelog

## [Unreleased]

## [1.0.6] - 2026-08-22

### Added
- A thing.

## [1.0.0] - 2026-08-18

Initial release.

[Unreleased]: https://github.com/antitree/meshhermes/compare/v1.0.6...HEAD
[1.0.6]: https://github.com/antitree/meshhermes/compare/v1.0.0...v1.0.6
[1.0.0]: https://github.com/antitree/meshhermes/releases/tag/v1.0.0
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "plugin.yaml").write_text(PLUGIN_YAML)
    (tmp_path / "__init__.py").write_text(INIT_PY)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    return tmp_path


def versions_in(root: Path) -> set:
    return set(bv.read_versions(root).values())


# --------------------------------------------------------------------------
# Version arithmetic
# --------------------------------------------------------------------------


class TestNextVersion:
    def test_patch_bump_is_the_automatic_one(self):
        # The user's "1.0.6 -> 1.0.7 after every PR".
        assert bv.next_version("1.0.6", "patch") == "1.0.7"

    def test_minor_bump_zeroes_the_patch(self):
        # The user's "1.0.7 -> 1.1.0", the manual release.
        assert bv.next_version("1.0.7", "minor") == "1.1.0"

    def test_patch_rolls_into_double_digits_not_into_minor(self):
        # The classic off-by-one-semver bug: 1.0.9 -> 1.0.10, never 1.1.0.
        assert bv.next_version("1.0.9", "patch") == "1.0.10"
        assert bv.next_version("1.0.99", "patch") == "1.0.100"

    def test_minor_rolls_into_double_digits_not_into_major(self):
        assert bv.next_version("1.9.3", "minor") == "1.10.0"

    def test_minor_from_an_already_zero_patch(self):
        assert bv.next_version("1.1.0", "minor") == "1.2.0"

    def test_major_is_left_alone_by_both_bumps(self):
        assert bv.next_version("2.4.5", "patch") == "2.4.6"
        assert bv.next_version("2.4.5", "minor") == "2.5.0"

    @pytest.mark.parametrize("bad", ["1.0", "1.0.6.1", "v1.0.6", "1.0.6rc1", "", "abc", "1.0.x"])
    def test_malformed_versions_are_rejected(self, bad):
        with pytest.raises(bv.BumpError):
            bv.next_version(bad, "patch")

    def test_leading_zeros_are_rejected(self):
        # 1.0.07 -> 1.0.8 would silently renumber the release.
        with pytest.raises(bv.BumpError):
            bv.next_version("1.0.07", "patch")

    def test_unknown_part_is_rejected(self):
        # Notably `major`: there is deliberately no major bump.
        with pytest.raises(bv.BumpError):
            bv.next_version("1.0.6", "major")


# --------------------------------------------------------------------------
# Reading the three files
# --------------------------------------------------------------------------


class TestReadVersions:
    def test_reads_all_three(self, repo):
        assert bv.read_versions(repo) == {
            "pyproject.toml": "1.0.6",
            "plugin.yaml": "1.0.6",
            "__init__.py": "1.0.6",
        }

    def test_current_version_collapses_the_three(self, repo):
        assert bv.current_version(repo) == "1.0.6"

    def test_a_missing_file_is_an_error(self, repo):
        (repo / "plugin.yaml").unlink()
        with pytest.raises(bv.BumpError, match="missing plugin.yaml"):
            bv.read_versions(repo)

    def test_a_file_without_a_version_is_an_error(self, repo):
        (repo / "__init__.py").write_text('__all__ = ["register"]\n')
        with pytest.raises(bv.BumpError, match="no version found"):
            bv.read_versions(repo)

    def test_refuses_to_bump_from_an_inconsistent_state(self, repo):
        # Drift already present: picking one file's number would overwrite
        # the others and hide exactly what TestVersionConsistency catches.
        (repo / "plugin.yaml").write_text(PLUGIN_YAML.replace("1.0.6", "1.0.4"))
        with pytest.raises(bv.BumpError, match="version mismatch"):
            bv.current_version(repo)

    def test_duplicate_version_lines_are_an_error(self, repo):
        (repo / "__init__.py").write_text(INIT_PY + '__version__ = "1.0.6"\n')
        with pytest.raises(bv.BumpError, match="expected 1"):
            bv.read_versions(repo)

    def test_indented_version_keys_are_not_matched(self, repo):
        # A `version:` nested under requires_env must not be picked up.
        (repo / "plugin.yaml").write_text(
            PLUGIN_YAML + "  - name: X\n    version: 9.9.9\n"
        )
        assert bv.read_versions(repo)["plugin.yaml"] == "1.0.6"


# --------------------------------------------------------------------------
# The three-file rewrite invariant
# --------------------------------------------------------------------------


class TestApplyVersion:
    def test_all_three_files_are_updated(self, repo):
        bv.apply_version("1.0.7", repo)
        assert versions_in(repo) == {"1.0.7"}

    def test_the_invariant_holds_after_a_patch_bump(self, repo):
        bv.apply_version(bv.next_version(bv.current_version(repo), "patch"), repo)
        assert bv.current_version(repo) == "1.0.7"

    def test_the_invariant_holds_after_a_minor_bump(self, repo):
        bv.apply_version(bv.next_version(bv.current_version(repo), "minor"), repo)
        assert bv.current_version(repo) == "1.1.0"

    def test_surrounding_syntax_is_preserved(self, repo):
        bv.apply_version("1.2.3", repo)
        toml = (repo / "pyproject.toml").read_text()
        yaml_text = (repo / "plugin.yaml").read_text()
        init = (repo / "__init__.py").read_text()
        assert 'version = "1.2.3"' in toml
        assert "requires-python = \">=3.10\"" in toml  # untouched
        assert "version: 1.2.3" in yaml_text
        assert "# A comment that must survive the rewrite." in yaml_text
        assert 'meshtastic>=2.3.0' in toml  # a dependency pin is not a version line
        assert '__version__ = "1.2.3"' in init
        assert '__all__ = ["register"]' in init

    def test_dry_run_writes_nothing(self, repo):
        bv.apply_version("9.9.9", repo, dry_run=True)
        assert versions_in(repo) == {"1.0.6"}

    def test_a_missing_file_aborts_before_any_write(self, repo):
        # The partial-write hazard: pyproject is rewritable, __init__ is
        # gone.  Nothing at all may change.
        (repo / "__init__.py").unlink()
        with pytest.raises(bv.BumpError):
            bv.apply_version("1.0.7", repo)
        assert bv.read_versions.__name__  # sanity
        assert "1.0.6" in (repo / "pyproject.toml").read_text()
        assert "1.0.7" not in (repo / "pyproject.toml").read_text()
        assert "1.0.7" not in (repo / "plugin.yaml").read_text()

    def test_an_unrewritable_file_aborts_before_any_write(self, repo):
        (repo / "plugin.yaml").write_text("name: meshtastic\nkind: platform\n")
        with pytest.raises(bv.BumpError):
            bv.apply_version("1.0.7", repo)
        assert "1.0.6" in (repo / "pyproject.toml").read_text()
        assert "1.0.6" in (repo / "__init__.py").read_text()

    def test_a_malformed_target_version_is_rejected(self, repo):
        with pytest.raises(bv.BumpError):
            bv.apply_version("not-a-version", repo)
        assert versions_in(repo) == {"1.0.6"}

    def test_repeated_bumps_compose(self, repo):
        for expected in ("1.0.7", "1.0.8", "1.0.9", "1.0.10"):
            bv.apply_version(bv.next_version(bv.current_version(repo), "patch"), repo)
            assert bv.current_version(repo) == expected


# --------------------------------------------------------------------------
# CHANGELOG handling
# --------------------------------------------------------------------------


class TestChangelogEntry:
    def test_adds_a_section_for_the_new_version(self, repo):
        assert bv.add_changelog_entry("1.0.7", repo) is True
        text = (repo / "CHANGELOG.md").read_text()
        assert "## [1.0.7]" in text

    def test_the_new_section_satisfies_the_registration_test(self, repo):
        # test_changelog_documents_the_current_version asserts exactly this,
        # so a bump that skipped it would leave main failing its own suite.
        bv.apply_version("1.0.7", repo)
        bv.add_changelog_entry("1.0.7", repo)
        version = bv.current_version(repo)
        assert f"## [{version}]" in (repo / "CHANGELOG.md").read_text()

    def test_the_entry_goes_under_unreleased_and_above_the_old_release(self, repo):
        bv.add_changelog_entry("1.0.7", repo)
        text = (repo / "CHANGELOG.md").read_text()
        assert text.index("## [Unreleased]") < text.index("## [1.0.7]") < text.index("## [1.0.6]")

    def test_link_references_are_updated(self, repo):
        bv.add_changelog_entry("1.0.7", repo)
        text = (repo / "CHANGELOG.md").read_text()
        assert "[Unreleased]: https://github.com/antitree/meshhermes/compare/v1.0.7...HEAD" in text
        assert "[1.0.7]: https://github.com/antitree/meshhermes/compare/v1.0.6...v1.0.7" in text
        # The old definitions survive.
        assert "[1.0.6]: https://github.com/antitree/meshhermes/compare/v1.0.0...v1.0.6" in text

    def test_an_existing_handwritten_section_is_left_alone(self, repo):
        # A human wrote real notes for this version; the stub must not
        # clobber them, and must not duplicate the heading.
        assert bv.add_changelog_entry("1.0.6", repo) is False
        text = (repo / "CHANGELOG.md").read_text()
        assert text.count("## [1.0.6]") == 1
        assert "### Added" in text

    def test_dry_run_writes_nothing(self, repo):
        bv.add_changelog_entry("1.0.7", repo, dry_run=True)
        assert "## [1.0.7]" not in (repo / "CHANGELOG.md").read_text()

    def test_a_missing_changelog_is_an_error(self, repo):
        (repo / "CHANGELOG.md").unlink()
        with pytest.raises(bv.BumpError, match="missing CHANGELOG.md"):
            bv.add_changelog_entry("1.0.7", repo)

    def test_a_changelog_without_unreleased_is_an_error(self, repo):
        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [1.0.6] - 2026-08-22\n\nx\n")
        with pytest.raises(bv.BumpError, match="Unreleased"):
            bv.add_changelog_entry("1.0.7", repo)


class TestExtractChangelogSection:
    def test_extracts_the_matching_section_only(self, repo):
        body = bv.extract_changelog_section(CHANGELOG, "1.0.6")
        assert "### Added" in body
        assert "A thing." in body
        # Must stop at the next heading rather than swallowing the file.
        assert "Initial release." not in body
        assert "## [1.0.0]" not in body

    def test_extracts_the_last_section(self, repo):
        body = bv.extract_changelog_section(CHANGELOG, "1.0.0")
        assert "Initial release." in body
        # The link-reference block trails the final section; it is included
        # but harmless, and the workflow only needs non-emptiness.
        assert body

    def test_a_missing_section_returns_empty(self):
        # The release workflow substitutes its own text rather than
        # publishing a blank release.
        assert bv.extract_changelog_section(CHANGELOG, "9.9.9") == ""

    def test_an_empty_section_returns_empty(self):
        text = "# Changelog\n\n## [1.0.7] - 2026-01-01\n\n\n## [1.0.6] - 2025-01-01\n\nx\n"
        assert bv.extract_changelog_section(text, "1.0.7") == ""

    def test_a_version_is_not_matched_as_a_prefix_of_another(self):
        text = "## [1.0.10] - 2026-01-01\n\nten\n\n## [1.0.1] - 2025-01-01\n\none\n"
        assert "ten" in bv.extract_changelog_section(text, "1.0.10")
        assert "one" in bv.extract_changelog_section(text, "1.0.1")
        assert "ten" not in bv.extract_changelog_section(text, "1.0.1")


# --------------------------------------------------------------------------
# CLI surface — this is what the workflows actually invoke
# --------------------------------------------------------------------------


class TestCli:
    def test_current_prints_the_version(self, repo, capsys):
        assert bv.main(["--current", "--root", str(repo)]) == 0
        assert capsys.readouterr().out.strip() == "1.0.6"

    def test_patch_bump_end_to_end(self, repo, capsys):
        assert bv.main(["patch", "--root", str(repo)]) == 0
        out = capsys.readouterr().out
        assert "old_version=1.0.6" in out
        assert "new_version=1.0.7" in out
        assert "tag=v1.0.7" in out
        assert bv.current_version(repo) == "1.0.7"
        assert "## [1.0.7]" in (repo / "CHANGELOG.md").read_text()

    def test_minor_bump_end_to_end(self, repo, capsys):
        assert bv.main(["minor", "--root", str(repo)]) == 0
        out = capsys.readouterr().out
        assert "new_version=1.1.0" in out
        assert "tag=v1.1.0" in out
        assert bv.current_version(repo) == "1.1.0"

    def test_dry_run_changes_nothing_on_disk(self, repo, capsys):
        assert bv.main(["patch", "--dry-run", "--root", str(repo)]) == 0
        assert "would bump 1.0.6 -> 1.0.7" in capsys.readouterr().out
        assert bv.current_version(repo) == "1.0.6"
        assert "## [1.0.7]" not in (repo / "CHANGELOG.md").read_text()

    def test_no_changelog_flag_skips_the_entry(self, repo):
        assert bv.main(["patch", "--no-changelog", "--root", str(repo)]) == 0
        assert bv.current_version(repo) == "1.0.7"
        assert "## [1.0.7]" not in (repo / "CHANGELOG.md").read_text()

    def test_a_broken_repo_exits_nonzero(self, repo, capsys):
        (repo / "plugin.yaml").unlink()
        assert bv.main(["patch", "--root", str(repo)]) == 1
        assert "error:" in capsys.readouterr().err

    def test_an_inconsistent_repo_exits_nonzero_without_writing(self, repo, capsys):
        (repo / "plugin.yaml").write_text(PLUGIN_YAML.replace("1.0.6", "1.0.4"))
        assert bv.main(["patch", "--root", str(repo)]) == 1
        assert "version mismatch" in capsys.readouterr().err
        assert '"1.0.6"' in (repo / "pyproject.toml").read_text()


# --------------------------------------------------------------------------
# Against the real repository
# --------------------------------------------------------------------------


class TestAgainstTheRealRepo:
    """The patterns must match this repo's actual files, not just fixtures."""

    def test_reads_the_real_versions(self):
        versions = bv.read_versions(PLUGIN_ROOT)
        assert set(versions) == {"pyproject.toml", "plugin.yaml", "__init__.py"}
        assert len(set(versions.values())) == 1

    def test_the_real_version_is_a_semver_triple(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", bv.current_version(PLUGIN_ROOT))

    def test_a_dry_run_bump_of_the_real_repo_succeeds(self):
        current = bv.current_version(PLUGIN_ROOT)
        bv.apply_version(bv.next_version(current, "patch"), PLUGIN_ROOT, dry_run=True)
        # Unchanged: a dry run must not touch the working tree.
        assert bv.current_version(PLUGIN_ROOT) == current

    def test_the_real_changelog_documents_the_current_version(self):
        changelog = (PLUGIN_ROOT / "CHANGELOG.md").read_text()
        body = bv.extract_changelog_section(changelog, bv.current_version(PLUGIN_ROOT))
        assert body, "release notes would be empty for the current version"
