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

import json
import os
import time
import uuid
import hashlib
import argparse
import traceback
from flask import Flask, request, Response, jsonify, stream_with_context
import requests as http_requests

app = Flask(__name__)

UPSTREAM_BASE = "https://api.deepseek.com"

MODEL_MAP = {
    "gpt-5.4": "deepseek-v4-pro",
    "gpt-5.4-mini": "deepseek-v4-flash",
    "gpt-4o": "deepseek-v4-pro",
    "gpt-4o-mini": "deepseek-v4-flash",
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE_DIR, "proxy.log")
RC_STORE = os.path.join(BASE_DIR, "reasoning_store.json")

import logging
logging.basicConfig(filename=LOG, level=logging.DEBUG, format="%(asctime)s %(message)s")
log = logging.getLogger("proxy")


def _rid(prefix="resp"):
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ------------------------------------------------------------------
# Reasoning content store (persist to disk, survive proxy restarts)
# ------------------------------------------------------------------

_rc_store = {}

def _load_rc_store():
    global _rc_store
    try:
        with open(RC_STORE, "r", encoding="utf-8") as f:
            _rc_store = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _rc_store = {}

def _save_rc_store():
    with open(RC_STORE, "w", encoding="utf-8") as f:
        json.dump(_rc_store, f, ensure_ascii=False)

def _content_hash(text):
    """Hash assistant message content to use as key for reasoning lookup."""
    return hashlib.md5(text.encode()).hexdigest()

def _store_reasoning(text, reasoning, tool_call_ids=None):
    """Save reasoning_content keyed by hash of assistant text and optionally by tool_call_ids."""
    if reasoning:
        h = _content_hash(text)
        _rc_store[h] = reasoning
        if tool_call_ids:
            for tc_id in tool_call_ids:
                _rc_store[f"tc_{tc_id}"] = reasoning
        _save_rc_store()
        log.info("STORED reasoning for hash=%s, rc_len=%d, tc_ids=%s", h[:12], len(reasoning), tool_call_ids or [])

def _lookup_reasoning(text):
    """Look up stored reasoning_content by hash of assistant text."""
    h = _content_hash(text)
    rc = _rc_store.get(h, "")
    if rc:
        log.info("FOUND reasoning for hash=%s, rc_len=%d", h[:12], len(rc))
    return rc


# ------------------------------------------------------------------
# Request: Responses API -> Chat Completions
# ------------------------------------------------------------------

def _convert_input(body):
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
                rc = _rc_store.get(f"tc_{tc['id']}", "")
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
                parts = []
                for p in tool_output:
                    if isinstance(p, str):
                        parts.append(p)
                    elif isinstance(p, dict) and p.get("type") in ("input_text", "text", "output_text"):
                        parts.append(p.get("text", ""))
                tool_output = "\n".join(parts)
            messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""), "content": tool_output})
        else:
            _emit_pending()
            _emit_tc()
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            content = item.get("content", "")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, str):
                        parts.append(p)
                    elif isinstance(p, dict) and p.get("type") in ("input_text", "text", "output_text"):
                        parts.append(p.get("text", ""))
                    # Skip non-text types like input_image (DeepSeek doesn't support them)
                content = "\n".join(parts)
            msg = {}
            if role:
                msg["role"] = role
            if content is not None:
                msg["content"] = content
            # For assistant messages, restore reasoning_content from our local store
            if role == "assistant":
                stored_rc = _lookup_reasoning(content or "")
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


# ------------------------------------------------------------------
# Main endpoint
# ------------------------------------------------------------------

@app.route("/v1/responses", methods=["POST"])
def handle():
    auth = request.headers.get("Authorization", "")
    api_key = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
    if not api_key:
        return jsonify({"error": {"message": "No API key"}}), 401

    raw = request.get_data(as_text=True)
    with open(os.path.join(BASE_DIR, "last_request.json"), "w", encoding="utf-8") as f:
        f.write(raw)
    log.info("REQ: %s", raw[:500])
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("JSON ERR: %s", e)
        return jsonify({"error": {"message": f"Bad JSON: {e}"}}), 400

    model = body.get("model", "glm-5.1")
    model = MODEL_MAP.get(model, model)
    # Fallback: any unknown gpt-* model maps to deepseek-v4-pro
    if model.startswith("gpt-"):
        model = "deepseek-v4-pro"
    stream = body.get("stream", False)
    messages = _convert_input(body)

    cc = {"model": model, "messages": messages, "stream": stream}
    cc_tools = _convert_tools(body.get("tools"))
    if cc_tools:
        cc["tools"] = cc_tools
    if body.get("tool_choice"):
        cc["tool_choice"] = body["tool_choice"]
    for k in ("temperature", "top_p", "max_tokens", "max_completion_tokens"):
        if k in body:
            cc[k] = body[k]

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    log.info("FWD: model=%s, msgs=%d, tools=%d, keys=%s", cc.get("model"), len(cc.get("messages",[])), len(cc.get("tools",[])), list(cc.keys()))

    if stream:
        return _stream(cc, headers, model)

    try:
        r = http_requests.post(f"{UPSTREAM_BASE}/chat/completions", json=cc, headers=headers, timeout=120)
        r.raise_for_status()
        return jsonify(_cc_to_responses(r.json(), model))
    except Exception as e:
        log.error("UPSTREAM: %s", e)
        return jsonify({"error": {"message": str(e)}}), 502


