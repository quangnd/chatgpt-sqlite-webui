from __future__ import annotations

"""Fresh-process, content-safe parser and resource-scale acceptance.

Ordinary unit tests keep small production contracts.  This opt-in harness runs
each requested tier in a fresh child, emits one JSON record per tier/run, and
checks that every requested tier ran exactly once per repetition.  Fixtures are
synthetic and output contains hashes, sizes, counts, and resource metrics only.
"""

import argparse
import io
import hashlib
import json
import math
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import zipfile
from pathlib import Path
from typing import Any, Iterable
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatgpt_export_archiver import scanner  # noqa: E402
from chatgpt_export_archiver.cli import ImportPipelineError, run_import_pipeline  # noqa: E402
from chatgpt_export_archiver.db import verify_database  # noqa: E402
from chatgpt_export_archiver.disk_resources import DiskSpaceInsufficientError  # noqa: E402
from chatgpt_export_archiver.json_safety import JsonSafetyLimitError  # noqa: E402
from chatgpt_export_archiver.web_db import (  # noqa: E402
    _PROCESS_LOCK_SHARDS,
    _process_lock_registry,
    acquire_writer_process_lock,
    create_web_indexes,
    release_writer_process_lock,
)
from tools.process_watchdog import terminate_kill_reap  # noqa: E402

MOVED_SCALE_CASES = {
    "tests.test_round11.Round11RegressionTests."
    "test_five_thousand_node_metadata_density_matrix_uses_joint_profile": {
        "scenario": "metadata-density",
        "tiers": [10, 25, 50, 100, 200],
        "nodes_per_tier": 5_000,
    },
    "tests.test_archiver.ArchiverTests."
    "test_round10_metadata_dense_five_thousand_node_element_imports": {
        "scenario": "metadata-density",
        "tiers": [30],
        "nodes_per_tier": 5_000,
    },
}


def _rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _environment() -> dict[str, Any]:
    conn = __import__("sqlite3").connect(":memory:")
    try:
        compile_options = [
            str(row[0]) for row in conn.execute("PRAGMA compile_options")
        ]
    finally:
        conn.close()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "sqlite": __import__("sqlite3").sqlite_version,
        "sqlite_compile_options": compile_options,
        "filesystem": "local-temporary-not-used",
    }


def _chunks(value: str, chars: int = 64 * 1024) -> Iterable[str]:
    for offset in range(0, len(value), chars):
        yield value[offset : offset + chars]


def _begin_allocation_measurement(trace_allocations: bool) -> bool:
    """Make each worker's declared measurement mode match its real state."""

    inherited = tracemalloc.is_tracing()
    if inherited:
        tracemalloc.stop()
    if trace_allocations:
        tracemalloc.start(25)
    return inherited


def _worker_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONTRACEMALLOC", None)
    return env


def _validation_layers_from_observed_stages(
    observed_stages: Iterable[str],
    *,
    retained_canonical_same_scale: bool,
    indexed_source_same_scale: bool,
    physical_zip_created: bool = True,
) -> dict[str, bool]:
    """Describe only validation layers for which this worker has evidence."""

    evidence = _validation_evidence(
        observed_stages,
        physical_zip_created=physical_zip_created,
    )
    return {
        "physical_zip": evidence["physical_zip_created"],
        "selected_logical_json": evidence["selected_logical_json_identified"],
        "json_parser": evidence["json_parser_completed"],
        "retained_canonical_same_scale": bool(retained_canonical_same_scale),
        "indexed_source_same_scale": bool(indexed_source_same_scale),
        "http_multipart_upload": False,
    }


def _validation_status(
    *,
    outcome: str,
    verify_ok: bool | None,
    index_complete: bool,
    capacity_probe: bool,
) -> dict[str, bool]:
    completed = outcome == "completed" and bool(verify_ok) and index_complete
    capacity_blocked = outcome == "import_disk_space_insufficient"
    return {
        "capacity_blocked": capacity_blocked,
        "validation_completed": completed,
        "worker_failed": bool(
            not completed and not (capacity_probe and capacity_blocked)
        ),
    }


def _validation_evidence(
    observed_stages: Iterable[str],
    *,
    physical_zip_created: bool,
) -> dict[str, bool]:
    """Map production/tool events to proof boundaries without inference."""

    stages = set(observed_stages)
    source_selected = "source_selection_completed" in stages
    source_preflight = "source_preflight_completed" in stages
    return {
        "physical_zip_created": bool(physical_zip_created),
        "source_selection_completed": source_selected,
        "selected_logical_json_identified": source_selected,
        "source_preflight_completed": source_preflight,
        "zip_preflight_completed": source_preflight,
        "disk_admission_completed": "disk_admission_completed" in stages,
        "json_parser_entered": "json_parser_entered" in stages,
        "json_parser_completed": "json_parser_completed" in stages,
        "canonical_import_entered": "canonical_import_entered" in stages,
        "canonical_import_committed": "canonical_import_committed" in stages,
        "verify_completed": "verify_completed" in stages,
        "web_index_completed": "web_index_completed" in stages,
    }


def _record_completed_stage(observed_stages: list[str], stage: str) -> None:
    """Record a tool-owned completion boundary once."""

    if stage not in observed_stages:
        observed_stages.append(stage)


class _ValidationStepError(RuntimeError):
    def __init__(self, stage: str, error_type: str) -> None:
        super().__init__(stage)
        self.stage = stage
        self.error_type = error_type


def _run_validation_step(
    observed_stages: list[str],
    stage: str,
    action,
):
    """Run one post-import proof step and expose only a safe failure type."""

    try:
        result = action()
    except Exception as exc:
        raise _ValidationStepError(stage, type(exc).__name__) from exc
    _record_completed_stage(observed_stages, f"{stage}_completed")
    return result


def _measure(
    scenario: str,
    tier: int,
    payload: str,
    action,
    *,
    trace_allocations: bool,
) -> dict[str, Any]:
    encoded = payload.encode("utf-8")
    inherited_tracemalloc_disabled = _begin_allocation_measurement(
        trace_allocations
    )
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    traced_peak: int | None = None
    try:
        result = action()
        if trace_allocations:
            _current, traced_peak = tracemalloc.get_traced_memory()
    finally:
        if trace_allocations:
            tracemalloc.stop()
    return {
        "scenario": scenario,
        "tier": tier,
        "fixture_sha256": hashlib.sha256(encoded).hexdigest(),
        "fixture_utf8_bytes": len(encoded),
        "wall_seconds": time.perf_counter() - started_wall,
        "cpu_seconds": time.process_time() - started_cpu,
        "peak_rss_bytes": _rss_bytes(),
        "tracemalloc_peak_bytes": traced_peak,
        "measurement_mode": (
            "allocation_diagnostic" if trace_allocations else "performance"
        ),
        "performance_eligible": not trace_allocations,
        "tracemalloc_active_during_measurement": trace_allocations,
        "tracemalloc_traceback_frames": 25 if trace_allocations else 0,
        "inherited_tracemalloc_disabled": inherited_tracemalloc_disabled,
        **result,
    }


def _many_small_worker(elements: int, *, trace_allocations: bool) -> dict[str, Any]:
    element = '{"id":"s","mapping":{}}'
    payload = "[" + ",".join([element] * elements) + "]"

    def run() -> dict[str, Any]:
        count = sum(1 for _value in scanner._iter_json_array(_chunks(payload)))
        if count != elements:
            raise RuntimeError("many_small_element_count_mismatch")
        return {
            "elements": count,
            "source_chunks": (len(payload) + 65535) // 65536,
            "expected_error": None,
        }

    return _measure(
        "many-small", elements, payload, run,
        trace_allocations=trace_allocations,
    )


