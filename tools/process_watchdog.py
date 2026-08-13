#!/usr/bin/env python3
"""Bounded subprocess-tree termination for acceptance tools."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _prefer_complete(previous: str, candidate: str | bytes | None) -> str:
    value = _text(candidate)
    if not value:
        return previous
    if value.startswith(previous) or len(value) >= len(previous):
        return value
    return previous + value


def _group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _close_pipes(process: subprocess.Popen[str]) -> None:
    for stream in (getattr(process, "stdout", None), getattr(process, "stderr", None)):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def terminate_kill_reap(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float = 0.75,
    owned_process_group: bool = False,
) -> tuple[str, str, dict[str, Any]]:
    """Terminate one owned worker tree and drain its pipes within a deadline.

    On POSIX, callers must start the worker with ``start_new_session=True`` and
    explicitly set ``owned_process_group``. Windows lacks a standard-library
    job-object equivalent, so timeout cleanup is reported as direct-child-only.
    """

    deadline = time.monotonic() + max(0.05, float(timeout_seconds))
    tree_supported = os.name == "posix" and bool(owned_process_group)
    tree_attempted = tree_supported
    stdout = ""
    stderr = ""
    terminate_attempted = False
    kill_attempted = False
    pipe_drain_incomplete = False
    signal_error_types: list[str] = []

    def signal_owned(sig: int) -> None:
        if tree_supported:
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            except OSError as exc:
                signal_error_types.append(type(exc).__name__)
            return
        try:
            if sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except OSError as exc:
            signal_error_types.append(type(exc).__name__)

    if tree_supported or process.poll() is None:
        terminate_attempted = True
        signal_owned(signal.SIGTERM)

    first_budget = max(
        0.01,
        min(0.20, max(0.01, deadline - time.monotonic()) * 0.4),
    )
    try:
        stdout, stderr = process.communicate(timeout=first_budget)
    except subprocess.TimeoutExpired as exc:
        stdout = _prefer_complete(stdout, exc.output)
        stderr = _prefer_complete(stderr, exc.stderr)
        kill_attempted = True
        signal_owned(signal.SIGKILL)
        remaining = max(0.01, deadline - time.monotonic())
        try:
            drained_stdout, drained_stderr = process.communicate(
                timeout=max(0.01, min(0.25, remaining * 0.65))
            )
            stdout = _prefer_complete(stdout, drained_stdout)
            stderr = _prefer_complete(stderr, drained_stderr)
        except subprocess.TimeoutExpired as second_exc:
            stdout = _prefer_complete(stdout, second_exc.output)
            stderr = _prefer_complete(stderr, second_exc.stderr)
            pipe_drain_incomplete = True

    # A descendant may close the inherited pipes and ignore TERM after the
    # direct child exits. Pipe EOF is therefore not proof that the owned group
    # is gone; finish the group cleanup explicitly.
    if tree_supported and _group_exists(process.pid):
        kill_attempted = True
        signal_owned(signal.SIGKILL)

    direct_reaped = process.poll() is not None
    descendants_remaining: bool | None = None
    if tree_supported:
        while time.monotonic() < deadline:
            direct_reaped = process.poll() is not None
            descendants_remaining = _group_exists(process.pid)
            if direct_reaped and not descendants_remaining:
                break
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        direct_reaped = process.poll() is not None
        descendants_remaining = _group_exists(process.pid)
    elif owned_process_group:
        # No standard-library Windows job object is available here.
        descendants_remaining = None

    if pipe_drain_incomplete:
        _close_pipes(process)

    tree_complete = bool(
        direct_reaped
        and (
            descendants_remaining is False
            if tree_supported
            else not owned_process_group
        )
    )
    fields = {
        "child_pid": process.pid,
        "return_code": process.returncode,
        "terminate_attempted": terminate_attempted,
        "kill_attempted": kill_attempted,
        "reaped": direct_reaped,
        "reap_incomplete": not direct_reaped,
        "process_tree_cleanup_attempted": tree_attempted,
        "process_tree_cleanup_supported": tree_supported,
        "process_tree_cleanup_limitation": (
            "windows_direct_child_only_without_job_object"
            if owned_process_group and not tree_supported
            else None
        ),
        "direct_child_reaped": direct_reaped,
        "owned_descendants_remaining": descendants_remaining,
        "process_tree_cleanup_complete": tree_complete,
        "pipe_drain_incomplete": pipe_drain_incomplete,
        "partial_output_available": bool(stdout or stderr),
        "signal_cleanup_degraded": bool(signal_error_types),
        "signal_error_types": sorted(set(signal_error_types)),
    }
    return stdout, stderr, fields
