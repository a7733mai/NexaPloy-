"""``nexaploy controlplane ...``: the control plane running as a bench of this server.

Works without the control plane (that is the point: recovery when the panel is down). The
bench, site, image and port come from ``<root>/config/control-plane.env`` (written by
``install.sh --with-control-plane``), overridable with options.

* ``status``                       - what runs, and the last ``controlplane.update`` job
* ``update [--image IMG]``         - job ``controlplane.update`` (backup, pull, switch, migrate,
                                     health check, automatic rollback); without ``--image`` the
                                     newest GitHub release's image (``<repo>:<version>``)
* ``backup --to DIR``              - one tarball with database + files + site_config (encryption key)
* ``import-backup SOURCE``         - stage a backup (tarball, dir, https URL, rclone remote) so
                                     ``site.restore`` can restore it (used by install.sh
                                     ``--restore-control-plane``); prints the backup id last
* ``repoint --url https://DOMAIN`` - point this agent at the control plane's public URL (after the
                                     control plane moved to another server)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

import httpx
import typer

from .runtime import controlplane as rc
from .runtime.backup import BackupError

cp_app = typer.Typer(no_args_is_help=True, help="The control plane hosted on this server (all-in-one).")


def _cli():
    from . import cli

    return cli


def _settings():
    from .config import load_settings

    try:
        return load_settings(_cli().state.config)
    except Exception as exc:  # noqa: BLE001
        _cli()._die(f"cannot load config: {exc}")


def _target(bench: Optional[str], site: Optional[str]) -> tuple[Any, dict[str, str], str, str]:
    settings = _settings()
    env = rc.read_cp_env(settings.config_dir)
    bench = bench or env.get("BENCH")
    site = site or env.get("DOMAIN")
    if not bench or not site:
        _cli()._die(
            f"no control plane on this server ({rc.cp_env_path(settings.config_dir)} not found); "
            "pass --bench and --site, or see ARCHITECTURE.md §5.9 for a control plane hosted elsewhere"
        )
    return settings, env, bench, site


def latest_release_image(repo: str = rc.DEFAULT_IMAGE_REPO, release_repo: str = rc.DEFAULT_RELEASE_REPO,
                         channel: str = "stable") -> tuple[str, str | None]:
    """(image, version) of the newest GitHub release; (``<repo>:latest``, None) when unknown."""
    try:
        resp = httpx.get(rc.RELEASES_API.format(repo=release_repo), timeout=10,
                         headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        rel = rc.pick_release(resp.json(), channel)
    except (httpx.HTTPError, ValueError):
        rel = None
    if not rel:
        return f"{repo}:latest", None
    version = rel["tag_name"].lstrip("v")
    return rc.image_for_version(version, repo), version


@cp_app.command("status")
def cp_status(bench: Optional[str] = typer.Option(None), site: Optional[str] = typer.Option(None)) -> None:
    """Control plane bench, site, image and the last update job."""
    cli = _cli()
    _, env, bench, site = _target(bench, site)
    info = cli.call("GET", f"/benches/{bench}")
    jobs = cli.call("GET", "/jobs", params={"kind": "controlplane.update", "limit": 1})
    data = {
        "site": site, "bench": bench, "image": info.get("image"), "status": info.get("status"),
        "port": rc.cp_port(env), "last_update": (jobs or [None])[0],
    }
    if cli.state.as_json:
        cli.show(data)
        return
    typer.echo(f"control plane: https://{site}  (bench {bench}, {data['status']})")
    typer.echo(f"image:         {data['image']}")
    typer.echo(f"local port:    127.0.0.1:{data['port']}")
    last = data["last_update"]
    if last:
        res = last.get("result") or {}
        typer.echo(f"last update:   {last['id']} {last['status']} {res.get('previous_image', '?')} -> {res.get('image', '?')}"
                   + (f" ({last['error']})" if last.get("error") else ""))


@cp_app.command("update")
def cp_update(
    image: Optional[str] = typer.Option(None, "--image", help="image to run (default: newest release)"),
    version: Optional[str] = typer.Option(None, "--version", help="release version, e.g. 0.2.0 (image <repo>:<version>)"),
    repo: str = typer.Option(rc.DEFAULT_IMAGE_REPO, "--repo", help="image repository"),
    channel: str = typer.Option("stable", "--channel", help="stable or beta (when resolving the newest release)"),
    backup: bool = typer.Option(True, "--backup/--no-backup", help="back up the control plane site first"),
    bench: Optional[str] = typer.Option(None),
    site: Optional[str] = typer.Option(None),
    wait: bool = typer.Option(True, "--wait/--no-wait"),
) -> None:
    """Update the control plane: backup -> pull -> switch -> migrate -> health check (rolls back on failure)."""
    cli = _cli()
    _, env, bench, site = _target(bench, site)
    if not image:
        if version:
            image = rc.image_for_version(version, repo)
        else:
            image, found = latest_release_image(repo, channel=channel)
            typer.echo(f"newest release: {found or 'unknown (GitHub not reachable)'} -> {image}")
    body = {"bench": bench, "site": site, "image": image, "previous_image": env.get("IMAGE") or None,
            "backup": backup, "port": rc.cp_port(env)}
    cli.submit("POST", "/controlplane/update", body, wait)


@cp_app.command("backup")
def cp_backup(
    to: Path = typer.Option(..., "--to", help="directory for the tarball"),
    upload: bool = typer.Option(False, "--upload/--no-upload", help="also upload to the backup storage"),
    bench: Optional[str] = typer.Option(None),
    site: Optional[str] = typer.Option(None),
) -> None:
    """One tarball (database + public/private files + site_config with the encryption key)."""
    cli = _cli()
    _, _, bench, site = _target(bench, site)
    res = cli.call("POST", f"/sites/{site}/backup", {"with_files": True, "upload": upload})
    typer.echo(f"job {res['job_id']} queued")
    job = cli.follow(res["job_id"])
    if job["status"] != "success":
        raise typer.Exit(1)
    path = Path(job["result"]["path"])
    try:
        out = rc.export_tarball(path, to, site)
    except BackupError as exc:
        cli._die(str(exc))
    typer.secho(f"control plane backup: {out}", fg=typer.colors.GREEN, bold=True)
    typer.echo("keep it private: it holds the whole database and the site encryption key.")
    typer.echo(f"restore on a new server: install.sh --with-control-plane {site} --restore-control-plane {out}")
    typer.echo(str(out))


def fetch_source(source: str, work: Path, rclone_conf: Path | None = None) -> Path:
    """Download an https URL / copy an rclone path into ``work``; a local path is returned as is."""
    kind = rc.source_kind(source)
    if kind == "path":
        return Path(os.path.expanduser(source))
    work.mkdir(parents=True, exist_ok=True)
    if kind == "url":
        name = source.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0] or "backup.tar"
        dest = work / name
        with httpx.stream("GET", source, follow_redirects=True, timeout=httpx.Timeout(60, read=600)) as resp:
            if resp.status_code >= 400:
                raise BackupError(f"download failed: HTTP {resp.status_code} for {source}")
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes(1024 * 1024):
                    fh.write(chunk)
        return dest
    argv = ["rclone"]
    if rclone_conf and not os.environ.get("RCLONE_CONFIG"):
        argv += ["--config", str(rclone_conf)]
    argv += ["copy", source, str(work)]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BackupError(f"rclone copy {source} failed: {proc.stderr.strip()[-500:]}")
    tars = sorted(p for p in work.iterdir() if p.is_file() and p.name.endswith((".tar", ".tar.gz", ".tgz")))
    if tars and not any(p.name.endswith("manifest.json") for p in work.rglob("manifest.json")):
        return tars[-1]
    return work


@cp_app.command("import-backup")
def cp_import_backup(
    source: str = typer.Argument(..., help="tarball, backup dir, https URL or rclone remote:path"),
    bench: Optional[str] = typer.Option(None),
    site: Optional[str] = typer.Option(None),
) -> None:
    """Stage a control plane backup under backups/<bench>/<site>/<id> (prints the id last)."""
    settings = _settings()
    env = rc.read_cp_env(settings.config_dir)
    bench = bench or env.get("BENCH")
    site = site or env.get("DOMAIN")
    if not bench or not site:
        _cli()._die("pass --bench and --site (no control-plane.env yet)")
    dest_root = settings.backups_dir / bench / site
    dest_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".fetch-", dir=dest_root))
    try:
        src = fetch_source(source, work, settings.rclone_conf)
        ts, path = rc.import_backup(src, dest_root, site=site, bench=bench, image=env.get("IMAGE"))
    except (BackupError, httpx.HTTPError, OSError) as exc:
        _cli()._die(f"cannot import {source}: {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    from .config import FRAPPE_UID
    from .fsutil import chown

    for p in (dest_root, path, *path.iterdir()):
        chown(p, FRAPPE_UID, FRAPPE_UID)
    typer.echo(f"backup staged in {path}")
    typer.echo(ts)


@cp_app.command("repoint")
def cp_repoint(url: str = typer.Option(..., "--url", help="public control plane URL, e.g. https://cp.example.com")) -> None:
    """Talk to the control plane at its public URL (after it moved to another server)."""
    from .config import config_path, update_config_file

    if not url.startswith(("https://", "http://")):
        _cli()._die("--url must be an http(s) URL")
    path = config_path(_cli().state.config)
    update_config_file(path, {"control_plane": {"url": url.rstrip("/"), "site_name": ""}})
    typer.echo(f"{path}: control_plane.url = {url.rstrip('/')} (server token kept)")
    typer.echo("restart the worker: systemctl restart nexaploy-agent-worker")