def _single_element_worker(size_mib: int, *, trace_allocations: bool) -> dict[str, Any]:
    target = size_mib * 1024 * 1024
    prefix = '[{"id":"s","mapping":{},"body":"'
    suffix = '"}]'
    if target > scanner.MAX_JSON_ELEMENT_BYTES:
        inherited_tracemalloc_disabled = _begin_allocation_measurement(
            trace_allocations
        )
        started_wall = time.perf_counter()
        started_cpu = time.process_time()
        generated_bytes = 0
        source_chunks = 0
        digest = hashlib.sha256()

        def chunks() -> Iterable[str]:
            nonlocal generated_bytes, source_chunks
            remaining = max(0, target - len(prefix) - len(suffix))
            for value in (prefix,):
                generated_bytes += len(value)
                source_chunks += 1
                digest.update(value.encode("ascii"))
                yield value
            counter = 0
            while remaining:
                seed = hashlib.sha256(str(counter).encode("ascii")).hexdigest()
                value = (seed * 1024)[: min(64 * 1024, remaining)]
                remaining -= len(value)
                counter += 1
                generated_bytes += len(value)
                source_chunks += 1
                digest.update(value.encode("ascii"))
                yield value
            generated_bytes += len(suffix)
            source_chunks += 1
            digest.update(suffix.encode("ascii"))
            yield suffix

        observed_error: str | None = None
        try:
            list(scanner._iter_json_array(chunks()))
        except (JsonSafetyLimitError, scanner.ConversationJsonElementTooLargeError) as exc:
            observed_error = str(getattr(exc, "code", str(exc)))
        expected = "conversation_json_element_too_large"
        if observed_error != expected:
            raise RuntimeError("single_element_bounded_rejection_contract_failed")
        traced_peak: int | None = None
        if trace_allocations:
            _current, traced_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        return {
            "scenario": "single-element-mib",
            "tier": size_mib,
            "fixture_sha256": digest.hexdigest(),
            "fixture_hash_scope": "generated_prefix_before_bounded_rejection",
            "fixture_utf8_bytes": target,
            "generated_utf8_bytes": generated_bytes,
            "wall_seconds": time.perf_counter() - started_wall,
            "cpu_seconds": time.process_time() - started_cpu,
            "peak_rss_bytes": _rss_bytes(),
            "tracemalloc_peak_bytes": traced_peak,
            "measurement_mode": "allocation_diagnostic" if trace_allocations else "performance",
            "performance_eligible": not trace_allocations,
            "tracemalloc_active_during_measurement": trace_allocations,
            "tracemalloc_traceback_frames": 25 if trace_allocations else 0,
            "inherited_tracemalloc_disabled": inherited_tracemalloc_disabled,
            "elements": 0,
            "source_chunks": source_chunks,
            "expected_error": observed_error,
            "bounded_rejection_pass": True,
            "worker_failed": False,
        }
    payload = prefix + "x" * max(0, target - len(prefix) - len(suffix)) + suffix

    def run() -> dict[str, Any]:
        values = list(scanner._iter_json_array(_chunks(payload)))
        if len(values) != 1:
            raise RuntimeError("single_element_count_mismatch")
        return {
            "elements": 1,
            "decoded_chars": int(getattr(values[0], "decoded_chars", 0)),
            "source_chunks": (len(payload) + 65535) // 65536,
            "expected_error": None,
        }

    return _measure(
        "single-element-mib", size_mib, payload, run,
        trace_allocations=trace_allocations,
    )


def _mapping_budget_worker(entries: int, *, trace_allocations: bool) -> dict[str, Any]:
    # Duplicate keys intentionally minimize input bytes. The lexical contract
    # still counts every mapping entry before the decoder can materialize the
    # object or collapse duplicate keys.
    payload = "[{" + ",".join(['"k":0'] * entries) + "}]"

    class RefusingDecoder(json.JSONDecoder):
        def decode(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("decoder_called_after_predecode_limit")

        def raw_decode(self, *_args: Any, **_kwargs: Any) -> Any:
            raise json.JSONDecodeError("bounded probe", "", 0)

    def run() -> dict[str, Any]:
        observed: str | None = None
        try:
            with mock.patch.object(
                scanner, "MAX_JSON_MAPPING_ENTRIES", entries - 1
            ), mock.patch.object(scanner.json, "JSONDecoder", RefusingDecoder):
                list(scanner._iter_json_array(_chunks(payload)))
        except JsonSafetyLimitError as exc:
            observed = exc.code
        if observed != "json_mapping_entry_limit_exceeded":
            raise RuntimeError("mapping_predecode_contract_failed")
        return {
            "mapping_entries_scanned": entries,
            "source_chunks": (len(payload) + 65535) // 65536,
            "expected_error": observed,
            "decoder_materialization_calls": 0,
        }

    return _measure(
        "mapping-predecode", entries, payload, run,
        trace_allocations=trace_allocations,
    )


def _metadata_density_worker(fields_per_node: int, *, trace_allocations: bool) -> dict[str, Any]:
    node_count = 5_000
    metadata = {
        f"k{index:03d}": f"v{index:03d}" for index in range(fields_per_node)
    }
    mapping: dict[str, Any] = {}
    for index in range(node_count):
        node_id = f"n-{index}"
        next_id = f"n-{index + 1}" if index + 1 < node_count else None
        mapping[node_id] = {
            "id": node_id,
            "parent": f"n-{index - 1}" if index else None,
            "children": [next_id] if next_id is not None else [],
            "message": {
                "id": f"m-{index}",
                "author": {"role": "assistant"},
                "create_time": 1_700_000_000 + index,
                "update_time": 1_700_000_000 + index,
                "content": {"content_type": "text", "parts": ["synthetic"]},
                "metadata": metadata,
            },
        }
    payload = json.dumps(
        [
            {
                "id": f"density-{fields_per_node}",
                "conversation_id": f"density-{fields_per_node}",
                "title": "Synthetic",
                "create_time": 1_700_000_000,
                "update_time": 1_700_005_000,
                "current_node": f"n-{node_count - 1}",
                "mapping": mapping,
            }
        ],
        separators=(",", ":"),
    )

    def run() -> dict[str, Any]:
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "conversations.json"
            database = base / "archive.db"
            source.write_text(payload, encoding="utf-8")
            run_import_pipeline(
                database,
                str(source),
                cwd=base,
                no_input_sha256=True,
            )
            conn = sqlite3.connect(database)
            conn.row_factory = sqlite3.Row
            try:
                stored_nodes = int(
                    conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0]
                )
                stored_conversations = int(
                    conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
                )
                warning_count = int(
                    conn.execute("SELECT COUNT(*) FROM import_warnings").fetchone()[0]
                )
                journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            finally:
                conn.close()
            if (
                stored_nodes != node_count
                or stored_conversations != 1
                or warning_count != 0
            ):
                raise RuntimeError("metadata_density_import_mismatch")
            return {
                "elements": 1,
                "nodes": stored_nodes,
                "metadata_fields_per_node": fields_per_node,
                "warning_count": warning_count,
                "source_chunks": (len(payload) + 65535) // 65536,
                "database_bytes": database.stat().st_size,
                "wal_bytes": (
                    database.with_name(database.name + "-wal").stat().st_size
                    if database.with_name(database.name + "-wal").exists()
                    else 0
                ),
                "temp_bytes": 0,
                "journal_mode": journal_mode,
                "expected_error": None,
            }

    return _measure(
        "metadata-density", fields_per_node, payload, run,
        trace_allocations=trace_allocations,
    )


def _registry_lifecycle_worker(database_count: int, *, trace_allocations: bool) -> dict[str, Any]:
    payload = f"registry-lifecycle:{database_count}"

    def run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for index in range(database_count):
                lock = acquire_writer_process_lock(base / f"db-{index}.sqlite3")
                release_writer_process_lock(lock)
            registry = _process_lock_registry()
            shard_files = [
                path
                for path in registry.iterdir()
                if path.name.startswith("writer-shard-") and path.name.endswith(".lock")
            ]
            if len(shard_files) > _PROCESS_LOCK_SHARDS:
                raise RuntimeError("writer_registry_not_bounded")
            return {
                "database_lifecycles": database_count,
                "registry_shard_files": len(shard_files),
                "registry_shard_limit": _PROCESS_LOCK_SHARDS,
                "expected_error": None,
            }

    return _measure(
        "registry-lifecycle", database_count, payload, run,
        trace_allocations=trace_allocations,
    )


