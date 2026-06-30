#!/usr/bin/env python3
"""
Codex Proxy - Bridges OpenAI Responses API <-> any Chat Completions API

Supports: DeepSeek, Zhipu GLM, and any OpenAI-compatible provider.

Usage:
  python codex_proxy.py --upstream https://api.deepseek.com [--port 9090]
  python codex_proxy.py --upstream https://open.bigmodel.cn/api/paas/v4 [--port 9090]

Config in ~/.codex/config.toml:
  [model_providers.custom]
  base_url = "http://localhost:9090/v1"
  wire_api = "responses"
  env_key = "DEEPSEEK_API_KEY"
"""

import os
import json
import time
import uuid
import logging
import hashlib
import argparse
import traceback
from dataclasses import dataclass, field

from flask import Flask, request, Response, jsonify, stream_with_context, current_app
import requests as http_requests


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

log = logging.getLogger("proxy")


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

@dataclass
class ProxyConfig:
    upstream_base: str = "https://integrate.api.nvidia.com/v1"
    timeout: int = 120
    host: str = "127.0.0.1"
    port: int = 9090
    default_model: str = "glm-5.1"          # used when request body omits "model"
    fallback_model: str = "deepseek-v4-pro"  # used when an unknown gpt-* is requested
    model_map: dict = field(default_factory=lambda: {
        "gpt-5.4": "deepseek-v4-pro",
        "gpt-5.4-mini": "deepseek-v4-flash",
        "gpt-4o": "deepseek-v4-pro",
        "gpt-4o-mini": "deepseek-v4-flash",
    })
    reasoning_store_path: str = os.path.join(BASE_DIR, "reasoning_store.json")
    log_path: str = os.path.join(BASE_DIR, "proxy.log")

    def resolve_model(self, requested):
        m = self.model_map.get(requested, requested)
        return self.fallback_model if m.startswith("gpt-") else m


def _cfg() -> ProxyConfig:
    return current_app.config["CFG"]


def _store() -> "ReasoningStore":
    return current_app.config["STORE"]


# ------------------------------------------------------------------
# Small utilities
# ------------------------------------------------------------------

def _rid(prefix="resp"):
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ------------------------------------------------------------------
# Reasoning content store (persist to disk, survive proxy restarts)
# ------------------------------------------------------------------

class ReasoningStore:
    """Persists assistant reasoning_content keyed by content hash and tool_call ids,
    so it can be restored on follow-up turns (DeepSeek thinking mode continuity)."""

    def __init__(self, path):
        self.path = path
        self._data = {}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {}

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False)

    @staticmethod
    def _content_hash(text):
        return hashlib.md5(text.encode()).hexdigest()

    def store(self, text, reasoning, tool_call_ids=None):
        """Save reasoning_content keyed by hash of assistant text and tool_call_ids."""
        if not reasoning:
            return
        h = self._content_hash(text)
        self._data[h] = reasoning
        if tool_call_ids:
            for tc_id in tool_call_ids:
                self._data[f"tc_{tc_id}"] = reasoning
        self.save()
        log.info("STORED reasoning for hash=%s, rc_len=%d, tc_ids=%s",
                 h[:12], len(reasoning), tool_call_ids or [])

    def lookup(self, text):
        """Look up stored reasoning_content by hash of assistant text."""
        rc = self._data.get(self._content_hash(text), "")
        if rc:
            log.info("FOUND reasoning for hash=%s, rc_len=%d",
                     self._content_hash(text)[:12], len(rc))
        return rc

    def lookup_by_tc(self, tc_id):
        return self._data.get(f"tc_{tc_id}", "")


# ------------------------------------------------------------------
# Request: Responses API -> Chat Completions
# ------------------------------------------------------------------

def _extract_text_parts(content):
    """Flatten a Responses-style content list into plain text, skipping non-text parts."""
    parts = []
    for p in content:
        if isinstance(p, str):
            parts.append(p)
        elif isinstance(p, dict) and p.get("type") in ("input_text", "text", "output_text"):
            parts.append(p.get("text", ""))
        # Skip non-text types like input_image (DeepSeek doesn't support them)
    return "\n".join(parts)


