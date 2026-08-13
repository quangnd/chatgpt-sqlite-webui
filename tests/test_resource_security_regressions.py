from __future__ import annotations

import json
import os
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chatgpt_export_archiver import current_path, exporter, scanner, search, web_api, web_db
from chatgpt_export_archiver.cli import ImportPipelineError, main, run_import_pipeline
from chatgpt_export_archiver.current_path import EffectiveCurrentResourceLimitError
from chatgpt_export_archiver.db import DatabaseMigrationError, connect, init_db, migrate_database
from chatgpt_export_archiver.disk_resources import (
    DiskSpaceInsufficientError,
    migration_capacity_plan,
)
from chatgpt_export_archiver.search import SearchContinuationError
from chatgpt_export_archiver.web_db import WebIndexBuildError, create_web_indexes, web_index_status
from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager, ImportJobStartError
from chatgpt_export_archiver.web_app import create_app
from tests.test_archiver import conversation, write_zip
from tools import acceptance_real_pipeline, acceptance_scale, benchmark_resources
from tools import make_release_zip


class ResourceSecurityRegressions(unittest.TestCase):
    def test_real_pipeline_process_sampling_does_not_spawn_cpu_polluters(self):
        backend = acceptance_real_pipeline._process_sample_backend()
        if backend == "unavailable":
            self.skipTest("native process sampling is unavailable on this platform")
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        samples = [
            acceptance_real_pipeline._process_sample(os.getpid())
            for _index in range(40)
        ]
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        child_cpu_delta = (
            after.ru_utime
            + after.ru_stime
            - before.ru_utime
            - before.ru_stime
        )
        self.assertTrue(all(rss > 0 for rss, _cpu in samples))
        self.assertTrue(all(cpu is not None for _rss, cpu in samples))
        self.assertLess(child_cpu_delta, 0.01)
        self.assertIn(backend, {"darwin_libproc", "linux_procfs"})

    def test_raw_display_sessions_evict_before_crossing_resource_budgets(self):
        import chatgpt_export_archiver.search as search_module

        with search_module._RAW_DISPLAY_ANCHOR_SESSIONS_LOCK:
            search_module._RAW_DISPLAY_ANCHOR_SESSIONS.clear()
        self.addCleanup(search_module._RAW_DISPLAY_ANCHOR_SESSIONS.clear)
        first = {
            "artifact_id": "first",
            "retained_bytes": 8,
            "total_chars": 8,
        }
        second = {
            "artifact_id": "second",
            "retained_bytes": 8,
            "total_chars": 8,
        }
        with mock.patch.object(
            search_module, "RAW_DISPLAY_ANCHOR_SESSION_MAX_BYTES", 10
        ), mock.patch.object(
            search_module, "RAW_DISPLAY_ANCHOR_SESSION_MAX_CHARS", 10
        ):
            self.assertTrue(search_module._store_raw_display_anchor("first", first))
            self.assertTrue(search_module._store_raw_display_anchor("second", second))
        with search_module._RAW_DISPLAY_ANCHOR_SESSIONS_LOCK:
            self.assertNotIn("first", search_module._RAW_DISPLAY_ANCHOR_SESSIONS)
            self.assertIn("second", search_module._RAW_DISPLAY_ANCHOR_SESSIONS)

    def test_unrelated_database_shard_collision_uses_independent_ranges(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            web_db, "_process_lock_name", return_value="writer-shard-00.lock"
        ):
            first = web_db.acquire_writer_process_lock(Path(td) / "one.db")
            try:
                second = web_db.acquire_writer_process_lock(Path(td) / "two.db")
                second.close()
            finally:
                first.close()

    def test_writer_cleanup_attempts_every_handle_and_retains_failure(self):
        handles = [
            (('registry', 'one'), 1),
            (('registry', 'two'), 2),
            (('registry', 'three'), 3),
        ]
        for failed in ({handles[0]}, {handles[1]}, {handles[2]}, {handles[0], handles[2]}):
            with self.subTest(failed=failed):
                lock = web_db._WriterProcessLock(handles.copy(), set(), set())
                attempted: list[object] = []

                def release(handle):
                    attempted.append(handle)
                    if handle in failed:
                        raise OSError("synthetic")

                with mock.patch.object(web_db, "_release_process_lock_handle", side_effect=release):
                    with self.assertRaises(WebIndexBuildError) as caught:
                        lock.close()
                self.assertEqual(caught.exception.code, "writer_process_lock_cleanup_failed")
                self.assertEqual(attempted, list(reversed(handles)))
                self.assertEqual(lock.fds, [handle for handle in handles if handle in failed])
                self.assertEqual(len(caught.exception.cleanup_warnings), len(failed))

    def test_writer_acquire_preserves_primary_when_cleanup_also_fails(self):
        acquired = (("registry", "first"), 1)
        primary = OSError("primary")
        calls = 0

        def acquire(*_args):
            nonlocal calls
            calls += 1
            if calls == 1:
                return acquired
            raise primary

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            web_db, "_database_lock_keys", return_value=[("path", "one"), ("entity", "two")]
        ), mock.patch.object(web_db, "_process_lock_domain", side_effect=[("writer-shard-00.lock", 1), ("writer-shard-01.lock", 2)]), mock.patch.object(
            web_db, "_acquire_process_lock_key", side_effect=acquire
        ), mock.patch.object(
            web_db, "_release_process_lock_handle", side_effect=OSError("cleanup")
        ):
            with self.assertRaises(OSError) as caught:
                web_db.acquire_writer_process_lock(Path(td) / "archive.db")
        self.assertIs(caught.exception, primary)
        self.assertEqual(
            caught.exception.cleanup_warnings[0]["code"],
            "writer_process_lock_cleanup_failed",
        )

    def test_pending_slot_and_thread_start_return_cleanup_warnings(self):
        warning = {
            "code": "writer_process_lock_cleanup_failed",
            "error_type": "OSError",
            "path_kind": "process_lock",
        }

        class FailingLock:
            def close(self):
                raise WebIndexBuildError(
                    "writer_process_lock_cleanup_failed",
                    cleanup_warnings=[warning],
                )

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manager = ImportJobManager(base / "archive.db")
            manager._running_job_id = "__pending_upload__"
            manager._pending_writer_lock = FailingLock()
            self.assertEqual(manager.release_pending_upload_slot(), [warning])
            self.assertFalse(manager.has_running_job())
            self.assertIsNotNone(manager._pending_writer_lock)

            upload = base / "upload.zip"
            upload.write_bytes(b"synthetic")
            manager._running_job_id = "__pending_upload__"
            manager._pending_writer_lock = FailingLock()
            with mock.patch(
                "chatgpt_export_archiver.web_jobs.threading.Thread",
                side_effect=RuntimeError("synthetic thread failure"),
            ), self.assertRaises(ImportJobStartError) as caught:
                manager.start_import(upload, filename="synthetic.zip", size=9)
            self.assertEqual(caught.exception.code, "import_job_start_failed")
            self.assertEqual(caught.exception.cleanup_warnings, [warning])
            self.assertFalse(manager.has_running_job())
            self.assertIsNotNone(manager._pending_writer_lock)

    def test_postcommit_writer_cleanup_cannot_regress_canonical_outcome(self):
        class FailingLock:
            def close(self):
                raise WebIndexBuildError(
                    "writer_process_lock_cleanup_failed",
                    cleanup_warnings=[{
                        "code": "writer_process_lock_cleanup_failed",
                        "error_type": "OSError",
                        "path_kind": "process_lock",
                    }],
                )

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            upload = base / "upload" / "upload.zip"
            manager = ImportJobManager(base / "archive.db")
            job = ImportJob(
                job_id="0" * 32,
                db_path=base / "archive.db",
                upload_path=upload,
                filename="synthetic.zip",
                size=0,
                status="running",
                stage="succeeded",
                outcome="succeeded",
                completion_outcome="success",
                canonical_import_outcome="success",
                canonical_commit_succeeded=True,
                _terminal_status="succeeded",
                _writer_lock=FailingLock(),
            )
            manager._jobs[job.job_id] = job
            manager._running_job_id = job.job_id
            manager._finalize_job(job)
            snapshot = job.snapshot()
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertTrue(snapshot["canonical_commit_succeeded"])
        self.assertEqual(snapshot["canonical_import_outcome"], "success")
        self.assertEqual(snapshot["completion_outcome"], "cleanup_warning")
        self.assertEqual(
            snapshot["cleanup_warnings"][0]["code"],
            "writer_process_lock_cleanup_failed",
        )
        self.assertIsNotNone(job._writer_lock)

    def test_writer_cleanup_retry_retains_owner_then_releases_process_pool(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            database = base / "archive.db"
            manager = ImportJobManager(database)
            writer_lock = web_db.acquire_writer_process_lock(database)
            job = ImportJob(
                job_id="1" * 32,
                db_path=database,
                upload_path=base / "upload" / "upload.zip",
                filename="synthetic.zip",
                size=0,
                status="running",
                stage="succeeded",
                outcome="succeeded",
                completion_outcome="success",
                canonical_import_outcome="success",
                canonical_commit_succeeded=True,
                _terminal_status="succeeded",
                _writer_lock=writer_lock,
            )
            manager._jobs[job.job_id] = job
            manager._running_job_id = job.job_id
            original_release = web_db._release_process_lock_handle
            failed_once = False

            def release_once(handle):
                nonlocal failed_once
                if not failed_once:
                    failed_once = True
                    raise OSError("synthetic")
                return original_release(handle)

            with mock.patch.object(
                web_db, "_release_process_lock_handle", side_effect=release_once
            ):
                manager._finalize_job(job)
            self.assertIs(job._writer_lock, writer_lock)
            self.assertEqual(web_db._pending_writer_process_lock_cleanup_count(), 1)
            self.assertTrue(manager.acquire_pending_upload_slot())
            self.assertIsNone(job._writer_lock)
            self.assertEqual(manager.release_pending_upload_slot(), [])
            self.assertEqual(web_db._pending_writer_process_lock_cleanup_count(), 0)
            self.assertEqual(
                sum(
                    len(entry["offsets"])
                    for entry in web_db._PROCESS_LOCK_POOL.values()
                ),
                0,
            )

    def test_persistent_writer_cleanup_failure_is_bounded_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "archive.db"
            manager = ImportJobManager(database)
            writer_lock = web_db.acquire_writer_process_lock(database)
            manager._pending_writer_lock = writer_lock
            manager._running_job_id = "__pending_upload__"
            with mock.patch.object(
                web_db,
                "_release_process_lock_handle",
                side_effect=OSError("synthetic"),
            ):
                self.assertTrue(manager.release_pending_upload_slot())
                for _index in range(100):
                    self.assertFalse(manager.acquire_pending_upload_slot())
                self.assertIs(manager._pending_writer_lock, writer_lock)
                self.assertEqual(
                    web_db._pending_writer_process_lock_cleanup_count(), 1
                )
                self.assertLessEqual(
                    len(web_db._PROCESS_LOCK_POOL), web_db._PROCESS_LOCK_SHARDS
                )
                self.assertLessEqual(
                    sum(
                        len(entry["offsets"])
                        for entry in web_db._PROCESS_LOCK_POOL.values()
                    ),
                    web_db._PROCESS_LOCK_MAX_HELD_RANGES,
                )
            self.assertTrue(manager.acquire_pending_upload_slot())
            self.assertEqual(manager.release_pending_upload_slot(), [])

    def test_real_pipeline_threshold_and_aggregate_use_all_pipeline_samples(self):
        threshold_case = {
            "pipeline_success": True,
            "correctness_pass": True,
            "workload_wall_seconds": 1.0,
            "required_pipeline_wall_seconds": 10.0,
            "job": {"elapsed_seconds": 1.0},
        }
        acceptance_real_pipeline._evaluate_acceptance(
            threshold_case, max_pipeline_seconds=5.0
        )
        self.assertFalse(threshold_case["performance_pass"])
        self.assertFalse(threshold_case["success"])

        samples = [
            {
                "success": True,
                "performance_pass": True,
                "correctness_pass": True,
                "workload_wall_seconds": 2.0,
                "required_pipeline_wall_seconds": 2.5,
                "external_process_wall_seconds": 3.0,
                "pipeline_wall_seconds": 2.0,
                "external_wall_seconds": 3.0,
                "upload_seconds": 0.1,
                "peak_rss_bytes": 1,
                "child_cpu_seconds": 1,
                "job": {"elapsed_seconds": 1.0},
            },
            {
                "success": False,
                "performance_pass": False,
                "correctness_pass": True,
                "workload_wall_seconds": 99.0,
                "required_pipeline_wall_seconds": 99.5,
                "external_process_wall_seconds": 100.0,
                "pipeline_wall_seconds": 99.0,
                "external_wall_seconds": 100.0,
                "upload_seconds": 0.2,
                "peak_rss_bytes": 9,
                "child_cpu_seconds": 8,
                "job": {"elapsed_seconds": 1.0},
            },
            {
                "success": False,
                "performance_pass": False,
                "correctness_pass": False,
                "workload_wall_seconds": 88.0,
                "required_pipeline_wall_seconds": 100.5,
                "external_process_wall_seconds": 101.0,
                "pipeline_wall_seconds": 88.0,
                "external_wall_seconds": 101.0,
                "upload_seconds": 0.3,
                "peak_rss_bytes": 8,
                "child_cpu_seconds": 7,
                "job": {"elapsed_seconds": 1.0},
            },
        ]
        aggregate = acceptance_real_pipeline._aggregate(samples)
        self.assertEqual(aggregate["total_runs"], 3)
        self.assertEqual(
            aggregate["pipeline_wall_seconds"]["all"], [2.0, 99.0, 88.0]
        )
        self.assertEqual(aggregate["pipeline_wall_seconds"]["worst"], 99.0)
        self.assertEqual(
            aggregate["external_wall_seconds"]["all"], [3.0, 100.0, 101.0]
        )
        self.assertEqual(
            aggregate["required_pipeline_wall_seconds"]["all"],
            [2.5, 99.5, 100.5],
        )
        self.assertEqual(
            aggregate["required_pipeline_wall_seconds"]["worst"], 100.5
        )

    def test_real_pipeline_upload_and_cleanup_use_absolute_deadlines(self):
        class SlowConnection:
            sock = None

            def __init__(self):
                self.timeout = None

            def putrequest(self, *_args, **_kwargs):
                time.sleep(0.03)

            def putheader(self, *_args, **_kwargs):
                return None

            def endheaders(self):
                time.sleep(0.03)

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "synthetic.zip"
            source.write_bytes(b"x")
            started = time.monotonic()
            with mock.patch.object(
                acceptance_real_pipeline.http.client,
                "HTTPConnection",
                return_value=SlowConnection(),
            ), self.assertRaises(acceptance_real_pipeline.HardRunTimeout):
                acceptance_real_pipeline._stream_upload(
                    1,
                    source,
                    hard_deadline=time.monotonic() + 0.05,
                )
            self.assertLess(time.monotonic() - started, 0.25)

            owned = Path(td) / "owned-cleanup"
            owned.mkdir()
            cleanup_started = time.monotonic()
            cleanup = acceptance_real_pipeline._remove_tree_bounded(
                owned,
                hard_deadline=time.monotonic() + 0.1,
                delay_seconds=5,
            )
            self.assertLess(time.monotonic() - cleanup_started, 0.35)
            self.assertFalse(cleanup["complete"])
            self.assertTrue(cleanup["terminated"] or cleanup["killed"])
            self.assertTrue(cleanup["reaped"])

    def test_performance_workers_disable_inherited_tracemalloc(self):
        with mock.patch.dict(os.environ, {"PYTHONTRACEMALLOC": "25"}):
            self.assertNotIn(
                "PYTHONTRACEMALLOC", acceptance_scale._worker_environment()
            )
        performance = acceptance_scale._many_small_worker(
            10, trace_allocations=False
        )
        diagnostic = acceptance_scale._many_small_worker(
            10, trace_allocations=True
        )
        self.assertTrue(performance["performance_eligible"])
        self.assertFalse(performance["tracemalloc_active_during_measurement"])
        self.assertFalse(diagnostic["performance_eligible"])
        self.assertTrue(diagnostic["tracemalloc_active_during_measurement"])
        self.assertEqual(diagnostic["tracemalloc_traceback_frames"], 25)
        self.assertIsNotNone(diagnostic["tracemalloc_peak_bytes"])

    def test_scale_huge_worker_confirmation_modes_and_watchdog(self):
        script = Path(acceptance_scale.__file__).resolve()
        with mock.patch.object(
            sys,
            "argv",
            [
                str(script),
                "--scenario",
                "physical-logical-parser-gib",
                "--tier",
                "10",
                "--worker",
                "--measurement-mode",
                "performance",
            ],
        ), mock.patch.object(acceptance_scale, "_worker") as worker:
            with self.assertRaisesRegex(SystemExit, "huge_worker_requires"):
                acceptance_scale.main()
            worker.assert_not_called()

        with mock.patch.object(
            sys,
            "argv",
            [
                str(script),
                "--scenario",
                "physical-logical-parser-gib",
                "--tier",
                "2",
                "--worker",
                "--measurement-mode",
                "performance",
                "--confirm-huge",
            ],
        ), mock.patch.object(acceptance_scale, "_worker") as worker:
            with self.assertRaisesRegex(SystemExit, "invalid_huge_scale_tier"):
                acceptance_scale.main()
            worker.assert_not_called()

        with mock.patch.object(
            sys,
            "argv",
            [
                str(script),
                "--scenario",
                "many-small",
                "--tier",
                "1",
                "--worker",
                "--measurement-mode",
                "performance",
            ],
        ), mock.patch.object(
            acceptance_scale,
            "_worker",
            return_value={"tiny_worker": True},
        ) as worker, mock.patch("builtins.print"):
            self.assertEqual(acceptance_scale.main(), 0)
            worker.assert_called_once()

        timeout = acceptance_scale._run_worker_subprocess(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            scenario="synthetic",
            tier=1,
            measurement_mode="performance",
            hard_timeout_seconds=0.1,
            heartbeat_seconds=0.05,
        )
        self.assertTrue(timeout["hard_timeout"])
        self.assertTrue(timeout["partial"])
        self.assertIsNotNone(timeout["worker_return_code"])
        self.assertTrue(timeout["terminate_attempted"])
        self.assertTrue(timeout["reaped"])
        self.assertFalse(timeout["reap_incomplete"])
        self.assertTrue(timeout["process_tree_cleanup_attempted"])
        self.assertTrue(timeout["process_tree_cleanup_complete"])
        self.assertFalse(timeout["pipe_drain_incomplete"])

        resource_timeout = benchmark_resources._run_worker_bounded(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            hard_seconds=0.1,
            heartbeat_seconds=0.05,
        )
        self.assertTrue(resource_timeout["worker_failed"])
        self.assertTrue(resource_timeout["hard_timeout"])
        self.assertTrue(resource_timeout["terminated"] or resource_timeout["killed"])
        self.assertTrue(resource_timeout["reaped"])
        self.assertIsNotNone(resource_timeout["worker_pid"])
        self.assertTrue(resource_timeout["process_tree_cleanup_attempted"])
        self.assertTrue(resource_timeout["process_tree_cleanup_complete"])

        class ResistantProcess:
            pid = 12345
            returncode = None

            def poll(self):
                return None

            def terminate(self):
                return None

            def kill(self):
                return None

            def communicate(self, timeout):
                raise subprocess.TimeoutExpired("synthetic", timeout)

            def wait(self, timeout):
                raise subprocess.TimeoutExpired("synthetic", timeout)

        for helper in (
            acceptance_scale._terminate_kill_reap,
            benchmark_resources._terminate_kill_reap,
        ):
            with self.subTest(helper=helper.__module__):
                _stdout, _stderr, lifecycle = helper(
                    ResistantProcess(), timeout_seconds=0.05
                )
                self.assertTrue(lifecycle["terminate_attempted"])
                self.assertTrue(lifecycle["kill_attempted"])
                self.assertFalse(lifecycle["reaped"])
                self.assertTrue(lifecycle["reap_incomplete"])
                self.assertFalse(lifecycle["process_tree_cleanup_complete"])
                self.assertTrue(lifecycle["pipe_drain_incomplete"])

    @unittest.skipUnless(os.name == "posix", "POSIX process-group contract")
    def test_watchdogs_kill_owned_grandchild_and_finish_pipe_drain(self):
        child_script = (
            "import pathlib,signal,subprocess,sys,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']);"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii');"
            "time.sleep(30)"
        )
        for helper in (
            acceptance_scale._terminate_kill_reap,
            benchmark_resources._terminate_kill_reap,
        ):
            with self.subTest(helper=helper.__module__), tempfile.TemporaryDirectory() as td:
                marker = Path(td) / "grandchild.pid"
                process = subprocess.Popen(
                    [sys.executable, "-c", child_script, str(marker)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                deadline = time.monotonic() + 2
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                grandchild_pid = int(marker.read_text(encoding="ascii"))
                try:
                    _stdout, _stderr, lifecycle = helper(
                        process,
                        timeout_seconds=1.0,
                        owned_process_group=True,
                    )
                    self.assertTrue(lifecycle["process_tree_cleanup_attempted"])
                    self.assertTrue(lifecycle["direct_child_reaped"])
                    self.assertFalse(lifecycle["owned_descendants_remaining"])
                    self.assertTrue(lifecycle["process_tree_cleanup_complete"])
                    self.assertFalse(lifecycle["pipe_drain_incomplete"])
                    with self.assertRaises(ProcessLookupError):
                        os.kill(grandchild_pid, 0)
                finally:
                    try:
                        os.killpg(process.pid, 9)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(os.name == "posix", "POSIX process-group contract")
    def test_real_pipeline_shutdown_proves_owned_grandchild_cleanup(self):
        child_script = (
            "import pathlib,signal,subprocess,sys,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']);"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii');"
            "time.sleep(30)"
        )
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "grandchild.pid"
            process = subprocess.Popen(
                [sys.executable, "-c", child_script, str(marker)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            grandchild_pid = int(marker.read_text(encoding="ascii"))
            try:
                lifecycle = acceptance_real_pipeline._stop_process_bounded(
                    process, time.monotonic() + 1.0
                )
                self.assertTrue(lifecycle["process_tree_cleanup_attempted"])
                self.assertTrue(lifecycle["process_tree_cleanup_supported"])
                self.assertTrue(lifecycle["direct_child_reaped"])
                self.assertFalse(lifecycle["owned_descendants_remaining"])
                self.assertTrue(lifecycle["process_tree_cleanup_complete"])
                self.assertFalse(lifecycle["pipe_drain_incomplete"])
                with self.assertRaises(ProcessLookupError):
                    os.kill(grandchild_pid, 0)
            finally:
                try:
                    os.killpg(process.pid, 9)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(os.name == "posix", "POSIX process-group contract")
    def test_watchdog_signal_permission_failure_is_structured_not_raised(self):
        class EventuallyReapedProcess:
            pid = 12345
            returncode = None
            stdout = None
            stderr = None

            def __init__(self):
                self.communications = 0

            def poll(self):
                return self.returncode

            def communicate(self, timeout):
                self.communications += 1
                if self.communications == 1:
                    raise subprocess.TimeoutExpired("synthetic", timeout)
                self.returncode = 0
                return "", ""

        def signal_result(_pid, sig):
            if sig == 9:
                raise PermissionError("synthetic")
            if sig == 0:
                raise ProcessLookupError

        process = EventuallyReapedProcess()
        with mock.patch.object(
            acceptance_real_pipeline.os, "killpg", side_effect=signal_result
        ):
            _stdout, _stderr, lifecycle = (
                acceptance_real_pipeline.terminate_kill_reap(
                    process,
                    timeout_seconds=0.2,
                    owned_process_group=True,
                )
            )
        self.assertTrue(lifecycle["direct_child_reaped"])
        self.assertFalse(lifecycle["owned_descendants_remaining"])
        self.assertTrue(lifecycle["process_tree_cleanup_complete"])
        self.assertTrue(lifecycle["signal_cleanup_degraded"])
        self.assertEqual(lifecycle["signal_error_types"], ["PermissionError"])

    def test_scale_validation_claims_require_observed_parser_stage(self):
        layers = acceptance_scale._validation_layers_from_observed_stages(
            [],
            retained_canonical_same_scale=False,
            indexed_source_same_scale=False,
        )
        self.assertFalse(layers["json_parser"])
        observed = acceptance_scale._validation_layers_from_observed_stages(
            [
                "source_selection_completed",
                "json_parser_entered",
                "json_parser_completed",
            ],
            retained_canonical_same_scale=False,
            indexed_source_same_scale=False,
        )
        self.assertTrue(observed["json_parser"])

        full = acceptance_scale._validation_status(
            outcome="import_disk_space_insufficient",
            verify_ok=None,
            index_complete=False,
            capacity_probe=False,
        )
        self.assertTrue(full["capacity_blocked"])
        self.assertFalse(full["validation_completed"])
        self.assertTrue(full["worker_failed"])
        probe = acceptance_scale._validation_status(
            outcome="import_disk_space_insufficient",
            verify_ok=None,
            index_complete=False,
            capacity_probe=True,
        )
        self.assertFalse(probe["worker_failed"])

    def test_import_proof_events_match_production_boundaries(self):
        proof_names = {
            "source_selection_completed",
            "source_preflight_completed",
            "disk_admission_completed",
            "json_parser_entered",
            "json_parser_completed",
            "canonical_import_entered",
            "canonical_import_committed",
        }

        def run_payload(payload, *, raw=False):
            td = tempfile.TemporaryDirectory()
            self.addCleanup(td.cleanup)
            base = Path(td.name)
            source = base / "synthetic.zip"
            database = base / "archive.db"
            data = payload if raw else json.dumps(payload).encode("utf-8")
            with zipfile.ZipFile(source, "w") as bundle:
                bundle.writestr("conversations.json", data)
            stages: list[str] = []
            outcome = "completed"
            try:
                run_import_pipeline(
                    database,
                    str(source),
                    cwd=base,
                    no_input_sha256=True,
                    progress_callback=lambda stage, _summary: stages.append(stage),
                )
            except ImportPipelineError as exc:
                outcome = exc.code
            for stage in proof_names:
                self.assertLessEqual(stages.count(stage), 1, stage)
            return outcome, stages

        valid = conversation("proof-valid")
        cases = (
            (
                "top-level-object",
                {"id": "not-an-array"},
                False,
                "conversation_json_top_level_not_list",
                {"json_parser_entered"},
                {"json_parser_completed", "canonical_import_entered", "canonical_import_committed"},
            ),
            (
                "syntax-before-first",
                b"[{",
                True,
                "invalid_conversation_json",
                {"json_parser_entered"},
                {"json_parser_completed", "canonical_import_entered", "canonical_import_committed"},
            ),
            (
                "only-invalid",
                [None, {}],
                False,
                "completed",
                {"json_parser_entered", "json_parser_completed"},
                {"canonical_import_entered", "canonical_import_committed"},
            ),
            (
                "invalid-then-valid",
                [None, valid],
                False,
                "completed",
                {"json_parser_completed", "canonical_import_entered", "canonical_import_committed"},
                set(),
            ),
            (
                "normal-valid",
                [valid],
                False,
                "completed",
                {"json_parser_completed", "canonical_import_entered", "canonical_import_committed"},
                set(),
            ),
        )
        for name, payload, raw, expected_outcome, present, absent in cases:
            with self.subTest(name=name):
                outcome, stages = run_payload(payload, raw=raw)
                self.assertEqual(outcome, expected_outcome)
                self.assertTrue(
                    {"source_selection_completed", "source_preflight_completed", "disk_admission_completed"}.issubset(stages)
                )
                self.assertTrue(present.issubset(stages))
                self.assertTrue(absent.isdisjoint(stages))
                evidence = acceptance_scale._validation_evidence(
                    stages,
                    physical_zip_created=True,
                )
                for stage in present:
                    if stage in evidence:
                        self.assertTrue(evidence[stage])
                for stage in absent:
                    self.assertFalse(evidence[stage])

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        source = base / "capacity.zip"
        write_zip(source, {"conversations.json": [valid]})
        stages: list[str] = []
        failure = DiskSpaceInsufficientError(
            "import_disk_space_insufficient",
            required_bytes=2,
            free_bytes=1,
        )
        with mock.patch(
            "chatgpt_export_archiver.cli.require_free_space",
            side_effect=failure,
        ):
            with self.assertRaisesRegex(
                ImportPipelineError,
                "import_disk_space_insufficient",
            ):
                run_import_pipeline(
                    base / "capacity.db",
                    str(source),
                    cwd=base,
                    no_input_sha256=True,
                    progress_callback=lambda stage, _summary: stages.append(stage),
                )
        capacity_evidence = acceptance_scale._validation_evidence(
            stages,
            physical_zip_created=True,
        )
        self.assertTrue(capacity_evidence["source_selection_completed"])
        self.assertTrue(capacity_evidence["source_preflight_completed"])
        self.assertFalse(capacity_evidence["disk_admission_completed"])
        self.assertFalse(capacity_evidence["json_parser_entered"])

    def test_scale_machine_evidence_reports_verify_and_web_index_failures(self):
        with mock.patch.object(
            acceptance_scale,
            "verify_database",
            side_effect=RuntimeError("synthetic"),
        ):
            verify_failure = acceptance_scale._retained_indexed_worker(
                1,
                indexed=True,
                trace_allocations=False,
                capacity_probe=False,
            )
        self.assertEqual(verify_failure["outcome"], "verify_failed")
        self.assertTrue(verify_failure["canonical_import_committed"])
        self.assertFalse(verify_failure["verify_completed"])
        self.assertFalse(verify_failure["web_index_completed"])
        self.assertEqual(verify_failure["validation_failure"]["stage"], "verify")
        self.assertTrue(verify_failure["worker_failed"])

        with mock.patch.object(
            acceptance_scale,
            "create_web_indexes",
            side_effect=RuntimeError("synthetic"),
        ):
            index_failure = acceptance_scale._retained_indexed_worker(
                1,
                indexed=True,
                trace_allocations=False,
                capacity_probe=False,
            )
        self.assertEqual(index_failure["outcome"], "web_index_failed")
        self.assertTrue(index_failure["canonical_import_committed"])
        self.assertTrue(index_failure["verify_completed"])
        self.assertFalse(index_failure["web_index_completed"])
        self.assertEqual(index_failure["validation_failure"]["stage"], "web_index")
        self.assertTrue(index_failure["worker_failed"])

    def test_sampler_coverage_requires_every_entered_stage_and_join(self):
        stages = [
            "pre_upload",
            "upload",
            "import",
            "verify",
            "stats",
            "web_index",
            "server_shutdown",
            "required_cleanup",
        ]
        for missing in ("import", "verify", "stats", "web_index"):
            with self.subTest(missing=missing):
                observed = [stage for stage in stages if stage != missing]
                unsampled, complete = acceptance_real_pipeline._sampling_coverage(
                    stages,
                    observed,
                    thread_alive=False,
                )
                self.assertEqual(unsampled, [missing])
                self.assertFalse(complete)
        unsampled, complete = acceptance_real_pipeline._sampling_coverage(
            stages,
            stages,
            thread_alive=True,
        )
        self.assertEqual(unsampled, [])
        self.assertFalse(complete)
        unsampled, complete = acceptance_real_pipeline._sampling_coverage(
            stages,
            stages,
            thread_alive=False,
        )
        self.assertEqual(unsampled, [])
        self.assertTrue(complete)

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                sampler = acceptance_real_pipeline._ResourceSampler(
                    base,
                    base / "archive.db",
                    process,
                    interval_seconds=60,
                    heartbeat_seconds=60,
                    started=time.monotonic(),
                )
                sampler.start()
                for stage in stages[1:]:
                    sampler.update(stage=stage)
                sampled = sampler.stop(hard_deadline=time.monotonic() + 1)
                self.assertTrue(sampled["coverage_complete"])
                self.assertEqual(sampled["unsampled_entered_stages"], [])
                self.assertTrue(set(stages).issubset(sampled["observed_stages"]))
                self.assertGreaterEqual(
                    sampled["sampling_overhead_wall_seconds"],
                    0,
                )

                join_failure = acceptance_real_pipeline._ResourceSampler(
                    base,
                    base / "archive.db",
                    process,
                    interval_seconds=60,
                    heartbeat_seconds=60,
                    started=time.monotonic(),
                )
                join_failure.start()
                with mock.patch.object(
                    join_failure._thread,
                    "join",
                    return_value=None,
                ), mock.patch.object(
                    join_failure._thread,
                    "is_alive",
                    return_value=True,
                ):
                    failed_join = join_failure.stop(
                        hard_deadline=time.monotonic() + 1
                    )
                join_failure._thread.join(timeout=1)
                self.assertFalse(failed_join["coverage_complete"])
                self.assertTrue(failed_join["sampler_cleanup_failed"])
            finally:
                process.terminate()
                process.wait(timeout=2)

    def test_resource_stress_small_capacity_fixture_cannot_claim_parser(self):
        failure = acceptance_scale.ImportPipelineError(
            "import_disk_space_insufficient",
            stage="input_preflight",
            detail={"required_bytes": 2_000_000, "free_bytes": 1_000_000},
        )
        with mock.patch.object(
            acceptance_scale,
            "run_import_pipeline",
            side_effect=failure,
        ):
            full = acceptance_scale._resource_stress_worker(
                1,
                trace_allocations=False,
                capacity_probe=False,
                _target_bytes=1024 * 1024,
            )
        self.assertEqual(full["observed_stages"], [])
        self.assertTrue(full["physical_zip_created"])
        self.assertFalse(full["selected_logical_json_identified"])
        self.assertFalse(full["zip_preflight_completed"])
        self.assertFalse(full["json_parser_entered"])
        self.assertFalse(full["json_parser_completed"])
        self.assertFalse(full["canonical_import_entered"])
        self.assertFalse(full["canonical_import_committed"])
        self.assertFalse(full["verify_completed"])
        self.assertFalse(full["web_index_completed"])
        self.assertFalse(full["validation_layers"]["json_parser"])
        self.assertTrue(full["capacity_blocked"])
        self.assertFalse(full["validation_completed"])
        self.assertTrue(full["worker_failed"])

        with mock.patch.object(
            acceptance_scale,
            "run_import_pipeline",
            side_effect=failure,
        ):
            probe = acceptance_scale._resource_stress_worker(
                1,
                trace_allocations=False,
                capacity_probe=True,
                _target_bytes=1024 * 1024,
            )
        self.assertFalse(probe["validation_layers"]["json_parser"])
        self.assertTrue(probe["physical_zip_created"])
        self.assertFalse(probe["selected_logical_json_identified"])
        self.assertFalse(probe["json_parser_entered"])
        self.assertFalse(probe["validation_completed"])
        self.assertFalse(probe["worker_failed"])

    def test_scale_huge_parent_defaults_to_untraced_performance(self):
        sample = {
            "scenario": "physical-logical-parser-gib",
            "tier": 1,
            "measurement_mode": "performance",
            "performance_eligible": True,
            "wall_seconds": 1.0,
            "cpu_seconds": 1.0,
            "peak_rss_bytes": 1,
            "tracemalloc_peak_bytes": None,
            "hard_timeout": False,
            "partial": False,
        }
        script = Path(acceptance_scale.__file__).resolve()
        with mock.patch.object(
            sys,
            "argv",
            [
                str(script),
                "--scenario",
                "physical-logical-parser-gib",
                "--tiers",
                "1",
                "--runs",
                "1",
                "--confirm-huge",
            ],
        ), mock.patch.object(
            acceptance_scale,
            "_run_worker_subprocess",
            return_value=sample,
        ) as child, mock.patch.object(
            acceptance_scale, "_environment", return_value={}
        ), mock.patch("builtins.print"):
            self.assertEqual(acceptance_scale.main(), 0)
        self.assertEqual(child.call_count, 1)
        self.assertEqual(child.call_args.kwargs["measurement_mode"], "performance")
        self.assertIn("--confirm-huge", child.call_args.args[0])
        self.assertNotIn("allocation_diagnostic", child.call_args.args[0])

        with mock.patch.object(
            sys,
            "argv",
            [
                str(script),
                "--scenario",
                "physical-logical-parser-gib",
                "--tiers",
                "1",
                "--confirm-huge",
                "--measurement-mode",
                "both",
            ],
        ), mock.patch.object(
            acceptance_scale, "_run_worker_subprocess"
        ) as traced_child:
            with self.assertRaisesRegex(
                SystemExit,
                "huge_allocation_diagnostic_requires",
            ):
                acceptance_scale.main()
            traced_child.assert_not_called()

    def test_migration_benchmarks_identify_exact_step_resource_model(self):
        heavy = benchmark_resources._migration_worker(1, "WAL", from_schema=4)
        light = benchmark_resources._migration_worker(1, "DELETE", from_schema=5)
        required = {
            "from_schema",
            "to_schema",
            "step_class",
            "rewrite_expected",
            "scan_expected",
            "estimated_peak_class",
            "journal_mode",
            "row_count",
        }
        self.assertTrue(required.issubset(heavy))
        self.assertTrue(required.issubset(light))
        self.assertTrue(heavy["rewrite_expected"])
        self.assertFalse(light["rewrite_expected"])
        self.assertEqual(heavy["to_schema"], light["to_schema"])
        self.assertNotEqual(heavy["step_class"], light["step_class"])

    def test_migration_v4_v5_alias_is_explicitly_deprecated(self):
        script = Path(benchmark_resources.__file__).resolve()
        with mock.patch.object(
            sys,
            "argv",
            [
                str(script),
                "--scenario",
                "migration-v4-v5",
                "--rows",
                "1",
                "--worker",
            ],
        ), mock.patch("builtins.print") as printer:
            self.assertEqual(benchmark_resources.main(), 0)
        payloads = []
        for call in printer.call_args_list:
            if call.args and isinstance(call.args[0], str):
                try:
                    payloads.append(json.loads(call.args[0]))
                except json.JSONDecodeError:
                    pass
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertTrue(payload["alias_deprecated"])
        self.assertEqual(payload["requested_scenario"], "migration-v4-v5")
        self.assertEqual(payload["actual_scenario"], "migration-v4-v6")
        self.assertEqual((payload["from_schema"], payload["to_schema"]), (4, 6))

    def test_release_publication_reports_directory_durability_faults(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "release.zip"
            old = b"old-release"

            output.write_bytes(old)
            with mock.patch.object(
                make_release_zip,
                "_fsync_archive_file",
                side_effect=OSError("synthetic"),
            ), self.assertRaises(OSError):
                make_release_zip.build_release(root, output, check=False)
            self.assertEqual(output.read_bytes(), old)

            output.write_bytes(old)
            with mock.patch.object(
                make_release_zip.os,
                "replace",
                side_effect=OSError("synthetic"),
            ), self.assertRaises(OSError):
                make_release_zip.build_release(root, output, check=False)
            self.assertEqual(output.read_bytes(), old)

            for helper, expected_code in (
                ("_open_release_directory", "release_directory_open_failed"),
                ("_fsync_release_directory", "release_directory_fsync_failed"),
            ):
                output.write_bytes(old)
                with self.subTest(helper=helper), mock.patch.object(
                    make_release_zip,
                    helper,
                    side_effect=OSError("synthetic"),
                ), self.assertRaises(make_release_zip.ReleasePublicationError) as caught:
                    make_release_zip.build_release(root, output, check=False)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertTrue(caught.exception.published)
                self.assertFalse(caught.exception.durability_confirmed)
                self.assertFalse(caught.exception.publication_verified)
                self.assertNotEqual(output.read_bytes(), old)
                self.assertEqual(
                    list(output.parent.glob(f".{output.name}.*.tmp.zip")), []
                )

            output.write_bytes(old)
            with mock.patch.object(
                make_release_zip,
                "_verify_published_archive",
                side_effect=ValueError("synthetic"),
            ), self.assertRaises(make_release_zip.ReleasePublicationError) as caught:
                make_release_zip.build_release(root, output, check=False)
            self.assertEqual(
                caught.exception.code, "release_published_verification_failed"
            )
            self.assertTrue(caught.exception.published)
            self.assertTrue(caught.exception.durability_confirmed)
            self.assertFalse(caught.exception.publication_verified)
            self.assertNotEqual(output.read_bytes(), old)

    def test_historical_audit_report_is_not_release_authority(self):
        report = "chatgpt-sqlite-webui-audit-report.md"
        self.assertNotIn(report, make_release_zip.INCLUDE_PATHS)
        self.assertNotIn(report, make_release_zip.AUTHORITATIVE_REQUIRED_FILES)
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "release.zip"
            manifest, _assets = make_release_zip.build_release(
                Path(__file__).resolve().parents[1], output, check=False
            )
            self.assertNotIn(report, {item["path"] for item in manifest})

    def test_zip_metadata_limits_run_before_zipfile_materialization(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "input.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("conversations.json", b"[]")
            with archive.open("rb") as stream, mock.patch.object(
                scanner, "MAX_ZIP_CENTRAL_DIRECTORY_BYTES", 1
            ):
                with self.assertRaisesRegex(
                    ValueError, "source_zip_central_directory_limit_exceeded"
                ):
                    scanner.preflight_zip_central_directory(stream, max_members=10)

    def test_zip_member_path_limit_is_checked_in_central_directory(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "input.zip"
            long_name = "x" * 33_000 + "/conversations.json"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr(long_name, b"[]")
            with archive.open("rb") as stream:
                with self.assertRaisesRegex(
                    ValueError, "source_relative_path_limit_exceeded"
                ):
                    scanner.preflight_zip_central_directory(stream, max_members=10)

    def test_high_width_unicode_coalesces_by_utf8_bytes(self):
        with mock.patch.object(scanner, "MAX_JSON_ELEMENT_BYTES", 8):
            windows = list(scanner._coalesce_json_text_chunks(iter(["😀", "😀", "😀"])))
        self.assertEqual(windows, ["😀😀", "😀"])

    def test_search_continuation_rejects_unused_bit_alias(self):
        token = search._encode_search_continuation({"synthetic": True})
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        index = alphabet.index(token[-1])
        alias = token[:-1] + alphabet[index ^ 1]
        self.assertNotEqual(alias, token)
        with self.assertRaises(SearchContinuationError) as caught:
            search._decode_search_continuation(alias)
        self.assertEqual(caught.exception.code, "invalid_search_continuation")

    def test_global_scope_limit_rejects_before_scope_population(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from chatgpt_export_archiver.db import init_db

        init_db(conn)
        conn.execute(
            "INSERT INTO conversations(conversation_id,title,aggregate_hash) "
            "VALUES ('one','synthetic','hash')"
        )
        conn.commit()
        with mock.patch.object(current_path, "MAX_EFFECTIVE_CURRENT_CONVERSATIONS", 0):
            with self.assertRaises(EffectiveCurrentResourceLimitError):
                current_path.ensure_effective_current_views(conn, None)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM effective_current_scope").fetchone()[0],
            0,
        )
        conn.close()

    def test_project_node_time_only_reimport_preserves_text_domains(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "input.zip"
            database = base / "archive.db"
            initial = conversation("time-only")
            write_zip(archive, {"conversations.json": [initial]})
            self.assertEqual(
                main(["--db", str(database), "import", "--input", str(archive), "--no-input-sha256"]),
                0,
            )
            create_web_indexes(database)
            reader = connect(database)
            before_generations = dict(reader.execute(
                "SELECT name,generation FROM archive_generations"
            ))
            before_revision, before_raw = reader.execute(
                "SELECT display_revision,raw_message_json FROM conversation_nodes "
                "WHERE conversation_id='time-only' AND node_id='u1'"
            ).fetchone()
            reader.close()

            changed = conversation("time-only")
            changed["mapping"]["u1"]["message"]["create_time"] += 10
            changed["mapping"]["u1"]["message"]["update_time"] += 10
            write_zip(archive, {"conversations.json": [changed]})
            self.assertEqual(
                main(["--db", str(database), "import", "--input", str(archive), "--no-input-sha256"]),
                0,
            )
            reader = connect(database)
            after_generations = dict(reader.execute(
                "SELECT name,generation FROM archive_generations"
            ))
            after_revision, after_raw = reader.execute(
                "SELECT display_revision,raw_message_json FROM conversation_nodes "
                "WHERE conversation_id='time-only' AND node_id='u1'"
            ).fetchone()
            summary = json.loads(reader.execute(
                "SELECT summary_json FROM import_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])
            self.assertEqual(after_generations["query"], before_generations["query"] + 1)
            for domain in ("message", "title", "address", "graph"):
                self.assertEqual(after_generations[domain], before_generations[domain])
            self.assertEqual(after_revision, before_revision)
            self.assertNotEqual(after_raw, before_raw)
            self.assertEqual(summary["dirty_domains"], ["query"])
            self.assertTrue(web_index_status(reader)["web_index_format_current"])
            reader.close()

    def test_visible_copy_uses_one_snapshot_across_selected_rows(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "input.zip"
            database = base / "archive.db"
            fixture = conversation("copy-snapshot")
            fixture["mapping"]["u1"]["message"]["content"]["parts"] = ["old-first"]
            fixture["mapping"]["a1"]["message"]["content"]["parts"] = ["old-second"]
            write_zip(archive, {"conversations.json": [fixture]})
            self.assertEqual(
                main(["--db", str(database), "import", "--input", str(archive), "--no-input-sha256"]),
                0,
            )
            original_resolver = exporter.recover_message_display_text
            calls = 0

            def update_between_rows(content_text, raw_message_json):
                nonlocal calls
                calls += 1
                value = original_resolver(content_text, raw_message_json)
                if calls == 1:
                    writer = sqlite3.connect(database, timeout=5)
                    try:
                        writer.execute(
                            "UPDATE conversation_nodes SET content_text='new-second' "
                            "WHERE conversation_id='copy-snapshot' AND node_id='a1'"
                        )
                        writer.commit()
                    finally:
                        writer.close()
                return value

            with mock.patch.object(web_api, "VISIBLE_COPY_QUERY_BATCH", 1), mock.patch.object(
                exporter, "recover_message_display_text", side_effect=update_between_rows
            ):
                response = TestClient(create_app(database)).post(
                    "/api/by-id/copy-visible?conversation_id=copy-snapshot",
                    json={"node_ids": ["u1", "a1"]},
                )
            self.assertEqual(response.status_code, 200)
            self.assertIn("old-first", response.text)
            self.assertIn("old-second", response.text)
            self.assertNotIn("new-second", response.text)
            stale = TestClient(create_app(database)).post(
                "/api/by-id/copy-visible?conversation_id=copy-snapshot",
                json={"node_ids": ["u1", "missing-node"]},
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json()["detail"], "copy_selection_stale")

    def test_display_chunk_unclassifiable_large_canonical_is_never_exact(self):
        from chatgpt_export_archiver.utils import sha256_text

        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "display-budget.db"
            conn = connect(database)
            init_db(conn)
            big = "[non-text content: " + "x" * (40 * 1024 * 1024)
            conn.execute(
                "INSERT INTO conversations(conversation_id,title,aggregate_hash) "
                "VALUES ('c','synthetic','h')"
            )
            conn.execute(
                "INSERT INTO conversation_nodes("
                "conversation_id,node_id,content_type,content_text,content_hash,"
                "is_on_current_path,display_revision) VALUES ("
                "'c','n','text',?,?,1,'deadbeefdeadbeefdeadbeefdeadbeef')",
                (big, sha256_text("synthetic")),
            )
            conn.commit()
            chunk = search.get_message_display_chunk(
                conn, "c", "n", offset=0, limit=65536
            )
            self.assertIsNotNone(chunk)
            self.assertTrue(chunk["resolver_input_truncated"])
            self.assertFalse(chunk["total_chars_exact"])
            self.assertFalse(chunk["has_more"])
            self.assertIsNone(chunk["next_offset"])
            self.assertIsNone(chunk["next_cursor"])
            self.assertLess(chunk["returned_chars"], 40 * 1024 * 1024)
            conn.close()

    def test_display_chunk_completion_and_continuation_matrix(self):
        from chatgpt_export_archiver.utils import sha256_text

        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "display-matrix.db"
            conn = connect(database)
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id,title,aggregate_hash) "
                "VALUES ('c','synthetic','h')"
            )
            raw_body = "raw-fallback-" * 20
            rows = (
                (
                    "placeholder-no-raw",
                    "legacy",
                    "[non-text content: " + "x" * (1024 * 1024) + "]",
                    None,
                ),
                (
                    "raw-fallback",
                    "legacy",
                    "[non-text content: legacy]",
                    json.dumps({
                        "content": {"content_type": "text", "parts": [raw_body]}
                    }),
                ),
                ("canonical", "text", "canonical-body-" * 20, None),
            )
            conn.executemany(
                "INSERT INTO conversation_nodes("
                "conversation_id,node_id,content_type,content_text,raw_message_json,"
                "content_hash,is_on_current_path) VALUES ('c',?,?,?,?,?,1)",
                (
                    (node_id, content_type, content_text, raw, sha256_text(node_id))
                    for node_id, content_type, content_text, raw in rows
                ),
            )
            conn.commit()

            unavailable = search.get_message_display_chunk(
                conn, "c", "placeholder-no-raw", offset=0, limit=40
            )
            self.assertEqual(unavailable["source"], "canonical_placeholder")
            self.assertFalse(unavailable["resolver_input_truncated"])
            self.assertFalse(unavailable["total_chars_exact"])
            self.assertFalse(unavailable["has_more"])
            self.assertIsNone(unavailable["next_offset"])
            self.assertIsNone(unavailable["next_cursor"])

            raw_first = search.get_message_display_chunk(
                conn, "c", "raw-fallback", offset=0, limit=40
            )
            self.assertEqual(raw_first["source"], "raw_fallback")
            self.assertTrue(raw_first["total_chars_exact"])
            self.assertTrue(raw_first["has_more"])
            self.assertEqual(raw_first["next_offset"], 40)
            self.assertTrue(raw_first["next_cursor"])
            raw_last = search.get_message_display_chunk(
                conn,
                "c",
                "raw-fallback",
                offset=raw_first["next_offset"],
                limit=1024,
                cursor=raw_first["next_cursor"],
            )
            self.assertFalse(raw_last["has_more"])
            self.assertTrue(raw_last["total_chars_exact"])

            conn.execute(
                "UPDATE conversation_nodes SET raw_message_json = ? "
                "WHERE conversation_id = 'c' AND node_id = 'raw-fallback'",
                (json.dumps({"content": {"content_type": "text", "parts": ["changed"]}}),),
            )
            conn.commit()
            with self.assertRaises(search.DisplayCursorError) as stale_raw:
                search.get_message_display_chunk(
                    conn,
                    "c",
                    "raw-fallback",
                    offset=raw_first["next_offset"],
                    limit=1024,
                    cursor=raw_first["next_cursor"],
                )
            self.assertEqual(stale_raw.exception.code, "display_cursor_stale")

            canonical_first = search.get_message_display_chunk(
                conn, "c", "canonical", offset=0, limit=40
            )
            self.assertTrue(canonical_first["has_more"])
            self.assertGreater(canonical_first["next_offset"], 0)
            self.assertTrue(canonical_first["next_cursor"])
            canonical_last = search.get_message_display_chunk(
                conn,
                "c",
                "canonical",
                offset=canonical_first["next_offset"],
                limit=1024,
                cursor=canonical_first["next_cursor"],
            )
            self.assertFalse(canonical_last["has_more"])
            self.assertTrue(canonical_last["total_chars_exact"])
            conn.close()

    def test_migration_capacity_is_step_specific(self):
        light = migration_capacity_plan(5, 8 * 1024 * 1024, 1_000_000)
        rewrite = migration_capacity_plan(4, 8 * 1024 * 1024, 1_000_000)
        self.assertEqual(light["planned_steps"], [
            "query_generation_metadata", "compatibility_scan"
        ])
        self.assertEqual(light["estimated_peak_category"], "metadata_and_scan")
        self.assertLess(light["required_free_bytes"], 64 * 1024 * 1024)
        self.assertGreater(rewrite["required_free_bytes"], light["required_free_bytes"])

    def test_copy_visible_ignores_large_raw_payload_for_canonical_rows(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "copy-visible.db"
            conn = connect(database)
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id,title,aggregate_hash) "
                "VALUES ('c','synthetic','h')"
            )
            conn.executemany(
                """INSERT INTO conversation_nodes(
                       conversation_id,node_id,role,content_type,content_text,
                       raw_message_json,content_hash,is_on_current_path)
                   VALUES ('c',?,'assistant','text','canonical',zeroblob(?),?,1)""",
                ((f"n{index}", 1024 * 1024, f"h{index}") for index in range(200)),
            )
            conn.commit()
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            selected = [(f"n{index}", index + 1) for index in range(200)]
            # Resolve the actual rowids rather than depending on allocation order.
            selected = [
                (str(row["node_id"]), int(row["storage_rowid"]))
                for row in conn.execute(
                    "SELECT rowid AS storage_rowid,node_id FROM conversation_nodes ORDER BY node_id"
                )
            ]
            text = "".join(
                exporter.iter_copy_conversation(
                    exporter.iter_selected_copy_nodes(conn, selected)
                )
            )
            conn.set_trace_callback(None)
            self.assertEqual(text.count("assistant:\ncanonical"), 200)
            self.assertFalse(
                any(
                    "SELECT * FROM conversation_nodes" in statement
                    for statement in statements
                )
            )
            self.assertFalse(
                any(
                    "raw_message_json," in statement
                    and "SELECT" in statement.upper()
                    for statement in statements
                )
            )
            conn.close()

    def test_current_compatibility_refresh_cancels_mid_scan_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "compatibility.db"
            conn = connect(database)
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id,title,aggregate_hash) "
                "VALUES ('c','synthetic','h')"
            )
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id,node_id,content_text) "
                "VALUES ('c',?,'synthetic')",
                ((f"n-{index}",) for index in range(6_000)),
            )
            conn.execute(
                "UPDATE archive_generations SET generation=generation+1 WHERE name='address'"
            )
            conn.commit()
            before = [tuple(row) for row in conn.execute(
                "SELECT domain,checked_generation,status,incompatible_count "
                "FROM archive_compatibility_state ORDER BY domain"
            )]
            cancel = False
            progress: list[int] = []

            def on_progress(stage, value):
                nonlocal cancel
                if stage == "compatibility_scan":
                    progress.append(value["processed"])
                    if value["processed"] >= 5_000:
                        cancel = True

            with self.assertRaises(DatabaseMigrationError) as caught:
                migrate_database(
                    conn,
                    refresh_compatibility=True,
                    progress_callback=on_progress,
                    cancel_check=lambda: cancel,
                )
            self.assertEqual(caught.exception.code, "database_migration_cancelled")
            self.assertTrue(progress)
            self.assertFalse(conn.in_transaction)
            self.assertEqual(
                [tuple(row) for row in conn.execute(
                    "SELECT domain,checked_generation,status,incompatible_count "
                    "FROM archive_compatibility_state ORDER BY domain"
                )],
                before,
            )
            # A cancelled progress handler must not contaminate later SQL.
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0], 6_000)
            conn.close()

if __name__ == "__main__":
    unittest.main()