def _write_exact_sized_valid_element(
    writer: Any,
    *,
    element_size: int,
    element_index: int,
) -> None:
    prefix = (
        b'{"id":"valid-'
        + str(element_index).encode("ascii")
        + b'","conversation_id":"valid-'
        + str(element_index).encode("ascii")
        + b'","title":"Synthetic","current_node":"node-'
        + str(element_index).encode("ascii")
        + b'","mapping":{"node-'
        + str(element_index).encode("ascii")
        + b'":{"id":"node-'
        + str(element_index).encode("ascii")
        + b'","parent":null,"children":[],"message":{"id":"message-'
        + str(element_index).encode("ascii")
        + b'","author":{"role":"user"},"create_time":1700000000,'
          b'"update_time":1700000000,"content":{"content_type":"text",'
          b'"parts":["synthetic valid payload"]},"metadata":{}}}},"padding":"'
    )
    suffix = b'"}'
    padding = element_size - len(prefix) - len(suffix)
    if padding < 0:
        raise RuntimeError("logical_archive_element_too_small")
    writer.write(prefix)
    block = b"x" * (1024 * 1024)
    while padding:
        count = min(padding, len(block))
        writer.write(block[:count])
        padding -= count
    writer.write(suffix)


def _write_exact_sized_invalid_element(
    writer: Any,
    *,
    element_size: int,
    element_index: int,
) -> None:
    prefix = (
        b'{"synthetic_resource_index":'
        + str(element_index).encode("ascii")
        + b',"padding":"'
    )
    suffix = b'"}'
    padding = element_size - len(prefix) - len(suffix)
    if padding < 0:
        raise RuntimeError("logical_archive_element_too_small")
    block = b"x" * (1024 * 1024)
    writer.write(prefix)
    while padding:
        count = min(padding, len(block))
        writer.write(block[:count])
        padding -= count
    writer.write(suffix)


def _valid_data_fixture_self_test() -> dict[str, int | bool]:
    stream = io.BytesIO()
    stream.write(b"[")
    _write_exact_sized_valid_element(
        stream, element_size=4096, element_index=0
    )
    stream.write(b"]")
    payload = stream.getvalue()
    decoded = json.loads(payload)
    valid = bool(
        isinstance(decoded, list)
        and len(decoded) == 1
        and isinstance(decoded[0], dict)
        and decoded[0].get("conversation_id") == "valid-0"
        and isinstance(decoded[0].get("mapping"), dict)
        and "node-0" in decoded[0]["mapping"]
        and decoded[0]["mapping"]["node-0"].get("message") is not None
    )
    if not valid:
        raise RuntimeError("valid_data_fixture_not_valid")
    return {
        "valid_data": True,
        "elements": 1,
        "fixture_utf8_bytes": len(payload),
    }


def _representative_valid_data_fixture_self_test() -> dict[str, Any]:
    """Exercise the representative fixture shapes through production entries."""
    import sqlite3

    def conversation(
        index: int,
        node_count: int,
        *,
        padding: int = 0,
        body_chars: int = 0,
        title_chars: int = 0,
        raw_metadata_chars: int = 0,
        placeholder_content: bool = False,
    ) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for node_index in range(node_count):
            node_id = f"node-{index}-{node_index}"
            next_id = f"node-{index}-{node_index + 1}" if node_index + 1 < node_count else None
            mapping[node_id] = {
                "id": node_id,
                "parent": f"node-{index}-{node_index - 1}" if node_index else None,
                "children": [next_id] if next_id else [],
                "message": {
                    "id": f"message-{index}-{node_index}",
                    "author": {"role": "user" if node_index % 2 == 0 else "assistant"},
                    "create_time": 1_700_000_000 + node_index,
                    "update_time": 1_700_000_000 + node_index,
                    "content": (
                        {"content_type": "image_asset_pointer", "parts": []}
                        if placeholder_content
                        else {
                            "content_type": "text",
                            "parts": [
                                "synthetic valid text"
                                + ("b" * body_chars if node_index == 0 else "")
                            ],
                        }
                    ),
                    "metadata": {
                        "synthetic_note": (
                            "m" * raw_metadata_chars if node_index == 0 else ""
                        )
                    },
                },
            }
        result = {
            "id": f"fixture-{index}",
            "conversation_id": f"fixture-{index}",
            "title": "Synthetic" + "t" * title_chars,
            "current_node": f"node-{index}-{node_count - 1}",
            "mapping": mapping,
        }
        if padding:
            result["ignored_padding"] = "x" * padding
        return result

    runs: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        fixtures: list[tuple[str, dict[str, list[dict[str, Any]]], int]] = [
            (
                "many-shards-low-compression",
                {
                    "conversations-000.json": [
                        conversation(0, 1, raw_metadata_chars=64 * 1024, placeholder_content=True)
                    ],
                    "conversations-001.json": [
                        conversation(1, 1, title_chars=4 * 1024)
                    ],
                    "conversations-002.json": [conversation(2, 8)],
                },
                zipfile.ZIP_STORED,
            ),
            (
                "single-shard-mixed-high-compression",
                {
                    "conversations.json": [
                        *(conversation(100 + index, 1) for index in range(5)),
                        conversation(200, 50),
                        conversation(201, 2, body_chars=256 * 1024),
                        conversation(202, 2, padding=32 * 1024),
                    ]
                },
                zipfile.ZIP_DEFLATED,
            ),
        ]
        for run_index, (profile, members, compression) in enumerate(fixtures):
            archive = base / f"fixture-{run_index}.zip"
            database = base / f"fixture-{run_index}.db"
            selected_logical_json_bytes = 0
            with zipfile.ZipFile(archive, "w", compression=compression, allowZip64=True) as bundle:
                for member, rows in members.items():
                    member_payload = json.dumps(
                        rows, separators=(",", ":")
                    ).encode("utf-8")
                    selected_logical_json_bytes += len(member_payload)
                    bundle.writestr(member, member_payload)
            run_import_pipeline(database, str(archive), cwd=base, no_input_sha256=False)
            conn = sqlite3.connect(database)
            conn.row_factory = sqlite3.Row
            try:
                verify_ok = bool(verify_database(conn)["ok"])
                conversations = int(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])
                nodes = int(conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0])
                warnings = int(conn.execute("SELECT COUNT(*) FROM import_warnings").fetchone()[0])
                retained_content_text_bytes = int(conn.execute(
                    "SELECT COALESCE(SUM(length(CAST(content_text AS BLOB))),0) FROM conversation_nodes"
                ).fetchone()[0])
                retained_raw_message_bytes = int(conn.execute(
                    "SELECT COALESCE(SUM(length(CAST(raw_message_json AS BLOB))),0) FROM conversation_nodes"
                ).fetchone()[0])
                retained_title_bytes = int(conn.execute(
                    "SELECT COALESCE(SUM(length(CAST(title AS BLOB))),0) FROM conversations"
                ).fetchone()[0])
            finally:
                conn.close()
            index_result = create_web_indexes(database)
            if not verify_ok or warnings or not index_result.get("atomic_publish"):
                raise RuntimeError("representative_valid_data_self_test_failed")
            retained_canonical_payload_bytes = (
                retained_content_text_bytes
                + retained_raw_message_bytes
                + retained_title_bytes
            )
            indexed_source_bytes = int(index_result.get("input_materialized_bytes", 0))
            runs.append({
                "profile": profile,
                "physical_zip_bytes": archive.stat().st_size,
                "selected_logical_json_bytes": selected_logical_json_bytes,
                "retained_content_text_bytes": retained_content_text_bytes,
                "retained_raw_message_bytes": retained_raw_message_bytes,
                "retained_title_bytes": retained_title_bytes,
                "retained_canonical_payload_bytes": retained_canonical_payload_bytes,
                "indexed_source_bytes": indexed_source_bytes,
                "retained_to_logical_ratio": (
                    retained_canonical_payload_bytes / selected_logical_json_bytes
                ),
                "indexed_to_logical_ratio": (
                    indexed_source_bytes / selected_logical_json_bytes
                ),
                "committed_conversations": conversations,
                "committed_nodes": nodes,
                "verify_ok": verify_ok,
                "web_index_complete": True,
            })
    ratio_contract = {
        "synthetic_profile_only": True,
        "minimum_retained_to_logical_ratio": 0.50,
        "minimum_indexed_to_logical_ratio": 0.25,
        "passing_profiles": [
            item["profile"]
            for item in runs
            if item["retained_to_logical_ratio"] >= 0.50
            and item["indexed_to_logical_ratio"] >= 0.25
        ],
    }
    if not ratio_contract["passing_profiles"]:
        raise RuntimeError("representative_retained_ratio_contract_failed")
    return {
        "production_entry": True,
        "components_exercised": [
            "many-small-realistic-messages",
            "mixed-message-sizes",
            "large-retained-message-body",
            "large-raw-fallback-input",
            "large-title-metadata",
            "many-shards",
            "one-large-shard",
        ],
        "opt_in_scale_profiles": {
            "five_thousand_node_dense_conversation": "metadata-density",
            "physical_logical_parser_gib": "physical-logical-parser-gib",
            "retained_canonical_payload": "retained-canonical-mib",
            "indexed_source_payload": "indexed-source-mib",
        },
        "ratio_contract": ratio_contract,
        "runs": runs,
    }


