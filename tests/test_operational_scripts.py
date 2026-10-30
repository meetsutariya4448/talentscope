import hashlib
import json
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


def _run_restore_with_manifest(tmp_path: Path, manifest: str):
    env, docker_marker = _restore_env(tmp_path)
    archive = tmp_path / "valid.dump.gz"
    subprocess.run(
        ["gzip", "-c"], input=b"archive payload", stdout=archive.open("wb"), check=True
    )
    manifest = manifest.replace("__BYTES__", str(archive.stat().st_size)).replace(
        "__SHA256__", hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    env.update({"VALID_ARCHIVE": str(archive), "MANIFEST": manifest})
    _write_executable(
        tmp_path / "bin" / "aws",
        """#!/bin/sh
for argument in "$@"; do
  case "$argument" in
    "$LOCAL_DIR"/*.manifest.json) printf '%s' "$MANIFEST" > "$argument"; exit 0 ;;
    "$LOCAL_DIR"/*.dump.gz) cp "$VALID_ARCHIVE" "$argument"; exit 0 ;;
  esac
done
exit 1
""",
    )
    result = subprocess.run(
        ["bash", str(RESTORE_SCRIPT), "--key", "talentscope-test.dump.gz", "--yes"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    return result, docker_marker


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


def test_backup_uses_one_timestamp_for_filename_and_manifest(tmp_path):
    env, _ = _restore_env(tmp_path)
    backup_dir = tmp_path / "backups"
    env["LOCAL_DIR"] = str(backup_dir)
    _write_executable(
        tmp_path / "bin" / "date",
        "#!/bin/sh\nprintf '2026-10-23T23:59:59Z\\n'\n",
    )
    _write_executable(tmp_path / "bin" / "git", "#!/bin/sh\nprintf 'deadbee\\n'\n")
    _write_executable(
        tmp_path / "bin" / "docker",
        """#!/bin/sh
case "$*" in
  *"-tAc"*) printf 'postings=1 companies=1 skills=1 embedded=1\n' ;;
  *"pg_dump"*) head -c 2048 /dev/urandom ;;
  *"pg_restore --list"*) cat >/dev/null ;;
  *) exit 99 ;;
esac
""",
    )

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT), "--local-only"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    backup = backup_dir / "talentscope-talentscope-20261023T235959Z-deadbee.dump.gz"
    manifest = json.loads(Path(f"{backup}.manifest.json").read_text(encoding="utf-8"))
    assert backup.exists()
    assert manifest["created_at"] == "2026-10-23T23:59:59Z"
    assert manifest["sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()


def test_backup_rejects_invalid_source_counts_before_dump(tmp_path):
    env, _ = _restore_env(tmp_path)
    backup_dir = tmp_path / "backups"
    dump_marker = tmp_path / "pg-dump-called"
    env["LOCAL_DIR"] = str(backup_dir)
    _write_executable(
        tmp_path / "bin" / "docker",
        f"""#!/bin/sh
case "$*" in
  *"-tAc"*) printf 'database warning instead of counts\n' ;;
  *"pg_dump"*) touch '{dump_marker}'; exit 99 ;;
  *) exit 99 ;;
esac
""",
    )

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT), "--local-only"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "source row-count query returned invalid evidence" in result.stderr
    assert not dump_marker.exists()
    assert list(backup_dir.iterdir()) == []


def test_backup_rejects_impossible_embedded_count_before_dump(tmp_path):
    env, _ = _restore_env(tmp_path)
    backup_dir = tmp_path / "backups"
    dump_marker = tmp_path / "pg-dump-called"
    env["LOCAL_DIR"] = str(backup_dir)
    _write_executable(
        tmp_path / "bin" / "docker",
        f"""#!/bin/sh
case "$*" in
  *"-tAc"*) printf 'postings=1 companies=1 skills=1 embedded=2\n' ;;
  *"pg_dump"*) touch '{dump_marker}'; exit 99 ;;
  *) exit 99 ;;
esac
""",
    )

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT), "--local-only"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "embedded posting count exceeds total postings" in result.stderr
    assert not dump_marker.exists()
    assert list(backup_dir.iterdir()) == []


def test_backup_inspects_archive_before_publishing_evidence(tmp_path):
    env, _ = _restore_env(tmp_path)
    backup_dir = tmp_path / "backups"
    inspect_marker = tmp_path / "pg-restore-called"
    env["LOCAL_DIR"] = str(backup_dir)
    _write_executable(
        tmp_path / "bin" / "docker",
        f"""#!/bin/sh
case "$*" in
  *"-tAc"*) printf 'postings=1 companies=1 skills=1 embedded=1\n' ;;
  *"pg_dump"*) head -c 2048 /dev/urandom ;;
  *"pg_restore --list"*) cat >/dev/null; touch '{inspect_marker}'; exit 1 ;;
  *) exit 99 ;;
esac
""",
    )

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT), "--local-only"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "not a readable custom-format archive" in result.stderr
    assert inspect_marker.exists()
    assert list(backup_dir.iterdir()) == []


