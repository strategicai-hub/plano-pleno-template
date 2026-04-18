"""
Verifica que o buffer de log por sessao nao vaza entre chamadas concorrentes
(regressao do bug do `_session_log` global).
"""
import asyncio
import pytest

from app import consumer


@pytest.mark.asyncio
async def test_session_logs_do_not_leak_across_tasks():
    """
    Duas coroutines concorrentes logam simultaneamente e cada uma deve ter
    apenas suas proprias linhas no buffer.
    """
    async def work(tag: str) -> list[str]:
        consumer._begin_session_log()
        for i in range(5):
            consumer.log(f"{tag}-{i}")
            await asyncio.sleep(0)
        return list(consumer._session_log_var.get())

    a, b = await asyncio.gather(work("A"), work("B"))

    assert all(line.startswith("A-") for line in a)
    assert all(line.startswith("B-") for line in b)
    assert len(a) == 5
    assert len(b) == 5


@pytest.mark.asyncio
async def test_child_task_inherits_parent_buffer():
    """create_task sem isolamento herda o buffer do pai."""
    consumer._begin_session_log()
    consumer.log("parent-line")

    async def child():
        consumer.log("child-line")

    await asyncio.create_task(child())
    buf = consumer._session_log_var.get()
    assert "parent-line" in buf
    assert "child-line" in buf


@pytest.mark.asyncio
async def test_begin_session_inside_child_isolates_buffer():
    """Se o filho chama _begin_session_log, ele tem seu proprio buffer."""
    consumer._begin_session_log()
    consumer.log("parent-line")

    async def child():
        consumer._begin_session_log()
        consumer.log("child-line")
        return list(consumer._session_log_var.get())

    child_buf = await asyncio.create_task(child())
    parent_buf = consumer._session_log_var.get()

    assert child_buf == ["child-line"]
    assert parent_buf == ["parent-line"]