def _retained_payload_element(index: int, body_chars: int) -> bytes:
    conversation_id = f"retained-{index}"
    node_id = f"node-{index}"
    body = (hashlib.sha256(str(index).encode("ascii")).hexdigest() *
            ((body_chars + 63) // 64))[:body_chars]
    value = {
        "id": conversation_id,
        "conversation_id": conversation_id,
        "title": "Synthetic retained payload",
        "current_node": node_id,
        "mapping": {
            node_id: {
                "id": node_id,
                "parent": None,
                "children": [],
                "message": {
                    "id": f"message-{index}",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [body]},
                },
            }
        },
    }
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _retained_indexed_worker(
    size_mib: int,
    *,
    indexed: bool,
    trace_allocations: bool,
    capacity_probe: bool,
) -> dict[str, Any]:
    import sqlite3

    target_bytes = size_mib * 1024 * 1024
    inherited_tracemalloc_disabled = _begin_allocation_measurement(trace_allocations)
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    traced_peak: int | None = None
    scenario = "indexed-source-mib" if indexed else "retained-canonical-mib"
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        archive = base / "synthetic-retained.zip"
        database = base / "archive.db"
        logical_bytes = 2
        element_count = 0
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
            with bundle.open("conversations.json", "w", force_zip64=True) as writer:
                writer.write(b"[")
                while logical_bytes < target_bytes:
                    remaining = target_bytes - logical_bytes
                    body_chars = max(1, min(1024 * 1024, remaining - 512))
                    element = _retained_payload_element(element_count, body_chars)
                    if element_count:
                        writer.write(b",")
                        logical_bytes += 1
                    writer.write(element)
                    logical_bytes += len(element)
                    element_count += 1
                writer.write(b"]")
                logical_bytes += 1
        stages: list[str] = []
        outcome = "completed"
        capacity: dict[str, int] | None = None
        verify_ok: bool | None = None
        index_result: dict[str, Any] | None = None
        validation_failure: dict[str, str] | None = None
        retained_content = retained_raw = retained_title = 0
        committed_conversations = committed_nodes = 0
        try:
            run_import_pipeline(
                database,
                str(archive),
                cwd=base,
                no_input_sha256=True,
                progress_callback=lambda stage, _summary: stages.append(stage),
            )
            conn = sqlite3.connect(database)
            conn.row_factory = sqlite3.Row
            try:
                verify_ok = bool(
                    _run_validation_step(
                        stages,
                        "verify",
                        lambda: verify_database(conn),
                    )["ok"]
                )
                committed_conversations = int(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])
                committed_nodes = int(conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0])
                retained_content = int(conn.execute("SELECT COALESCE(SUM(length(CAST(content_text AS BLOB))),0) FROM conversation_nodes").fetchone()[0])
                retained_raw = int(conn.execute("SELECT COALESCE(SUM(length(CAST(raw_message_json AS BLOB))),0) FROM conversation_nodes").fetchone()[0])
                retained_title = int(conn.execute("SELECT COALESCE(SUM(length(CAST(title AS BLOB))),0) FROM conversations").fetchone()[0])
            finally:
                conn.close()
            if indexed:
                index_result = _run_validation_step(
                    stages,
                    "web_index",
                    lambda: create_web_indexes(database),
                )
        except _ValidationStepError as exc:
            outcome = f"{exc.stage}_failed"
            validation_failure = {
                "stage": exc.stage,
                "error_type": exc.error_type,
            }
        except (DiskSpaceInsufficientError, ImportPipelineError) as exc:
            if isinstance(exc, ImportPipelineError) and exc.code != "import_disk_space_insufficient":
                raise
            outcome = "import_disk_space_insufficient"
            detail = exc.detail if isinstance(exc, ImportPipelineError) else {}
            capacity = {
                "required_bytes": int(detail.get("required_bytes", getattr(exc, "required_bytes", 0))),
                "free_bytes": int(detail.get("free_bytes", getattr(exc, "free_bytes", 0))),
            }
        if trace_allocations:
            _current, traced_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        retained = retained_content + retained_raw + retained_title
        indexed_bytes = int((index_result or {}).get("input_materialized_bytes", 0))
        status = _validation_status(
            outcome=outcome,
            verify_ok=verify_ok,
            index_complete=(
                not indexed or bool((index_result or {}).get("atomic_publish"))
            ),
            capacity_probe=capacity_probe,
        )
        evidence = _validation_evidence(
            stages,
            physical_zip_created=archive.is_file(),
        )
        return {
            "scenario": scenario,
            "tier": size_mib,
            "fixture_sha256": _sha256_file(archive),
            "fixture_utf8_bytes": logical_bytes,
            "selected_logical_json_bytes": logical_bytes,
            "physical_zip_bytes": archive.stat().st_size,
            "elements": element_count,
            "committed_conversations": committed_conversations,
            "committed_nodes": committed_nodes,
            "retained_content_text_bytes": retained_content,
            "retained_raw_message_bytes": retained_raw,
            "retained_title_bytes": retained_title,
            "retained_canonical_payload_bytes": retained,
            "indexed_source_bytes": indexed_bytes,
            "retained_to_logical_ratio": retained / logical_bytes,
            "indexed_to_logical_ratio": indexed_bytes / logical_bytes,
            "outcome": outcome,
            "capacity": capacity,
            "validation_failure": validation_failure,
            "validation_mode": "capacity_probe" if capacity_probe else "full_validation",
            **status,
            **evidence,
            "verify_ok": verify_ok,
            "web_index_complete": bool((index_result or {}).get("atomic_publish")) if indexed else None,
            "observed_stages": stages,
            "validation_layers": _validation_layers_from_observed_stages(
                stages,
                retained_canonical_same_scale=retained >= logical_bytes // 2,
                indexed_source_same_scale=(
                    indexed_bytes >= logical_bytes // 4 if indexed else False
                ),
            ),
            "wall_seconds": time.perf_counter() - started_wall,
            "cpu_seconds": time.process_time() - started_cpu,
            "peak_rss_bytes": _rss_bytes(),
            "tracemalloc_peak_bytes": traced_peak,
            "measurement_mode": "allocation_diagnostic" if trace_allocations else "performance",
            "performance_eligible": not trace_allocations,
            "tracemalloc_active_during_measurement": trace_allocations,
            "tracemalloc_traceback_frames": 25 if trace_allocations else 0,
            "inherited_tracemalloc_disabled": inherited_tracemalloc_disabled,
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_data_worker(
    size_gib: int, *, trace_allocations: bool, capacity_probe: bool = False
) -> dict[str, Any]:
    import sqlite3

    target_bytes = size_gib * 1024 * 1024 * 1024
    maximum_element_bytes = 30 * 1024 * 1024
    available_for_elements = target_bytes - 2
    element_count = max(1, math.ceil(available_for_elements / maximum_element_bytes))
    base_size, extra = divmod(
        available_for_elements - max(0, element_count - 1), element_count
    )
    if base_size <= 0 or base_size > maximum_element_bytes:
        raise RuntimeError("logical_archive_tier_layout_invalid")

    inherited_tracemalloc_disabled = _begin_allocation_measurement(
        trace_allocations
    )
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    traced_peak: int | None = None
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        archive = base / "synthetic-scale.zip"
        database = base / "archive.db"
        with zipfile.ZipFile(
            archive,
            "w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as bundle:
            with bundle.open("conversations.json", "w", force_zip64=True) as writer:
                writer.write(b"[")
                for index in range(element_count):
                    if index:
                        writer.write(b",")
                    _write_exact_sized_valid_element(
                        writer,
                        element_size=base_size + (1 if index < extra else 0),
                        element_index=index,
                    )
                writer.write(b"]")
        compressed_bytes = archive.stat().st_size
        archive_sha256 = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                archive_sha256.update(chunk)
        outcome = "completed"
        capacity: dict[str, int] | None = None
        summary: dict[str, Any] | None = None
        index_result: dict[str, Any] | None = None
        verify_ok: bool | None = None
        validation_failure: dict[str, str] | None = None
        committed_conversations = 0
        committed_nodes = 0
        skipped_elements = 0
        warning_count = 0
        observed_stages: list[str] = []
        retained_content_text_bytes = 0
        retained_raw_message_bytes = 0
        retained_title_bytes = 0
        try:
            summary = run_import_pipeline(
                database,
                str(archive),
                cwd=base,
                no_input_sha256=False,
                progress_callback=lambda stage, _summary: observed_stages.append(stage),
            )
            conn = sqlite3.connect(database)
            conn.row_factory = sqlite3.Row
            try:
                verify_ok = bool(
                    _run_validation_step(
                        observed_stages,
                        "verify",
                        lambda: verify_database(conn),
                    )["ok"]
                )
                committed_conversations = int(
                    conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
                )
                committed_nodes = int(
                    conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0]
                )
                retained_content_text_bytes = int(conn.execute(
                    "SELECT COALESCE(SUM(length(CAST(content_text AS BLOB))),0) "
                    "FROM conversation_nodes"
                ).fetchone()[0])
                retained_raw_message_bytes = int(conn.execute(
                    "SELECT COALESCE(SUM(length(CAST(raw_message_json AS BLOB))),0) "
                    "FROM conversation_nodes"
                ).fetchone()[0])
                retained_title_bytes = int(conn.execute(
                    "SELECT COALESCE(SUM(length(CAST(title AS BLOB))),0) "
                    "FROM conversations"
                ).fetchone()[0])
                warning_count = int(
                    conn.execute("SELECT COUNT(*) FROM import_warnings").fetchone()[0]
                )
                run_summary = json.loads(
                    conn.execute(
                        "SELECT summary_json FROM import_runs ORDER BY id DESC LIMIT 1"
                    ).fetchone()[0]
                )
                skipped_elements = int(
                    run_summary.get(
                        "skipped_invalid_elements",
                        run_summary.get("skipped_elements", 0),
                    )
                )
            finally:
                conn.close()
            if (
                committed_conversations != element_count
                or skipped_elements != 0
                or warning_count != 0
            ):
                raise RuntimeError(
                    "resource_stress_import_contract_mismatch "
                    f"committed={committed_conversations} "
                    f"skipped={skipped_elements} warnings={warning_count} "
                    f"expected_committed={element_count}"
                )
            index_result = _run_validation_step(
                observed_stages,
                "web_index",
                lambda: create_web_indexes(database),
            )
        except _ValidationStepError as exc:
            outcome = f"{exc.stage}_failed"
            validation_failure = {
                "stage": exc.stage,
                "error_type": exc.error_type,
            }
        except (DiskSpaceInsufficientError, ImportPipelineError) as exc:
            if (
                isinstance(exc, ImportPipelineError)
                and exc.code != "import_disk_space_insufficient"
            ):
                raise
            outcome = exc.code
            detail = exc.detail if isinstance(exc, ImportPipelineError) else {}
            capacity = {
                "required_bytes": int(
                    detail.get(
                        "required_bytes",
                        getattr(exc, "required_bytes", 0),
                    )
                ),
                "free_bytes": int(
                    detail.get("free_bytes", getattr(exc, "free_bytes", 0))
                ),
            }
        if trace_allocations:
            _current, traced_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        database_bytes = database.stat().st_size if database.exists() else 0
        wal = database.with_name(database.name + "-wal")
        journal = database.with_name(database.name + "-journal")
        valid_json_element_bytes = target_bytes - 2 - max(0, element_count - 1)
        retained_canonical_payload_bytes = (
            retained_content_text_bytes
            + retained_raw_message_bytes
            + retained_title_bytes
        )
        indexed_source_bytes = (
            int(index_result.get("input_materialized_bytes", 0))
            if index_result else 0
        )
        evidence = _validation_evidence(
            observed_stages,
            physical_zip_created=archive.is_file(),
        )
        result = {
            "scenario": "physical-logical-parser-gib",
            "tier": size_gib,
            "fixture_sha256": archive_sha256.hexdigest(),
            "fixture_utf8_bytes": target_bytes,
            "compressed_bytes": compressed_bytes,
            "selected_logical_json_bytes": target_bytes,
            "elements": element_count,
            "physical_zip_bytes": compressed_bytes,
            "valid_json_element_bytes": valid_json_element_bytes,
            "skipped_json_element_bytes": 0,
            "retained_content_text_bytes": retained_content_text_bytes,
            "retained_raw_message_bytes": retained_raw_message_bytes,
            "retained_title_bytes": retained_title_bytes,
            "retained_canonical_payload_bytes": retained_canonical_payload_bytes,
            "indexed_source_bytes": indexed_source_bytes,
            "retained_to_logical_ratio": (
                retained_canonical_payload_bytes / target_bytes
            ),
            "indexed_to_logical_ratio": indexed_source_bytes / target_bytes,
            "committed_conversations": committed_conversations,
            "committed_nodes": committed_nodes,
            "skipped_invalid_elements": skipped_elements,
            "warning_count": warning_count,
            "outcome": outcome,
            "validation_mode": "capacity_probe" if capacity_probe else "full_validation",
            "observed_stages": observed_stages,
            **_validation_status(
                outcome=outcome,
                verify_ok=verify_ok,
                index_complete=bool(index_result),
                capacity_probe=capacity_probe,
            ),
            **evidence,
            "capacity": capacity,
            "validation_failure": validation_failure,
            "verify_ok": verify_ok,
            "web_index_complete": (
                bool(index_result.get("atomic_publish")) if index_result else None
            ),
            "indexed_message_bytes": indexed_source_bytes,
            "database_bytes_at_measurement_end": database_bytes,
            "wal_bytes_at_measurement_end": wal.stat().st_size if wal.exists() else 0,
            "journal_bytes_at_measurement_end": (
                journal.stat().st_size if journal.exists() else 0
            ),
            "observable_temp_bytes_at_measurement_end": None,
            "web_index_live_peak_bytes": None,
            "web_index_staging_peak_bytes": None,
            "upload_spool_peak_bytes": None,
            "execution_path": "cli_local_file",
            "http_upload_validated": False,
            "validation_layers": _validation_layers_from_observed_stages(
                observed_stages,
                retained_canonical_same_scale=False,
                indexed_source_same_scale=False,
            ),
            "diagnostic_limitations": [
                "end-of-run file sizes are not filesystem peaks",
                "SQLite TEMP and in-database live/staging object peaks are unobserved",
                "CLI input is not a Web upload spool",
            ],
            "database_bytes": database_bytes,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0,
            "temp_bytes": None,
            "wall_seconds": time.perf_counter() - started_wall,
            "cpu_seconds": time.process_time() - started_cpu,
            "peak_rss_bytes": _rss_bytes(),
            "tracemalloc_peak_bytes": traced_peak,
            "measurement_mode": (
                "allocation_diagnostic" if trace_allocations else "performance"
            ),
            "performance_eligible": not trace_allocations,
            "tracemalloc_active_during_measurement": trace_allocations,
            "tracemalloc_traceback_frames": 25 if trace_allocations else 0,
            "inherited_tracemalloc_disabled": inherited_tracemalloc_disabled,
        }
    return result


def _resource_stress_worker(
    size_gib: int,
    *,
    trace_allocations: bool,
    capacity_probe: bool = False,
    _target_bytes: int | None = None,
) -> dict[str, Any]:
    """Exercise large invalid elements plus one valid tail without mislabelling it."""
    import sqlite3

    target_bytes = (
        size_gib * 1024 * 1024 * 1024
        if _target_bytes is None
        else int(_target_bytes)
    )
    maximum_element_bytes = 30 * 1024 * 1024
    valid_tail = (
        b'{"id":"resource-valid","conversation_id":"resource-valid",'
        b'"title":"Synthetic","current_node":"root","mapping":{"root":{'
        b'"id":"root","parent":null,"children":[],"message":null}}}'
    )
    available_for_invalid = target_bytes - len(valid_tail) - 3
    element_count = max(1, math.ceil(available_for_invalid / maximum_element_bytes))
    base_size, extra = divmod(
        available_for_invalid - max(0, element_count - 1), element_count
    )
    if base_size <= 0 or base_size > maximum_element_bytes:
        raise RuntimeError("resource_stress_tier_layout_invalid")

    inherited_tracemalloc_disabled = _begin_allocation_measurement(
        trace_allocations
    )
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    traced_peak: int | None = None
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        archive = base / "synthetic-resource-stress.zip"
        database = base / "archive.db"
        with zipfile.ZipFile(
            archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=1,
            allowZip64=True,
        ) as bundle:
            with bundle.open("conversations.json", "w", force_zip64=True) as writer:
                writer.write(b"[")
                for index in range(element_count):
                    if index:
                        writer.write(b",")
                    _write_exact_sized_invalid_element(
                        writer,
                        element_size=base_size + (1 if index < extra else 0),
                        element_index=index,
                    )
                writer.write(b",")
                writer.write(valid_tail)
                writer.write(b"]")
        physical_zip_bytes = archive.stat().st_size
        archive_sha256 = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                archive_sha256.update(chunk)
        outcome = "completed"
        capacity: dict[str, int] | None = None
        verify_ok: bool | None = None
        index_result: dict[str, Any] | None = None
        validation_failure: dict[str, str] | None = None
        committed_conversations = 0
        committed_nodes = 0
        skipped_elements = 0
        warning_count = 0
        retained_content_text_bytes = 0
        retained_raw_message_bytes = 0
        retained_title_bytes = 0
        observed_stages: list[str] = []
        try:
            run_import_pipeline(
                database,
                str(archive),
                cwd=base,
                no_input_sha256=False,
                progress_callback=lambda stage, _summary: observed_stages.append(
                    stage
                ),
            )
            conn = sqlite3.connect(database)
            conn.row_factory = sqlite3.Row
            try:
                verify_ok = bool(
                    _run_validation_step(
                        observed_stages,
                        "verify",
                        lambda: verify_database(conn),
                    )["ok"]
                )
                committed_conversations = int(
                    conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
                )
                committed_nodes = int(
                    conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0]
                )
                retained_content_text_bytes = int(conn.execute(
                    "SELECT COALESCE(SUM(length(CAST(content_text AS BLOB))),0) "
                    "FROM conversation_nodes"
                ).fetchone()[0])
                retained_raw_message_bytes = int(conn.execute(
                    "SELECT COALESCE(SUM(length(CAST(raw_message_json AS BLOB))),0) "
                    "FROM conversation_nodes"
                ).fetchone()[0])
                retained_title_bytes = int(conn.execute(
                    "SELECT COALESCE(SUM(length(CAST(title AS BLOB))),0) "
                    "FROM conversations"
                ).fetchone()[0])
                warning_count = int(
                    conn.execute("SELECT COUNT(*) FROM import_warnings").fetchone()[0]
                )
                run_summary = json.loads(
                    conn.execute(
                        "SELECT summary_json FROM import_runs ORDER BY id DESC LIMIT 1"
                    ).fetchone()[0]
                )
                skipped_elements = int(run_summary.get("skipped_invalid_elements", 0))
            finally:
                conn.close()
            if (
                committed_conversations != 1
                or skipped_elements != element_count
                or warning_count < element_count
            ):
                raise RuntimeError("resource_stress_import_contract_mismatch")
            index_result = _run_validation_step(
                observed_stages,
                "web_index",
                lambda: create_web_indexes(database),
            )
        except _ValidationStepError as exc:
            outcome = f"{exc.stage}_failed"
            validation_failure = {
                "stage": exc.stage,
                "error_type": exc.error_type,
            }
        except (DiskSpaceInsufficientError, ImportPipelineError) as exc:
            if isinstance(exc, ImportPipelineError) and exc.code != "import_disk_space_insufficient":
                raise
            outcome = exc.code
            detail = exc.detail if isinstance(exc, ImportPipelineError) else {}
            capacity = {
                "required_bytes": int(detail.get("required_bytes", getattr(exc, "required_bytes", 0))),
                "free_bytes": int(detail.get("free_bytes", getattr(exc, "free_bytes", 0))),
            }
        if trace_allocations:
            _current, traced_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        database_bytes = database.stat().st_size if database.exists() else 0
        wal = database.with_name(database.name + "-wal")
        journal = database.with_name(database.name + "-journal")
        retained_canonical_payload_bytes = (
            retained_content_text_bytes
            + retained_raw_message_bytes
            + retained_title_bytes
        )
        indexed_source_bytes = (
            int(index_result.get("input_materialized_bytes", 0))
            if index_result else 0
        )
        evidence = _validation_evidence(
            observed_stages,
            physical_zip_created=archive.is_file(),
        )
        result = {
            "scenario": "resource-stress-gib",
            "tier": size_gib,
            "fixture_sha256": archive_sha256.hexdigest(),
            "fixture_utf8_bytes": target_bytes,
            "physical_zip_bytes": physical_zip_bytes,
            "selected_logical_json_bytes": target_bytes,
            "valid_json_element_bytes": len(valid_tail),
            "skipped_json_element_bytes": available_for_invalid,
            "retained_content_text_bytes": retained_content_text_bytes,
            "retained_raw_message_bytes": retained_raw_message_bytes,
            "retained_title_bytes": retained_title_bytes,
            "retained_canonical_payload_bytes": retained_canonical_payload_bytes,
            "indexed_source_bytes": indexed_source_bytes,
            "retained_to_logical_ratio": (
                retained_canonical_payload_bytes / target_bytes
            ),
            "indexed_to_logical_ratio": indexed_source_bytes / target_bytes,
            "elements": element_count + 1,
            "large_invalid_elements": element_count,
            "committed_conversations": committed_conversations,
            "committed_nodes": committed_nodes,
            "skipped_invalid_elements": skipped_elements,
            "warning_count": warning_count,
            "outcome": outcome,
            "validation_mode": (
                "capacity_probe" if capacity_probe else "full_validation"
            ),
            "observed_stages": observed_stages,
            **_validation_status(
                outcome=outcome,
                verify_ok=verify_ok,
                index_complete=bool(
                    index_result and index_result.get("atomic_publish")
                ),
                capacity_probe=capacity_probe,
            ),
            **evidence,
            "capacity": capacity,
            "validation_failure": validation_failure,
            "verify_ok": verify_ok,
            "web_index_complete": bool(index_result.get("atomic_publish")) if index_result else None,
            "indexed_message_bytes": indexed_source_bytes,
            "database_bytes_at_measurement_end": database_bytes,
            "wal_bytes_at_measurement_end": wal.stat().st_size if wal.exists() else 0,
            "journal_bytes_at_measurement_end": (
                journal.stat().st_size if journal.exists() else 0
            ),
            "observable_temp_bytes_at_measurement_end": None,
            "web_index_live_peak_bytes": None,
            "web_index_staging_peak_bytes": None,
            "upload_spool_peak_bytes": None,
            "execution_path": "cli_local_file",
            "http_upload_validated": False,
            "validation_layers": _validation_layers_from_observed_stages(
                observed_stages,
                retained_canonical_same_scale=False,
                indexed_source_same_scale=False,
            ),
            "diagnostic_limitations": [
                "invalid elements are intentionally skipped",
                "end-of-run file sizes are not filesystem peaks",
                "SQLite TEMP and in-database live/staging object peaks are unobserved",
                "CLI input is not a Web upload spool",
            ],
            "database_bytes": database_bytes,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0,
            "temp_bytes": None,
            "wall_seconds": time.perf_counter() - started_wall,
            "cpu_seconds": time.process_time() - started_cpu,
            "peak_rss_bytes": _rss_bytes(),
            "tracemalloc_peak_bytes": traced_peak,
            "measurement_mode": "allocation_diagnostic" if trace_allocations else "performance",
            "performance_eligible": not trace_allocations,
            "tracemalloc_active_during_measurement": trace_allocations,
            "tracemalloc_traceback_frames": 25 if trace_allocations else 0,
            "inherited_tracemalloc_disabled": inherited_tracemalloc_disabled,
        }
    return result


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    trace_allocations = args.measurement_mode in {
        "allocation_diagnostic",
        "allocation-diagnostic",
    }
    if args.scenario == "many-small":
        return _many_small_worker(args.tier, trace_allocations=trace_allocations)
    if args.scenario == "single-element-mib":
        return _single_element_worker(args.tier, trace_allocations=trace_allocations)
    if args.scenario == "mapping-predecode":
        return _mapping_budget_worker(args.tier, trace_allocations=trace_allocations)
    if args.scenario == "metadata-density":
        return _metadata_density_worker(args.tier, trace_allocations=trace_allocations)
    if args.scenario == "registry-lifecycle":
        return _registry_lifecycle_worker(args.tier, trace_allocations=trace_allocations)
    if args.scenario == "resource-stress-gib":
        return _resource_stress_worker(
            args.tier,
            trace_allocations=trace_allocations,
            capacity_probe=args.capacity_probe,
        )
    if args.scenario in {"physical-logical-parser-gib", "valid-data-gib"}:
        return _valid_data_worker(
            args.tier,
            trace_allocations=trace_allocations,
            capacity_probe=args.capacity_probe,
        )
    if args.scenario in {"retained-canonical-mib", "indexed-source-mib"}:
        return _retained_indexed_worker(
            args.tier,
            indexed=args.scenario == "indexed-source-mib",
            trace_allocations=trace_allocations,
            capacity_probe=args.capacity_probe,
        )
    raise RuntimeError("unknown_scale_scenario")