def test_backup_refuses_to_overwrite_remote_evidence(tmp_path):
    env, _ = _restore_env(tmp_path)
    env["LOCAL_DIR"] = str(tmp_path / "backups")
    upload_marker = tmp_path / "upload-called"
    _write_executable(
        tmp_path / "bin" / "docker",
        """#!/bin/sh
case "$*" in
  *"-tAc"*) printf 'postings=1 companies=1 skills=1 embedded=1\n' ;;
  *"pg_dump"*) head -c 2048 /dev/urandom ;;
  *"pg_restore --list"*) cat >/dev/null ;;
  *) exit 99 ;;
esac
""",
    )
    _write_executable(
        tmp_path / "bin" / "aws",
        f"""#!/bin/sh
case "$*" in
  *"s3api head-object"*) exit 0 ;;
  *"s3 cp"*) touch '{upload_marker}'; exit 99 ;;
esac
""",
    )

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "refusing to overwrite existing s3://" in result.stderr
    assert not upload_marker.exists()


def test_backup_stops_when_remote_collision_check_fails(tmp_path):
    env, _ = _restore_env(tmp_path)
    env["LOCAL_DIR"] = str(tmp_path / "backups")
    upload_marker = tmp_path / "upload-called"
    _write_executable(
        tmp_path / "bin" / "docker",
        """#!/bin/sh
case "$*" in
  *"-tAc"*) printf 'postings=1 companies=1 skills=1 embedded=1\n' ;;
  *"pg_dump"*) head -c 2048 /dev/urandom ;;
  *"pg_restore --list"*) cat >/dev/null ;;
  *) exit 99 ;;
esac
""",
    )
    _write_executable(
        tmp_path / "bin" / "aws",
        f"""#!/bin/sh
case "$*" in
  *"s3api head-object"*) echo 'AccessDenied' >&2; exit 7 ;;
  *"s3 cp"*) touch '{upload_marker}'; exit 99 ;;
esac
""",
    )

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unable to verify whether" in result.stderr
    assert "AccessDenied" in result.stderr
    assert not upload_marker.exists()


def test_backup_removes_remote_archive_when_manifest_upload_fails(tmp_path):
    env, _ = _restore_env(tmp_path)
    env["LOCAL_DIR"] = str(tmp_path / "backups")
    aws_log = tmp_path / "aws.log"
    env["AWS_LOG"] = str(aws_log)
    _write_executable(
        tmp_path / "bin" / "docker",
        """#!/bin/sh
case "$*" in
  *"-tAc"*) printf 'postings=1 companies=1 skills=1 embedded=1\n' ;;
  *"pg_dump"*) head -c 2048 /dev/urandom ;;
  *"pg_restore --list"*) cat >/dev/null ;;
  *) exit 99 ;;
esac
""",
    )
    _write_executable(
        tmp_path / "bin" / "aws",
        """#!/bin/sh
printf '%s\n' "$*" >> "$AWS_LOG"
case "$*" in
  *"s3api head-object"*) echo '404 Not Found' >&2; exit 1 ;;
  *"s3 cp"*".manifest.json"*) exit 7 ;;
  *"s3 cp"*) exit 0 ;;
  *"s3 rm"*".dump.gz") exit 0 ;;
  *) exit 99 ;;
esac
""",
    )

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 7
    log = aws_log.read_text(encoding="utf-8")
    assert "s3 cp" in log
    assert "s3 rm" in log
    assert ".dump.gz --only-show-errors" in log


