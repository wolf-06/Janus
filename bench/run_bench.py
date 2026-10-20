"""Repeatable benchmark harness for the Janus code that exists today.

Run with ``uv run python bench/run_bench.py``. Progress goes to stderr; the Markdown
report is written to ``bench/RESULTS.md`` (and echoed to stdout).

Every number in the report comes from the run that produced it. A section that cannot run
in this environment is reported as ``n/a`` with the reason; it never aborts the run.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import docker
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = ROOT / "bench"
RESULTS_PATH = BENCH_DIR / "RESULTS.md"
FIXTURE = ROOT / "tests" / "fixtures" / "sample_repo"
FAILING_TEST = "tests/test_logic.py::test_sum_to_includes_the_last_term"
DEFAULT_DB_URL = "postgresql+asyncpg://janus:janus@localhost:5432/janus_test"

ALL_SECTIONS: tuple[str, ...] = (
    "code",
    "static",
    "tests",
    "db_ingest",
    "queue_drain",
    "sandbox_overhead",
    "reproduce",
    "triage",
    "sbfl",
    "llm",
    "caveats",
)

FULL_INGEST_N = 200
FAST_INGEST_N = 50
FULL_TRIVIAL_N = 5
FAST_TRIVIAL_N = 2
FULL_K = 5
FAST_K = 2

_SUMMARY_RE = re.compile(
    r"(?:(?P<passed>\d+) passed)?"
    r"(?:(?:, )?(?P<failed>\d+) failed)?"
    r"(?:(?:, )?(?P<errors>\d+) error(?:s)?)?"
    r"(?:(?:, )?(?P<skipped>\d+) skipped)?"
    r"(?:(?:, )?(?P<xfailed>\d+) xfailed)?"
    r"(?:(?:, )?(?P<xpassed>\d+) xpassed)?"
)


def progress(message: str) -> None:
    print(f"[bench] {message}", file=sys.stderr, flush=True)


def na(reason: str) -> str:
    return f"n/a — {reason}"


def fmt(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}f}"


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (p / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * weight


def run_command(argv: Sequence[str], *, timeout: float = 1800.0) -> tuple[float, int, str, str]:
    start = time.monotonic()
    completed = subprocess.run(
        list(argv),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    elapsed = time.monotonic() - start
    return elapsed, completed.returncode, completed.stdout, completed.stderr


def _tail(text: str, limit: int = 1200) -> str:
    collapsed = text.strip()
    if len(collapsed) <= limit:
        return collapsed
    return "..." + collapsed[-limit:]


def _pytest_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in output.splitlines():
        if " passed" not in line and " failed" not in line and " skipped" not in line:
            continue
        match = _SUMMARY_RE.search(line)
        if match is None:
            continue
        for key, value in match.groupdict().items():
            if value is not None:
                counts[key] = int(value)
        if counts:
            break
    return counts


def _summary_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else "(no output)"


async def _postgres_version(url: str) -> str:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version()"))
            return str(result.scalar_one())
    finally:
        await engine.dispose()


async def collect_environment() -> dict[str, str]:
    environment: dict[str, str] = {
        "os": platform.platform(),
        "cpu_count": str(os.cpu_count() or 0),
        "python": platform.python_version(),
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    try:
        client = docker.from_env(timeout=10)
        try:
            version = client.version()
            environment["docker"] = str(version.get("Version", "unknown"))
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 - environment probing is best effort
        environment["docker"] = na(f"{type(exc).__name__}: {exc}")

    url = os.environ.get("JANUS_BENCH_DATABASE_URL", DEFAULT_DB_URL)
    try:
        environment["postgres"] = await _postgres_version(url)
    except Exception as exc:  # noqa: BLE001 - environment probing is best effort
        environment["postgres"] = na(f"{type(exc).__name__}: {exc}")

    return environment


def section_code() -> str:
    files = sorted(
        path for path in (ROOT / "janus").rglob("*.py") if "__pycache__" not in path.parts
    )
    raw_lines = 0
    code_lines = 0
    for path in files:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        raw_lines += len(lines)
        code_lines += sum(1 for line in lines if line.strip() and not line.strip().startswith("#"))

    collected, returncode, stdout, stderr = run_command(
        ["uv", "run", "pytest", "--collect-only", "-q"]
    )
    last_line = _summary_line(stdout or stderr)
    test_count = ""
    match = re.search(r"(\d+)\s+tests?\s+collected", last_line)
    if match is not None:
        test_count = match.group(1)

    packages: list[tuple[str, int]] = []
    janus_dir = ROOT / "janus"
    for entry in sorted(janus_dir.iterdir()):
        if not entry.is_dir() or entry.name == "__pycache__":
            continue
        count = sum(1 for path in entry.rglob("*.py") if "__pycache__" not in path.parts)
        packages.append((entry.name, count))
    root_modules = sum(1 for path in janus_dir.glob("*.py"))

    package_rows = "\n".join(f"| `{name}` | {count} |" for name, count in packages)
    collected_note = ""
    if returncode != 0:
        collected_note = f"\n`pytest --collect-only` exited {returncode}: {_tail(stderr or stdout)}"

    return (
        f"- Python files under `janus/`: **{len(files)}**\n"
        f"- Raw lines: **{raw_lines}**\n"
        f"- Non-blank, non-comment lines (docstrings included): **{code_lines}**\n"
        f"- Tests (`pytest --collect-only -q`): **{test_count or 'unknown'}** "
        f"— last line: `{last_line}`\n"
        f"- Collection wall time: {fmt(collected)} s\n"
        f"{collected_note}\n\n"
        "| Top-level package | Python modules |\n"
        "|---|---:|\n"
        f"{package_rows}\n"
        f"| `janus` (root modules) | {root_modules} |\n"
    )


def section_static() -> str:
    commands = (
        ("ruff check", ["uv", "run", "ruff", "check", "."]),
        ("ruff format --check", ["uv", "run", "ruff", "format", "--check", "."]),
        ("mypy", ["uv", "run", "mypy", "janus"]),
    )
    rows: list[str] = [
        "| Check | Wall time (s) | Exit code |",
        "|---|---:|---:|",
    ]
    notes: list[str] = []
    for label, argv in commands:
        elapsed, returncode, stdout, stderr = run_command(argv)
        rows.append(f"| `{label}` | {fmt(elapsed)} | {returncode} |")
        if returncode != 0:
            notes.append(f"- `{label}` output tail: `{_tail(stdout or stderr)}`")
    body = "\n".join(rows)
    if notes:
        body += "\n\n" + "\n".join(notes)
    body += "\n\nWall time includes the `uv run` process; tool caches may be warm.\n"
    return body


def section_tests() -> str:
    elapsed, returncode, stdout, stderr = run_command(["uv", "run", "pytest", "-q"])
    counts = _pytest_counts(stdout + "\n" + stderr)
    detail = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    if not detail:
        detail = "no parsed counts"
    last = _summary_line(stdout or stderr)
    return (
        f"- Wall time: **{fmt(elapsed)} s**\n"
        f"- Exit code: **{returncode}**\n"
        f"- Parsed counts: {detail}\n"
        f"- Last line: `{last}`\n"
    )


class DatabaseBench:
    """The Postgres schema and seed used by the ingestion and drain sections."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine: AsyncEngine | None = None
        self.factory: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        self.engine = create_async_engine(self.url, pool_pre_ping=True)
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        self.factory = async_sessionmaker(self.engine, autoflush=False, expire_on_commit=False)

    async def reset_schema(self) -> None:
        from janus.db.models import Base

        assert self.engine is not None
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

    async def seed(self) -> None:
        from janus.db.repository import (
            get_or_create_run,
            upsert_installation,
            upsert_repository,
        )

        assert self.factory is not None
        async with self.factory() as session:
            await upsert_installation(session, installation_id=1, account="bench", app_id=1)
            await upsert_repository(
                session,
                repo_id=1,
                owner="bench",
                name="sample",
                default_branch="main",
                installation_id=1,
            )
            await get_or_create_run(
                session,
                run_id=1,
                repo_id=1,
                head_sha="a" * 40,
                head_branch="main",
                conclusion="failure",
                event="push",
            )
            await session.commit()

    async def enqueue(self, count: int, *, prefix: str, measure: bool) -> list[float]:
        from janus.db.repository import record_delivery
        from janus.jobs.queue import enqueue

        assert self.factory is not None
        latencies: list[float] = []
        for index in range(count):
            delivery_id = f"{prefix}-{index}"
            start = time.perf_counter()
            async with self.factory() as session:
                inserted = await record_delivery(
                    session, delivery_id=delivery_id, repo_id=1, event="workflow_run"
                )
                if measure and not inserted:
                    raise RuntimeError(f"delivery {delivery_id} already existed")
                await enqueue(
                    session,
                    delivery_id=delivery_id,
                    repo_id=1,
                    run_id=1,
                    max_attempts=5,
                )
                await session.commit()
            if measure:
                latencies.append(time.perf_counter() - start)
        return latencies

    async def queued_count(self) -> int:
        from janus.db.models import Job

        assert self.factory is not None
        async with self.factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(Job).where(Job.state == "queued")
            )
            return int(count or 0)

    async def state_counts(self) -> dict[str, int]:
        from janus.db.models import Job

        assert self.factory is not None
        async with self.factory() as session:
            rows = (
                await session.execute(select(Job.state, func.count()).group_by(Job.state))
            ).all()
            return {str(state): int(count) for state, count in rows}

    async def clear_jobs(self) -> None:
        from janus.db.models import Delivery, Job

        assert self.factory is not None
        async with self.factory() as session:
            await session.execute(delete(Job))
            await session.execute(delete(Delivery))
            await session.commit()

    async def dispose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()