def _convert_input(body, store):
    messages = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})

    inp = body.get("input", [])
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
        return messages
    if not isinstance(inp, list):
        return messages

    pending_tc = []
    pending_assistant = None  # Hold assistant msg; may merge with subsequent function_calls

    def _emit_pending():
        nonlocal pending_assistant
        if pending_assistant:
            messages.append(pending_assistant)
            pending_assistant = None

    def _emit_tc():
        nonlocal pending_tc
        if pending_tc:
            # Standalone tool_calls (no preceding assistant text) — look up reasoning by tc_id
            rc = ""
            for tc in pending_tc:
                rc = store.lookup_by_tc(tc["id"])
                if rc:
                    break
            msg = {"role": "assistant", "content": None, "tool_calls": list(pending_tc)}
            if rc:
                msg["reasoning_content"] = rc
            messages.append(msg)
            pending_tc = []

    for item in inp:
        if isinstance(item, str):
            _emit_pending()
            _emit_tc()
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        t = item.get("type", "")

        if t == "function_call":
            pending_tc.append({
                "id": item.get("call_id", _rid("call")),
                "type": "function",
                "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "{}")},
            })
        elif t == "function_call_output":
            # Merge pending_assistant + pending_tc into ONE assistant message
            if pending_assistant:
                if pending_tc:
                    pending_assistant["tool_calls"] = list(pending_tc)
                    pending_tc = []
                messages.append(pending_assistant)
                pending_assistant = None
            else:
                _emit_tc()
            tool_output = item.get("output", "")
            if isinstance(tool_output, list):
                tool_output = _extract_text_parts(tool_output)
            messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""), "content": tool_output})
        else:
            _emit_pending()
            _emit_tc()
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            content = item.get("content", "")
            if isinstance(content, list):
                content = _extract_text_parts(content)
            msg = {}
            if role:
                msg["role"] = role
            if content is not None:
                msg["content"] = content
            # For assistant messages, restore reasoning_content from our local store
            if role == "assistant":
                stored_rc = store.lookup(content or "")
                if stored_rc:
                    msg["reasoning_content"] = stored_rc
                pending_assistant = msg  # Hold — may merge with next function_calls
                continue
            if msg:
                messages.append(msg)

    _emit_pending()
    _emit_tc()
    return messages


def _convert_tools(tools):
    if not tools:
        return None
    out = []
    for tool in tools:
        if tool.get("type") == "function":
            func = {"name": tool.get("name", "")}
            if tool.get("description"):
                func["description"] = tool["description"]
            if tool.get("parameters"):
                func["parameters"] = tool["parameters"]
            out.append({"type": "function", "function": func})
    return out or None


def _build_cc_payload(body, cfg, store):
    """Build the Chat Completions request payload from a Responses API body."""
    model = cfg.resolve_model(body.get("model", cfg.default_model))
    cc = {
        "model": model,
        "messages": _convert_input(body, store),
        "stream": body.get("stream", False),
    }
    cc_tools = _convert_tools(body.get("tools"))
    if cc_tools:
        cc["tools"] = cc_tools
    if body.get("tool_choice"):
        cc["tool_choice"] = body["tool_choice"]
    for k in ("temperature", "top_p", "max_tokens", "max_completion_tokens"):
        if k in body:
            cc[k] = body[k]
    return cc, model


# ------------------------------------------------------------------
# SSE event builders (pure functions returning event dicts)
# ------------------------------------------------------------------

def _ev_response_created(resp_id, created, model):
    return {
        "type": "response.created",
        "response": {"id": resp_id, "object": "response", "created_at": created,
                     "model": model, "status": "in_progress", "output": [], "metadata": {}},
    }


def _ev_output_item_added_message(msg_id):
    return {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {"type": "message", "id": msg_id, "status": "in_progress",
                 "role": "assistant", "content": []},
    }