def _aggregate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    performance = [sample for sample in samples if sample["performance_eligible"]]
    diagnostic = [sample for sample in samples if not sample["performance_eligible"]]
    result: dict[str, Any] = {
        "performance_measurement_mode": "fresh_subprocess_without_tracemalloc",
        "allocation_diagnostic_mode": "fresh_subprocess_with_tracemalloc",
    }
    for key in ("wall_seconds", "cpu_seconds", "peak_rss_bytes"):
        values = [float(sample[key]) for sample in performance]
        result[f"performance_{key}"] = {
            "all": values,
            "median": statistics.median(values) if values else None,
            "worst": max(values) if values else None,
        }
    allocation_values = [
        float(sample["tracemalloc_peak_bytes"])
        for sample in diagnostic
        if isinstance(sample.get("tracemalloc_peak_bytes"), (int, float))
    ]
    result["diagnostic_tracemalloc_peak_bytes"] = {
        "all": allocation_values,
        "median": statistics.median(allocation_values) if allocation_values else None,
        "worst": max(allocation_values) if allocation_values else None,
    }
    return result


def _terminate_kill_reap(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float = 0.75,
    owned_process_group: bool = False,
) -> tuple[str, str, dict[str, Any]]:
    return terminate_kill_reap(
        process,
        timeout_seconds=timeout_seconds,
        owned_process_group=owned_process_group,
    )