async def section_db_ingest(db: DatabaseBench, count: int) -> str:
    await db.seed()
    latencies = await db.enqueue(count, prefix="ingest", measure=True)
    queued = await db.queued_count()
    return (
        f"- N: **{count}** deliveries/jobs\n"
        f"- Path measured: `record_delivery` + `enqueue` + `commit` in one transaction "
        f"(the ingest critical path)\n"
        f"- p50: **{fmt(percentile(latencies, 50) * 1000)} ms**\n"
        f"- p95: **{fmt(percentile(latencies, 95) * 1000)} ms**\n"
        f"- p99: **{fmt(percentile(latencies, 99) * 1000)} ms**\n"
        f"- min: {fmt(min(latencies) * 1000)} ms; max: {fmt(max(latencies) * 1000)} ms; "
        f"mean: {fmt((sum(latencies) / len(latencies)) * 1000)} ms\n"
        f"- Total enqueue wall time: {fmt(sum(latencies))} s\n"
        f"- Queued jobs after ingest: **{queued}**\n"
    )


async def section_queue_drain(db: DatabaseBench, count: int) -> str:
    from janus.app.config import Settings
    from janus.jobs.worker import Worker, stub_processor

    await db.seed()
    if await db.queued_count() != count:
        await db.clear_jobs()
        await db.enqueue(count, prefix="drain", measure=False)

    assert db.factory is not None
    settings = Settings(
        repo_quota_max_jobs=count + 100,
        worker_poll_interval_seconds=0.01,
    )
    worker = Worker(db.factory, stub_processor, settings=settings)

    processed = 0
    start = time.perf_counter()
    while await worker.run_once():
        processed += 1
    elapsed = time.perf_counter() - start
    states = await db.state_counts()
    rate = processed / elapsed if elapsed > 0 else 0.0
    state_text = ", ".join(f"{key}={value}" for key, value in sorted(states.items()))
    return (
        f"- Jobs processed: **{processed}**\n"
        f"- Total drain time: **{fmt(elapsed)} s**\n"
        f"- Throughput: **{fmt(rate, 1)} jobs/s** (single worker, default `stub_processor`)\n"
        f"- Final job states: {state_text}\n"
    )


