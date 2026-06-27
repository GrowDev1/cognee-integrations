"""Unit tests for the session->graph bridge background+poll behavior
(_plugin_common._post_remember_document and persist_session_cache_to_graph_via_http).

Confirms the NGINX-safe contract:
  * the bridge POSTs run_in_background=true and parses the enqueue handle;
  * the SHA256 dedup digest is marked written ONLY when the graph is confirmed
    queryable (completed) or genuinely unpollable (unknown/no-id) — errored/timeout
    stay unmarked so the detached retry re-submits;
  * an already-synced document is not re-posted.

Run: python integrations/claude-code/tests/test_bridge_poll.py (or via pytest).
"""

import hashlib
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402


class _Resp:
    def __init__(self, body=b"{}", status=200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_post_remember_document_background_and_parses_ids():
    captured = {}
    orig = urllib.request.urlopen

    def _fake(req, timeout=None):
        captured["req"] = req
        return _Resp(b'{"status":"running","dataset_id":"d1","pipeline_run_id":"p1"}')

    urllib.request.urlopen = _fake
    try:
        res = pc._post_remember_document("http://x", "k", "ds", "doc", "user_context", 30.0)
    finally:
        urllib.request.urlopen = orig
    assert b'name="run_in_background"\r\n\r\ntrue' in captured["req"].data
    assert res == {"ok": True, "dataset_id": "d1", "pipeline_run_id": "p1"}


def _run_bridge(outcome, *, post_result=None, preseed_state=None):
    """Drive persist_session_cache_to_graph_via_http with the HTTP seams mocked.

    Returns (wrote, written_state, calls) where calls tracks post/wait invocations.
    """
    calls = {"post": 0, "wait": 0}
    written = {}
    saved = {
        k: getattr(pc, k)
        for k in (
            "_local_api_url",
            "_backend_reachable",
            "_api_key",
            "_format_cached_bridge_document",
            "_bridge_file",
            "_load_json_file",
            "_write_json_file",
            "_post_remember_document",
            "wait_for_cognify",
            "hook_log",
        )
    }

    def _post(*a, **k):
        calls["post"] += 1
        return post_result or {"ok": True, "dataset_id": "d1", "pipeline_run_id": "p1"}

    def _wait(*a, **k):
        calls["wait"] += 1
        return outcome

    pc._local_api_url = lambda: "http://x"
    pc._backend_reachable = lambda url: True
    pc._api_key = lambda: "k"
    pc._format_cached_bridge_document = lambda dataset, sid: ("qa text", "")
    pc._bridge_file = lambda sid: pathlib.Path("/tmp/_bridge_test.json")
    pc._load_json_file = lambda p: {"_state": dict(preseed_state)} if preseed_state else {}
    pc._write_json_file = lambda p, data: written.update(data)
    pc._post_remember_document = _post
    pc.wait_for_cognify = _wait
    pc.hook_log = lambda *a, **k: None
    try:
        wrote = pc.persist_session_cache_to_graph_via_http("ds", "sid")
    finally:
        for k, v in saved.items():
            setattr(pc, k, v)
    return wrote, written.get("_state", {}), calls


def test_dedup_marks_only_on_completed():
    wrote, state, calls = _run_bridge("completed")
    assert wrote is True
    assert len(state) == 1
    assert calls["wait"] == 1


def test_dedup_not_marked_on_errored():
    wrote, state, _ = _run_bridge("errored")
    assert wrote is False
    assert state == {}


def test_dedup_not_marked_on_timeout():
    wrote, state, _ = _run_bridge("timeout")
    assert wrote is False
    assert state == {}


def test_dedup_marked_on_unknown():
    wrote, state, _ = _run_bridge("unknown")
    assert wrote is True
    assert len(state) == 1


def test_no_dataset_id_marks_and_skips_poll():
    wrote, state, calls = _run_bridge(
        "completed", post_result={"ok": True, "dataset_id": "", "pipeline_run_id": ""}
    )
    assert wrote is True
    assert len(state) == 1
    assert calls["wait"] == 0  # nothing to poll without a dataset_id


def test_already_synced_skips_post():
    key = f"{pc._bridge_cache_key('ds', 'sid')}:qa"
    digest = hashlib.sha256("qa text".encode("utf-8")).hexdigest()
    wrote, state, calls = _run_bridge("completed", preseed_state={key: digest})
    assert calls["post"] == 0  # unchanged document is not re-posted
    assert wrote is False


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
