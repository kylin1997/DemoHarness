"""Await blocking workspace operations without moving Task state off the loop."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

_Result = TypeVar("_Result")
_POOL = ThreadPoolExecutor(thread_name_prefix="demoharness-io")


async def run_io(
    operation: Callable[..., _Result], /, *args: object, record_result_on_cancel: bool = False,
) -> _Result:
    """Keep in-flight I/O inside its Task's lifetime, even during cancellation."""
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    reader, writer = os.pipe()
    try:
        # A pipe also wakes selectors in sandboxes that restrict asyncio's socketpair.
        loop.add_reader(reader, ready.set)
        work = _POOL.submit(operation, *args)
        work.add_done_callback(lambda _: os.write(writer, b"\0"))
        try:
            await ready.wait()
        except asyncio.CancelledError:
            work.cancel()
            while not ready.is_set():
                try:
                    await ready.wait()
                except asyncio.CancelledError:
                    continue
            if not record_result_on_cancel or work.cancelled():
                raise
        return work.result()
    finally:
        loop.remove_reader(reader)
        os.close(reader)
        os.close(writer)
