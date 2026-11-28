"""A conversation with tools, in each provider's own shape and back.

One pair of pure functions per provider: ``*_request`` turns the messages and
the tools into what that provider's API takes, ``*_turn`` reads its answer
(as plain data) back into a :class:`ModelTurn`. Nothing here opens a
connection, so each shape is tested without one.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from typing import Any

from truejoin.domain.olap.assistant import ChatMessage, ModelTurn, ToolRequest, ToolSpec


def _parameters(tool: ToolSpec) -> dict[str, Any]:
    """A tool's JSON Schema; one that takes nothing is still an object with no properties."""
    schema = dict(tool.parameters)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def _arguments(raw: Any) -> tuple[dict[str, Any], str]:
    """A call's arguments as an object, and what was wrong with them when they are not one."""
    value = raw
    if isinstance(raw, (str, bytes)):
        if not raw:
            return {}, ""
        try:
            value = json.loads(raw)
        except ValueError as e:
            return {}, f"The arguments were not valid JSON: {e}"
    if value is None:
        return {}, ""
    if not isinstance(value, Mapping):
        return {}, "The arguments must be a JSON object"
    return dict(value), ""


def _request(call_id: str, name: Any, raw: Any, signature: str = "") -> ToolRequest:
    arguments, error = _arguments(raw)
    return ToolRequest(
        id=call_id, name=str(name or ""), arguments=arguments, signature=signature, error=error
    )


def _system(messages: Sequence[ChatMessage]) -> str:
    return "\n\n".join(m.text for m in messages if m.role == "system" and m.text)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) else None


# --- OpenAI Chat Completions (and every server that speaks it) -----------------


def openai_request(
    model: str,
    messages: Sequence[ChatMessage],
    tools: Sequence[ToolSpec],
    *,
    allow_tools: bool,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    """The keyword arguments of ``chat.completions.create``."""
    sent: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            sent.append(
                {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.text}
            )
        elif message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.text or None}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(dict(call.arguments), default=str),
                        },
                    }
                    for call in message.tool_calls
                ]
            sent.append(entry)
        else:
            sent.append({"role": message.role, "content": message.text})
    request: dict[str, Any] = {
        "model": model,
        "messages": sent,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        request["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": _parameters(tool),
                },
            }
            for tool in tools
        ]
        request["tool_choice"] = "auto" if allow_tools else "none"
    return request


def openai_turn(response: Mapping[str, Any]) -> ModelTurn:
    """A chat completion, as ``model_dump()`` gives it."""
    choices = response.get("choices") or [{}]
    message = choices[0].get("message") or {}
    calls = []
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        calls.append(
            _request(
                call.get("id") or f"call_{index}", function.get("name"), function.get("arguments")
            )
        )
    usage = response.get("usage") or {}
    return ModelTurn(
        text=(message.get("content") or "").strip(),
        tool_calls=tuple(calls),
        input_tokens=_count(usage.get("prompt_tokens")),
        output_tokens=_count(usage.get("completion_tokens")),
    )


# --- Anthropic Messages ---------------------------------------------------------


