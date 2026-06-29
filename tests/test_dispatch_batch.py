"""ToolRegistry.dispatch_batch: call-order, off-loop offload, on-loop inline."""

import threading

from pydantic import BaseModel

from openadventure.engine.tools.registry import Tool, ToolOutcome, ToolRegistry


class _Args(BaseModel):
    pass


def _record_thread(name: str):
    def handler(ctx, args) -> ToolOutcome:
        return ToolOutcome(content=name, summary=str(threading.get_ident()))

    return handler


async def test_dispatch_batch_preserves_order_and_thread_placement():
    registry = ToolRegistry()
    registry.register(Tool("inline_a", "", _Args, _record_thread("inline_a")))
    registry.register(Tool("par", "", _Args, _record_thread("par"), parallel_safe=True))
    registry.register(Tool("inline_b", "", _Args, _record_thread("inline_b")))

    loop_thread = str(threading.get_ident())
    calls = [("inline_a", {}), ("par", {}), ("inline_b", {})]
    outcomes = await registry.dispatch_batch(None, calls)

    # outcomes come back in call order regardless of the parallel/inline split
    assert [o.content for o in outcomes] == ["inline_a", "par", "inline_b"]
    # inline tools (those that touch rng / spawn background work) stay on the loop
    assert outcomes[0].summary == loop_thread
    assert outcomes[2].summary == loop_thread
    # the parallel_safe tool was offloaded to a worker thread
    assert outcomes[1].summary != loop_thread


async def test_dispatch_batch_runs_parallel_safe_tools_concurrently():
    # A 2-party barrier only clears if both handlers are in flight at once; under
    # serial dispatch the first would wait out the timeout and break the barrier.
    barrier = threading.Barrier(2, timeout=5)

    def gated(name: str):
        def handler(ctx, args) -> ToolOutcome:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                return ToolOutcome(content=name, summary="serialized", ok=False)
            return ToolOutcome(content=name, summary="concurrent")

        return handler

    registry = ToolRegistry()
    registry.register(Tool("search_rules", "", _Args, gated("search_rules"), parallel_safe=True))
    registry.register(
        Tool("search_campaign", "", _Args, gated("search_campaign"), parallel_safe=True)
    )

    outcomes = await registry.dispatch_batch(None, [("search_rules", {}), ("search_campaign", {})])
    assert [o.summary for o in outcomes] == ["concurrent", "concurrent"]
    assert [o.content for o in outcomes] == ["search_rules", "search_campaign"]