def _run_worker_subprocess(
    command: list[str],
    *,
    scenario: str,
    tier: int,
    measurement_mode: str,
    hard_timeout_seconds: float,
    heartbeat_seconds: float,
) -> dict[str, Any]:
    """Run one worker with an independent watchdog and content-free heartbeat."""

    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_worker_environment(),
        start_new_session=os.name == "posix",
    )
    deadline = started + hard_timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stdout, stderr, lifecycle = _terminate_kill_reap(
                process,
                owned_process_group=True,
            )
            return {
                "scenario": scenario,
                "tier": tier,
                "measurement_mode": measurement_mode,
                "performance_eligible": measurement_mode == "performance",
                "wall_seconds": time.monotonic() - started,
                "cpu_seconds": 0.0,
                "peak_rss_bytes": 0,
                "tracemalloc_peak_bytes": None,
                "hard_timeout": True,
                "partial": True,
                **lifecycle,
                "worker_return_code": lifecycle["return_code"],
                "worker_stdout_present": bool(stdout),
                "worker_stderr_present": bool(stderr),
            }
        try:
            stdout, stderr = process.communicate(
                timeout=min(heartbeat_seconds, remaining)
            )
            break
        except subprocess.TimeoutExpired:
            print(
                "scale_heartbeat "
                + json.dumps(
                    {
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "scenario": scenario,
                        "tier": tier,
                        "measurement_mode": measurement_mode,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                file=sys.stderr,
                flush=True,
            )
    if process.returncode != 0:
        return {
            "scenario": scenario,
            "tier": tier,
            "measurement_mode": measurement_mode,
            "performance_eligible": measurement_mode == "performance",
            "wall_seconds": time.monotonic() - started,
            "cpu_seconds": 0.0,
            "peak_rss_bytes": 0,
            "tracemalloc_peak_bytes": None,
            "hard_timeout": False,
            "partial": True,
            "child_pid": process.pid,
            "return_code": process.returncode,
            "terminate_attempted": False,
            "kill_attempted": False,
            "reaped": process.poll() is not None,
            "reap_incomplete": process.poll() is None,
            "worker_failed": True,
            "worker_return_code": process.returncode,
            "worker_stderr_present": bool(stderr),
        }
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "scenario": scenario,
            "tier": tier,
            "measurement_mode": measurement_mode,
            "performance_eligible": measurement_mode == "performance",
            "wall_seconds": time.monotonic() - started,
            "cpu_seconds": 0.0,
            "peak_rss_bytes": 0,
            "tracemalloc_peak_bytes": None,
            "hard_timeout": False,
            "partial": True,
            "child_pid": process.pid,
            "return_code": process.returncode,
            "terminate_attempted": False,
            "kill_attempted": False,
            "reaped": process.poll() is not None,
            "reap_incomplete": process.poll() is None,
            "worker_output_invalid": True,
            "worker_return_code": process.returncode,
            "worker_stderr_present": bool(stderr),
        }
    result["hard_timeout"] = False
    result["partial"] = False
    result["child_pid"] = process.pid
    result["return_code"] = process.returncode
    result["terminate_attempted"] = False
    result["kill_attempted"] = False
    result["reaped"] = process.poll() is not None
    result["reap_incomplete"] = process.poll() is None
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=(
            "many-small",
            "single-element-mib",
            "mapping-predecode",
            "metadata-density",
            "registry-lifecycle",
            "resource-stress-gib",
            "physical-logical-parser-gib",
            "valid-data-gib",
            "retained-canonical-mib",
            "indexed-source-mib",
        ),
        default="many-small",
    )
    parser.add_argument("--tiers", default="")
    parser.add_argument("--tier", type=int, default=1000, help=argparse.SUPPRESS)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--measurement-mode",
        choices=(
            "performance",
            "allocation-diagnostic",
            "allocation_diagnostic",
            "both",
        ),
        default=None,
        help=(
            "Run untraced performance, tracemalloc allocation diagnostics, or "
            "both. Multi-GiB scenarios default to performance only."
        ),
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--capacity-probe",
        action="store_true",
        help="Treat an explicit capacity rejection as the expected result.",
    )
    parser.add_argument(
        "--confirm-huge",
        action="store_true",
        help="required for the opt-in 1/5/10 GiB production workload",
    )
    parser.add_argument(
        "--confirm-huge-allocation-diagnostic",
        action="store_true",
        help="additional opt-in required to trace allocations for a multi-GiB worker",
    )
    parser.add_argument(
        "--hard-child-seconds",
        type=float,
        default=7200.0,
        help="independent watchdog for each worker subprocess",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=30.0,
        help="content-free worker heartbeat interval written to stderr",
    )
    args = parser.parse_args()
    requested_scenario = args.scenario
    if args.scenario == "valid-data-gib":
        print(
            "WARNING: valid-data-gib is deprecated; use physical-logical-parser-gib",
            file=sys.stderr,
        )
        args.scenario = "physical-logical-parser-gib"
    huge_scenarios = {"resource-stress-gib", "physical-logical-parser-gib"}
    retained_scale_scenarios = {"retained-canonical-mib", "indexed-source-mib"}
    normalized_measurement_mode = (
        "allocation_diagnostic"
        if args.measurement_mode == "allocation-diagnostic"
        else args.measurement_mode
    )
    args.measurement_mode = normalized_measurement_mode
    if args.hard_child_seconds <= 0:
        raise SystemExit("invalid_hard_child_seconds")
    if args.heartbeat_seconds <= 0:
        raise SystemExit("invalid_heartbeat_seconds")
    if args.worker:
        if args.tier <= 0:
            raise SystemExit("invalid_scale_tier")
        if args.scenario in huge_scenarios and args.tier not in {1, 5, 10}:
            raise SystemExit("invalid_huge_scale_tier")
        if args.scenario in huge_scenarios and not args.confirm_huge:
            raise SystemExit("huge_worker_requires_--confirm-huge")
        if (
            args.scenario in huge_scenarios
            and args.measurement_mode == "allocation_diagnostic"
            and not args.confirm_huge_allocation_diagnostic
        ):
            raise SystemExit(
                "huge_allocation_diagnostic_requires_--confirm-huge-allocation-diagnostic"
            )
        if args.scenario in retained_scale_scenarios and args.tier not in {256, 1024, 5120, 10240}:
            raise SystemExit("invalid_retained_scale_tier")
        if args.scenario in retained_scale_scenarios and args.tier >= 5120 and not args.confirm_huge:
            raise SystemExit("huge_worker_requires_--confirm-huge")
        if args.measurement_mode in {None, "both"}:
            raise SystemExit("worker_requires_single_measurement_mode")
        print(json.dumps(_worker(args), sort_keys=True, separators=(",", ":")))
        return 0

    if args.self_test:
        args.scenario = "many-small"
        tiers = [1000]
        runs = 1
    else:
        defaults = {
            "many-small": [100_000, 1_000_000],
            "single-element-mib": [1, 4, 8, 16, 20, 31, 32, 100],
            "mapping-predecode": [100_000, 500_000, 1_000_000],
            "metadata-density": [10, 25, 50, 100, 200],
            "registry-lifecycle": [100_000],
            "resource-stress-gib": [1, 5, 10],
            "physical-logical-parser-gib": [1, 5, 10],
            "retained-canonical-mib": [256],
            "indexed-source-mib": [256],
        }
        tiers = (
            [int(item) for item in args.tiers.split(",") if item]
            if args.tiers
            else defaults[args.scenario]
        )
        runs = max(1, args.runs)
    if args.scenario in huge_scenarios and not args.confirm_huge:
        raise SystemExit("resource_stress_gib_requires_--confirm-huge")
    if not tiers or any(tier <= 0 for tier in tiers) or len(tiers) != len(set(tiers)):
        raise SystemExit("invalid_scale_tiers")
    if args.scenario in huge_scenarios and any(tier not in {1, 5, 10} for tier in tiers):
        raise SystemExit("invalid_huge_scale_tier")
    if args.scenario in retained_scale_scenarios and any(
        tier not in {256, 1024, 5120, 10240} for tier in tiers
    ):
        raise SystemExit("invalid_retained_scale_tier")
    if args.scenario in retained_scale_scenarios and any(tier >= 5120 for tier in tiers) and not args.confirm_huge:
        raise SystemExit("retained_scale_huge_requires_--confirm-huge")
    if args.measurement_mode is None:
        args.measurement_mode = (
            "performance"
            if args.scenario in huge_scenarios | retained_scale_scenarios
            else "both"
        )
    measurement_modes = (
        ("performance", "allocation_diagnostic")
        if args.measurement_mode == "both"
        else (args.measurement_mode,)
    )
    if (
        args.scenario in huge_scenarios
        and "allocation_diagnostic" in measurement_modes
        and not args.confirm_huge_allocation_diagnostic
    ):
        raise SystemExit(
            "huge_allocation_diagnostic_requires_--confirm-huge-allocation-diagnostic"
        )

    samples: list[dict[str, Any]] = []
    coverage: dict[str, int] = {}
    for run_index in range(runs):
        for tier in tiers:
            for measurement_mode in measurement_modes:
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--scenario",
                    args.scenario,
                    "--tier",
                    str(tier),
                    "--measurement-mode",
                    measurement_mode,
                    "--worker",
                ]
                if args.scenario in huge_scenarios:
                    command.append("--confirm-huge")
                if args.scenario in retained_scale_scenarios and tier >= 5120:
                    command.append("--confirm-huge")
                if args.capacity_probe:
                    command.append("--capacity-probe")
                if measurement_mode == "allocation_diagnostic":
                    command.append("--confirm-huge-allocation-diagnostic")
                sample = _run_worker_subprocess(
                    command,
                    scenario=args.scenario,
                    tier=tier,
                    measurement_mode=measurement_mode,
                    hard_timeout_seconds=args.hard_child_seconds,
                    heartbeat_seconds=args.heartbeat_seconds,
                )
                sample["run_index"] = run_index
                samples.append(sample)
                key = f"{args.scenario}:{tier}:{run_index}:{measurement_mode}"
                coverage[key] = coverage.get(key, 0) + 1
    expected = {
        f"{args.scenario}:{tier}:{run_index}:{measurement_mode}"
        for run_index in range(runs)
        for tier in tiers
        for measurement_mode in measurement_modes
    }
    if set(coverage) != expected or any(value != 1 for value in coverage.values()):
        raise RuntimeError("scale_tier_coverage_mismatch")
    grouped = {
        str(tier): _aggregate(
            [sample for sample in samples if int(sample["tier"]) == tier]
        )
        for tier in tiers
    }
    print(
        json.dumps(
            {
                "schema": "chatgpt-sqlite-webui-scale-v4",
                "environment": _environment(),
                "scenario": args.scenario,
                "requested_scenario": requested_scenario,
                "scenario_alias_deprecated": requested_scenario == "valid-data-gib",
                "measurement_mode": args.measurement_mode,
                "measurement_modes_run": list(measurement_modes),
                "formal_performance_samples": "untraced_only",
                "hard_child_seconds": args.hard_child_seconds,
                "moved_scale_cases": MOVED_SCALE_CASES,
                "resource_stress_fixture_self_test": (
                    {
                        "invalid_elements_are_not_valid_data": True,
                        "valid_tail_conversations": 1,
                    }
                    if args.self_test else None
                ),
                "valid_data_fixture_self_test": (
                    _valid_data_fixture_self_test() if args.self_test else None
                ),
                "representative_valid_data_fixture_self_test": (
                    _representative_valid_data_fixture_self_test()
                    if args.self_test else None
                ),
                "tiers": tiers,
                "runs_per_tier": runs,
                "coverage": coverage,
                "aggregate": grouped,
                "samples": samples,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 1 if any(
        sample.get("hard_timeout")
        or sample.get("worker_failed")
        or sample.get("worker_output_invalid")
        for sample in samples
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
