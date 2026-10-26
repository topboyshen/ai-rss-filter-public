import hashlib
import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL = PROJECT_ROOT / "ops" / "install.sh"
VERIFY = PROJECT_ROOT / "ops" / "verify.sh"


def _run(script: Path, *args: str, env: dict[str, str]):
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _install_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    cron = tmp_path / "root.crontab"
    cron.write_text(
        """SHELL=/bin/bash
0 3 * * * /root/backup_miniflux.sh
30 3 * * * /root/backup_system.sh
* * * * * /root/health_check.sh
*/30 8-16 * * 1-5 /root/ai-rss-filter/scripts/cron_refresh.sh
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_flock = fake_bin / "flock"
    fake_flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_flock.chmod(0o755)
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "OPS_SYSTEMD_DIR": str(tmp_path / "systemd"),
            "OPS_ROOT_SCRIPT_DIR": str(tmp_path / "root-scripts"),
            "OPS_LOCAL_SBIN_DIR": str(tmp_path / "sbin"),
            "OPS_CONFIG_DIR": str(tmp_path / "etc-ai-filter"),
            "OPS_LOGROTATE_DIR": str(tmp_path / "logrotate"),
            "OPS_CRONTAB_FILE": str(cron),
            "OPS_CRONTAB_LOCK": str(tmp_path / "install-lock" / "crontab.lock"),
            "OPS_SKIP_SYSTEMCTL": "1",
            "OPS_SKIP_SYSTEMD_ANALYZE": "1",
        }
    )
    return env, cron


def _fake_crontab_env(tmp_path: Path, script: str) -> tuple[dict[str, str], Path]:
    env, cron = _install_env(tmp_path)
    cron.unlink()
    env.pop("OPS_CRONTAB_FILE")
    fake_crontab = tmp_path / "bin" / "crontab"
    fake_crontab.write_text(script, encoding="utf-8")
    fake_crontab.chmod(0o755)
    return env, fake_crontab


def test_host_asset_install_is_idempotent_and_preserves_unrelated_cron(tmp_path):
    env, cron = _install_env(tmp_path)

    first = _run(INSTALL, "--apply", env=env)
    assert first.returncode == 0, first.stderr
    first_cron = cron.read_bytes()
    first_hashes = {
        path.relative_to(tmp_path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    second = _run(INSTALL, "--apply", env=env)
    assert second.returncode == 0, second.stderr
    assert cron.read_bytes() == first_cron
    second_hashes = {
        path.relative_to(tmp_path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert second_hashes == first_hashes

    text = cron.read_text(encoding="utf-8")
    assert text.count("# BEGIN AI_FILTER_MANAGED") == 1
    assert text.count("# END AI_FILTER_MANAGED") == 1
    assert text.count("/root/backup_system.sh") == 1
    assert text.count("/root/health_check.sh") == 1
    assert "/root/backup_miniflux.sh" in text
    assert (tmp_path / "etc-ai-filter" / "ops-monitor.env").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "etc-ai-filter" / "backup.env").stat().st_mode & 0o777 == 0o600
    installed_logrotate = tmp_path / "logrotate" / "ai-filter"
    assert installed_logrotate.stat().st_mode & 0o777 == 0o644
    assert "/var/log/ai-filter-event-enrichment.log" in installed_logrotate.read_text(
        encoding="utf-8"
    )
    cron_lock = Path(env["OPS_CRONTAB_LOCK"])
    assert cron_lock.stat().st_mode & 0o777 == 0o600
    assert cron_lock.parent.stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "etc-ai-filter" / "root.crontab.previous").read_text(
        encoding="utf-8"
    ).startswith("SHELL=/bin/bash")
    assert (tmp_path / "systemd" / "ai-filter-market-close-repair.service").is_file()
    assert (tmp_path / "systemd" / "ai-filter-market-close-repair.timer").is_file()
    assert (tmp_path / "systemd" / "ai-filter-market-close-history.service").is_file()
    assert (tmp_path / "systemd" / "ai-filter-market-close-history.timer").is_file()

    verified = _run(VERIFY, env=env)
    assert verified.returncode == 0, verified.stderr

    launcher = tmp_path / "sbin" / "ai-filter-restore-drill"
    selector = tmp_path / "libexec" / "ai-filter" / "restore-drill-current"
    bundle_name = selector.read_text(encoding="ascii").strip()
    bundle = selector.parent / bundle_name
    assert bundle_name.startswith("restore-drill-")
    assert (bundle / "restore_drill.py").stat().st_mode & 0o777 == 0o600
    assert (bundle / "event_core.py").stat().st_mode & 0o777 == 0o600
    installed = subprocess.run(
        [str(launcher), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    assert "Isolated AI-Filter restore drill" in installed.stdout


def test_verify_detects_installed_hash_drift(tmp_path):
    env, _cron = _install_env(tmp_path)
    assert _run(INSTALL, "--apply", env=env).returncode == 0
    timer = tmp_path / "systemd" / "ai-filter-market-close.timer"
    timer.write_text(timer.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    result = _run(VERIFY, env=env)

    assert result.returncode == 1
    assert "DRIFT systemd:timer" in result.stderr


def test_installed_restore_drill_rejects_tampered_bundle(tmp_path):
    env, _cron = _install_env(tmp_path)
    assert _run(INSTALL, "--apply", env=env).returncode == 0
    selector = tmp_path / "libexec" / "ai-filter" / "restore-drill-current"
    bundle = selector.parent / selector.read_text(encoding="ascii").strip()
    verifier = bundle / "event_core.py"
    verifier.write_bytes(verifier.read_bytes() + b"\n# drift\n")

    result = subprocess.run(
        [str(tmp_path / "sbin" / "ai-filter-restore-drill"), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "bundle digest does not match" in result.stderr


def test_install_refuses_unbalanced_marker_without_touching_crontab_or_assets(tmp_path):
    env, cron = _install_env(tmp_path)
    original = "SHELL=/bin/bash\n# BEGIN AI_FILTER_MANAGED\n0 2 * * * /root/important-user-job\n"
    cron.write_text(original, encoding="utf-8")

    result = _run(INSTALL, "--apply", env=env)

    assert result.returncode == 78
    assert cron.read_text(encoding="utf-8") == original
    assert not (tmp_path / "systemd").exists()
    assert "refusing to modify" in result.stderr


def test_install_refuses_custom_unmanaged_ai_cron_without_deleting_it(tmp_path):
    env, cron = _install_env(tmp_path)
    custom = (
        "SHELL=/bin/bash\n"
        "17 4 * * * flock -n /tmp/custom.lock /root/backup_system.sh --verify-only\n"
    )
    cron.write_text(custom, encoding="utf-8")

    result = _run(INSTALL, "--apply", env=env)

    assert result.returncode == 78
    assert cron.read_text(encoding="utf-8") == custom
    assert not (tmp_path / "systemd").exists()


def test_install_refuses_unmanaged_event_enrichment_cron(tmp_path):
    env, cron = _install_env(tmp_path)
    custom = (
        "SHELL=/bin/bash\n"
        "7 * * * * /root/ai-rss-filter/scripts/cron_event_enrichment.sh --custom\n"
    )
    cron.write_text(custom, encoding="utf-8")

    result = _run(INSTALL, "--apply", env=env)

    assert result.returncode == 78
    assert cron.read_text(encoding="utf-8") == custom
    assert not (tmp_path / "systemd").exists()


def test_install_fails_closed_when_root_crontab_cannot_be_read(tmp_path):
    env, _fake = _fake_crontab_env(
        tmp_path,
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"-l\" ]; then exit 2; fi\n"
        "exit 99\n",
    )

    result = _run(INSTALL, "--apply", env=env)

    assert result.returncode == 78
    assert "existing root crontab could not be read" in result.stderr
    assert not (tmp_path / "systemd").exists()


def test_install_detects_concurrent_crontab_change_before_write(tmp_path):
    state = tmp_path / "fake-crontab.state"
    concurrent = tmp_path / "fake-crontab.concurrent"
    calls = tmp_path / "fake-crontab.calls"
    write_attempt = tmp_path / "fake-crontab.write-attempt"
    state.write_text("SHELL=/bin/bash\n0 3 * * * /root/backup_miniflux.sh\n", encoding="utf-8")
    concurrent.write_text(
        "SHELL=/bin/bash\n0 3 * * * /root/backup_miniflux.sh\n17 5 * * * /root/user-added.sh\n",
        encoding="utf-8",
    )
    env, _fake = _fake_crontab_env(
        tmp_path,
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"-l\" ]; then\n"
        "  count=0\n"
        "  [ ! -f \"$FAKE_CRONTAB_CALLS\" ] || count=$(cat \"$FAKE_CRONTAB_CALLS\")\n"
        "  count=$((count + 1))\n"
        "  printf '%s\\n' \"$count\" > \"$FAKE_CRONTAB_CALLS\"\n"
        "  cat \"$FAKE_CRONTAB_STATE\"\n"
        "  if [ \"$count\" -eq 1 ]; then cp \"$FAKE_CRONTAB_CONCURRENT\" \"$FAKE_CRONTAB_STATE\"; fi\n"
        "  exit 0\n"
        "fi\n"
        "printf 'write attempted\\n' > \"$FAKE_CRONTAB_WRITE_ATTEMPT\"\n"
        "exit 0\n",
    )
    env.update(
        {
            "FAKE_CRONTAB_STATE": str(state),
            "FAKE_CRONTAB_CONCURRENT": str(concurrent),
            "FAKE_CRONTAB_CALLS": str(calls),
            "FAKE_CRONTAB_WRITE_ATTEMPT": str(write_attempt),
        }
    )

    result = _run(INSTALL, "--apply", env=env)

    assert result.returncode == 75
    assert "changed concurrently" in result.stderr
    assert state.read_text(encoding="utf-8") == concurrent.read_text(encoding="utf-8")
    assert not write_attempt.exists()


def test_install_rejects_symlink_crontab_lock_without_touching_target(tmp_path):
    env, _cron = _install_env(tmp_path)
    target = tmp_path / "important-root-file"
    target.write_text("must-stay-unchanged\n", encoding="utf-8")
    lock = Path(env["OPS_CRONTAB_LOCK"])
    lock.parent.mkdir()
    lock.symlink_to(target)

    result = _run(INSTALL, "--apply", env=env)

    assert result.returncode == 78
    assert "lock must be a regular file" in result.stderr
    assert target.read_text(encoding="utf-8") == "must-stay-unchanged\n"
    assert not (tmp_path / "systemd").exists()


def test_install_rejects_symlink_local_sbin_before_writing_assets(tmp_path):
    env, _cron = _install_env(tmp_path)
    physical = tmp_path / "physical-sbin"
    physical.mkdir()
    configured = Path(env["OPS_LOCAL_SBIN_DIR"])
    configured.symlink_to(physical, target_is_directory=True)

    result = _run(INSTALL, "--apply", env=env)

    assert result.returncode == 78
    assert "local sbin directory must be a canonical physical path" in result.stderr
    assert not (tmp_path / "systemd").exists()
    assert not list(physical.iterdir())


def test_install_rejects_noncanonical_local_sbin_before_writing_assets(tmp_path):
    env, _cron = _install_env(tmp_path)
    env["OPS_LOCAL_SBIN_DIR"] = env["OPS_LOCAL_SBIN_DIR"] + "/."

    result = _run(INSTALL, "--apply", env=env)

    assert result.returncode == 78
    assert "local sbin directory must be a canonical physical path" in result.stderr
    assert not (tmp_path / "systemd").exists()


def test_install_repairs_existing_operator_file_modes(tmp_path):
    env, _cron = _install_env(tmp_path)
    config_dir = Path(env["OPS_CONFIG_DIR"])
    config_dir.mkdir()
    monitor_config = config_dir / "ops-monitor.env"
    backup_config = config_dir / "backup.env"
    monitor_config.write_text("OPS_ALERT_COOLDOWN_SECONDS=10\n", encoding="utf-8")
    backup_config.write_text("SYSTEM_BACKUP_UPLOAD_REQUIRED=0\n", encoding="utf-8")
    monitor_config.chmod(0o644)
    backup_config.chmod(0o666)

    result = _run(INSTALL, "--apply", env=env)

    assert result.returncode == 0, result.stderr
    assert monitor_config.stat().st_mode & 0o777 == 0o600
    assert backup_config.stat().st_mode & 0o777 == 0o600


def test_source_verify_runs_systemd_analyze_contract(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "systemd-analyze.args"
    fake = fake_bin / "systemd-analyze"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$FAKE_ANALYZE_MARKER\"\nexit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "FAKE_ANALYZE_MARKER": str(marker),
            "OPS_SKIP_SYSTEMD_ANALYZE": "0",
        }
    )

    result = _run(VERIFY, "--source-only", env=env)

    assert result.returncode == 0, result.stderr
    args = marker.read_text(encoding="utf-8")
    assert args.startswith("verify ")
    assert "ai-filter-market-close.service" in args
    assert "ai-filter-market-close.timer" in args
    assert "ai-filter-market-close-repair.service" in args
    assert "ai-filter-market-close-repair.timer" in args
    assert "ai-filter-market-close-history.service" in args
    assert "ai-filter-market-close-history.timer" in args


def test_versioned_units_and_backup_hook_are_fail_closed():
    service = (PROJECT_ROOT / "ops/systemd/ai-filter-market-close.service").read_text(
        encoding="utf-8"
    )
    timer = (PROJECT_ROOT / "ops/systemd/ai-filter-market-close.timer").read_text(
        encoding="utf-8"
    )
    repair_service = (
        PROJECT_ROOT / "ops/systemd/ai-filter-market-close-repair.service"
    ).read_text(encoding="utf-8")
    repair_timer = (
        PROJECT_ROOT / "ops/systemd/ai-filter-market-close-repair.timer"
    ).read_text(encoding="utf-8")
    history_service = (
        PROJECT_ROOT / "ops/systemd/ai-filter-market-close-history.service"
    ).read_text(encoding="utf-8")
    history_timer = (
        PROJECT_ROOT / "ops/systemd/ai-filter-market-close-history.timer"
    ).read_text(encoding="utf-8")
    install_script = (PROJECT_ROOT / "ops/install.sh").read_text(encoding="utf-8")
    backup = (PROJECT_ROOT / "ops/backup/backup_system.sh").read_text(
        encoding="utf-8"
    )
    cron = (PROJECT_ROOT / "ops/cron/ai-filter.crontab").read_text(
        encoding="utf-8"
    )
    hook = PROJECT_ROOT / "ops/backup/offsite_upload_hook.example.sh"

    assert "ExecStart=/bin/bash /root/ai-rss-filter/scripts/run_market_close.sh" in service
    assert "OnCalendar=Mon..Fri *-*-* 15:10:00 Asia/Shanghai" in timer
    assert "Persistent=false" in timer
    assert (
        "ExecStart=/usr/bin/python3 /root/ai-rss-filter/scripts/repair_market_close.py"
        in repair_service
    )
    assert "OnCalendar=Mon..Fri *-*-* 15:25:00 Asia/Shanghai" in repair_timer
    assert "Persistent=true" in repair_timer
    assert "Unit=ai-filter-market-close-repair.service" in repair_timer
    assert "systemctl enable --now ai-filter-market-close-repair.timer" in install_script
    assert "--scope=history" in history_service
    assert "OnCalendar=Mon..Fri *-*-* 16:30:00 Asia/Shanghai" in history_timer
    assert "Persistent=false" in history_timer
    assert "systemctl enable --now ai-filter-market-close-history.timer" in install_script
    assert "--kill-after=60s 18m /root/ai-rss-filter/scripts/cron_event_enrichment.sh" in cron
    ops_jobs = [line for line in cron.splitlines() if "/usr/local/sbin/ai-filter-ops-check" in line]
    assert len(ops_jobs) == 1 and ops_jobs[0].startswith("0 * * * * ")
    assert "* * * * * /root/health_check.sh" in cron
    assert "src_conn.backup(dst_conn)" in backup
    assert "PRAGMA quick_check" in backup
    assert 'canonical_db="${project}/data/cache.db"' in backup
    assert 'canonical_backup="${out}/sqlite/data/cache.db"' in backup
    assert "offsite_upload=unconfigured local_only=true" in backup
    assert 'source "$BACKUP_CONFIG_FILE"' not in backup
    assert "allowed = {" in backup
    result = subprocess.run(
        ["bash", str(hook), "/does/not/exist", "/does/not/exist.sha256"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode != 0


def test_ai_filter_backup_uses_one_release_lock_for_the_consistent_stage_only():
    backup = (PROJECT_ROOT / "ops" / "backup" / "backup_system.sh").read_text(
        encoding="utf-8"
    )
    acquire = backup.rindex("\nacquire_ai_release_snapshot_lock\n")
    compose_snapshot = backup.rindex("\nbackup_compose_files\n")
    app_snapshot = backup.rindex("\nbackup_ai_filter\n")
    release = backup.rindex("\nrelease_ai_release_snapshot_lock\n")
    archive_hash = backup.index("archive_hash=$(sha256sum")
    manifest_publish = backup.index('mv -f -- "$MANIFEST_TMP" "$MANIFEST"')
    archive_publish = backup.index('mv -f -- "$ARCHIVE_TMP" "$ARCHIVE"')

    assert acquire < compose_snapshot < app_snapshot < release < archive_hash
    assert archive_hash < manifest_publish < archive_publish
    runtime_function = backup.split("snapshot_ai_runtime()", 1)[1].split(
        "backup_npm()", 1
    )[0]
    assert 'exec 7>"${data_dir}/releases/release.lock"' not in runtime_function
    # The remaining fd 7 lock belongs to the independently-mutated webhook
    # spool and must not be removed with the duplicate release lock.
    assert 'exec 7>"${data_dir}/webhook_spool/.lock"' in runtime_function