class FixtureImage:
    """One generated image for the fixture repo, shared by the sandbox sections."""

    def __init__(self) -> None:
        from janus.app.config import Settings
        from janus.sandbox.image import resolve_image
        from janus.sandbox.settings import ImageSource

        self.client = docker.from_env(timeout=30)
        self.client.ping()
        base = Settings().sandbox
        self.generated = base.model_copy(
            update={
                "image_source": ImageSource.GENERATED,
                "network": False,
            }
        )
        self.resolved = resolve_image(self.generated, FIXTURE)
        if not self.resolved.build or self.resolved.context is None:
            raise RuntimeError("fixture image did not resolve to a buildable generated image")
        self.tag = self.resolved.tag

    def exists(self) -> bool:
        try:
            self.client.images.get(self.tag)
            return True
        except docker.errors.ImageNotFound:
            return False

    def remove_tag(self) -> None:
        with contextlib.suppress(docker.errors.DockerException):
            self.client.images.remove(self.tag, force=True)

    def build(self, *, cold: bool) -> float:
        from janus.sandbox.image import build_image

        if cold:
            self.remove_tag()
        start = time.perf_counter()
        build_image(self.client, self.resolved, self.generated)
        return time.perf_counter() - start

    def ensure(self) -> None:
        if not self.exists():
            self.build(cold=False)

    def prebuilt(self) -> Any:
        from janus.sandbox.settings import ImageSource

        return self.generated.model_copy(
            update={
                "image_source": ImageSource.PREBUILT,
                "image": self.tag,
                "pull_image": False,
                "remove_image": False,
            }
        )

    def cleanup(self) -> None:
        self.remove_tag()
        if self.resolved.context is not None:
            shutil.rmtree(self.resolved.context, ignore_errors=True)
        self.client.close()