def _ev_content_part_added():
    return {
        "type": "response.content_part.added",
        "output_index": 0, "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    }


def _ev_output_text_delta(delta):
    return {
        "type": "response.output_text.delta",
        "output_index": 0, "content_index": 0, "delta": delta,
    }


def _ev_output_text_done(full_text):
    return {
        "type": "response.output_text.done",
        "output_index": 0, "content_index": 0, "text": full_text,
    }


def _ev_message_item_done(msg_id, full_text):
    return {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {"type": "message", "id": msg_id, "status": "completed",
                 "role": "assistant",
                 "content": [{"type": "output_text", "text": full_text, "annotations": []}]},
    }


def _function_call_item(fc_id, tc, status="completed"):
    return {"type": "function_call", "id": fc_id, "call_id": tc["id"],
            "name": tc["name"], "arguments": tc["arguments"], "status": status}


def _ev_function_call_item(event_type, output_index, fc_id, tc):
    return {"type": event_type, "output_index": output_index, "item": _function_call_item(fc_id, tc)}


def _message_output_item(msg_id, full_text):
    return {"type": "message", "id": msg_id, "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": full_text, "annotations": []}]}


def _map_usage(usage):
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def _ev_response_completed(resp_id, created, model, final_output, usage):
    return {
        "type": "response.completed",
        "response": {
            "id": resp_id, "object": "response", "created_at": created,
            "model": model, "status": "completed", "output": final_output,
            "parallel_tool_calls": True,
            "usage": _map_usage(usage),
            "metadata": {},
        },
    }


# ------------------------------------------------------------------
# Upstream error handling (shared by streaming & non-streaming paths)
# ------------------------------------------------------------------

def _upstream_connect_error(e):
    log.error("UPSTREAM CONNECT: %s", e)
    return jsonify({"error": {"message": str(e), "type": "upstream_error"}}), 502


def _build_upstream_error(r):
    """Return (err_body, status_code, passthrough_headers) for an upstream >= 400 response.

    Propagates the real status code and rate-limit headers (Retry-After etc.) so the
    client can back off and retry properly instead of looping on a 200 + inline error.
    """
    body_text = r.text[:1000]
    log.error("UPSTREAM %d: %s", r.status_code, body_text)
    passthrough_headers = {}
    for h in ("Retry-After", "X-RateLimit-Reset", "X-RateLimit-Remaining"):
        if h in r.headers:
            passthrough_headers[h] = r.headers[h]
    try:
        err_body = r.json()
    except Exception:
        err_body = {"error": {"message": body_text or f"upstream returned {r.status_code}",
                              "type": "upstream_error"}}
    return err_body, r.status_code, passthrough_headers


# ------------------------------------------------------------------
# Streaming response
# ------------------------------------------------------------------

@dataclass
class StreamState:
    resp_id: str
    msg_id: str
    created: int
    model: str
    full_text: str = ""
    full_reasoning: str = ""
    tool_calls_acc: dict = field(default_factory=dict)  # idx -> {id, name, arguments}
    started: bool = False  # whether response.created has already been emitted
    final_usage: dict = field(default_factory=dict)

    def tool_call_ids(self):
        return [self.tool_calls_acc[i]["id"] for i in sorted(self.tool_calls_acc)] or None


@dataclass
class ChunkResult:
    finish_reason: str = None
    content_delta: str = None
    has_tool_calls: bool = False


def _iter_upstream_chunks(r):
    """Yield parsed SSE chunk dicts from the upstream response.

    Uses byte-level iteration + manual utf-8 decode so multi-byte characters split
    across chunks don't raise; stops at [DONE]."""
    for line_bytes in r.iter_lines(decode_unicode=False):
        if not line_bytes:
            continue
        try:
            line = line_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not line or not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            log.info("UPSTREAM DONE")
            return
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def _parse_chunk_into_state(chunk, state):
    """Accumulate usage/content/reasoning/tool_calls into state. Emits no events."""
    result = ChunkResult()

    if chunk.get("usage"):
        state.final_usage = chunk["usage"]

    choices = chunk.get("choices", [])
    if not choices:
        return result

    delta = choices[0].get("delta", {})
    result.finish_reason = choices[0].get("finish_reason")

    reasoning = delta.get("reasoning_content")
    if reasoning:
        state.full_reasoning += reasoning

    content = delta.get("content")
    if content:
        state.full_text += content
        result.content_delta = content

    tc_delta = delta.get("tool_calls")
    if tc_delta:
        result.has_tool_calls = True
        for tc in tc_delta:
            idx = tc.get("index", 0)
            if idx not in state.tool_calls_acc:
                state.tool_calls_acc[idx] = {"id": tc.get("id", _rid("call")), "name": "", "arguments": ""}
            if tc.get("id"):
                state.tool_calls_acc[idx]["id"] = tc["id"]
            fn = tc.get("function", {})
            if fn.get("name"):
                state.tool_calls_acc[idx]["name"] = fn["name"]
            if fn.get("arguments"):
                state.tool_calls_acc[idx]["arguments"] += fn["arguments"]

    return result


