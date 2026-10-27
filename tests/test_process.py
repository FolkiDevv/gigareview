import asyncio
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


def test_timeout_kills_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    script = f"(sleep 1; touch {marker}) & sleep 30"
    outcome = asyncio.run(process.run_process(("bash", "-c", script), cwd=tmp_path, time_limit=0.3))
    assert outcome.timed_out
    assert outcome.exit_code is None
    asyncio.run(asyncio.sleep(1.2))
    assert not marker.exists()


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
    async def scenario() -> None:
        task = asyncio.create_task(
            process.run_process(("bash", "-c", "sleep 30"), cwd=tmp_path, time_limit=60),
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
