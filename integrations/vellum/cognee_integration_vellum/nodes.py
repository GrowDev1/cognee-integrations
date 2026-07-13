"""Vellum Workflows SDK nodes backed by cognee memory.

Pushed to Vellum with ``vellum workflows push``, these appear as named node
blocks in the visual editor (docstring included).
"""

from vellum.workflows.nodes import BaseNode

from . import client


class CogneeRememberNode(BaseNode):
    """Store data in cognee memory so later workflow executions can recall it.

    Synchronous: the node blocks until cognee finishes building the graph, so
    ``status`` / ``error`` are real the moment the node completes and downstream
    nodes can branch on them.
    """

    data: str = ""
    dataset_name: str = client.DEFAULT_DATASET_NAME
    user_id: str = ""

    class Outputs(BaseNode.Outputs):
        status: str
        pipeline_run_id: str
        error: str
        dataset_name: str

    def run(self) -> "CogneeRememberNode.Outputs":
        result = client.run_sync(
            client.remember(
                self.data,
                dataset_name=self.dataset_name,
                user_id=self.user_id or None,
            )
        )
        return self.Outputs(
            status=getattr(result, "status", "") or "",
            pipeline_run_id=getattr(result, "pipeline_run_id", "") or "",
            error=getattr(result, "error", "") or "",
            dataset_name=getattr(result, "dataset_name", self.dataset_name),
        )


class CogneeRecallNode(BaseNode):
    """Answer from cognee memory, with citations to the source data.

    Surfaces the retrieved ``answer`` plus typed ``citations`` (which
    dataset/document/chunk each result came from) as node outputs, so a
    downstream node can render "answered from ...".
    """

    query: str = ""
    dataset_name: str = client.DEFAULT_DATASET_NAME
    user_id: str = ""
    top_k: int = 15

    class Outputs(BaseNode.Outputs):
        answer: str
        citations: list

    def run(self) -> "CogneeRecallNode.Outputs":
        responses = client.run_sync(
            client.recall(
                self.query,
                dataset_name=self.dataset_name,
                user_id=self.user_id or None,
                top_k=self.top_k,
                include_references=True,
            )
        )
        answer, citations = client.extract_answer_and_citations(responses)
        return self.Outputs(answer=answer, citations=citations)