def _emit_message_start(state):
    """Initial events for a text message: created + output_item.added + content_part.added."""
    yield _sse("response.created", _ev_response_created(state.resp_id, state.created, state.model))
    yield _sse("response.output_item.added", _ev_output_item_added_message(state.msg_id))
    yield _sse("response.content_part.added", _ev_content_part_added())


def _emit_response_created_only(state):
    """Initial event for a tool-call-first turn: only response.created."""
    yield _sse("response.created", _ev_response_created(state.resp_id, state.created, state.model))



def _build_final_output(state):
    final_output = [_message_output_item(state.msg_id, state.full_text)]
    for idx in sorted(state.tool_calls_acc):
        tc = state.tool_calls_acc[idx]
        final_output.append(_function_call_item(_rid("fc"), tc))
    return final_output


def _stream(cc, headers, model, cfg, store):
    resp_id = _rid()
    msg_id = _rid("msg")

    # Open the upstream connection BEFORE returning the streaming Response, so that
    # upstream errors (e.g. 429) surface with the correct HTTP status instead of a
    # 200 + inline SSE error (which breaks the client's backoff and loops forever).
    try:
        r = http_requests.post(
            f"{cfg.upstream_base}/chat/completions",
            json=cc, headers=headers, stream=True, timeout=cfg.timeout,
        )
    except Exception as e:
        return _upstream_connect_error(e)

    if r.status_code >= 400:
        body, status, hdrs = _build_upstream_error(r)
        r.close()
        return jsonify(body), status, hdrs

    def gen():
        try:
            state = StreamState(resp_id, msg_id, int(time.time()), model)

            for chunk in _iter_upstream_chunks(r):
                res = _parse_chunk_into_state(chunk, state)

                if res.content_delta is not None:
                    if not state.started:
                        state.started = True
                        yield from _emit_message_start(state)
                    yield _sse("response.output_text.delta",
                               _ev_output_text_delta(res.content_delta))

                if res.has_tool_calls and not state.started:
                    state.started = True
                    yield from _emit_response_created_only(state)

                if res.finish_reason in ("stop", "tool_calls"):
                    break

            # Persist reasoning for this turn (keyed by text hash + tool_call_ids)
            store.store(state.full_text, state.full_reasoning, state.tool_call_ids())

            # If we got no content at all (only reasoning), still open a minimal
            # message item (matches original behaviour: it does NOT set started
            # and therefore the text item is not closed below).
            if not state.started and not state.tool_calls_acc:
                yield from _emit_message_start(state)

            # Close the text item whenever a turn was started (text or tool-call).
            if state.started:
                yield _sse("response.output_text.done", _ev_output_text_done(state.full_text))
                yield _sse("response.output_item.done", _ev_message_item_done(state.msg_id, state.full_text))

            # Tool call items
            for idx in sorted(state.tool_calls_acc):
                tc = state.tool_calls_acc[idx]
                fc_id = _rid("fc")
                oi = 1 + idx
                yield _sse("response.output_item.added",
                           _ev_function_call_item("response.output_item.added", oi, fc_id, tc))
                yield _sse("response.output_item.done",
                           _ev_function_call_item("response.output_item.done", oi, fc_id, tc))

            final_output = _build_final_output(state)
            log.info("SENDING response.completed, text_len=%d, tools=%d, reasoning_len=%d",
                     len(state.full_text), len(state.tool_calls_acc), len(state.full_reasoning))
            yield _sse("response.completed",
                       _ev_response_completed(resp_id, state.created, model, final_output, state.final_usage))

        except Exception as e:
            log.error("STREAM: %s\n%s", e, traceback.format_exc())
            yield _sse("error", {"type": "server_error", "message": str(e)})

    return Response(stream_with_context(gen()), content_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


# ------------------------------------------------------------------
# Non-streaming response: Chat Completions -> Responses API
# ------------------------------------------------------------------

def _cc_to_responses(cc_resp, model, store):
    """Convert a non-streaming Chat Completions response into a Responses API object."""
    created = int(time.time())
    resp_id = _rid()
    msg_id = _rid("msg")

    choice = (cc_resp.get("choices") or [{}])[0]
    message = choice.get("message", {}) or {}
    full_text = message.get("content") or ""
    full_reasoning = message.get("reasoning_content") or ""

    output = [_message_output_item(msg_id, full_text)]
    tc_ids = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {})
        norm = {"id": tc.get("id", _rid("call")),
                "name": fn.get("name", ""), "arguments": fn.get("arguments", "")}
        output.append(_function_call_item(_rid("fc"), norm))
        tc_ids.append(norm["id"])

    store.store(full_text, full_reasoning, tc_ids or None)

    return {
        "id": resp_id, "object": "response", "created_at": created,
        "model": model, "status": "completed", "output": output,
        "parallel_tool_calls": True,
        "usage": _map_usage(cc_resp.get("usage", {})),
        "metadata": {},
    }


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