def test_backup_rejects_remote_archive_with_different_content(tmp_path):
    env, _ = _restore_env(tmp_path)
    env["LOCAL_DIR"] = str(tmp_path / "backups")
    aws_log = tmp_path / "aws.log"
    env["AWS_LOG"] = str(aws_log)
    _write_executable(
        tmp_path / "bin" / "docker",
        """#!/bin/sh
case "$*" in
  *"-tAc"*) printf 'postings=1 companies=1 skills=1 embedded=1\n' ;;
  *"pg_dump"*) head -c 2048 /dev/urandom ;;
  *"pg_restore --list"*) cat >/dev/null ;;
  *) exit 99 ;;
esac
""",
    )
    _write_executable(
        tmp_path / "bin" / "aws",
        """#!/bin/sh
printf '%s\n' "$*" >> "$AWS_LOG"
case "$*" in
  *"s3api head-object"*) echo '404 Not Found' >&2; exit 1 ;;
  *"s3 cp s3://"*".dump.gz -"*) head -c 2048 /dev/zero ;;
  *"s3 cp"*) exit 0 ;;
  *"s3 rm"*) exit 0 ;;
  *) exit 99 ;;
esac
""",
    )

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "checksum does not match" in result.stderr
    assert aws_log.read_text(encoding="utf-8").count("s3 rm") == 2


def test_backup_rejects_remote_manifest_with_different_content(tmp_path):
    env, _ = _restore_env(tmp_path)
    env["LOCAL_DIR"] = str(tmp_path / "backups")
    env["UPLOADED_ARCHIVE"] = str(tmp_path / "uploaded.dump.gz")
    aws_log = tmp_path / "aws.log"
    env["AWS_LOG"] = str(aws_log)
    _write_executable(
        tmp_path / "bin" / "docker",
        """#!/bin/sh
case "$*" in
  *"-tAc"*) printf 'postings=1 companies=1 skills=1 embedded=1\n' ;;
  *"pg_dump"*) head -c 2048 /dev/urandom ;;
  *"pg_restore --list"*) cat >/dev/null ;;
  *) exit 99 ;;
esac
""",
    )
    _write_executable(
        tmp_path / "bin" / "aws",
        """#!/bin/sh
printf '%s\n' "$*" >> "$AWS_LOG"
case "$*" in
  *"s3api head-object"*) echo '404 Not Found' >&2; exit 1 ;;
  *"s3 cp s3://"*".dump.gz -"*) cat "$UPLOADED_ARCHIVE" ;;
  *"s3 cp s3://"*".manifest.json -"*) printf '{"corrupted": true}\n' ;;
  *"s3 cp"*".manifest.json"*) exit 0 ;;
  *"s3 cp"*".dump.gz"*)
    for argument in "$@"; do
      case "$argument" in /*.dump.gz) cp "$argument" "$UPLOADED_ARCHIVE" ;; esac
    done
    ;;
  *"s3 rm"*) exit 0 ;;
  *) exit 99 ;;
esac
""",
    )

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "manifest content does not match" in result.stderr
    assert aws_log.read_text(encoding="utf-8").count("s3 rm") == 2


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


