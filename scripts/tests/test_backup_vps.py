from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "backup-vps.sh"


def test_backup_vps_script_syntax_and_dry_run(tmp_path: Path) -> None:
    syntax = subprocess.run(  # noqa: S603
        ["bash", "-n", str(SCRIPT_PATH)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    env = os.environ.copy()
    env.update(
        {
            "ENV_FILE": str(tmp_path / "missing.env"),
            "BACKUP_DIR": str(tmp_path / "backups"),
            "POSTGRES_DB": "trip_test",
            "POSTGRES_USER": "trip_tester",
            "BACKUP_RETENTION_DAYS": "7",
            "BACKUP_S3_URI": "s3://example-backups/trip",
        }
    )
    res = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT_PATH), "--dry-run"],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert "dry_run=true" in res.stdout
    assert "db=trip_test" in res.stdout
    assert "user=trip_tester" in res.stdout
    assert "retention_days=7" in res.stdout
    assert "offsite=s3://example-backups/trip" in res.stdout


def test_backup_vps_script_reads_env_file_overrides(tmp_path: Path) -> None:
    env_file = tmp_path / "test.env"
    backup_dir = tmp_path / "env-backups"
    env_file.write_text(
        "\n".join(
            [
                "POSTGRES_DB=env_db",
                "POSTGRES_USER=env_user",
                f"BACKUP_DIR={backup_dir}",
                "BACKUP_RETENTION_DAYS=21",
                "BACKUP_S3_URI=s3://env-bucket/prefix",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "POSTGRES_DB",
            "POSTGRES_USER",
            "BACKUP_DIR",
            "BACKUP_RETENTION_DAYS",
            "BACKUP_S3_URI",
            "BACKUP_S3_ENDPOINT_URL",
        }
    }
    env["ENV_FILE"] = str(env_file)
    res = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT_PATH), "--dry-run"],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert f"backup_file={backup_dir}/trip-animator-env_db-" in res.stdout
    assert "retention_days=21" in res.stdout
    assert "offsite=s3://env-bucket/prefix" in res.stdout
