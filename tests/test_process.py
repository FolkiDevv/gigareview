import asyncio
import os
import time
from pathlib import Path

import pytest

from gigareview import process


def _run(
    *argv: str,
    cwd: Path,
    stdin_text: str = "",
    merge_stderr: bool = True,
) -> process.ProcessOutcome:
    return asyncio.run(
        process.run_process(
            argv,
            cwd=cwd,
            time_limit=10,
            stdin_text=stdin_text,
            merge_stderr=merge_stderr,
        ),
    )


def test_exit_code_and_merged_output(tmp_path: Path) -> None:
    outcome = _run("bash", "-c", "echo out; echo err >&2; exit 3", cwd=tmp_path)
    assert outcome.exit_code == 3
    assert "out" in outcome.output
    assert "err" in outcome.output
    assert not outcome.timed_out


def test_stdin_and_separate_stderr(tmp_path: Path) -> None:
    outcome = _run(
        "bash",
        "-c",
        "cat; echo oops >&2",
        cwd=tmp_path,
        stdin_text="hello",
        merge_stderr=False,
    )
    assert outcome.output == "hello"
    assert outcome.stderr == "oops\n"


def _assert_gone(pid_file: Path) -> None:
    """Процесс из ``pid_file`` завершён (ядро снимает его не мгновенно — ждём до 5 с)."""
    pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"процесс {pid} пережил остановку")


def test_timeout_kills_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = f"sleep 30 & echo $! > {pid_file}; wait"
    outcome = asyncio.run(process.run_process(("bash", "-c", script), cwd=tmp_path, time_limit=0.5))
    assert outcome.timed_out
    assert outcome.exit_code is None
    _assert_gone(pid_file)


def test_endless_output_keeps_memory_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "MAX_OUTPUT_CHARS", 5)
    outcome = _run("bash", "-c", "yes | head -c 50000000", cwd=tmp_path)
    assert outcome.exit_code == 0
    assert outcome.output == "y\ny\ny\n… (вывод обрезан)"


def test_process_ignoring_stdin(tmp_path: Path) -> None:
    outcome = _run("bash", "-c", "exec 0<&-; echo done", cwd=tmp_path, stdin_text="x" * 1_000_000)
    assert outcome.output == "done\n"


def test_long_output_is_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "MAX_OUTPUT_CHARS", 5)
    outcome = _run("bash", "-c", "echo 1234567890", cwd=tmp_path)
    assert outcome.output.startswith("12345\n")
    assert "обрезан" in outcome.output


def test_subprocess_env_hides_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GR_SECRET_UNDER_TEST", "x")
    env = process.subprocess_env({"EXTRA": "1"}, hidden=("GR_SECRET_UNDER_TEST",))
    assert "GR_SECRET_UNDER_TEST" not in env
    assert env["EXTRA"] == "1"


def test_cancel_kills_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = f"sleep 30 & echo $! > {pid_file}; wait"

    async def scenario() -> None:
        task = asyncio.create_task(
            process.run_process(("bash", "-c", script), cwd=tmp_path, time_limit=60),
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    _assert_gone(pid_file)