def anthropic_request(
    model: str,
    messages: Sequence[ChatMessage],
    tools: Sequence[ToolSpec],
    *,
    allow_tools: bool,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    """The keyword arguments of ``messages.create``.

    Tool results travel as blocks of a user message; the results of one turn's
    calls go in one message, which is what the API asks for.
    """
    sent: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.text,
            }
            last = sent[-1] if sent else None
            if last and last["role"] == "user" and isinstance(last["content"], list):
                last["content"].append(block)
            else:
                sent.append({"role": "user", "content": [block]})
        elif message.role == "assistant" and message.provider_state:
            # The turn as it came, reasoning blocks included: the API wants those back
            # unchanged while a tool loop goes on, and tells an edited one apart.
            sent.append({"role": "assistant", "content": json.loads(message.provider_state)})
        elif message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.text:
                blocks.append({"type": "text", "text": message.text})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": dict(call.arguments),
                }
                for call in message.tool_calls
            )
            sent.append({"role": "assistant", "content": blocks or message.text})
        else:
            sent.append({"role": "user", "content": message.text})
    request: dict[str, Any] = {
        "model": model,
        "messages": sent,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    system = _system(messages)
    if system:
        request["system"] = system
    if tools:
        request["tools"] = [
            {"name": tool.name, "description": tool.description, "input_schema": _parameters(tool)}
            for tool in tools
        ]
        request["tool_choice"] = {"type": "auto" if allow_tools else "none"}
    return request


def anthropic_turn(response: Mapping[str, Any]) -> ModelTurn:
    """A message, as ``model_dump()`` gives it."""
    texts: list[str] = []
    calls: list[ToolRequest] = []
    for index, block in enumerate(response.get("content") or []):
        if block.get("type") == "text" and block.get("text"):
            texts.append(block["text"])
        elif block.get("type") == "tool_use":
            calls.append(
                _request(block.get("id") or f"call_{index}", block.get("name"), block.get("input"))
            )
    usage = response.get("usage") or {}
    content = response.get("content") or []
    return ModelTurn(
        text="\n".join(texts).strip(),
        tool_calls=tuple(calls),
        input_tokens=_count(usage.get("input_tokens")),
        output_tokens=_count(usage.get("output_tokens")),
        # Only a turn that asks for tools is sent back; an answer ends the exchange.
        provider_state=json.dumps(content, default=str) if calls else "",
        stop=str(response.get("stop_reason") or ""),
    )


def anthropic_stopped_short(response: Mapping[str, Any]) -> str:
    """Why this message is not an answer to use, or "" when it is one.

    A refusal is said as one, with its category when the API gives it. A turn
    cut at the token limit is a failure only when the cut left nothing usable:
    no text, or a tool call whose arguments stop in the middle.
    """
    reason = response.get("stop_reason")
    if reason == "refusal":
        details = response.get("stop_details") or {}
        category = details.get("category")
        return "The model declined this request" + (f" ({category})" if category else "")
    if reason == "max_tokens":
        blocks = response.get("content") or []
        asked = any(block.get("type") == "tool_use" for block in blocks)
        said = any(block.get("type") == "text" and block.get("text") for block in blocks)
        if asked or not said:
            return (
                "The model ran out of tokens before it finished: raise max_tokens "
                "for the assistant's model in the llm settings"
            )
    return ""


# --- Gemini (google-genai) ------------------------------------------------------


def _signature_out(value: Any) -> str:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return str(value or "")


def _tool_answer(text: str) -> dict[str, Any]:
    """A tool's answer as the object a function response must be."""
    try:
        value = json.loads(text)
    except ValueError:
        return {"result": text}
    return value if isinstance(value, dict) else {"result": value}


def gemini_request(
    model: str,
    messages: Sequence[ChatMessage],
    tools: Sequence[ToolSpec],
    *,
    allow_tools: bool,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    """The keyword arguments of ``models.generate_content``.

    The SDK would run the functions itself if left to: that is turned off, the
    assistant runs them as the caller.
    """
    contents: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            part: dict[str, Any] = {
                "function_response": {
                    "name": message.name,
                    "response": _tool_answer(message.text),
                }
            }
            last = contents[-1] if contents else None
            if last and last["role"] == "user" and "function_response" in last["parts"][0]:
                last["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
        elif message.role == "assistant":
            parts: list[dict[str, Any]] = []
            if message.text:
                parts.append({"text": message.text})
            for call in message.tool_calls:
                called: dict[str, Any] = {
                    "function_call": {"name": call.name, "args": dict(call.arguments)}
                }
                if call.signature:
                    called["thought_signature"] = base64.b64decode(call.signature)
                parts.append(called)
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
        else:
            contents.append({"role": "user", "parts": [{"text": message.text}]})
    config: dict[str, Any] = {"temperature": temperature, "max_output_tokens": max_tokens}
    system = _system(messages)
    if system:
        config["system_instruction"] = system
    if tools:
        config["tools"] = [
            {
                "function_declarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters_json_schema": _parameters(tool),
                    }
                    for tool in tools
                ]
            }
        ]
        config["tool_config"] = {
            "function_calling_config": {"mode": "AUTO" if allow_tools else "NONE"}
        }
        config["automatic_function_calling"] = {"disable": True}
    return {"model": model, "contents": contents, "config": config}


def gemini_turn(response: Mapping[str, Any]) -> ModelTurn:
    """A generate-content response, as ``model_dump()`` gives it."""
    candidates = response.get("candidates") or [{}]
    content = candidates[0].get("content") or {}
    texts: list[str] = []
    calls: list[ToolRequest] = []
    for index, part in enumerate(content.get("parts") or []):
        call = part.get("function_call")
        if call:
            calls.append(
                _request(
                    call.get("id") or f"call_{index}",
                    call.get("name"),
                    call.get("args"),
                    _signature_out(part.get("thought_signature")),
                )
            )
        elif part.get("text") and not part.get("thought"):
            texts.append(part["text"])
    usage = response.get("usage_metadata") or {}
    return ModelTurn(
        text="".join(texts).strip(),
        tool_calls=tuple(calls),
        input_tokens=_count(usage.get("prompt_token_count")),
        output_tokens=_count(usage.get("candidates_token_count")),
    )


# --- Ollama /api/chat -----------------------------------------------------------


def ollama_request(
    model: str,
    messages: Sequence[ChatMessage],
    tools: Sequence[ToolSpec],
    *,
    allow_tools: bool,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    """The JSON body of ``POST /api/chat``.

    Ollama has no way to declare tools and forbid calling them, so a turn that
    must be an answer is sent without them.
    """
    sent: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            sent.append({"role": "tool", "content": message.text, "tool_name": message.name})
        elif message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.text}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {"function": {"name": call.name, "arguments": dict(call.arguments)}}
                    for call in message.tool_calls
                ]
            sent.append(entry)
        else:
            sent.append({"role": message.role, "content": message.text})
    request: dict[str, Any] = {
        "model": model,
        "messages": sent,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if tools and allow_tools:
        request["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": _parameters(tool),
                },
            }
            for tool in tools
        ]
    return request


def ollama_turn(response: Mapping[str, Any]) -> ModelTurn:
    """The JSON body ``/api/chat`` answers with."""
    message = response.get("message") or {}
    calls = []
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        calls.append(
            _request(
                call.get("id") or f"call_{index}", function.get("name"), function.get("arguments")
            )
        )
    return ModelTurn(
        text=(message.get("content") or "").strip(),
        tool_calls=tuple(calls),
        input_tokens=_count(response.get("prompt_eval_count")),
        output_tokens=_count(response.get("eval_count")),
    )
