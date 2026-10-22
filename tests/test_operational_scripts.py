import os
from pathlib import Path
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RESTORE_SCRIPT = REPO_ROOT / "scripts" / "restore_db.sh"
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup_db.sh"
COMPOSE_HELPER = REPO_ROOT / "scripts" / "_compose.sh"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def _restore_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_marker = tmp_path / "docker-called"
    _write_executable(
        bin_dir / "docker",
        f"#!/bin/sh\ntouch '{docker_marker}'\nexit 99\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_ENDPOINT_URL": "http://localstack.test",
            "LOCAL_DIR": str(tmp_path / "downloads"),
            "_TS_STATE_FILE": str(tmp_path / "missing-deploy-state"),
        }
    )
    return env, docker_marker


def test_compose_helper_supports_empty_argument_array_under_nounset(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    _write_executable(
        bin_dir / "docker",
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" > '{docker_log}'\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "_TS_STATE_FILE": str(tmp_path / "missing-deploy-state"),
        }
    )

    result = subprocess.run(
        ["bash", "-c", f"set -u; . '{COMPOSE_HELPER}'; dc version"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert docker_log.read_text(encoding="utf-8").strip() == "compose version"


def test_backup_rejects_unknown_arguments_before_external_commands(tmp_path):
    env, docker_marker = _restore_env(tmp_path)
    aws_marker = tmp_path / "aws-called"
    _write_executable(
        tmp_path / "bin" / "aws",
        f"#!/bin/sh\ntouch '{aws_marker}'\nexit 99\n",
    )

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT), "--local-onyl"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "usage: backup_db.sh [--local-only]" in result.stderr
    assert not aws_marker.exists()
    assert not docker_marker.exists()


def test_backup_refuses_to_overwrite_existing_evidence(tmp_path):
    env, docker_marker = _restore_env(tmp_path)
    _write_executable(
        tmp_path / "bin" / "date",
        "#!/bin/sh\nprintf '20260101T000000Z\\n'\n",
    )
    _write_executable(
        tmp_path / "bin" / "git",
        "#!/bin/sh\nprintf 'deadbee\\n'\n",
    )
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    existing = backup_dir / "talentscope-talentscope-20260101T000000Z-deadbee.dump.gz"
    existing.write_bytes(b"existing evidence")
    env["LOCAL_DIR"] = str(backup_dir)

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT), "--local-only"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "refusing to overwrite existing backup evidence" in result.stderr
    assert existing.read_bytes() == b"existing evidence"
    assert not docker_marker.exists()


def test_backup_removes_partial_output_when_dump_fails(tmp_path):
    env, _ = _restore_env(tmp_path)
    backup_dir = tmp_path / "backups"
    env["LOCAL_DIR"] = str(backup_dir)

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT), "--local-only"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert list(backup_dir.iterdir()) == []