def _stream(cc, headers, model):
    resp_id = _rid()
    msg_id = _rid("msg")

    def gen():
        try:
            r = http_requests.post(
                f"{UPSTREAM_BASE}/chat/completions",
                json=cc, headers=headers, stream=True, timeout=120,
            )
            if r.status_code >= 400:
                log.error("UPSTREAM %d: %s", r.status_code, r.text[:500])
            r.raise_for_status()

            created = int(time.time())
            full_text = ""
            full_reasoning = ""
            tool_calls_acc = {}
            has_content = False
            final_usage = {}

            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    log.info("UPSTREAM DONE")
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    final_usage = chunk["usage"]

                choices = chunk.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                finish = choices[0].get("finish_reason")

                # Capture reasoning_content from DeepSeek thinking mode
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    full_reasoning += reasoning

                content = delta.get("content")
                if content:
                    if not has_content:
                        has_content = True
                        yield _sse("response.created", {
                            "type": "response.created",
                            "response": {"id": resp_id, "object": "response", "created_at": created,
                                         "model": model, "status": "in_progress", "output": [], "metadata": {}},
                        })
                        yield _sse("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {"type": "message", "id": msg_id, "status": "in_progress",
                                     "role": "assistant", "content": []},
                        })
                        yield _sse("response.content_part.added", {
                            "type": "response.content_part.added",
                            "output_index": 0, "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        })
                    full_text += content
                    yield _sse("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "output_index": 0, "content_index": 0, "delta": content,
                    })

                # Tool calls
                tc_delta = delta.get("tool_calls")
                if tc_delta:
                    if not has_content:
                        has_content = True
                        yield _sse("response.created", {
                            "type": "response.created",
                            "response": {"id": resp_id, "object": "response", "created_at": created,
                                         "model": model, "status": "in_progress", "output": [], "metadata": {}},
                        })
                    for tc in tc_delta:
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {"id": tc.get("id", _rid("call")),
                                                   "name": "", "arguments": ""}
                        if tc.get("id"):
                            tool_calls_acc[idx]["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            tool_calls_acc[idx]["name"] = fn["name"]
                        if fn.get("arguments"):
                            tool_calls_acc[idx]["arguments"] += fn["arguments"]

                if finish in ("stop", "tool_calls"):
                    break

            # Store reasoning for this turn (keyed by text hash + tool_call_ids)
            tc_ids = [tool_calls_acc[i]["id"] for i in sorted(tool_calls_acc.keys())] if tool_calls_acc else None
            _store_reasoning(full_text, full_reasoning, tc_ids)

            # If we got no content at all (only reasoning), still send minimal response
            if not has_content and not tool_calls_acc:
                yield _sse("response.created", {
                    "type": "response.created",
                    "response": {"id": resp_id, "object": "response", "created_at": created,
                                 "model": model, "status": "in_progress", "output": [], "metadata": {}},
                })
                yield _sse("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"type": "message", "id": msg_id, "status": "in_progress",
                             "role": "assistant", "content": []},
                })
                yield _sse("response.content_part.added", {
                    "type": "response.content_part.added",
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                })

            # Close text
            if has_content:
                yield _sse("response.output_text.done", {
                    "type": "response.output_text.done",
                    "output_index": 0, "content_index": 0, "text": full_text,
                })
                yield _sse("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {"type": "message", "id": msg_id, "status": "completed",
                             "role": "assistant",
                             "content": [{"type": "output_text", "text": full_text, "annotations": []}]},
                })

            # Tool call items
            for idx in sorted(tool_calls_acc.keys()):
                tc = tool_calls_acc[idx]
                fc_id = _rid("fc")
                oi = 1 + idx
                yield _sse("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": oi,
                    "item": {"type": "function_call", "id": fc_id, "call_id": tc["id"],
                             "name": tc["name"], "arguments": tc["arguments"], "status": "completed"},
                })
                yield _sse("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": oi,
                    "item": {"type": "function_call", "id": fc_id, "call_id": tc["id"],
                             "name": tc["name"], "arguments": tc["arguments"], "status": "completed"},
                })

            # Final output list
            final_output = [{
                "type": "message", "id": msg_id, "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": full_text, "annotations": []}],
            }]
            for idx in sorted(tool_calls_acc.keys()):
                tc = tool_calls_acc[idx]
                final_output.append({
                    "type": "function_call", "id": _rid("fc"), "call_id": tc["id"],
                    "name": tc["name"], "arguments": tc["arguments"], "status": "completed",
                })

            log.info("SENDING response.completed, text_len=%d, tools=%d, reasoning_len=%d", len(full_text), len(tool_calls_acc), len(full_reasoning))
            yield _sse("response.completed", {
                "type": "response.completed",
                "response": {
                    "id": resp_id, "object": "response", "created_at": created,
                    "model": model, "status": "completed", "output": final_output,
                    "parallel_tool_calls": True,
                    "usage": {
                        "input_tokens": final_usage.get("prompt_tokens", 0),
                        "output_tokens": final_usage.get("completion_tokens", 0),
                        "total_tokens": final_usage.get("total_tokens", 0),
                    },
                    "metadata": {},
                },
            })

        except Exception as e:
            log.error("STREAM: %s\n%s", e, traceback.format_exc())
            yield _sse("error", {"type": "server_error", "message": str(e)})

    return Response(stream_with_context(gen()), content_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.route("/v1/models", methods=["GET"])
def models():
    return jsonify({"object": "list", "data": [
        {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
        {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
        {"id": "glm-5.1", "object": "model", "owned_by": "zhipu"},
    ]})


if __name__ == "__main__":
    _load_rc_store()
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9090)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--upstream", default="https://api.deepseek.com",
                   help="Upstream Chat Completions API base URL")
    a = p.parse_args()
    UPSTREAM_BASE = a.upstream
    print(f"[proxy] http://{a.host}:{a.port} -> {UPSTREAM_BASE} (reasoning_store: {len(_rc_store)} entries)")
    app.run(host=a.host, port=a.port, threaded=True)