def section_sandbox_overhead(image: FixtureImage, trivial_n: int) -> str:
    from janus.sandbox.runner import SandboxRunner
    from janus.sandbox.spec import build_spec

    cold = image.build(cold=True)
    cached = image.build(cold=False)

    settings = image.prebuilt()
    runner = SandboxRunner(settings=settings)
    latencies: list[float] = []
    peaks: list[float] = []
    cpus: list[float] = []
    errors: list[str] = []
    for _ in range(trivial_n):
        spec = build_spec(settings, command=("python", "-c", "print(1)"))
        start = time.perf_counter()
        result = runner.run(spec)
        latencies.append(time.perf_counter() - start)
        peaks.append(result.peak_mem_mb)
        cpus.append(result.cpu_seconds)
        if result.error is not None:
            errors.append(result.error)

    error_line = (
        f"\n- Trivial-run errors: {', '.join(errors)}" if errors else "\n- Trivial-run errors: none"
    )
    return (
        f"- Generated image tag: `{image.tag}`\n"
        f"- First build after removing the tag (cold): **{fmt(cold)} s**\n"
        f"- Second build of the same tag (layers cached): **{fmt(cached)} s**\n"
        f'- Trivial command `python -c "print(1)"`, N={trivial_n}:\n'
        f"  - p50: **{fmt(percentile(latencies, 50))} s**\n"
        f"  - p95: **{fmt(percentile(latencies, 95))} s**\n"
        f"  - min/max: {fmt(min(latencies))} / {fmt(max(latencies))} s\n"
        f"  - peak memory (max over N): **{fmt(max(peaks), 1)} MiB**\n"
        f"  - CPU seconds (max over N): **{fmt(max(cpus))} s**"
        f"{error_line}\n"
    )


@dataclass
class ReproductionRun:
    reproduction: Any
    wall_seconds: float


def _produce_reproduction(image: FixtureImage, k: int) -> ReproductionRun:
    from janus.adapters.python.adapter import PythonAdapter
    from janus.app.config import Settings
    from janus.core.adapter import TestTarget
    from janus.core.reproducer import Reproducer, ReproduceRequest
    from janus.sandbox.profiles import Profile
    from janus.sandbox.runner import SandboxRunner

    settings = image.prebuilt()
    adapter = PythonAdapter()
    runner = SandboxRunner(settings=settings)
    reproducer = Reproducer(
        adapter=adapter,
        runner=runner,
        settings=Settings(sandbox=settings, k_reruns=k),
    )
    target = TestTarget(id=FAILING_TEST, node_ids=(FAILING_TEST,))
    start = time.perf_counter()
    reproduction = reproducer.reproduce(
        FIXTURE,
        ReproduceRequest(targets=(target,), k=k, profile=Profile.CLEAN),
    )
    return ReproductionRun(reproduction=reproduction, wall_seconds=time.perf_counter() - start)


