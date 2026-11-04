"""Point-in-time recovery (PITR) helpers: binlog positions, archive selection, replay commands.

Design (ARCHITECTURE.md §14):

* The shared MariaDB writes a ROW-format binary log (``--log-bin=mysql-bin`` in the data dir,
  ``/opt/nexaploy/db/data`` on the host).
* A PITR-enabled ``site.backup`` replaces the database dump made by ``bench backup`` with its
  own ``mariadb-dump --single-transaction --master-data=2 --gtid``. With ``--single-transaction``
  MariaDB reads the binlog coordinates *of the consistent snapshot* (``binlog_snapshot_file`` /
  ``binlog_snapshot_position``, no global read lock), so the dump contains exactly the
  transactions committed before that position: replaying the binlog from it applies every later
  transaction once, and none twice. (Recording ``SHOW MASTER STATUS`` around ``bench backup``
  instead would be wrong: transactions between the recorded position and the dump's snapshot
  would be replayed on top of rows that already contain them.) The coordinates are parsed from
  the dump header (commented out: ``--master-data=2``) and stored in the backup manifest.
* ``server.archive_binlogs`` uploads *closed* binlog files to ``<remote>:<prefix>/_binlogs/``
  (``_`` keeps the dir apart from bench names, which start with a letter).
* ``site.restore_pitr`` restores the base backup, then decodes the binlog from the recorded
  position to ``--stop-datetime`` (UTC), filtered to the site's database (renamed with
  ``--rewrite-db`` when restoring into another database) and applies it with
  ``sql_log_bin=0``.

Everything here is pure (argv / script builders, parsers, selection) and unit-tested.
"""

from __future__ import annotations

import gzip
import json
import re
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .. import validation as v

DB_CONTAINER = "nexaploy-db"
BINLOG_BASENAME = "mysql-bin"
ARCHIVE_DIR = "_binlogs"
# Container-side mount points for the one-shot dump / replay containers.
DUMP_MOUNT = "/out"
BINLOG_MOUNT = "/binlogs"
WORK_MOUNT = "/work"
REPLAY_SQL = "replay.sql"
# Files whose last write is this much after the target are surely past it (long transactions
# committing later than this after they started are the documented limit).
SELECT_SLACK = timedelta(minutes=10)
HEADER_BYTES = 256 * 1024
DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
BINLOG_NAME_RE = re.compile(r"^(?P<base>[A-Za-z0-9_-]{1,64})\.(?P<seq>\d{6,})$")
GTID_RE = re.compile(r"^\d+-\d+-\d+(,\d+-\d+-\d+)*$")

_CHANGE_RE = re.compile(
    r"^(?P<comment>--\s*)?CHANGE\s+(?:MASTER|REPLICATION\s+SOURCE)\s+TO\s+(?P<body>.*?);?\s*$",
    re.IGNORECASE,
)
_FILE_RE = re.compile(r"(?:MASTER|SOURCE)_LOG_FILE\s*=\s*'([^']+)'", re.IGNORECASE)
_POS_RE = re.compile(r"(?:MASTER|SOURCE)_LOG_POS\s*=\s*(\d+)", re.IGNORECASE)
_GTID_LINE_RE = re.compile(
    r"^(?P<comment>--\s*)?SET\s+GLOBAL\s+gtid_(?:slave|replica)_pos\s*=\s*'(?P<gtid>[^']*)'", re.IGNORECASE
)


class PitrError(RuntimeError):
    """PITR precondition or input problem (message is shown to the user)."""


# --------------------------------------------------------------------------- names / time
def db_name(value: Any) -> str:
    if not isinstance(value, str) or not DB_NAME_RE.match(value):
        raise v.ValidationError(f"invalid database name {value!r}")
    return value


def binlog_name(value: Any) -> str:
    if not isinstance(value, str) or not BINLOG_NAME_RE.match(value):
        raise v.ValidationError(f"invalid binlog file name {value!r}")
    return value


def split_binlog(name: str) -> tuple[str, int]:
    m = BINLOG_NAME_RE.match(name or "")
    if not m:
        raise v.ValidationError(f"invalid binlog file name {name!r}")
    return m.group("base"), int(m.group("seq"))


