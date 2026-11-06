import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from doonex.skills import SkillError, SkillRegistry, prepare_import


@pytest.fixture
def skill_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: sample\n---\nUse the reference.\n")
    (source / "refs").mkdir()
    (source / "refs/guide.txt").write_text("Read this reference.\n")
    (source / "scripts").mkdir()
    (source / "scripts/check.sh").write_text("#!/bin/sh\nprintf 'manual only'\n")
    (source / "scripts/check.sh").chmod(0o751)
    (source / "assets").mkdir()
    (source / "assets/diagram.bin").write_bytes(b"\x00\xffasset")
    (source / "empty").mkdir()
    return source


@pytest.fixture
def registry(tmp_path):
    return SkillRegistry(tmp_path / "private")


def test_private_snapshot_preserves_all_files_and_modes_and_survives_source_removal(
    skill_source, registry
):
    metadata = {
        "source": "https://example.invalid/skill",
        "license_status": "declared",
        "license_text": "MIT",
        "commit": "A" * 40,
        "scope": ["python", "coding", "python"],
        "permissions": ["workspace.read", "workspace.write"],
    }
    prepared = prepare_import(skill_source, metadata)
    record = registry.persist(prepared)
    assert len(record["version_id"]) == len(record["content_digest"]) == 64
    assert record["metadata"] == {
        **metadata,
        "commit": "a" * 40,
        "scope": ["coding", "python"],
    }
    assert record["visibility"] == "private"
    assert record["directories"] == ["assets", "empty", "refs", "scripts"]
    assert {entry["path"] for entry in record["files"]} == {
        "SKILL.md",
        "refs/guide.txt",
        "scripts/check.sh",
        "assets/diagram.bin",
    }
    context = registry.context(record["version_id"], record["report_sha256"])
    assert context.path.is_absolute()
    assert context.instructions == (skill_source / "SKILL.md").read_text()
    assert context.assets == (
        "assets/diagram.bin",
        "refs/guide.txt",
        "scripts/check.sh",
    )
    assert (context.path / "assets/diagram.bin").read_bytes() == b"\x00\xffasset"
    assert stat.S_IMODE((context.path / "scripts/check.sh").stat().st_mode) == 0o751
    for path in [
        registry.home,
        registry.root,
        registry.versions,
        context.path.parent,
        context.path,
        context.path / "empty",
        context.path / "refs",
    ]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE((context.path.parent / "report.json").stat().st_mode) == 0o600
    # The binding is JSON serializable and does not depend on the source directory.
    json.loads(json.dumps(record))
    skill_source.rename(skill_source.with_name("moved-source"))
    assert registry.inspect(record["version_id"], record["report_sha256"])["valid"]
    assert registry.context(record["version_id"], record["report_sha256"]) == context


def test_reimport_is_idempotent_timestamps_do_not_change_identity(
    skill_source, registry
):
    first = registry.import_local(skill_source, {})
    os.utime(skill_source / "SKILL.md", (1, 1))
    second = registry.import_local(skill_source, {})
    assert second == first
    assert len(list(registry.versions.iterdir())) == 1
    assert first["metadata"]["license_status"] == "unknown"
    assert first["metadata"]["license_text"] is None


@pytest.mark.parametrize("change", ["text", "asset", "mode", "metadata", "directory"])
def test_changes_create_new_versions_and_old_selection_stays_pinned(
    skill_source, registry, change
):
    first = registry.import_local(skill_source, {})
    metadata = {}
    if change == "text":
        (skill_source / "SKILL.md").write_text("Changed instructions\n")
    elif change == "asset":
        (skill_source / "assets/diagram.bin").write_bytes(b"changed")
    elif change == "mode":
        (skill_source / "scripts/check.sh").chmod(0o750)
    elif change == "metadata":
        metadata["scope"] = ["other"]
    else:
        (skill_source / "another-empty").mkdir()
    second = registry.import_local(skill_source, metadata)
    assert first["version_id"] != second["version_id"]
    assert len(list(registry.versions.iterdir())) == 2
    assert registry.inspect(first["version_id"], first["report_sha256"])["valid"]
    assert registry.inspect(second["version_id"], second["report_sha256"])["valid"]
    assert registry.context(
        first["version_id"], first["report_sha256"]
    ).instructions.endswith("Use the reference.\n")


