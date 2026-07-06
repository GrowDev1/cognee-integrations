"""Unit tests for the warmup entry buffer
(_plugin_common.append_warmup_entry and drain_warmup_entries).

Entries captured while the local server is still warming must be buffered as
structured /remember/entry payloads and replayed IN ORDER once the server is
ready, so the server-side session cache (which improve() bridges from) holds
the complete session. A replay failure keeps the unreplayed tail buffered.

Run: python integrations/claude-code/tests/test_warmup_drain.py (or via pytest).
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402


def _with_tmp_bridge(fn):
    """Run fn() with the bridge file pointed at a temp path; return its result."""
    saved = {k: getattr(pc, k) for k in ("_bridge_file", "hook_log")}
    with tempfile.TemporaryDirectory() as tmp:
        bridge = pathlib.Path(tmp) / "bridge_test.json"
        pc._bridge_file = lambda sid="": bridge
        pc.hook_log = lambda *a, **k: None
        try:
            return fn()
        finally:
            for k, v in saved.items():
                setattr(pc, k, v)


def test_append_and_drain_in_order():
    def _run():
        replayed = []
        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "Bash"})
        pc.append_warmup_entry("ds", "sid", {"type": "qa", "question": "q", "answer": "a"})
        saved = pc.remember_entry_via_http
        pc.remember_entry_via_http = lambda d, s, entry, **k: replayed.append(entry) or {}
        try:
            drained = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved
        return drained, replayed

    drained, replayed = _with_tmp_bridge(_run)
    assert drained == 2
    assert [e["type"] for e in replayed] == ["trace", "qa"]


def test_drain_empty_buffer_is_noop():
    def _run():
        calls = []
        saved = pc.remember_entry_via_http
        pc.remember_entry_via_http = lambda *a, **k: calls.append(a) or {}
        try:
            drained = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved
        return drained, calls

    drained, calls = _with_tmp_bridge(_run)
    assert drained == 0
    assert calls == []


def test_partial_failure_keeps_tail_buffered():
    def _run():
        replayed = []

        def _flaky(dataset, session_id, entry, **k):
            if len(replayed) >= 1:
                raise OSError("server went away")
            replayed.append(entry)
            return {}

        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "Read"})
        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "Edit"})
        pc.append_warmup_entry("ds", "sid", {"type": "qa", "question": "q", "answer": "a"})
        saved = pc.remember_entry_via_http
        pc.remember_entry_via_http = _flaky
        try:
            first = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved

        # Second drain replays the surviving tail, in order.
        pc.remember_entry_via_http = lambda d, s, entry, **k: replayed.append(entry) or {}
        try:
            second = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved
        return first, second, replayed

    first, second, replayed = _with_tmp_bridge(_run)
    assert first == 1
    assert second == 2
    assert [e.get("origin_function") or e["type"] for e in replayed] == ["Read", "Edit", "qa"]


def test_drain_leaves_legacy_shadow_untouched():
    # The qa/trace text mirrors (legacy document-bridge data) must survive a drain.
    def _run():
        pc.append_http_bridge_entry("ds", "sid", trace="Bash [success]")
        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "Bash"})
        saved = pc.remember_entry_via_http
        pc.remember_entry_via_http = lambda *a, **k: {}
        try:
            pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved
        cache = pc._load_json_file(pc._bridge_file("sid"))
        return cache.get(pc._bridge_cache_key("ds", "sid"), {})

    session_cache = _with_tmp_bridge(_run)
    assert session_cache.get("trace") == ["Bash [success]"]
    assert session_cache.get("pending_entries") == []


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
