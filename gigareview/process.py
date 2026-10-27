"""Запуск дочерних процессов: таймаут, убийство всей группы процессов, вывод одной строкой."""

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

__all__ = ["MAX_OUTPUT_CHARS", "ProcessOutcome", "run_process", "subprocess_env"]

#: Сколько символов вывода сохраняется: остальное обрезается с пометкой.
MAX_OUTPUT_CHARS = 2_000_000

_TRUNCATED = "\n… (вывод обрезан)"

# UTF-8 — до 4 байт на символ; поток читается кусками по 64 КБ.
_BYTES_PER_CHAR = 4
_CHUNK_BYTES = 65_536


class _Read(NamedTuple):
    """Прочитанный поток: сохранённые байты и признак, что лишнее отброшено."""

    raw: bytes
    overflow: bool


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
            _communicate(proc, stdin_text.encode()),
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


async def _communicate(proc: asyncio.subprocess.Process, payload: bytes) -> tuple[_Read, _Read]:
    """Как ``communicate``, но память под вывод ограничена (см. :func:`_drain`)."""
    _, stdout, stderr = await asyncio.gather(
        _feed(proc.stdin, payload),
        _drain(proc.stdout),
        _drain(proc.stderr),
    )
    await proc.wait()
    return stdout, stderr


async def _feed(stream: asyncio.StreamWriter | None, payload: bytes) -> None:
    """Пишет ``payload`` в stdin и закрывает его; процессу, не читающему stdin, не мешает."""
    if stream is None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        stream.write(payload)
        await stream.drain()
        stream.close()


async def _drain(stream: asyncio.StreamReader | None) -> _Read:
    """Читает поток до конца, храня не больше ``4 × MAX_OUTPUT_CHARS`` байт.

    Остальное отбрасывается: бесконечный вывод (``yes``, зациклившийся лог) не съест память
    демона до таймаута. Возвращает сохранённые байты и признак, что вывод был длиннее.
    """
    if stream is None:
        return _Read(b"", overflow=False)
    limit = MAX_OUTPUT_CHARS * _BYTES_PER_CHAR
    kept = bytearray()
    overflow = False
    while chunk := await stream.read(_CHUNK_BYTES):
        room = limit - len(kept)
        overflow = overflow or len(chunk) > room
        kept.extend(chunk[: max(room, 0)])
    return _Read(bytes(kept), overflow=overflow)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Убивает группу процессов ``proc`` (сценарий мог породить своих детей) и ждёт её."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await proc.wait()


def _decode(read: _Read) -> str:
    """Байты вывода в текст: битые символы заменяются, длинный вывод обрезается."""
    if not read.raw:
        return ""
    text = read.raw.decode(errors="replace")
    if read.overflow or len(text) > MAX_OUTPUT_CHARS:
        kept = text[:MAX_OUTPUT_CHARS]
        return f"{kept}{_TRUNCATED}"
    return text