@pytest.mark.parametrize(
    "tamper",
    [
        "text",
        "asset",
        "mode",
        "directory_mode",
        "extra_file",
        "extra_directory",
        "missing",
        "report",
        "report_mode",
        "metadata",
        "symlink",
        "parent_symlink",
        "extra_root",
    ],
)
def test_inspection_and_reimport_reject_tampering(
    skill_source, registry, tmp_path, tamper
):
    record = registry.import_local(skill_source, {})
    context = Path(record["context_path"])
    if tamper == "text":
        (context / "SKILL.md").write_text("Changed\n")
    elif tamper == "asset":
        (context / "assets/diagram.bin").write_bytes(b"changed")
    elif tamper == "mode":
        (context / "scripts/check.sh").chmod(0o755)
    elif tamper == "directory_mode":
        (context / "refs").chmod(0o755)
    elif tamper == "extra_file":
        (context / "new.txt").write_text("extra")
    elif tamper == "extra_directory":
        (context / "unexpected").mkdir()
    elif tamper == "missing":
        (context / "refs/guide.txt").unlink()
    elif tamper == "report":
        (context.parent / "report.json").write_text("{}")
    elif tamper == "report_mode":
        (context.parent / "report.json").chmod(0o644)
    elif tamper == "metadata":
        path = context.parent / "report.json"
        report = json.loads(path.read_text())
        report["metadata"]["scope"] = ["tampered"]
        path.write_text(json.dumps(report))
    elif tamper == "symlink":
        (context / "refs/guide.txt").unlink()
        (context / "refs/guide.txt").symlink_to(skill_source / "refs/guide.txt")
    elif tamper == "parent_symlink":
        context.rename(tmp_path / "moved-context")
        context.symlink_to(tmp_path / "moved-context", target_is_directory=True)
    else:
        (context.parent / "extra").mkdir()
    status = registry.inspect(record["version_id"], record["report_sha256"])
    assert status["valid"] is False and status["errors"]
    with pytest.raises(SkillError):
        registry.context(record["version_id"], record["report_sha256"])
    with pytest.raises(SkillError):
        registry.import_local(skill_source, {})


def test_report_requires_trusted_binding_and_version_cannot_escape_registry(
    skill_source, registry
):
    record = registry.import_local(skill_source, {})
    for version in ("../outside", "bad", "A" * 64):
        assert not registry.inspect(version, record["report_sha256"])["valid"]
    for expected in (None, "", "bad", "0" * 64):
        assert not registry.inspect(record["version_id"], expected)["valid"]


def test_import_and_context_do_not_execute_scripts_frontmatter_or_commands(
    skill_source, registry, tmp_path
):
    marker = tmp_path / "must-not-exist"
    instructions = f"---\ncommand: touch {marker}\nmcp_tools: [shell]\nhooks: [run]\n---\n!`touch {marker}`\n"
    (skill_source / "SKILL.md").write_text(instructions)
    (skill_source / "scripts/check.sh").write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    record = registry.import_local(
        skill_source, {"permissions": ["host.shell", "network"]}
    )
    context = registry.context(record["version_id"], record["report_sha256"])
    assert context.instructions == instructions
    assert context.metadata["permissions"] == ["host.shell", "network"]
    assert not marker.exists()


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.production",
        "credentials.json",
        "key.pem",
        ".ssh/key",
        "model.json",
        "service.json",
        ".codex/config.toml",
    ],
)
def test_secret_or_management_files_are_rejected(skill_source, name):
    target = skill_source / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic sensitive fixture")
    with pytest.raises(SkillError):
        prepare_import(skill_source, {})


@pytest.mark.parametrize(
    "kind",
    [
        "file_symlink",
        "directory_symlink",
        "source_symlink",
        "parent_symlink",
        "fifo",
        "privileged",
    ],
)
def test_links_special_files_and_privileged_modes_are_rejected(
    skill_source, tmp_path, kind
):
    source = skill_source
    if kind == "file_symlink":
        (source / "linked").symlink_to(source / "SKILL.md")
    elif kind == "directory_symlink":
        (source / "linked").symlink_to(source / "refs", target_is_directory=True)
    elif kind == "source_symlink":
        source = tmp_path / "linked"
        source.symlink_to(skill_source, target_is_directory=True)
    elif kind == "parent_symlink":
        parent = tmp_path / "linked"
        parent.symlink_to(tmp_path, target_is_directory=True)
        source = parent / skill_source.name
    elif kind == "fifo":
        os.mkfifo(source / "fifo")
    else:
        (source / "scripts/check.sh").chmod(0o4751)
    with pytest.raises(SkillError):
        prepare_import(source, {})


def test_private_storage_and_metadata_roots_cannot_be_imported(
    registry, skill_source, monkeypatch, tmp_path
):
    (registry.home / "SKILL.md").write_text("private fixture")
    with pytest.raises(SkillError, match="management"):
        registry.import_local(registry.home, {})
    with pytest.raises(SkillError, match="management"):
        prepare_import(tmp_path, {}, protected_roots=(registry.home,))
    monkeypatch.setenv("DOONEX_HOME", str(skill_source))
    with pytest.raises(SkillError, match="management"):
        prepare_import(skill_source, {})
    monkeypatch.setenv("DOONEX_HOME", str(tmp_path / "other"))
    monkeypatch.setenv("DOONEX_MODEL_CONFIG", str(skill_source / "secret.json"))
    with pytest.raises(SkillError, match="management"):
        prepare_import(skill_source, {})