def test_restore_rejects_path_like_backup_keys_before_external_commands(tmp_path):
    env, docker_marker = _restore_env(tmp_path)
    aws_marker = tmp_path / "aws-called"
    _write_executable(
        tmp_path / "bin" / "aws",
        f"#!/bin/sh\ntouch '{aws_marker}'\nexit 99\n",
    )

    result = subprocess.run(
        ["bash", str(RESTORE_SCRIPT), "--key", "../escape.dump.gz", "--yes"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "Invalid backup key" in result.stderr
    assert not aws_marker.exists()
    assert not docker_marker.exists()


def test_restore_preserves_existing_local_recovery_evidence(tmp_path):
    env, docker_marker = _restore_env(tmp_path)
    aws_marker = tmp_path / "aws-called"
    _write_executable(
        tmp_path / "bin" / "aws",
        f"#!/bin/sh\ntouch '{aws_marker}'\nexit 99\n",
    )
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    existing = downloads / "talentscope-test.dump.gz"
    existing.write_bytes(b"existing recovery evidence")

    result = subprocess.run(
        ["bash", str(RESTORE_SCRIPT), "--key", existing.name, "--yes"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Refusing to overwrite existing local recovery evidence" in result.stderr
    assert existing.read_bytes() == b"existing recovery evidence"
    assert not aws_marker.exists()
    assert not docker_marker.exists()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--key"], "--key requires a backup name"),
        (["--key", "one.dump.gz", "--latest"], "choose exactly one"),
    ],
)
def test_restore_rejects_ambiguous_backup_selection_before_external_commands(
    tmp_path, arguments, message
):
    env, docker_marker = _restore_env(tmp_path)
    aws_marker = tmp_path / "aws-called"
    _write_executable(
        tmp_path / "bin" / "aws",
        f"#!/bin/sh\ntouch '{aws_marker}'\nexit 99\n",
    )

    result = subprocess.run(
        ["bash", str(RESTORE_SCRIPT), *arguments],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert message in result.stderr
    assert not aws_marker.exists()
    assert not docker_marker.exists()


def test_restore_latest_reports_empty_backup_bucket(tmp_path):
    env, docker_marker = _restore_env(tmp_path)
    _write_executable(tmp_path / "bin" / "aws", "#!/bin/sh\nexit 0\n")

    result = subprocess.run(
        ["bash", str(RESTORE_SCRIPT), "--latest", "--yes"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "No backups found" in result.stderr
    assert not docker_marker.exists()


@pytest.mark.parametrize(
    ("script", "arguments", "environment"),
    [
        (BACKUP_SCRIPT, ["--local-only"], {"PGDATABASE": "../outside"}),
        (
            RESTORE_SCRIPT,
            ["--key", "talentscope-test.dump.gz", "--yes"],
            {"PGUSER": "operator;drop_role"},
        ),
    ],
)
def test_database_tools_reject_unsafe_postgres_identifiers_before_external_commands(
    tmp_path, script, arguments, environment
):
    env, docker_marker = _restore_env(tmp_path)
    env.update(environment)
    aws_marker = tmp_path / "aws-called"
    _write_executable(
        tmp_path / "bin" / "aws",
        f"#!/bin/sh\ntouch '{aws_marker}'\nexit 99\n",
    )

    result = subprocess.run(
        ["bash", str(script), *arguments],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "must be PostgreSQL identifiers" in result.stderr
    assert not aws_marker.exists()
    assert not docker_marker.exists()


def test_restore_validates_gzip_before_touching_database(tmp_path):
    env, docker_marker = _restore_env(tmp_path)
    _write_executable(
        tmp_path / "bin" / "aws",
        """#!/bin/sh
for argument in "$@"; do
  case "$argument" in
    s3://*) ;;
    *.dump.gz) printf 'truncated archive' > "$argument" ;;
  esac
done
""",
    )

    result = subprocess.run(
        ["bash", str(RESTORE_SCRIPT), "--key", "talentscope-test.dump.gz", "--yes"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "not a valid gzip archive" in result.stderr
    assert not docker_marker.exists()


def test_restore_rejects_invalid_manifest_before_touching_database(tmp_path):
    env, docker_marker = _restore_env(tmp_path)
    valid_archive = tmp_path / "valid.dump.gz"
    subprocess.run(
        ["gzip", "-c"],
        input=b"valid archive payload",
        stdout=valid_archive.open("wb"),
        check=True,
    )
    env["VALID_ARCHIVE"] = str(valid_archive)
    _write_executable(
        tmp_path / "bin" / "aws",
        """#!/bin/sh
for argument in "$@"; do
  case "$argument" in
    "$LOCAL_DIR"/*.manifest.json) printf '{invalid json' > "$argument" ;;
    "$LOCAL_DIR"/*.dump.gz) cp "$VALID_ARCHIVE" "$argument" ;;
  esac
done
""",
    )

    result = subprocess.run(
        ["bash", str(RESTORE_SCRIPT), "--key", "talentscope-test.dump.gz", "--yes"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "backup manifest is invalid" in result.stderr
    assert not docker_marker.exists()


def test_restore_validates_pg_dump_archive_before_touching_database(tmp_path):
    env, _ = _restore_env(tmp_path)
    archive = tmp_path / "valid-gzip-invalid-dump.gz"
    subprocess.run(
        ["gzip", "-c"],
        input=b"not a PostgreSQL custom-format archive",
        stdout=archive.open("wb"),
        check=True,
    )
    env["VALID_ARCHIVE"] = str(archive)
    docker_log = tmp_path / "docker.log"
    destructive_marker = tmp_path / "destructive-command"
    _write_executable(
        tmp_path / "bin" / "aws",
        """#!/bin/sh
for argument in "$@"; do
  case "$argument" in
    "$LOCAL_DIR"/*.dump.gz) cp "$VALID_ARCHIVE" "$argument"; exit 0 ;;
  esac
done
exit 1
""",
    )
    _write_executable(
        tmp_path / "bin" / "docker",
        f"""#!/bin/sh
printf '%s\\n' "$*" >> '{docker_log}'
case "$*" in
  *"pg_restore --list"*) exit 1 ;;
  *) touch '{destructive_marker}'; exit 99 ;;
esac
""",
    )

    result = subprocess.run(
        ["bash", str(RESTORE_SCRIPT), "--key", "talentscope-test.dump.gz", "--yes"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "not a readable pg_dump archive" in result.stderr
    assert "pg_restore --list" in docker_log.read_text(encoding="utf-8")
    assert not destructive_marker.exists()
