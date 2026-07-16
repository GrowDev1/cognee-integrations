"""Sessionized Cognee memory tools for the Aider CLI.

Each session (typically one software project) maps to its own Cognee dataset
and node set, so memories from different projects stay isolated on both write
and read. cognee's ``datasets`` filter alone does not isolate graph-completion
retrieval (the graph is a shared store), so writes are tagged with a per-session
``node_set`` and reads are scoped with the matching ``node_name`` — the
combination is what actually isolates one project's recall from another's.
"""

import functools
import hashlib
from typing import Callable

import cognee
from cognee.modules.data.exceptions import DatasetNotFoundError


def session_dataset(session: str) -> str:
    """Return the per-session Cognee dataset / node-set name, isolating a project.

    The name keeps a readable, sanitized slug of the session id and appends a
    short hash of the raw id, so two distinct sessions never collapse onto the
    same identifier (character-stripping alone is lossy — ``a/b`` and ``ab``
    would otherwise clash).
    """
    slug = "".join(c for c in session if c.isalnum() or c in ("_", "-"))
    digest = hashlib.sha1(session.encode("utf-8")).hexdigest()[:8]
    return f"aider_session_{slug}_{digest}"


async def add_project_memory(session: str, content: str) -> str:
    """Store ``content`` in the session's memory and build its knowledge graph.

    ``cognee.add`` only ingests raw data; ``cognee.cognify`` is what turns it
    into the graph that search reads from, so both are required for recall. The
    ``node_set`` tag is what scopes recall back to this session.
    """
    dataset = session_dataset(session)
    await cognee.add(content, dataset_name=dataset, node_set=[dataset])
    await cognee.cognify(datasets=[dataset])
    return f"Memory added to session '{session}'."


async def search_project_memory(session: str, query: str) -> str:
    """Retrieve memories relevant to ``query``, scoped to this session only."""
    dataset = session_dataset(session)
    try:
        results = await cognee.search(query, datasets=[dataset], node_name=[dataset])
    except DatasetNotFoundError:
        # Nothing has been stored in this session yet — recall before the first add.
        return "No memories found."
    if not results:
        return "No memories found."
    return "\n".join(str(r) for r in results)


def get_sessionized_cognee_tools(session: str) -> tuple[Callable, Callable]:
    """Return ``(add, search)`` bound to ``session`` for isolated project memory.

    Mirrors the CrewAI integration's sessionized-tools factory: the returned
    callables take only the memory payload, so an Aider Python script can wire
    them in without threading the session id through every call.
    """
    return (
        functools.partial(add_project_memory, session),
        functools.partial(search_project_memory, session),
    )
