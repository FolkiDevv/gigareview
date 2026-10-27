"""Запуск дочерних процессов: таймаут, убийство всей группы процессов, вывод одной строкой."""

import asyncio
import os
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["MAX_OUTPUT_CHARS", "ProcessOutcome", "run_process", "subprocess_env"]

#: Сколько символов вывода сохраняется: остальное обрезается с пометкой.
MAX_OUTPUT_CHARS = 2_000_000

_TRUNCATED = "\n… (вывод обрезан)"


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """Итог процесса: код завершения (``None`` — убит по таймауту), вывод и длительность."""

    exit_code: int | None
    output: str
    stderr: str
    duration: float

    @property
    def timed_out(self) -> bool:
        """Процесс не уложился в таймаут и был убит."""
        return self.exit_code is None


def subprocess_env(
    extra: Mapping[str, str],
    *,
    hidden: Sequence[str] = (),
) -> dict[str, str]:
    """Окружение дочернего процесса: текущее без переменных ``hidden`` плюс ``extra``."""
    env = {name: text for name, text in os.environ.items() if name not in hidden}
    env.update(extra)
    return env


# У запуска процесса много независимых настроек, все — именованные.
async def run_process(  # noqa: WPS211
    argv: Sequence[str],
    *,
    cwd: Path,
    time_limit: float,
    env: Mapping[str, str] | None = None,
    stdin_text: str = "",
    merge_stderr: bool = True,
) -> ProcessOutcome:
    """Запускает ``argv`` в ``cwd``; по таймауту убивает группу процессов целиком.

    ``merge_stderr`` — stderr идёт в общий вывод (в порядке появления), иначе отдельно.
    """
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_text.encode()),
            timeout=time_limit,
        )
    except TimeoutError:
        await _kill(proc)
        return ProcessOutcome(None, "", "", time.monotonic() - started)
    except asyncio.CancelledError:
        await _kill(proc)
        raise
    return ProcessOutcome(
        exit_code=proc.returncode,
        output=_decode(stdout),
        stderr=_decode(stderr),
        duration=time.monotonic() - started,
    )


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Убивает группу процессов ``proc`` (сценарий мог породить своих детей) и ждёт её."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await proc.wait()


def _decode(raw: bytes | None) -> str:
    """Байты вывода в текст: битые символы заменяются, длинный вывод обрезается."""
    if not raw:
        return ""
    text = raw.decode(errors="replace")
    if len(text) > MAX_OUTPUT_CHARS:
        kept = text[:MAX_OUTPUT_CHARS]
        return f"{kept}{_TRUNCATED}"
    return text
