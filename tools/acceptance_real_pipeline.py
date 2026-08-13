from __future__ import annotations

"""Opt-in, content-safe localhost production acceptance for a real export ZIP.

The harness never prints archive content or logical identifiers. It starts the
shipped CLI Web entry point, streams multipart from the read-only source file,
waits for the complete import/verify/stats/Web-index job, runs bounded API
smoke checks, and removes only its fresh temporary workspace.
"""

import argparse
import hashlib
import http.client
import json
import os
import platform
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatgpt_export_archiver.scanner import (  # noqa: E402
    list_source_entries,
    resolve_input,
    select_conversation_sources,
)
from tools.process_watchdog import terminate_kill_reap  # noqa: E402

READ_CHUNK = 1024 * 1024
TERMINAL_JOB_STATES = {"succeeded", "failed", "postcheck_failed"}


class HardRunTimeout(RuntimeError):
    pass


def _hard_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HardRunTimeout("hard_timeout")
    return remaining


def _untraced_environment(**updates: str) -> dict[str, str]:
    """Build a child environment whose timing is not polluted by parent tracing."""

    env = os.environ.copy()
    env.pop("PYTHONTRACEMALLOC", None)
    env.update(updates)
    return env


def _evaluate_acceptance(
    metrics: dict[str, Any], *, max_pipeline_seconds: float
) -> None:
    pipeline_wall = metrics.get("required_pipeline_wall_seconds")
    metrics["performance_pass"] = bool(
        metrics.get("pipeline_success")
        and not metrics.get("hard_timeout")
        and isinstance(pipeline_wall, (int, float))
        and float(pipeline_wall) <= max_pipeline_seconds
    )
    metrics["success"] = bool(
        metrics.get("correctness_pass") and metrics["performance_pass"]
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> dict[str, Any]:
    info = path.lstat()
    return {
        "absolute_path": str(path.resolve()),
        "file_type": "regular" if path.is_file() and not path.is_symlink() else "other",
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(info.st_ctime_ns),
        "mode": int(info.st_mode & 0o7777),
        "nlink": int(info.st_nlink),
        "sha256": _sha256(path),
    }


def _identity_equal(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return before == after


def _public_identity(value: dict[str, Any]) -> dict[str, Any]:
    """Retain the required identity evidence without exposing a user path."""

    return {
        key: item
        for key, item in value.items()
        if key != "absolute_path"
    } | {
        "absolute_path_sha256": hashlib.sha256(
            str(value["absolute_path"]).encode("utf-8")
        ).hexdigest()
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request_json(
    port: int,
    method: str,
    path: str,
    *,
    timeout: float = 30.0,
    hard_deadline: float | None = None,
) -> tuple[int, Any]:
    deadline = hard_deadline or (time.monotonic() + timeout)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {
            "Accept": "application/json",
            "Host": f"127.0.0.1:{port}",
        }
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers.update({
                "Origin": f"http://127.0.0.1:{port}",
                "Sec-Fetch-Site": "same-origin",
            })
        _apply_connection_deadline(connection, deadline)
        connection.request(
            method,
            path,
            headers=headers,
        )
        _hard_remaining(deadline)
        _apply_connection_deadline(connection, deadline)
        response = connection.getresponse()
        _hard_remaining(deadline)
        parts: list[bytes] = []
        while True:
            _apply_connection_deadline(connection, deadline)
            chunk = response.read(64 * 1024)
            _hard_remaining(deadline)
            if not chunk:
                break
            parts.append(chunk)
        payload = b"".join(parts)
        return response.status, json.loads(payload) if payload else None
    finally:
        connection.close()


def _stream_upload(
    port: int,
    source: Path,
    *,
    timeout_seconds: float | None = None,
    hard_deadline: float | None = None,
) -> tuple[dict[str, Any], float]:
    if hard_deadline is None:
        hard_deadline = time.monotonic() + float(timeout_seconds or 30.0)
    boundary = "chatgpt-archive-acceptance-boundary"
    preamble = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="archive.zip"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode("ascii")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    total = len(preamble) + source.stat().st_size + len(suffix)
    connection = http.client.HTTPConnection(
        "127.0.0.1", port, timeout=max(0.01, min(3600.0, _hard_remaining(hard_deadline)))
    )
    started = time.perf_counter()
    try:
        _apply_connection_deadline(connection, hard_deadline)
        connection.putrequest("POST", "/api/import/upload", skip_host=True)
        connection.putheader("Host", f"127.0.0.1:{port}")
        connection.putheader("Origin", f"http://127.0.0.1:{port}")
        connection.putheader("Sec-Fetch-Site", "same-origin")
        connection.putheader("Accept", "application/json")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(total))
        _hard_remaining(hard_deadline)
        _apply_connection_deadline(connection, hard_deadline)
        connection.endheaders()
        _hard_remaining(hard_deadline)
        _apply_connection_deadline(connection, hard_deadline)
        connection.send(preamble)
        _hard_remaining(hard_deadline)
        with source.open("rb") as stream:
            while True:
                _hard_remaining(hard_deadline)
                chunk = stream.read(READ_CHUNK)
                _hard_remaining(hard_deadline)
                if not chunk:
                    break
                _apply_connection_deadline(connection, hard_deadline)
                connection.send(chunk)
                _hard_remaining(hard_deadline)
        _apply_connection_deadline(connection, hard_deadline)
        connection.send(suffix)
        _hard_remaining(hard_deadline)
        _apply_connection_deadline(connection, hard_deadline)
        response = connection.getresponse()
        _hard_remaining(hard_deadline)
        response_parts: list[bytes] = []
        while True:
            _apply_connection_deadline(connection, hard_deadline)
            response_chunk = response.read(64 * 1024)
            _hard_remaining(hard_deadline)
            if not response_chunk:
                break
            response_parts.append(response_chunk)
        payload = b"".join(response_parts)
        if response.status not in {200, 201, 202}:
            try:
                error_payload = json.loads(payload)
                detail = error_payload.get("detail") if isinstance(error_payload, dict) else None
                code = (
                    detail
                    if isinstance(detail, str)
                    else error_payload.get("code")
                    if isinstance(error_payload, dict)
                    else None
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                code = None
            safe_code = (
                code
                if isinstance(code, str)
                and code.replace("_", "").isalnum()
                else f"upload_http_{response.status}"
            )
            raise RuntimeError(safe_code)
        value = json.loads(payload)
        if not isinstance(value, dict) or not isinstance(value.get("job_id"), str):
            raise RuntimeError("upload_job_response_invalid")
        return value, time.perf_counter() - started
    finally:
        connection.close()


def _apply_connection_deadline(
    connection: http.client.HTTPConnection,
    hard_deadline: float,
) -> float:
    """Apply the remaining absolute budget to the next socket boundary."""

    remaining = max(0.01, min(3600.0, _hard_remaining(hard_deadline)))
    connection.timeout = remaining
    if connection.sock is not None:
        connection.sock.settimeout(remaining)
    return remaining


def _linux_process_sample(pid: int) -> tuple[int, float] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2 :].split()
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        cpu_seconds = (int(fields[11]) + int(fields[12])) / clock_ticks
        rss_bytes = max(0, int(fields[21])) * page_size
        return rss_bytes, cpu_seconds
    except (OSError, ValueError, IndexError):
        return None


def _darwin_process_sample(pid: int) -> tuple[int, float] | None:
    try:
        import ctypes

        class ProcTaskInfo(ctypes.Structure):
            _fields_ = [
                ("virtual_size", ctypes.c_uint64),
                ("resident_size", ctypes.c_uint64),
                ("total_user", ctypes.c_uint64),
                ("total_system", ctypes.c_uint64),
                ("threads_user", ctypes.c_uint64),
                ("threads_system", ctypes.c_uint64),
                ("policy", ctypes.c_int32),
                ("faults", ctypes.c_int32),
                ("pageins", ctypes.c_int32),
                ("cow_faults", ctypes.c_int32),
                ("messages_sent", ctypes.c_int32),
                ("messages_received", ctypes.c_int32),
                ("syscalls_mach", ctypes.c_int32),
                ("syscalls_unix", ctypes.c_int32),
                ("csw", ctypes.c_int32),
                ("threadnum", ctypes.c_int32),
                ("numrunning", ctypes.c_int32),
                ("priority", ctypes.c_int32),
            ]

        info = ProcTaskInfo()
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        returned = proc_pidinfo(
            int(pid), 4, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        if returned != ctypes.sizeof(info):
            return None
        cpu_seconds = (info.total_user + info.total_system) / 1_000_000_000
        return int(info.resident_size), float(cpu_seconds)
    except (OSError, ValueError, AttributeError):
        return None


def _process_sample(pid: int) -> tuple[int, float | None]:
    """Read one process without spawning helpers that contaminate CPU totals."""

    sample = (
        _darwin_process_sample(pid)
        if sys.platform == "darwin"
        else _linux_process_sample(pid)
        if sys.platform.startswith("linux")
        else None
    )
    return sample if sample is not None else (0, None)


def _process_sample_backend() -> str:
    if sys.platform == "darwin":
        return "darwin_libproc"
    if sys.platform.startswith("linux"):
        return "linux_procfs"
    return "unavailable"


def _tree_metrics(root: Path, database: Path) -> dict[str, int | None]:
    total = 0
    pipeline_input_copy = 0
    observable_temp = 0
    for path in root.rglob("*"):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        total += size
        name = path.name
        is_pipeline_input_copy = (
            name == "upload.zip"
            or any(part.startswith("chatgpt-archive-upload-") for part in path.parts)
        )
        if is_pipeline_input_copy:
            pipeline_input_copy += size
        if (
            "tmp" in path.relative_to(root).parts
            and not is_pipeline_input_copy
            and name != database.name
            and not name.startswith(database.name)
        ):
            observable_temp += size
    def size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0
    return {
        "workspace_bytes": total,
        # Starlette's multipart spool may be unnamed or already unlinked and
        # is therefore not observable through workspace traversal.
        "upload_spool_bytes": None,
        "pipeline_input_copy_bytes": pipeline_input_copy,
        "database_bytes": size(database),
        "wal_bytes": size(Path(str(database) + "-wal")),
        "journal_bytes": size(Path(str(database) + "-journal")),
        "observable_temp_bytes": observable_temp,
    }


def _sampling_coverage(
    entered_stages: list[str],
    observed_stages: list[str],
    *,
    thread_alive: bool,
) -> tuple[list[str], bool]:
    unsampled = sorted(set(entered_stages).difference(observed_stages))
    return unsampled, not unsampled and not thread_alive


class _ResourceSampler:
    """Low-overhead sampled filesystem/process diagnostics for one run."""

    def __init__(
        self,
        root: Path,
        database: Path,
        process: subprocess.Popen[bytes],
        *,
        interval_seconds: float,
        heartbeat_seconds: float,
        started: float,
    ) -> None:
        self.root = root
        self.database = database
        self.process = process
        self.interval_seconds = max(0.05, float(interval_seconds))
        self.heartbeat_seconds = max(1.0, float(heartbeat_seconds))
        self.started = started
        self.stage = "pre_upload"
        self.job_status = "not_started"
        self.peaks: dict[str, int] = {}
        self.entered_stages: set[str] = {self.stage}
        self.observed_stages: set[str] = set()
        self.sampled_stage_counts: dict[str, int] = {}
        self.peak_rss_bytes = 0
        self.peak_cpu_percent_sample = 0.0
        self.process_cpu_first: float | None = None
        self.process_cpu_latest: float | None = None
        self.process_sample_at: float | None = None
        self.sampling_overhead_wall_seconds = 0.0
        self.samples = 0
        self._next_heartbeat = time.monotonic() + self.heartbeat_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name="real-pipeline-resource-sampler",
            daemon=True,
        )

    def start(self) -> None:
        self.sample_now()
        self._thread.start()

    def update(self, *, stage: str | None = None, job_status: str | None = None) -> bool:
        stage_changed = False
        with self._lock:
            changed = False
            if stage is not None:
                stage_changed = stage != self.stage
                changed = changed or stage_changed
                self.stage = stage
                self.entered_stages.add(stage)
            if job_status is not None:
                changed = changed or job_status != self.job_status
                self.job_status = job_status
        if stage_changed:
            # Stage transitions are few and important. Sampling them
            # synchronously prevents a long interval from hiding short stages.
            self.sample_now()
        return changed

    def sample_now(self) -> None:
        sample_started = time.perf_counter()
        current = _tree_metrics(self.root, self.database)
        rss, process_cpu = _process_sample(self.process.pid)
        sampled_at = time.monotonic()
        with self._lock:
            for key, value in current.items():
                if value is None:
                    continue
                self.peaks[key] = max(self.peaks.get(key, 0), int(value))
            self.peak_rss_bytes = max(self.peak_rss_bytes, rss)
            cpu_percent = 0.0
            if process_cpu is not None:
                if self.process_cpu_first is None:
                    self.process_cpu_first = process_cpu
                if (
                    self.process_cpu_latest is not None
                    and self.process_sample_at is not None
                    and sampled_at > self.process_sample_at
                ):
                    cpu_percent = max(
                        0.0,
                        (process_cpu - self.process_cpu_latest)
                        / (sampled_at - self.process_sample_at)
                        * 100.0,
                    )
                self.process_cpu_latest = process_cpu
                self.process_sample_at = sampled_at
            self.peak_cpu_percent_sample = max(
                self.peak_cpu_percent_sample, cpu_percent
            )
            self.sampling_overhead_wall_seconds += max(
                0.0, time.perf_counter() - sample_started
            )
            self.samples += 1
            stage = self.stage
            self.observed_stages.add(stage)
            self.sampled_stage_counts[stage] = self.sampled_stage_counts.get(stage, 0) + 1
            job_status = self.job_status
            rss_value = self.peak_rss_bytes
            storage = dict(self.peaks)
        now = time.monotonic()
        if now >= self._next_heartbeat:
            print(
                "acceptance_heartbeat "
                + json.dumps(
                    {
                        "elapsed_seconds": round(now - self.started, 3),
                        "stage": stage,
                        "job_status": job_status,
                        "rss_bytes": rss_value,
                        "sampled_storage_bytes": storage,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                file=sys.stderr,
                flush=True,
            )
            self._next_heartbeat = now + self.heartbeat_seconds

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.sample_now()

    def stop(self, *, hard_deadline: float | None = None) -> dict[str, Any]:
        self.sample_now()
        self._stop.set()
        join_budget = max(0.01, self.interval_seconds * 4)
        if hard_deadline is not None:
            join_budget = max(0.01, min(join_budget, hard_deadline - time.monotonic()))
        self._thread.join(timeout=join_budget)
        thread_alive = self._thread.is_alive()
        with self._lock:
            peaks = dict(self.peaks)
            samples = self.samples
            peak_rss = self.peak_rss_bytes
            peak_cpu = self.peak_cpu_percent_sample
            entered_stages = sorted(self.entered_stages)
            observed_stages = sorted(self.observed_stages)
            sampled_stage_counts = dict(sorted(self.sampled_stage_counts.items()))
            process_cpu_seconds = (
                max(0.0, self.process_cpu_latest - self.process_cpu_first)
                if self.process_cpu_latest is not None
                and self.process_cpu_first is not None
                else None
            )
            sampling_overhead = self.sampling_overhead_wall_seconds
        unsampled_entered_stages, coverage_complete = _sampling_coverage(
            entered_stages,
            observed_stages,
            thread_alive=thread_alive,
        )
        elapsed = max(0.0, time.monotonic() - self.started)
        return {
            "sampling_interval_seconds": self.interval_seconds,
            "sample_count": samples,
            "process_sample_backend": _process_sample_backend(),
            "sampling_overhead_wall_seconds": sampling_overhead,
            "sampling_overhead_ratio": (
                sampling_overhead / elapsed if elapsed > 0 else None
            ),
            "process_cpu_seconds": process_cpu_seconds,
            "process_cpu_semantics": "target_server_process_only",
            "sampled_peaks": peaks,
            "peak_rss_bytes": peak_rss,
            "peak_cpu_percent_sample": peak_cpu,
            "entered_stages": entered_stages,
            "observed_stages": observed_stages,
            "sampled_stage_counts": sampled_stage_counts,
            "unsampled_entered_stages": unsampled_entered_stages,
            "coverage_complete": coverage_complete,
            "sampler_thread_alive": thread_alive,
            "sampler_cleanup_failed": thread_alive,
            "web_index_live_bytes": None,
            "web_index_staging_bytes": None,
            "sqlite_unlinked_temp_bytes": None,
            "unobservable": [
                "web index live/staging objects share the SQLite database file",
                "SQLite may unlink TEMP files while they are open",
                "multipart spool bytes are reported by the upload route when observable, not inferred from upload.zip",
            ],
        }


def _wait_server(
    port: int,
    process: subprocess.Popen[bytes],
    *,
    hard_deadline: float,
) -> None:
    deadline = min(time.monotonic() + 60, hard_deadline)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("production_server_exited")
        try:
            status, _payload = _request_json(
                port,
                "GET",
                "/api/health",
                timeout=min(2.0, max(0.01, hard_deadline - time.monotonic())),
                hard_deadline=hard_deadline,
            )
            if status in {200, 409, 503}:
                return
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(min(0.1, max(0.0, hard_deadline - time.monotonic())))
    if time.monotonic() >= hard_deadline:
        raise HardRunTimeout("hard_timeout")
    raise RuntimeError("production_server_start_timeout")


def _bounded_smoke(port: int, *, hard_deadline: float) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name, path in (
        ("health", "/api/health"),
        ("stats", "/api/stats"),
        ("schema", "/api/schema"),
        ("suggest", "/api/search/suggest?q=&limit=1"),
    ):
        remaining = hard_deadline - time.monotonic()
        if remaining <= 0:
            raise HardRunTimeout("hard_timeout")
        status, payload = _request_json(
            port, "GET", path, timeout=min(30.0, remaining), hard_deadline=hard_deadline
        )
        results[name] = {"status": status, "json_object": isinstance(payload, dict)}
        if status != 200:
            raise RuntimeError(f"{name}_smoke_failed")
    remaining = hard_deadline - time.monotonic()
    if remaining <= 0:
        raise HardRunTimeout("hard_timeout")
    status, page = _request_json(
        port,
        "GET",
        "/api/conversations?limit=1&offset=0",
        timeout=min(30.0, remaining), hard_deadline=hard_deadline,
    )
    if status != 200 or not isinstance(page, dict):
        raise RuntimeError("list_smoke_failed")
    items = page.get("items")
    results["list"] = {
        "status": status,
        "returned": len(items) if isinstance(items, list) else 0,
        "total_nonnegative": isinstance(page.get("total"), int) and page["total"] >= 0,
    }
    if isinstance(items, list) and items:
        conversation_id = items[0].get("conversation_id")
        if not isinstance(conversation_id, str):
            raise RuntimeError("list_identifier_missing")
        encoded = urlencode({"conversation_id": conversation_id})
        for name, path in (
            ("detail", f"/api/by-id/conversation?{encoded}"),
            ("messages", f"/api/by-id/messages?{encoded}&limit=1&offset=0"),
            (
                "message_search",
                f"/api/search/messages?{encoded}&q=&limit=1&offset=0&count_total=false",
            ),
        ):
            remaining = hard_deadline - time.monotonic()
            if remaining <= 0:
                raise HardRunTimeout("hard_timeout")
            item_status, payload = _request_json(
                port, "GET", path, timeout=min(30.0, remaining), hard_deadline=hard_deadline
            )
            results[name] = {
                "status": item_status,
                "json_object": isinstance(payload, dict),
            }
            if item_status != 200:
                raise RuntimeError(f"{name}_smoke_failed")
    return results


def _stop_process_bounded(
    process: subprocess.Popen[bytes], hard_deadline: float
) -> dict[str, Any]:
    timeout_seconds = max(0.05, min(5.0, hard_deadline - time.monotonic()))
    _stdout, _stderr, lifecycle = terminate_kill_reap(
        process,
        timeout_seconds=timeout_seconds,
        owned_process_group=True,
    )
    result: dict[str, Any] = dict(lifecycle)
    result.update({
        "attempted": True,
        "complete": bool(lifecycle["process_tree_cleanup_complete"]),
        "terminated": bool(lifecycle["terminate_attempted"]),
        "killed": bool(lifecycle["kill_attempted"]),
        "hard_timeout": bool(
            not lifecycle["process_tree_cleanup_complete"]
            and time.monotonic() >= hard_deadline
        ),
    })
    return result


def _remove_tree_bounded(
    path: Path,
    *,
    hard_deadline: float,
    delay_seconds: float = 0.0,
    force_failure: bool = False,
) -> dict[str, Any]:
    """Remove one exact owned tree in a killable subprocess."""

    result: dict[str, Any] = {
        "attempted": True,
        "complete": False,
        "terminated": False,
        "killed": False,
        "terminate_attempted": False,
        "kill_attempted": False,
        "reaped": False,
        "reap_incomplete": False,
        "hard_timeout": False,
        "remaining_path": path.exists(),
    }
    if force_failure:
        result["error_type"] = "PermissionError"
        return result
    remaining = hard_deadline - time.monotonic()
    if remaining <= 0:
        result["deadline_exhausted_before_cleanup"] = True
        return result
    script = (
        "import pathlib,shutil,sys,time;"
        "time.sleep(float(sys.argv[2]));"
        "shutil.rmtree(pathlib.Path(sys.argv[1]))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path), str(max(0.0, delay_seconds))],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_worker_environment_for_cleanup(),
        start_new_session=os.name == "posix",
    )
    result["pid"] = process.pid
    result["child_pid"] = process.pid
    try:
        wait_budget = max(0.01, hard_deadline - time.monotonic())
        process.wait(timeout=max(0.01, wait_budget * 0.75))
    except subprocess.TimeoutExpired:
        _stdout, _stderr, lifecycle = terminate_kill_reap(
            process,
            timeout_seconds=max(
                0.05, min(0.5, hard_deadline - time.monotonic())
            ),
            owned_process_group=True,
        )
        result.update(lifecycle)
        result["terminated"] = bool(lifecycle["terminate_attempted"])
        result["killed"] = bool(lifecycle["kill_attempted"])
    if "process_tree_cleanup_complete" not in result:
        _stdout, _stderr, lifecycle = terminate_kill_reap(
            process,
            timeout_seconds=max(
                0.05, min(0.25, hard_deadline - time.monotonic())
            ),
            owned_process_group=True,
        )
        result.update(lifecycle)
    result["return_code"] = process.poll()
    result["reaped"] = process.poll() is not None
    result["reap_incomplete"] = not result["reaped"]
    result["hard_timeout"] = bool(
        result["reap_incomplete"] and time.monotonic() >= hard_deadline
    )
    result["remaining_path"] = path.exists()
    result["complete"] = bool(
        result["reaped"]
        and process.returncode == 0
        and not result["remaining_path"]
        and result.get("process_tree_cleanup_complete", True)
    )
    if process.poll() not in {None, 0} and "error_type" not in result:
        result["error_type"] = "CleanupSubprocessError"
    return result


def _worker_environment_for_cleanup() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONTRACEMALLOC", None)
    return env


def _run_once(
    source: Path,
    python: Path,
    run_number: int,
    *,
    max_pipeline_seconds: float,
    hard_run_seconds: float,
    sample_interval_seconds: float,
    heartbeat_seconds: float,
    force_cleanup_failure: bool = False,
    cleanup_delay_seconds: float = 0.0,
) -> dict[str, Any]:
    external_started = time.perf_counter()
    hard_deadline = time.monotonic() + hard_run_seconds
    inherited_tracemalloc_disabled = tracemalloc.is_tracing()
    if inherited_tracemalloc_disabled:
        tracemalloc.stop()
    pipeline_started: float | None = None
    workspace = Path(tempfile.mkdtemp(prefix="chatgpt-real-pipeline-"))
    database = workspace / "fresh.db"
    temp_root = workspace / "tmp"
    temp_root.mkdir()
    log_path = workspace / "server.log"
    process: subprocess.Popen[bytes] | None = None
    sampler: _ResourceSampler | None = None
    port: int | None = None
    job_id: str | None = None
    metrics: dict[str, Any] = {
        "run": run_number,
        "success": False,
        "pipeline_success": False,
        "cleanup_success": False,
        "correctness_pass": False,
        "performance_pass": False,
        "hard_timeout": False,
        "cleanup": {"attempted": True, "complete": False},
        "server_shutdown": {"attempted": False, "complete": False},
        "timing_semantics": {
            "job_elapsed_seconds": "server job snapshot only",
            "workload_wall_seconds": "upload start through terminal job and bounded smoke checks",
            "required_pipeline_wall_seconds": "upload start through required server shutdown and required cleanup attempt",
            "external_process_wall_seconds": "inspect, preflight, server lifecycle, workload, shutdown, and cleanup",
            "formal_threshold": "required_pipeline_wall_seconds",
        },
        "instrumentation": {
            "measurement_mode": "performance_without_tracemalloc",
            "inherited_tracemalloc_disabled": inherited_tracemalloc_disabled,
            "hard_run_seconds": hard_run_seconds,
            "sample_interval_seconds": sample_interval_seconds,
            "heartbeat_seconds": heartbeat_seconds,
        },
    }
    try:
        inspect_before = _identity(source)
        inspect_started = time.perf_counter()
        inspected = subprocess.run(
            [
                str(python),
                str(ROOT / "chatgpt_archive.py"),
                "--log-level",
                "none",
                "inspect",
                "--input",
                str(source),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_untraced_environment(TMPDIR=str(temp_root)),
            check=False,
            timeout=_hard_remaining(hard_deadline),
        )
        metrics["inspect_seconds"] = time.perf_counter() - inspect_started
        metrics["inspect_exit_code"] = inspected.returncode
        if inspected.returncode != 0:
            raise RuntimeError("production_inspect_failed")
        inspect_after = _identity(source)
        if not _identity_equal(inspect_before, inspect_after):
            raise RuntimeError("input_identity_changed_after_inspect")
        metrics["inspect_input_identity"] = {
            "before": _public_identity(inspect_before),
            "after": _public_identity(inspect_after),
            "unchanged": True,
        }

        preflight_before = _identity(source)
        source_scan_started = time.perf_counter()
        resolved = resolve_input(str(source), ROOT)
        entries = list_source_entries(resolved)
        selected = select_conversation_sources(entries)
        with source.open("rb") as source_stream:
            with zipfile.ZipFile(source_stream) as archive:
                central_directory_members = len(archive.infolist())
        metrics["zip_preflight_seconds"] = time.perf_counter() - source_scan_started
        preflight_after = _identity(source)
        if not _identity_equal(preflight_before, preflight_after):
            raise RuntimeError("input_identity_changed_after_preflight")
        metrics["preflight_input_identity"] = {
            "before": _public_identity(preflight_before),
            "after": _public_identity(preflight_after),
            "unchanged": True,
        }
        metrics["archive"] = {
            "compressed_bytes": source.stat().st_size,
            "central_directory_members": central_directory_members,
            "discovered_non_metadata_files": len(entries),
            "selected_json_members": len(selected),
            "selected_logical_json_bytes": sum(entry.size for entry in selected),
        }

        upload_before = _identity(source)
        port = _free_port()
        env = _untraced_environment(
            TMPDIR=str(temp_root),
            PYTHONUNBUFFERED="1",
        )
        log_stream = log_path.open("wb")
        try:
            process = subprocess.Popen(
                [
                    str(python),
                    str(ROOT / "chatgpt_archive.py"),
                    "--db",
                    str(database),
                    "--log-level",
                    "none",
                    "web",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=env,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=os.name == "posix",
            )
        finally:
            log_stream.close()
        _wait_server(port, process, hard_deadline=hard_deadline)
        pipeline_started = time.perf_counter()
        sampler = _ResourceSampler(
            workspace,
            database,
            process,
            interval_seconds=sample_interval_seconds,
            heartbeat_seconds=heartbeat_seconds,
            started=pipeline_started,
        )
        sampler.start()
        sampler.update(stage="upload", job_status="uploading")
        sampler.sample_now()
        try:
            job, upload_seconds = _stream_upload(
                port,
                source,
                hard_deadline=hard_deadline,
            )
        except (TimeoutError, socket.timeout) as exc:
            if time.monotonic() >= hard_deadline:
                raise HardRunTimeout("hard_timeout") from exc
            raise
        metrics["upload_seconds"] = upload_seconds
        job_id = job["job_id"]
        sampler.update(stage="job_poll", job_status=str(job.get("status", "queued")))
        sampler.sample_now()
        while True:
            remaining = _hard_remaining(hard_deadline)
            status, snapshot = _request_json(
                port,
                "GET",
                f"/api/import/jobs/{job_id}",
                timeout=min(30.0, remaining),
                hard_deadline=hard_deadline,
            )
            if status != 200 or not isinstance(snapshot, dict):
                raise RuntimeError("job_poll_failed")
            stage_changed = sampler.update(
                stage=str(snapshot.get("stage") or "job_poll"),
                job_status=str(snapshot.get("status") or "unknown"),
            )
            if stage_changed:
                sampler.sample_now()
            if snapshot.get("status") in TERMINAL_JOB_STATES:
                job = snapshot
                break
            time.sleep(min(0.25, max(0.0, hard_deadline - time.monotonic())))
        if job.get("status") != "succeeded":
            raise RuntimeError(str(job.get("error_code") or "production_job_failed"))
        upload_after = _identity(source)
        if not _identity_equal(upload_before, upload_after):
            raise RuntimeError("input_identity_changed_after_upload")
        metrics["input_identity"] = {
            "before": _public_identity(upload_before),
            "after": _public_identity(upload_after),
            "unchanged": True,
        }
        metrics["job"] = {
            "status": job.get("status"),
            "completion_outcome": job.get("completion_outcome"),
            "canonical_import_outcome": job.get("canonical_import_outcome"),
            "elapsed_seconds": job.get("elapsed_seconds"),
            "stage_timings": job.get("stage_timings"),
            "committed_conversations": (job.get("summary") or {}).get(
                "committed_conversations"
            ),
            "committed_nodes": (job.get("summary") or {}).get("committed_nodes"),
            "warnings": (job.get("summary") or {}).get("warnings"),
            "web_index_status": (job.get("web_index") or {}).get("status", "built"),
            "multipart_spool": job.get("multipart_spool"),
        }
        sampler.update(stage="bounded_smoke", job_status="succeeded")
        sampler.sample_now()
        metrics["smoke"] = _bounded_smoke(port, hard_deadline=hard_deadline)
        metrics["workload_wall_seconds"] = time.perf_counter() - pipeline_started
        # Compatibility aliases retain the previous workload-only meaning.
        metrics["pipeline_wall_seconds"] = metrics["workload_wall_seconds"]
        metrics["wall_seconds"] = metrics["workload_wall_seconds"]
        metrics["pipeline_success"] = True
        return metrics
    except BaseException as exc:
        hard_timeout = isinstance(exc, HardRunTimeout) or (
            isinstance(exc, (subprocess.TimeoutExpired, TimeoutError, socket.timeout))
            and time.monotonic() >= hard_deadline
        )
        if hard_timeout:
            metrics["hard_timeout"] = True
        metrics["error_code"] = (
            "hard_timeout"
            if hard_timeout
            else str(exc)
            if isinstance(exc, RuntimeError)
            and str(exc).replace("_", "").isalnum()
            else type(exc).__name__
        )
        return metrics
    finally:
        if pipeline_started is not None and "workload_wall_seconds" not in metrics:
            metrics["workload_wall_seconds"] = (
                time.perf_counter() - pipeline_started
            )
            metrics["pipeline_wall_seconds"] = metrics["workload_wall_seconds"]
            metrics["wall_seconds"] = metrics["workload_wall_seconds"]
        if metrics.get("hard_timeout") and port is not None and job_id is not None:
            try:
                _request_json(
                    port,
                    "POST",
                    f"/api/import/jobs/{job_id}/web-index/cancel",
                    timeout=2.0,
                )
                metrics["hard_timeout_cancel_requested"] = True
            except (OSError, ValueError, json.JSONDecodeError):
                metrics["hard_timeout_cancel_requested"] = False
        shutdown_started = time.perf_counter()
        metrics["server_shutdown"]["attempted"] = process is not None
        if sampler is not None:
            sampler.update(stage="server_shutdown")
            sampler.sample_now()
        if process is not None:
            metrics["server_shutdown"].update(
                _stop_process_bounded(process, hard_deadline)
            )
        metrics["server_shutdown"]["seconds"] = (
            time.perf_counter() - shutdown_started
        )
        if sampler is not None:
            sampler.sample_now()
        cleanup_started = time.perf_counter()
        if sampler is not None:
            sampler.update(stage="required_cleanup")
            sampler.sample_now()
            resource_metrics = sampler.stop(hard_deadline=hard_deadline)
            metrics["resource_sampler"] = resource_metrics
            metrics["child_cpu_seconds"] = resource_metrics[
                "process_cpu_seconds"
            ]
            metrics["peak_rss_bytes"] = resource_metrics["peak_rss_bytes"]
            metrics["peak_cpu_percent_sample"] = resource_metrics[
                "peak_cpu_percent_sample"
            ]
            metrics["storage_peaks"] = resource_metrics["sampled_peaks"]
        cleanup_result = _remove_tree_bounded(
            workspace,
            hard_deadline=hard_deadline,
            delay_seconds=cleanup_delay_seconds,
            force_failure=force_cleanup_failure,
        )
        metrics["cleanup"].update(cleanup_result)
        metrics["cleanup_success"] = bool(metrics["cleanup"]["complete"])
        metrics["cleanup"]["seconds"] = time.perf_counter() - cleanup_started
        if pipeline_started is not None:
            metrics["required_pipeline_wall_seconds"] = (
                time.perf_counter() - pipeline_started
            )
        if time.monotonic() >= hard_deadline:
            metrics["hard_timeout"] = True
            metrics.setdefault("error_code", "hard_timeout")
        metrics["correctness_pass"] = bool(
            metrics.get("pipeline_success")
            and metrics["cleanup_success"]
            and metrics["server_shutdown"].get("complete")
            and not (metrics.get("resource_sampler") or {}).get("sampler_cleanup_failed")
            and not metrics.get("hard_timeout")
        )
        metrics["external_process_wall_seconds"] = (
            time.perf_counter() - external_started
        )
        metrics["external_wall_seconds"] = metrics[
            "external_process_wall_seconds"
        ]
        _evaluate_acceptance(
            metrics,
            max_pipeline_seconds=max_pipeline_seconds,
        )


def _aggregate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "workload_wall_seconds",
        "required_pipeline_wall_seconds",
        "external_process_wall_seconds",
        "pipeline_wall_seconds",
        "external_wall_seconds",
        "upload_seconds",
        "peak_rss_bytes",
        "child_cpu_seconds",
    )
    result: dict[str, Any] = {
        "total_runs": len(samples),
        "successful_runs": sum(bool(sample.get("success")) for sample in samples),
    }
    for field in fields:
        all_values = [
            float(sample[field]) if isinstance(sample.get(field), (int, float)) else None
            for sample in samples
        ]
        values = [value for value in all_values if value is not None]
        result[field] = {
            "median": statistics.median(values) if values else None,
            "worst": max(values) if values else None,
            "all": all_values,
            "sample_count": len(samples),
            "measured_count": len(values),
        }
    job_values = [
        float(sample["job"]["elapsed_seconds"])
        for sample in samples
        if isinstance((sample.get("job") or {}).get("elapsed_seconds"), (int, float))
    ]
    result["job_elapsed_seconds"] = {
        "median": statistics.median(job_values) if job_values else None,
        "worst": max(job_values) if job_values else None,
        "all": [
            float(sample["job"]["elapsed_seconds"])
            if isinstance((sample.get("job") or {}).get("elapsed_seconds"), (int, float))
            else None
            for sample in samples
        ],
        "sample_count": len(samples),
        "measured_count": len(job_values),
    }
    result["performance_pass"] = all(
        bool(sample.get("performance_pass")) for sample in samples
    )
    result["correctness_pass"] = all(
        bool(sample.get("correctness_pass")) for sample in samples
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--max-pipeline-seconds",
        "--max-job-seconds",
        dest="max_pipeline_seconds",
        type=float,
        default=300.0,
        help=(
            "Formal upload-through-server-shutdown-and-required-cleanup threshold. "
            "--max-job-seconds is a compatibility alias."
        ),
    )
    parser.add_argument(
        "--hard-run-seconds",
        type=float,
        default=3600.0,
        help=(
            "Independent operational watchdog for a stuck run; this is not the "
            "300-second performance threshold."
        ),
    )
    parser.add_argument(
        "--sample-interval-seconds",
        type=float,
        default=0.25,
        help="Filesystem/process sampling interval for content-free peak diagnostics.",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=30.0,
        help="Content-free progress heartbeat interval written to stderr.",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
    )
    parser.add_argument(
        "--confirm-read-only-real-input",
        action="store_true",
        help="Required opt-in. The input is opened read-only and never staged or deleted.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run one tiny synthetic production pipeline; no real input is used.",
    )
    parser.add_argument(
        "--self-test-cleanup-failure",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--self-test-cleanup-delay-seconds",
        type=float,
        default=0.0,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--self-test-outer-cleanup-failure",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    synthetic_root: Path | None = None
    if (
        args.self_test
        or args.self_test_cleanup_failure
        or args.self_test_cleanup_delay_seconds > 0
        or args.self_test_outer_cleanup_failure
    ):
        synthetic_root = Path(tempfile.mkdtemp(prefix="chatgpt-harness-selftest-"))
        source = synthetic_root / "synthetic.zip"
        payload = [
            {
                "id": "synthetic-conversation",
                "title": "Synthetic",
                "current_node": "node-1",
                "mapping": {
                    "node-1": {
                        "id": "node-1",
                        "parent": None,
                        "children": [],
                        "message": {
                            "id": "message-1",
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["synthetic"]},
                        },
                    }
                },
            }
        ]
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "conversations.json",
                json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            )
        args.runs = 1
    elif args.input is None:
        parser.error("--input is required unless --self-test is used")
    elif not args.confirm_read_only_real_input:
        parser.error("--confirm-read-only-real-input is required")
    else:
        source = args.input.expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        parser.error("--input must be a regular ZIP file")
    if (
        not isinstance(args.max_pipeline_seconds, float)
        or args.max_pipeline_seconds < 0
    ):
        parser.error("--max-pipeline-seconds must be nonnegative")
    if args.hard_run_seconds <= 0:
        parser.error("--hard-run-seconds must be positive")
    if args.sample_interval_seconds <= 0:
        parser.error("--sample-interval-seconds must be positive")
    if args.heartbeat_seconds <= 0:
        parser.error("--heartbeat-seconds must be positive")
    if args.self_test_cleanup_delay_seconds < 0:
        parser.error("--self-test-cleanup-delay-seconds must be nonnegative")
    samples: list[dict[str, Any]] = []
    synthetic_fixture_cleanup: dict[str, Any] = {
        "attempted": False,
        "complete": True,
    }
    try:
        samples = [
            _run_once(
                source,
                args.python.expanduser().resolve(),
                run_number,
                max_pipeline_seconds=args.max_pipeline_seconds,
                hard_run_seconds=args.hard_run_seconds,
                sample_interval_seconds=args.sample_interval_seconds,
                heartbeat_seconds=args.heartbeat_seconds,
                force_cleanup_failure=args.self_test_cleanup_failure,
                cleanup_delay_seconds=args.self_test_cleanup_delay_seconds,
            )
            for run_number in range(1, max(1, args.runs) + 1)
        ]
    finally:
        if synthetic_root is not None:
            synthetic_fixture_cleanup = _remove_tree_bounded(
                synthetic_root,
                hard_deadline=time.monotonic() + 5.0,
                force_failure=args.self_test_outer_cleanup_failure,
            )
            if (
                args.self_test_outer_cleanup_failure
                and not synthetic_fixture_cleanup.get("complete")
            ):
                synthetic_fixture_cleanup["recovery_cleanup"] = (
                    _remove_tree_bounded(
                        synthetic_root,
                        hard_deadline=time.monotonic() + 5.0,
                    )
                )
    canonical_runs_success = bool(samples) and all(
        sample.get("success") for sample in samples
    )
    acceptance_success = bool(
        canonical_runs_success and synthetic_fixture_cleanup.get("complete")
    )
    output = {
        "schema": "chatgpt-sqlite-webui-real-pipeline-v3",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "runs": len(samples),
        "aggregate": _aggregate(samples),
        "samples": samples,
        "canonical_runs_success": canonical_runs_success,
        "synthetic_fixture_cleanup": synthetic_fixture_cleanup,
        "outer_cleanup_complete": bool(synthetic_fixture_cleanup.get("complete")),
        "outer_cleanup_warning": (
            None
            if synthetic_fixture_cleanup.get("complete")
            else synthetic_fixture_cleanup.get("error_type", "cleanup_incomplete")
        ),
        "outer_cleanup_remaining_owned_path": bool(
            synthetic_fixture_cleanup.get("remaining_path")
        ),
        "acceptance_success": acceptance_success,
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0 if acceptance_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