def test_restore_removes_partial_download_after_transfer_failure(tmp_path):
    env, docker_marker = _restore_env(tmp_path)
    downloads = tmp_path / "downloads"
    _write_executable(
        tmp_path / "bin" / "aws",
        """#!/bin/sh
for argument in "$@"; do
  case "$argument" in
    "$LOCAL_DIR"/*.dump.gz) printf 'partial transfer' > "$argument"; exit 7 ;;
  esac
done
exit 7
""",
    )

    result = subprocess.run(
        ["bash", str(RESTORE_SCRIPT), "--key", "talentscope-test.dump.gz", "--yes"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 7
    assert list(downloads.iterdir()) == []
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


def test_restore_rejects_duplicate_manifest_fields_before_touching_database(tmp_path):
    result, docker_marker = _run_restore_with_manifest(
        tmp_path,
        '{"backup":"talentscope-test.dump.gz",'
        '"backup":"other.dump.gz"}',
    )

    assert result.returncode == 1
    assert "backup manifest is invalid" in result.stderr
    assert not docker_marker.exists()


def test_restore_requires_complete_manifest_schema_before_touching_database(tmp_path):
    result, docker_marker = _run_restore_with_manifest(
        tmp_path,
        json.dumps(
            {
                "backup": "talentscope-test.dump.gz",
                "database": "talentscope",
                "bytes": 123,
                "row_counts": "postings=1 companies=1 skills=1 embedded=1",
            }
        ),
    )

    assert result.returncode == 1
    assert "backup manifest is invalid" in result.stderr
    assert not docker_marker.exists()


def test_restore_rejects_impossible_manifest_counts_before_touching_database(tmp_path):
    result, docker_marker = _run_restore_with_manifest(
        tmp_path,
        """{
          "backup": "talentscope-test.dump.gz",
          "created_at": "2026-10-29T00:00:00Z",
          "database": "talentscope",
          "git_sha": "deadbee",
          "bytes": __BYTES__,
          "sha256": "__SHA256__",
          "row_counts": "postings=1 companies=1 skills=1 embedded=2",
          "format": "pg_dump -Fc | gzip -9"
        }""",
    )

    assert result.returncode == 1
    assert "backup manifest is invalid" in result.stderr
    assert not docker_marker.exists()


def test_restore_rejects_manifest_checksum_mismatch_before_touching_database(tmp_path):
    env, docker_marker = _restore_env(tmp_path)
    archive = tmp_path / "valid.dump.gz"
    subprocess.run(
        ["gzip", "-c"],
        input=b"archive payload",
        stdout=archive.open("wb"),
        check=True,
    )
    env["VALID_ARCHIVE"] = str(archive)
    env["MANIFEST"] = json.dumps(
        {
            "backup": "talentscope-test.dump.gz",
            "created_at": "2026-10-29T00:00:00Z",
            "database": "talentscope",
            "git_sha": "deadbee",
            "bytes": archive.stat().st_size,
            "sha256": "0" * 64,
            "row_counts": "postings=1 companies=1 skills=1 embedded=1",
            "format": "pg_dump -Fc | gzip -9",
        }
    )
    _write_executable(
        tmp_path / "bin" / "aws",
        """#!/bin/sh
for argument in "$@"; do
  case "$argument" in
    "$LOCAL_DIR"/*.manifest.json) printf '%s' "$MANIFEST" > "$argument" ;;
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


def test_restore_requires_manifest_before_touching_database(tmp_path):
    env, docker_marker = _restore_env(tmp_path)
    archive = tmp_path / "valid.dump.gz"
    subprocess.run(
        ["gzip", "-c"],
        input=b"archive payload",
        stdout=archive.open("wb"),
        check=True,
    )
    env["VALID_ARCHIVE"] = str(archive)
    _write_executable(
        tmp_path / "bin" / "aws",
        """#!/bin/sh
for argument in "$@"; do
  case "$argument" in
    "$LOCAL_DIR"/*.manifest.json) echo '404 Not Found' >&2; exit 1 ;;
    "$LOCAL_DIR"/*.dump.gz) cp "$VALID_ARCHIVE" "$argument"; exit 0 ;;
  esac
done
exit 1
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
    assert "manifest is missing" in result.stderr
    assert "--allow-unverified" in result.stderr
    assert not docker_marker.exists()


def test_restore_does_not_treat_manifest_transport_failure_as_missing(tmp_path):
    env, docker_marker = _restore_env(tmp_path)
    archive = tmp_path / "valid.dump.gz"
    subprocess.run(
        ["gzip", "-c"], input=b"archive payload", stdout=archive.open("wb"), check=True
    )
    env["VALID_ARCHIVE"] = str(archive)
    _write_executable(
        tmp_path / "bin" / "aws",
        """#!/bin/sh
for argument in "$@"; do
  case "$argument" in
    "$LOCAL_DIR"/*.manifest.json) echo 'AccessDenied' >&2; exit 7 ;;
    "$LOCAL_DIR"/*.dump.gz) cp "$VALID_ARCHIVE" "$argument"; exit 0 ;;
  esac
done
exit 1
""",
    )

    result = subprocess.run(
        [
            "bash",
            str(RESTORE_SCRIPT),
            "--key",
            "talentscope-test.dump.gz",
            "--yes",
            "--allow-unverified",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "manifest download failed" in result.stderr
    assert "AccessDenied" in result.stderr
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
    "$LOCAL_DIR"/*.manifest.json) echo '404 Not Found' >&2; exit 1 ;;
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
        [
            "bash",
            str(RESTORE_SCRIPT),
            "--key",
            "talentscope-test.dump.gz",
            "--yes",
            "--allow-unverified",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "not a readable pg_dump archive" in result.stderr
    assert "pg_restore --list" in docker_log.read_text(encoding="utf-8")
    assert not destructive_marker.exists()