def section_reproduce(image: FixtureImage, k: int) -> tuple[str, ReproductionRun]:
    image.ensure()
    run = _produce_reproduction(image, k)
    reproduction = run.reproduction
    attempt_durations = ", ".join(fmt(attempt.duration_s) for attempt in reproduction.attempts)
    full = reproduction.full_suite
    full_text = "n/a (no full-suite attempt)" if full is None else fmt(full.duration_s)
    full_state = "n/a"
    if full is not None:
        full_state = "passed" if full.passed else "failed"
        if full.sandbox_error is not None:
            full_state = f"error: {full.sandbox_error}"
    body = (
        f"- Target: `{FAILING_TEST}`; k={k}\n"
        f"- Total wall time: **{fmt(run.wall_seconds)} s**\n"
        f"- K attempt durations (s): {attempt_durations}\n"
        f"- Attempts: {len(reproduction.attempts)}; failures: {reproduction.failures}\n"
        f"- Full-suite duration: **{full_text} s** ({full_state})\n"
        f"- `cost_minutes`: **{fmt(reproduction.cost_minutes)}**\n"
    )
    return body, run


def section_triage(run: ReproductionRun, *, prerequisite: bool) -> str:
    from janus.core.evidence import ChangeSummary
    from janus.core.signals import collect_all, default_collectors
    from janus.core.signals.base import SignalContext
    from janus.core.triage.routes import SIGNAL_NAMES
    from janus.core.triage.scorer import score

    reproduction = run.reproduction
    context = SignalContext(
        reproduction=reproduction,
        change=ChangeSummary(base_sha=None, head_sha="0" * 40),
        failing_targets=(FAILING_TEST,),
        flake_history={},
    )
    start = time.perf_counter()
    evidence = collect_all(default_collectors(), context)
    elapsed = time.perf_counter() - start
    decision = score(evidence)

    reported = [item.name for item in evidence]
    abstained = [name for name in SIGNAL_NAMES if name not in reported]
    score_text = ", ".join(
        f"{route.value}={fmt(value)}" for route, value in decision.scores.items()
    )
    note = (
        "\n- Note: the Reproduction was produced as a prerequisite because the `reproduce` "
        "section was not selected; that production is not included in collection time."
        if prerequisite
        else ""
    )
    return (
        f"- Collection time: **{fmt(elapsed, 6)} s**\n"
        f"- Reported signals ({len(reported)}): {', '.join(reported) or 'none'}\n"
        f"- Abstained signals ({len(abstained)}): {', '.join(abstained) or 'none'}\n"
        f"- Route: **{decision.route.value}**\n"
        f"- Confidence (top score): **{fmt(decision.confidence)}**\n"
        f"- Top-2 gap: **{fmt(decision.top2_gap)}**\n"
        f"- Scores: {score_text}\n"
        f"- This is a **single-case smoke measurement**, not an accuracy metric."
        f"{note}\n"
    )


def _is_calc_path(path: str) -> bool:
    return path.replace("\\", "/").endswith("src/calc.py")


def _contexts_from_coverage_json(raw: bytes) -> list[Any]:
    """Convert coverage.py 7.x's per-file ``contexts`` into adapter ``CoverageContext``s.

    This is a benchmark-side format shim, not a replacement for the adapter's parser: the
    real ``PythonAdapter.parse_coverage`` expects a top-level ``contexts`` mapping, while
    coverage.py writes ``files[<file>]["contexts"] = {<line>: [<context>, ...]}``.
    """
    import json

    from janus.core.adapter import CoverageContext

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return []
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        return []

    by_context: dict[str, dict[str, set[int]]] = {}
    for file, entry in files.items():
        if not isinstance(file, str) or not isinstance(entry, dict):
            continue
        contexts = entry.get("contexts")
        if not isinstance(contexts, dict):
            continue
        for line_text, names in contexts.items():
            if not isinstance(names, list):
                continue
            try:
                line = int(line_text)
            except (TypeError, ValueError):
                continue
            if line <= 0:
                continue
            for name in names:
                if not isinstance(name, str) or not name:
                    continue
                context_id = name.split("|", 1)[0]
                by_context.setdefault(context_id, {}).setdefault(file, set()).add(line)

    return [
        CoverageContext(test_id=test_id, files=files_for_test)
        for test_id, files_for_test in by_context.items()
    ]