app = Flask(__name__)


@app.route("/v1/responses", methods=["POST"])
def handle():
    auth = request.headers.get("Authorization", "")
    api_key = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
    if not api_key:
        return jsonify({"error": {"message": "No API key"}}), 401

    raw = request.get_data(as_text=True)
    log.info("REQ: %s", raw[:500])
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("JSON ERR: %s", e)
        return jsonify({"error": {"message": f"Bad JSON: {e}"}}), 400

    cfg = _cfg()
    store = _store()
    cc, model = _build_cc_payload(body, cfg, store)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    log.info("FWD: model=%s, msgs=%d, tools=%d, keys=%s",
             cc.get("model"), len(cc.get("messages", [])), len(cc.get("tools", [])), list(cc.keys()))

    if cc["stream"]:
        return _stream(cc, headers, model, cfg, store)

    try:
        r = http_requests.post(f"{cfg.upstream_base}/chat/completions",
                               json=cc, headers=headers, timeout=cfg.timeout)
    except Exception as e:
        return _upstream_connect_error(e)

    if r.status_code >= 400:
        body, status, hdrs = _build_upstream_error(r)
        return jsonify(body), status, hdrs

    try:
        return jsonify(_cc_to_responses(r.json(), model, store))
    except Exception as e:
        log.error("UPSTREAM PARSE: %s", e)
        return jsonify({"error": {"message": str(e), "type": "upstream_error"}}), 502


@app.route("/v1/models", methods=["GET"])
def models():
    return jsonify({"object": "list", "data": [
        {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
        {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
        {"id": "glm-5.1", "object": "model", "owned_by": "zhipu"},
    ]})


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------

def create_app(cfg):
    logging.basicConfig(filename=cfg.log_path, level=logging.DEBUG, format="%(asctime)s %(message)s")
    app.config["CFG"] = cfg
    app.config["STORE"] = ReasoningStore(cfg.reasoning_store_path)
    return app


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9090)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--upstream", default="https://integrate.api.nvidia.com/v1",
                   help="Upstream Chat Completions API base URL")
    a = p.parse_args()
    cfg = ProxyConfig(upstream_base=a.upstream, host=a.host, port=a.port)
    create_app(cfg)
    store = app.config["STORE"]
    print(f"[proxy] http://{cfg.host}:{cfg.port} -> {cfg.upstream_base} "
          f"(reasoning_store: {len(store._data)} entries)")
    app.run(host=cfg.host, port=cfg.port, threaded=True)
