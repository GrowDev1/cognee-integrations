"""The single cognee-facing surface for the Vellum integration.

Everything the Workflow nodes and the Agent Node tools do goes through here, and
it is built only on cognee's public ``remember()`` / ``recall()`` API — no
reimplemented ingestion or session handling.
"""

import asyncio
import concurrent.futures
import threading
from typing import Any, Optional

import cognee

from . import bootstrap  # noqa: F401  (loads .env on import)

DEFAULT_DATASET_NAME = "main_dataset"

# Vellum's node.run() / Agent Node tools are synchronous, but cognee is async.
# Run every cognee coroutine on one persistent background event loop — a fresh
# loop per call (asyncio.run) would strand cognee's loop-bound DB engine on a
# closed loop. Mirrors the strands / langgraph integrations.
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_lock = threading.Lock()

# cognee isn't safe to initialise concurrently, so serialise writes.
_write_lock = asyncio.Lock()


def _background_loop() -> asyncio.AbstractEventLoop:
    """Lazily start one daemon thread running a dedicated event loop for cognee."""
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()

            def _run() -> None:
                # Pin the loop as this thread's current loop so cognee / its DB
                # drivers resolve it via asyncio.get_event_loop() too.
                asyncio.set_event_loop(_loop)
                _loop.run_forever()

            threading.Thread(target=_run, daemon=True).start()
        return _loop


def run_sync(coro, timeout: float = 300):
    """Run an async cognee coroutine from synchronous Vellum code and block for
    the result. Raises ``TimeoutError`` if it does not finish within ``timeout``
    seconds, so a stalled call surfaces instead of hanging the workflow."""
    future = asyncio.run_coroutine_threadsafe(coro, _background_loop())
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        # concurrent.futures.TimeoutError is a distinct type from builtins on
        # Python 3.10; normalise so callers can always ``except TimeoutError``.
        raise TimeoutError(f"cognee call did not finish within {timeout}s") from exc


async def remember(
    data,
    *,
    dataset_name: str = DEFAULT_DATASET_NAME,
    user_id: Optional[str] = None,
):
    """Store data in cognee memory via the public ``remember()`` API.

    Synchronous: ``remember()`` blocks until the pipeline finishes, so the
    caller gets a real terminal status. Per-end-user scoping maps ``user_id``
    onto a cognee node set; one Vellum workflow deployment maps to one
    ``dataset_name`` by default.
    """
    kwargs: dict[str, Any] = {"dataset_name": dataset_name}
    if user_id:
        kwargs["node_set"] = [user_id]

    async with _write_lock:
        return await cognee.remember(data, **kwargs)


async def recall(
    query_text: str,
    *,
    dataset_name: Optional[str] = DEFAULT_DATASET_NAME,
    user_id: Optional[str] = None,
    top_k: int = 15,
    include_references: bool = True,
):
    """Retrieve from cognee memory via the public ``recall()`` API.

    ``include_references`` is on by default so recall results carry the source
    lineage (which dataset/document/chunk each hit came from) that the nodes
    expose as typed ``citations``.
    """
    kwargs: dict[str, Any] = {
        "top_k": top_k,
        "include_references": include_references,
    }
    if dataset_name:
        kwargs["datasets"] = [dataset_name]
    if user_id:
        kwargs["node_name"] = [user_id]

    return await cognee.recall(query_text, **kwargs)


def extract_answer_and_citations(responses):
    """Flatten cognee ``recall()`` responses into a renderable answer plus typed
    citations (which dataset/document/chunk each hit came from).

    ``recall()`` returns a discriminated union of entry types, and the answer
    text lives in a different field per type: ``text`` on graph hits
    (SearchResultItem), ``content`` on graph/session context entries, ``answer``
    on session QA entries, and ``memory_context`` on agent-trace entries. We
    read all of them so no entry type is silently dropped. ``dataset_name`` /
    ``dataset_id`` / ``metadata`` / ``qa_id`` carry the source lineage.
    """
    answer_parts = []
    citations = []

    for r in responses:
        text = (
            getattr(r, "content", None)
            or getattr(r, "text", None)
            or getattr(r, "answer", None)
            or getattr(r, "memory_context", None)
        )
        if text:
            answer_parts.append(text)

        citation: dict[str, Any] = {"source": getattr(r, "source", None)}
        for field in ("dataset_name", "dataset_id", "score", "qa_id"):
            value = getattr(r, field, None)
            if value is not None:
                citation[field] = value
        metadata = getattr(r, "metadata", None)
        if metadata:
            # chunk_id / doc_id live here
            citation["metadata"] = metadata
        citations.append(citation)

    return "\n\n".join(answer_parts), citations