@pytest.mark.parametrize("change", ["missing", "empty", "binary"])
def test_root_skill_must_be_nonempty_utf8_text(skill_source, change):
    path = skill_source / "SKILL.md"
    if change == "missing":
        path.unlink()
    elif change == "empty":
        path.write_text(" \n")
    else:
        path.write_bytes(b"\xff")
    with pytest.raises(SkillError):
        prepare_import(skill_source, {})


@pytest.mark.parametrize(
    "metadata",
    [
        {"other": "unsupported"},
        {"source": ""},
        {"source": "bad\nsource"},
        {"source": "https://user:secret@example.invalid/skill"},
        {"source": "https://example.invalid/skill?token=secret"},
        {"license_status": "invented"},
        {"license_status": []},
        {"license_status": "declared"},
        {"license_text": 42},
        {"commit": "HEAD"},
        {"scope": "python"},
        {"permissions": [""]},
        {"permissions": [42]},
        {"scope": ["x" * 513]},
        {"license_text": "x" * 65536},
    ],
)
def test_metadata_is_bounded_and_validated(skill_source, metadata):
    with pytest.raises(SkillError):
        prepare_import(skill_source, metadata)


@pytest.mark.parametrize(
    "limit",
    [
        "MAX_SKILL_ENTRIES",
        "MAX_SKILL_FILE_BYTES",
        "MAX_SKILL_TOTAL_BYTES",
        "MAX_SKILL_DEPTH",
    ],
)
def test_resource_limits_fail_closed(skill_source, monkeypatch, limit):
    from doonex import skills

    if limit == "MAX_SKILL_DEPTH":
        (skill_source / "refs/deeper").mkdir()
        (skill_source / "refs/deeper/file.txt").write_text("too deep")
    monkeypatch.setattr(skills, limit, 1)
    with pytest.raises(SkillError):
        prepare_import(skill_source, {})


def test_mutating_prepared_snapshot_is_rejected_before_publish(skill_source, registry):
    prepared = prepare_import(skill_source, {})
    prepared.metadata["source"] = "changed"
    with pytest.raises(SkillError, match="no longer matches"):
        registry.persist(prepared)
    assert list(registry.versions.iterdir()) == []
    prepared = prepare_import(skill_source, {})
    with pytest.raises(SkillError, match="no longer matches"):
        registry.persist(replace(prepared, content_digest="0" * 64))


def test_registry_symlink_and_nonprivate_index_are_rejected(tmp_path, skill_source):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(SkillError):
        SkillRegistry(linked)
    home = tmp_path / "home"
    registry = SkillRegistry(home)
    (registry.root / ".lock").write_text("")
    (registry.root / ".lock").chmod(0o644)
    with pytest.raises(SkillError):
        registry.import_local(skill_source, {})


@pytest.mark.parametrize("status", ["unknown", "declared", "verified", "restricted"])
def test_supported_license_declarations_remain_private(skill_source, registry, status):
    record = registry.import_local(
        skill_source,
        {
            "source": "local fixture",
            "license_status": status,
            "license_text": None
            if status == "unknown"
            else "Fixture license declaration",
            "commit": "b" * 64,
            "scope": ["executor"],
            "permissions": [],
        },
    )
    assert record["metadata"]["license_status"] == status
    assert record["metadata"]["commit"] == "b" * 64
    assert record["visibility"] == "private"


def test_failed_write_does_not_publish_partial_version(
    skill_source, registry, monkeypatch
):
    prepared = prepare_import(skill_source, {})
    original = Path.open

    def fail_asset(path, *args, **kwargs):
        if path.name == "diagram.bin" and args and args[0] == "xb":
            raise OSError("synthetic write failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_asset)
    with pytest.raises(SkillError, match="persist"):
        registry.persist(prepared)
    assert list(registry.versions.iterdir()) == []
    monkeypatch.setattr(Path, "open", original)
    record = registry.persist(prepared)
    assert registry.inspect(record["version_id"], record["report_sha256"])["valid"]


def test_concurrent_imports_publish_one_complete_version(skill_source, registry):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda _: registry.import_local(skill_source, {}), range(8))
        )
    assert all(record == results[0] for record in results)
    assert len(list(registry.versions.iterdir())) == 1
    assert registry.inspect(results[0]["version_id"], results[0]["report_sha256"])[
        "valid"
    ]


def test_source_mutation_during_import_is_rejected(skill_source, monkeypatch):
    from doonex import skills

    original = skills._files
    calls = 0

    def changed(path):
        nonlocal calls
        files = original(path)
        calls += 1
        if calls == 1:
            (path / "refs/guide.txt").write_text("changed during import")
        return files

    monkeypatch.setattr(skills, "_files", changed)
    with pytest.raises(SkillError, match="changed"):
        prepare_import(skill_source, {})


def test_oversized_report_does_not_publish_version(skill_source, registry, monkeypatch):
    from doonex import skills

    prepared = prepare_import(skill_source, {})
    monkeypatch.setattr(skills, "MAX_REPORT_BYTES", 1)
    with pytest.raises(SkillError, match="report size"):
        registry.persist(prepared)
    assert list(registry.versions.iterdir()) == []