def _coverage_report(image: FixtureImage, source_dir: Path) -> tuple[float, Any]:
    from janus.adapters.python.adapter import PythonAdapter
    from janus.sandbox.runner import SandboxRunner
    from janus.sandbox.spec import build_spec

    settings = image.prebuilt()
    adapter = PythonAdapter()
    command = adapter.coverage_command(())
    spec = build_spec(settings, command=command.argv, artifacts=command.artifacts)
    runner = SandboxRunner(settings=settings)
    start = time.perf_counter()
    result = runner.run(spec, source_dir=source_dir)
    return time.perf_counter() - start, result


def _sbfl_text(
    contexts: Sequence[Any],
    *,
    prefix: str,
) -> tuple[str, list[Any]]:
    from janus.core.localize.sbfl import localize

    failing_ids = {
        context.test_id
        for context in contexts
        if context.test_id == FAILING_TEST or context.test_id.endswith(FAILING_TEST)
    }
    if not failing_ids:
        return na(f"failing test {FAILING_TEST} did not appear in the coverage contexts"), []

    suspects = localize(contexts, failing_ids, max_suspects=1000)
    if not suspects:
        return na("localize returned no suspects"), []

    calc_lines = (FIXTURE / "src" / "calc.py").read_text(encoding="utf-8").splitlines()
    faulty_line = next(
        (
            index
            for index, line in enumerate(calc_lines, start=1)
            if line.lstrip().startswith("for ") and "range(1, n)" in line
        ),
        None,
    )
    faulty_rank: int | None = None
    if faulty_line is not None:
        faulty_rank = next(
            (
                suspect.rank
                for suspect in suspects
                if _is_calc_path(suspect.file) and suspect.line == faulty_line
            ),
            None,
        )

    top3 = "\n".join(
        f"  {suspect.rank}. `{suspect.file}:{suspect.line}` score={fmt(suspect.score, 4)}"
        for suspect in suspects[:3]
    )
    if faulty_line is None:
        rank_text = "the `range(1, n)` line was not found in `src/calc.py`"
    elif faulty_rank is None:
        rank_text = f"not present in the ranked suspects (known line `src/calc.py:{faulty_line}`)"
    else:
        rank_text = (
            f"**rank {faulty_rank}** (known line `src/calc.py:{faulty_line}`); "
            f"rank 1: {'yes' if faulty_rank == 1 else 'no'}"
        )

    return (
        f"{prefix}"
        f"- Coverage contexts: **{len(contexts)}**\n"
        f"- Failing test ids used: {', '.join(sorted(failing_ids))}\n"
        f"- Top-3 suspects:\n{top3}\n"
        f"- Known faulty location: {rank_text}\n"
    ), suspects


