"""Run blocking report work behind a genuinely terminable process boundary."""
from __future__ import annotations

import importlib
import multiprocessing
import time
import traceback
from typing import Any


class IsolatedProcessTimeout(TimeoutError):
    def __init__(self, timeout: float):
        self.timeout = timeout
        super().__init__(f"isolated operation exceeded {timeout} seconds")


class IsolatedProcessError(RuntimeError):
    pass


def _invoke(send_connection, module_name: str, function_name: str, args, kwargs) -> None:
    try:
        function = getattr(importlib.import_module(module_name), function_name)
        send_connection.send((True, function(*args, **kwargs)))
    except BaseException as exc:
        send_connection.send((False, type(exc).__name__, str(exc), traceback.format_exc()))
    finally:
        send_connection.close()


def _stop_process(process: multiprocessing.Process) -> None:
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)
    if process.is_alive():
        process.kill()
        process.join(timeout=2)
    if process.is_alive():  # pragma: no cover - OS-level failure
        raise RuntimeError(f"unable to terminate isolated worker {process.pid}")


def run_in_spawned_process(
    module_name: str,
    function_name: str,
    *,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    timeout: float,
    cancellation_event=None,
) -> Any:
    """Run one importable function in a spawned child and kill it at deadline/cancel."""
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_invoke,
        args=(send_connection, module_name, function_name, args, kwargs or {}),
        daemon=False,
    )
    process.start()
    send_connection.close()
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                _stop_process(process)
                raise IsolatedProcessError("isolated operation cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                raise IsolatedProcessTimeout(timeout)
            if receive_connection.poll(min(remaining, 0.05)):
                try:
                    message = receive_connection.recv()
                except EOFError as exc:
                    process.join(timeout=2)
                    raise IsolatedProcessError(
                        f"isolated {module_name}.{function_name} exited with code "
                        f"{process.exitcode} without a result"
                    ) from exc
                process.join(timeout=2)
                if process.is_alive():
                    _stop_process(process)
                    raise IsolatedProcessError("isolated worker did not exit after returning")
                if message[0]:
                    return message[1]
                _, error_type, detail, child_traceback = message
                raise IsolatedProcessError(
                    f"isolated {module_name}.{function_name} failed: "
                    f"{error_type}: {detail}\n{child_traceback}"
                )
            if not process.is_alive():
                process.join()
                raise IsolatedProcessError(
                    f"isolated {module_name}.{function_name} exited with code "
                    f"{process.exitcode} without a result"
                )
    finally:
        receive_connection.close()
        _stop_process(process)
        process.close()