def parse_time(value: Any) -> datetime:
    """ISO-8601 (``Z``/offset; naive = UTC) -> aware UTC datetime, whole seconds."""
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value or "").strip()
        if not s:
            raise v.ValidationError("time is required")
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        # rclone / Go print up to 9 fractional digits; Python takes 6
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T", 1))
        except ValueError:
            raise v.ValidationError(f"invalid time {value!r} (ISO 8601 expected)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stop_datetime(target: datetime) -> str:
    """``--stop-datetime`` value. mariadb-binlog reads it in the *client's* local time zone,
    so the replay container runs with ``TZ=UTC``."""
    return target.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def pitr_site_name(site: str, target: datetime) -> str:
    """Default name of the inspection copy: ``<first label>-pitr-<YYYYmmddHHMM>.<rest>``."""
    v.site_name(site)
    first, _, rest = site.partition(".")
    stamp = target.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
    suffix = f"-pitr-{stamp}"
    label = (first[: 63 - len(suffix)].rstrip("-") or "site") + suffix
    return v.site_name(f"{label}.{rest}")


# --------------------------------------------------------------------------- dump
def dump_script(database: str, db_host: str, out_name: str) -> str:
    """bash script run in a one-shot container of the bench image (same client as
    ``bench backup``/``bench restore``): consistent dump with the snapshot's binlog
    coordinates, gzipped to a temp file, then renamed over ``out_name`` (all-or-nothing)."""
    db_name(database)
    if not re.match(r"^[A-Za-z0-9._-]{1,253}$", db_host or ""):
        raise v.ValidationError(f"invalid database host {db_host!r}")
    if "/" in out_name or out_name.startswith(".") or not out_name.endswith(".sql.gz"):
        raise v.ValidationError(f"invalid dump file name {out_name!r}")
    out = f"{DUMP_MOUNT}/{out_name}"
    tmp = f"{DUMP_MOUNT}/.{out_name}.pitr-tmp"
    flags = [
        "-h", db_host, "-u", "root",
        "--single-transaction", "--quick", "--lock-tables=false",
        "--master-data=2", "--gtid",
        "--routines", "--triggers",
        "--default-character-set=utf8mb4", "--max-allowed-packet=1G",
        database,
    ]
    return "\n".join([
        "set -euo pipefail",
        'dump="$(command -v mariadb-dump || command -v mysqldump)"',
        f'"$dump" {" ".join(shlex.quote(f) for f in flags)} | gzip -c > {shlex.quote(tmp)}',
        f"mv -f {shlex.quote(tmp)} {shlex.quote(out)}",
    ])


def dump_command(image: str, out_dir: Path | str, out_name: str, database: str, db_host: str,
                 uid: int = 3113, network: str = DB_CONTAINER) -> list[str]:
    """argv: ``docker run --rm`` of the bench image writing the dump into ``out_dir``.
    The root password reaches the container only via ``MYSQL_PWD`` in the docker CLI env."""
    v.image_ref(image)
    return [
        "docker", "run", "--rm", "--network", network, "--user", f"{uid}:{uid}",
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        "-e", "MYSQL_PWD", "-v", f"{out_dir}:{DUMP_MOUNT}",
        "--entrypoint", "bash", image, "-c", dump_script(database, db_host, out_name),
    ]


def parse_dump_coordinates(text: str) -> dict[str, Any]:
    """Binlog coordinates from a ``--master-data=2 [--gtid]`` dump header.

    Returns ``{file, position, gtid}`` (gtid may be None). Raises :class:`PitrError` when the
    coordinates are missing, or when a replication statement is *not* commented out (restoring
    such a dump would change the server's replication state).
    """
    file = pos = gtid = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = _CHANGE_RE.match(line)
        if m:
            if not m.group("comment"):
                raise PitrError("dump contains an active CHANGE MASTER statement (use --master-data=2)")
            fm, pm = _FILE_RE.search(m.group("body")), _POS_RE.search(m.group("body"))
            if fm and pm and file is None:
                file, pos = fm.group(1), int(pm.group(1))
            continue
        g = _GTID_LINE_RE.match(line)
        if g:
            if not g.group("comment"):
                raise PitrError("dump contains an active SET GLOBAL gtid_slave_pos (use --master-data=2)")
            gtid = g.group("gtid") or None
    if file is None or pos is None:
        raise PitrError("dump has no binlog coordinates (CHANGE MASTER TO MASTER_LOG_FILE=...)")
    binlog_name(file)
    if gtid is not None and not GTID_RE.match(gtid):
        gtid = None
    return {"file": file, "position": pos, "gtid": gtid}


def read_dump_header(path: Path, max_bytes: int = HEADER_BYTES) -> str:
    """First ``max_bytes`` of a (gzipped) SQL dump as text. The coordinates are written
    before any table data."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as fh:  # type: ignore[operator]
        return fh.read(max_bytes).decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- server state
def db_query_command(sql: str, container: str = DB_CONTAINER) -> list[str]:
    """argv running ``sql`` as root in the shared DB container (``MYSQL_PWD`` from the env)."""
    return ["docker", "exec", "-e", "MYSQL_PWD", container, "mariadb", "-uroot", "-N", "-B", "-e", sql]


BINLOG_STATUS_SQL = "SELECT @@log_bin, @@binlog_format"
FLUSH_SQL = "FLUSH BINARY LOGS"


def parse_binlog_status(text: str) -> dict[str, Any]:
    """``SELECT @@log_bin, @@binlog_format`` (tab separated) -> ``{enabled, format}``."""
    for line in (text or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 2 and parts[0].strip() in ("0", "1", "ON", "OFF"):
            return {"enabled": parts[0].strip() in ("1", "ON"), "format": parts[1].strip().upper()}
    return {"enabled": False, "format": None}


def pitr_ready(status: Mapping[str, Any]) -> str | None:
    """Reason PITR cannot work with this binlog status, else None."""
    if not status.get("enabled"):
        return "binary logging is off on the shared database (see bench-template/README.md)"
    if status.get("format") != "ROW":
        return f"binlog_format is {status.get('format')}, ROW is required"
    return None


# --------------------------------------------------------------------------- binlog files
def parse_binlog_index(text: str) -> list[str]:
    """``mysql-bin.index`` lines (``./mysql-bin.000001`` or absolute) -> file names, in order."""
    out = []
    for line in (text or "").splitlines():
        name = line.strip().rsplit("/", 1)[-1]
        if BINLOG_NAME_RE.match(name) and name not in out:
            out.append(name)
    return out


def closed_binlogs(index: list[str]) -> list[str]:
    """All but the last (active) file of the index."""
    return list(index[:-1])


def parse_rclone_lsjson(text: str) -> dict[str, dict[str, Any]]:
    """``rclone lsjson --files-only`` -> ``{name: {size, mtime}}`` for binlog-named files."""
    try:
        rows = json.loads(text or "[]")
    except json.JSONDecodeError:
        raise PitrError("cannot parse rclone lsjson output") from None
    out: dict[str, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("IsDir"):
            continue
        name = str(row.get("Name") or row.get("Path") or "")
        if not BINLOG_NAME_RE.match(name):
            continue
        try:
            mtime = parse_time(row.get("ModTime")) if row.get("ModTime") else None
        except v.ValidationError:
            mtime = None
        out[name] = {"size": row.get("Size"), "mtime": mtime}
    return out


def select_binlogs(available: Mapping[str, datetime | None], start_file: str, target: datetime,
                   exclude: Iterable[str] = (), slack: timedelta = SELECT_SLACK) -> list[str]:
    """Binlog files to decode for a replay from ``start_file`` up to ``target``.

    ``available`` maps file name -> last write time (mtime; None = unknown) of every file
    present locally or in the archive. Files must be consecutive from ``start_file``; a gap
    (a file lost before it was archived) raises :class:`PitrError`. Files after the first one
    last written more than ``slack`` after ``target`` are not needed (mariadb-binlog's
    ``--stop-datetime`` makes the exact cut). ``exclude`` = the active (still written) file.
    """
    base, start_seq = split_binlog(start_file)
    excluded = set(exclude)
    seqs: dict[int, str] = {}
    for name in available:
        m = BINLOG_NAME_RE.match(name)
        if m and m.group("base") == base and int(m.group("seq")) >= start_seq and name not in excluded:
            seqs[int(m.group("seq"))] = name
    if start_seq not in seqs:
        raise PitrError(
            f"binlog {start_file} (the position of the base backup) is no longer available "
            "locally or in the archive; choose a later backup"
        )
    out: list[str] = []
    expected = start_seq
    for seq in sorted(seqs):
        if seq != expected:
            missing = f"{base}.{expected:0{len(start_file) - len(base) - 1}d}"
            raise PitrError(f"binlog {missing} is missing (not archived before it expired); "
                            "the target time cannot be reached from this backup")
        name = seqs[seq]
        out.append(name)
        expected += 1
        mtime = available[name]
        if mtime is not None and mtime > target + slack:
            break
    return out


# --------------------------------------------------------------------------- replay
def decode_script(files: list[str], start_position: int, stop: datetime, src_db: str,
                  dst_db: str) -> str:
    """bash: decode the binlogs into ``/work/replay.sql`` and print event counts.

    * ``--start-position`` applies to the first file only (the backup's file);
    * ``--stop-datetime`` (UTC, the container has ``TZ=UTC``) stops at the first event at or
      after the target, so an open transaction at the cut is never committed;
    * ``--database`` keeps the site's database only (ROW events: by table; statements: by
      default database); with a different target database ``--rewrite-db`` renames it, and
      MariaDB applies the filter *after* the rewrite, so the filter names the target;
    * ``--disable-log-bin`` (``SET sql_log_bin=0``): the replayed changes are not written to
      the binlog again (no duplicate GTIDs, no loop).
    """
    db_name(src_db)
    db_name(dst_db)
    if not files:
        raise PitrError("no binlog files to replay")
    for f in files:
        binlog_name(f)
    args = ["--disable-log-bin", f"--start-position={int(start_position)}",
            f"--stop-datetime={stop_datetime(stop)}"]
    if src_db != dst_db:
        args.append(f"--rewrite-db={src_db}->{dst_db}")
    args.append(f"--database={dst_db}")
    args += [f"{BINLOG_MOUNT}/{f}" for f in files]
    out = f"{WORK_MOUNT}/{REPLAY_SQL}"
    return "\n".join([
        "set -euo pipefail",
        "umask 077",
        f"mariadb-binlog {' '.join(shlex.quote(a) for a in args)} > {shlex.quote(out)}",
        f"rows=$(grep -c '^BINLOG ' {shlex.quote(out)} || true)",
        f"commits=$(grep -c '^COMMIT' {shlex.quote(out)} || true)",
        f'echo "nexaploy-pitr: bytes=$(stat -c %s {shlex.quote(out)}) row_events=$rows commits=$commits"',
    ])


def apply_script(dst_db: str, db_host: str = DB_CONTAINER) -> str:
    """bash: apply ``/work/replay.sql`` to ``dst_db`` (``--binary-mode``: row images are binary)."""
    db_name(dst_db)
    out = f"{WORK_MOUNT}/{REPLAY_SQL}"
    return "\n".join([
        "set -euo pipefail",
        f"mariadb --binary-mode --max-allowed-packet=1G -h {shlex.quote(db_host)} -u root "
        f"{shlex.quote(dst_db)} < {shlex.quote(out)}",
    ])


def replay_container(image: str, script: str, binlog_dir: Path | str, work_dir: Path | str,
                     uid: int = 3101, network: str = DB_CONTAINER) -> list[str]:
    """argv: one-shot container of the DB server's own image (its mariadb-binlog reads that
    server's binlog format) with the binlogs read-only and a work dir; TZ=UTC for
    ``--stop-datetime``; the root password only via ``MYSQL_PWD`` in the docker CLI env."""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$", image or ""):
        raise v.ValidationError(f"invalid image {image!r}")
    return [
        "docker", "run", "--rm", "--network", network, "--user", f"{uid}:{uid}",
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        "-e", "MYSQL_PWD", "-e", "TZ=UTC",
        "-v", f"{binlog_dir}:{BINLOG_MOUNT}:ro", "-v", f"{work_dir}:{WORK_MOUNT}",
        "--entrypoint", "bash", image, "-c", script,
    ]


_COUNTS_RE = re.compile(r"nexaploy-pitr: bytes=(\d+) row_events=(\d+) commits=(\d+)")


def parse_decode_counts(text: str) -> dict[str, int] | None:
    m = _COUNTS_RE.search(text or "")
    if not m:
        return None
    return {"bytes": int(m.group(1)), "row_events": int(m.group(2)), "commits": int(m.group(3))}