def section_sbfl(image: FixtureImage) -> str:
    from janus.adapters.python.adapter import PythonAdapter

    image.ensure()
    elapsed, result = _coverage_report(image, FIXTURE)
    if result.error is not None:
        return na(f"coverage sandbox run failed: {result.error}")

    adapter = PythonAdapter()
    real_contexts = adapter.parse_coverage(result)
    if real_contexts:
        text, _ = _sbfl_text(
            real_contexts, prefix=f"- Coverage command wall time: **{fmt(elapsed)} s**\n"
        )
        return text

    raw = result.artifacts.get(adapter.coverage_command(()).artifacts[0])
    real_reason = (
        "`PythonAdapter.parse_coverage` returned 0 contexts: coverage.py 7.16.2 writes "
        "per-file `contexts`, while the adapter expects a top-level `contexts` mapping"
    )
    if not raw:
        real_reason += f"; artifact errors: {result.artifact_errors}"

    temp_root = Path(tempfile.mkdtemp(prefix="janus-bench-sbfl-"))
    temp_repo = temp_root / "repo"
    try:
        shutil.copytree(
            FIXTURE,
            temp_repo,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
        )
        (temp_repo / ".coveragerc").write_text("[json]\nshow_contexts = true\n", encoding="utf-8")
        (temp_repo / "tests" / "test_env.py").unlink(missing_ok=True)
        supp_elapsed, supp_result = _coverage_report(image, temp_repo)
        if supp_result.error is not None:
            return na(f"{real_reason}; supplementary coverage run failed: {supp_result.error}")
        supp_raw = supp_result.artifacts.get(adapter.coverage_command(()).artifacts[0])
        if not isinstance(supp_raw, bytes):
            return na(f"{real_reason}; supplementary run produced no coverage artifact")
        contexts = _contexts_from_coverage_json(supp_raw)
        if not contexts:
            return na(f"{real_reason}; supplementary run produced no per-test contexts")

        prefix = (
            f"- Real `coverage_command(())` wall time: **{fmt(elapsed)} s**\n"
            f"- Real adapter context count: **0** — {real_reason}\n"
            f"- Supplementary localizer check (same command; temp copy with `.coveragerc` "
            f"`show_contexts = true` and the infra-only `tests/test_env.py` removed because "
            f"its `RuntimeError` suppressed whole-suite context recording; benchmark-side "
            f"format shim for coverage 7.x): wall time **{fmt(supp_elapsed)} s**\n"
        )
        text, _ = _sbfl_text(contexts, prefix=prefix)
        return text
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def section_llm() -> str:
    from janus.app.config import Settings
    from janus.core.generate.llm import build_llm

    client = build_llm(Settings())
    if not client.available:
        return na("no LLM endpoint configured (`build_llm(Settings())` returned `NullLLM`)")
    return (
        f"- LLM configured: **yes** (`{type(client).__name__}`)\n"
        f"- Latency: {na('not exercised by this benchmark')}\n"
    )


def section_caveats() -> str:
    return (
        "- **Triage accuracy on a labelled set**: not measured; there is no BugsInPy harness "
        "in this repository.\n"
        "- **Real GitHub API latency**: not measured.\n"
        "- **LLM latency/quality**: not measured.\n"
        "- **Multi-worker throughput under load**: not measured; the drain uses one worker.\n"
        "- **Crash/chaos behaviour**: not measured.\n"
        "- **PR-mode end-to-end**: not measured; it needs a GitHub App installation.\n"
    )


def _parse_only(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ALL_SECTIONS
    requested = tuple(part.strip() for part in value.split(",") if part.strip())
    unknown = [part for part in requested if part not in ALL_SECTIONS]
    if unknown:
        raise SystemExit(
            f"unknown section(s): {', '.join(unknown)}; choose from {', '.join(ALL_SECTIONS)}"
        )
    return requested


def _table(environment: dict[str, str]) -> str:
    rows = "\n".join(f"| {key} | {value} |" for key, value in environment.items())
    return f"| Field | Value |\n|---|---|\n{rows}\n"


async def async_main(args: argparse.Namespace) -> int:
    selected = _parse_only(args.only)
    mode = "fast" if args.fast else "full"
    ingest_n = FAST_INGEST_N if args.fast else FULL_INGEST_N
    trivial_n = FAST_TRIVIAL_N if args.fast else FULL_TRIVIAL_N
    k = FAST_K if args.fast else FULL_K

    progress(f"mode={mode}; sections={','.join(selected)}")
    environment = await collect_environment()

    sections: dict[str, str] = {}
    db: DatabaseBench | None = None
    db_error: str | None = None
    image: FixtureImage | None = None
    image_error: str | None = None
    reproduction: ReproductionRun | None = None

    def get_image() -> FixtureImage:
        nonlocal image, image_error
        if image is not None:
            return image
        if image_error is not None:
            raise RuntimeError(image_error)
        try:
            image = FixtureImage()
        except Exception as exc:  # noqa: BLE001 - Docker may be unavailable
            image_error = f"{type(exc).__name__}: {exc}"
            raise
        return image

    if {"db_ingest", "queue_drain"} & set(selected):
        progress("preparing postgres schema")
        try:
            db = DatabaseBench(os.environ.get("JANUS_BENCH_DATABASE_URL", DEFAULT_DB_URL))
            await db.connect()
            await db.reset_schema()
        except Exception as exc:  # noqa: BLE001 - a missing DB must degrade to n/a
            db_error = f"{type(exc).__name__}: {exc}"
            if db is not None:
                await db.dispose()
            db = None

    try:
        for name in ALL_SECTIONS:
            if name not in selected:
                continue
            progress(f"running {name}")
            try:
                if name == "code":
                    body = section_code()
                elif name == "static":
                    body = section_static()
                elif name == "tests":
                    body = section_tests()
                elif name == "db_ingest":
                    body = na(db_error) if db is None else await section_db_ingest(db, ingest_n)
                elif name == "queue_drain":
                    body = na(db_error) if db is None else await section_queue_drain(db, ingest_n)
                elif name == "sandbox_overhead":
                    body = section_sandbox_overhead(get_image(), trivial_n)
                elif name == "reproduce":
                    try:
                        body, reproduction = section_reproduce(get_image(), k)
                    except Exception as exc:  # noqa: BLE001 - section-level degradation
                        reproduction = None
                        body = na(f"{type(exc).__name__}: {exc}")
                elif name == "triage":
                    if reproduction is None:
                        try:
                            progress("triage prerequisite: producing a reproduction")
                            run = _produce_reproduction(get_image(), k)
                            body = section_triage(run, prerequisite=True)
                        except Exception as exc:  # noqa: BLE001 - section-level degradation
                            body = na(f"could not produce a Reproduction: {exc}")
                    else:
                        body = section_triage(reproduction, prerequisite=False)
                elif name == "sbfl":
                    body = section_sbfl(get_image())
                elif name == "llm":
                    body = section_llm()
                else:
                    body = section_caveats()
            except Exception as exc:  # noqa: BLE001 - one section must not abort the run
                body = na(f"{type(exc).__name__}: {exc}")
            sections[name] = body
    finally:
        if db is not None:
            try:
                progress("dropping and recreating postgres tables")
                await db.reset_schema()
            except Exception as exc:  # noqa: BLE001 - teardown is best effort
                progress(f"postgres teardown failed: {exc}")
            await db.dispose()
        if image is not None:
            try:
                image.cleanup()
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                progress(f"image cleanup failed: {exc}")

    header = (
        "# Janus benchmark results\n\n"
        f"- Generated: {environment.get('timestamp', 'unknown')}\n"
        f"- Mode: **{mode}**\n"
        f"- Sections run: {', '.join(selected)}\n"
        f"- Benchmark: `uv run python bench/run_bench.py"
        f"{' --fast' if args.fast else ''}"
        f"{' --only ' + args.only if args.only else ''}`\n\n"
        "## Environment\n\n"
        f"{_table(environment)}\n"
    )

    titles = {
        "code": "A. code — static size",
        "static": "B. static — static analysis wall time",
        "tests": "C. tests — suite wall time",
        "db_ingest": "D. db_ingest — transactional enqueue against Postgres",
        "queue_drain": "E. queue_drain — stub worker drain",
        "sandbox_overhead": "F. sandbox_overhead — generated image and container",
        "reproduce": "G. reproduce — real Reproducer on the fixture",
        "triage": "H. triage — collectors and scorer on one Reproduction",
        "sbfl": "I. sbfl — coverage and localization correctness",
        "llm": "J. llm — configured endpoint",
        "caveats": "K. caveats — what this benchmark does not measure",
    }

    document = [header]
    for name in ALL_SECTIONS:
        if name not in sections:
            continue
        document.append(f"## {titles[name]}\n\n{sections[name]}\n")

    markdown = "\n".join(document)
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(markdown, encoding="utf-8")
    print(markdown)
    progress(f"wrote {RESULTS_PATH.relative_to(ROOT)}")
    return 0


def main() -> int:
    os.chdir(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        default=None,
        help=f"comma-separated section names; choose from {', '.join(ALL_SECTIONS)}",
    )
    parser.add_argument("--fast", action="store_true", help="fewer iterations")
    args = parser.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
