"""A conversation with tools, through each provider.

The shapes are pure functions, tested as data. Each provider is then run
through its real SDK over a transport that answers from here, so that what the
SDK accepts, sends and parses is the SDK's own: a parameter it no longer takes
fails here, not at a customer's.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import httpx
import pytest

from truejoin.adapters.outbound.llm import chat as shapes
from truejoin.adapters.outbound.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    LLMClient,
    LLMProvider,
    OllamaProvider,
    OpenAIProvider,
    _accepted,
)
from truejoin.domain.gateway.errors import LLMGenerationError
from truejoin.domain.olap.assistant import ChatMessage, ModelTurn, ToolRequest, ToolSpec

NO_ARGUMENTS = ToolSpec("list_sources", "What there is to read.")
DESCRIBE = ToolSpec(
    "describe_table",
    "One table's columns.",
    {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]},
)
TOOLS = (NO_ARGUMENTS, DESCRIBE)

ASKED = ToolRequest(id="c1", name="describe_table", arguments={"table": "db.orders"})
CONVERSATION = (
    ChatMessage("system", "You answer from the tools."),
    ChatMessage("user", "How many orders?"),
    ChatMessage("assistant", "Let me look.", tool_calls=(ASKED, ToolRequest("c2", "list_sources"))),
    ChatMessage("tool", '{"columns": ["id"]}', tool_call_id="c1", name="describe_table"),
    ChatMessage("tool", '{"sources": []}', tool_call_id="c2", name="list_sources"),
)
SETTINGS: dict[str, Any] = {"allow_tools": True, "temperature": 0.2, "max_tokens": 64}


class Server:
    """Answers a provider's HTTP calls from a queue and keeps what it was sent."""

    def __init__(self, *answers: dict[str, Any] | httpx.Response) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def _next(self, url: str, content: bytes) -> tuple[int, dict[str, Any] | None, str]:
        self.requests.append((url, json.loads(content)))
        answer = self.answers.pop(0)
        if isinstance(answer, httpx.Response):
            return answer.status_code, None, answer.text
        return 200, answer, ""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        status, body, text = self._next(str(request.url), request.content)
        return (
            httpx.Response(status, json=body)
            if body is not None
            else httpx.Response(status, text=text)
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def client2(self) -> Any:
        """The same server for an SDK built on ``httpx2`` (Anthropic's)."""
        import httpx2

        def handle(request: Any) -> Any:
            status, body, text = self._next(str(request.url), request.content)
            if body is not None:
                return httpx2.Response(status, json=body)
            return httpx2.Response(status, text=text)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))

    @property
    def sent(self) -> dict[str, Any]:
        return self.requests[-1][1]


class TestTheTypes:
    def test_a_turn_becomes_the_assistant_message_of_the_next_request(self):
        turn = ModelTurn(text="Looking.", tool_calls=(ASKED,))
        assert turn.as_message() == ChatMessage("assistant", "Looking.", tool_calls=(ASKED,))

    def test_a_provider_that_cannot_hold_one_says_so(self):
        class Plain(LLMProvider):
            async def generate(self, prompt, system_prompt=None, **_):  # type: ignore[override]
                return ""

            async def close(self) -> None:
                return None

        with pytest.raises(LLMGenerationError, match="cannot hold a conversation with tools"):
            import asyncio

            asyncio.run(Plain().chat(CONVERSATION, TOOLS))


class TestOpenAIShape:
    def test_the_conversation_as_chat_completions_takes_it(self):
        request = shapes.openai_request("m", CONVERSATION, TOOLS, **SETTINGS)
        assert [m["role"] for m in request["messages"]] == [
            "system",
            "user",
            "assistant",
            "tool",
            "tool",
        ]
        called = request["messages"][2]
        assert called["content"] == "Let me look."
        assert called["tool_calls"][0] == {
            "id": "c1",
            "type": "function",
            "function": {"name": "describe_table", "arguments": '{"table": "db.orders"}'},
        }
        assert request["messages"][3] == {
            "role": "tool",
            "tool_call_id": "c1",
            "content": '{"columns": ["id"]}',
        }
        assert request["tool_choice"] == "auto"
        assert (request["temperature"], request["max_tokens"]) == (0.2, 64)

    def test_a_tool_that_takes_nothing_is_still_an_object(self):
        request = shapes.openai_request("m", CONVERSATION, TOOLS, **SETTINGS)
        assert request["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}

    def test_an_answer_in_words_keeps_the_tools_declared(self):
        request = shapes.openai_request(
            "m", CONVERSATION, TOOLS, **{**SETTINGS, "allow_tools": False}
        )
        assert request["tool_choice"] == "none"
        assert len(request["tools"]) == 2

    def test_no_tools_names_none(self):
        request = shapes.openai_request("m", CONVERSATION[:2], (), **SETTINGS)
        assert "tools" not in request and "tool_choice" not in request

    def test_calls_and_counts_are_read_back(self):
        turn = shapes.openai_turn(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "x1",
                                    "function": {
                                        "name": "describe_table",
                                        "arguments": '{"table": "db.orders"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            }
        )
        assert turn.tool_calls == (ToolRequest("x1", "describe_table", {"table": "db.orders"}),)
        assert (turn.text, turn.input_tokens, turn.output_tokens) == ("", 12, 3)

    @pytest.mark.parametrize(
        ("written", "said"),
        [('{"table": ', "not valid JSON"), ('["db.orders"]', "must be a JSON object")],
    )
    def test_arguments_that_cannot_be_read_are_an_error_not_a_guess(self, written, said):
        turn = shapes.openai_turn(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "x", "function": {"name": "t", "arguments": written}}
                            ]
                        }
                    }
                ]
            }
        )
        (call,) = turn.tool_calls
        assert call.arguments == {} and said in call.error

    def test_every_key_is_one_the_sdk_takes(self):
        from openai.resources.chat.completions import AsyncCompletions

        request = shapes.openai_request("m", CONVERSATION, TOOLS, **SETTINGS)
        assert set(request) <= set(inspect.signature(AsyncCompletions.create).parameters)


class TestAnthropicShape:
    def test_the_results_of_one_turn_go_in_one_user_message(self):
        request = shapes.anthropic_request("m", CONVERSATION, TOOLS, **SETTINGS)
        assert request["system"] == "You answer from the tools."
        assert [m["role"] for m in request["messages"]] == ["user", "assistant", "user"]
        assert request["messages"][1]["content"] == [
            {"type": "text", "text": "Let me look."},
            {
                "type": "tool_use",
                "id": "c1",
                "name": "describe_table",
                "input": {"table": "db.orders"},
            },
            {"type": "tool_use", "id": "c2", "name": "list_sources", "input": {}},
        ]
        assert request["messages"][2]["content"] == [
            {"type": "tool_result", "tool_use_id": "c1", "content": '{"columns": ["id"]}'},
            {"type": "tool_result", "tool_use_id": "c2", "content": '{"sources": []}'},
        ]
        assert request["tools"][1]["input_schema"]["required"] == ["table"]
        assert request["tool_choice"] == {"type": "auto"}

    def test_an_answer_in_words_keeps_the_tools_declared(self):
        request = shapes.anthropic_request(
            "m", CONVERSATION, TOOLS, **{**SETTINGS, "allow_tools": False}
        )
        assert request["tool_choice"] == {"type": "none"} and len(request["tools"]) == 2

    def test_text_and_calls_are_read_back(self):
        turn = shapes.anthropic_turn(
            {
                "content": [
                    {"type": "text", "text": "Looking."},
                    {"type": "tool_use", "id": "t1", "name": "list_sources", "input": {}},
                ],
                "usage": {"input_tokens": 9, "output_tokens": 4},
            }
        )
        assert (turn.text, turn.tool_calls) == ("Looking.", (ToolRequest("t1", "list_sources"),))
        assert (turn.input_tokens, turn.output_tokens) == (9, 4)

    def test_a_turn_that_asks_for_tools_keeps_its_blocks_as_they_came(self):
        blocks = [
            {"type": "thinking", "thinking": "", "signature": "sig-1"},
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "t1", "name": "list_sources", "input": {}},
        ]
        turn = shapes.anthropic_turn({"content": blocks, "stop_reason": "tool_use"})
        assert json.loads(turn.provider_state) == blocks and turn.stop == "tool_use"
        # An answer ends the exchange: nothing of it has to go back.
        answer = shapes.anthropic_turn({"content": blocks[:2], "stop_reason": "end_turn"})
        assert answer.provider_state == "" and answer.text == "Looking."

    def test_those_blocks_go_back_unchanged_reasoning_first(self):
        blocks = [
            {"type": "thinking", "thinking": "", "signature": "sig-1"},
            {"type": "tool_use", "id": "t1", "name": "list_sources", "input": {}},
        ]
        turn = shapes.anthropic_turn({"content": blocks, "stop_reason": "tool_use"})
        said = (
            ChatMessage("user", "q"),
            turn.as_message(),
            ChatMessage("tool", "{}", tool_call_id="t1", name="list_sources"),
        )
        request = shapes.anthropic_request("m", said, TOOLS, **SETTINGS)
        assert request["messages"][1] == {"role": "assistant", "content": blocks}
        assert request["messages"][2]["content"][0]["tool_use_id"] == "t1"

    def test_a_message_with_no_state_is_built_from_its_text_and_calls(self):
        """What the browser sends back of a conversation, or another provider's turn."""
        said = (
            ChatMessage("user", "q"),
            ChatMessage("assistant", "Four."),
            ChatMessage("user", "and?"),
        )
        request = shapes.anthropic_request("m", said, TOOLS, **SETTINGS)
        assert request["messages"][1] == {
            "role": "assistant",
            "content": [{"type": "text", "text": "Four."}],
        }

    @pytest.mark.parametrize(
        ("message", "said"),
        [
            ({"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}, ""),
            ({"stop_reason": "tool_use", "content": [{"type": "tool_use"}]}, ""),
            (
                {"stop_reason": "refusal", "stop_details": {"category": "cyber"}, "content": []},
                "The model declined this request (cyber)",
            ),
            ({"stop_reason": "refusal", "content": []}, "The model declined this request"),
            # Cut, with words to show for it: kept.
            ({"stop_reason": "max_tokens", "content": [{"type": "text", "text": "So far"}]}, ""),
            # Cut inside its reasoning, or inside a tool call's arguments: nothing to use.
            ({"stop_reason": "max_tokens", "content": [{"type": "thinking"}]}, "raise max_tokens"),
            (
                {
                    "stop_reason": "max_tokens",
                    "content": [{"type": "text", "text": "Let me"}, {"type": "tool_use"}],
                },
                "raise max_tokens",
            ),
        ],
    )
    def test_why_a_message_is_not_an_answer(self, message, said):
        assert said in shapes.anthropic_stopped_short(message)
        assert bool(shapes.anthropic_stopped_short(message)) is bool(said)

    def test_what_this_sdk_no_longer_takes_is_left_out_and_the_rest_is_kept(self):
        """``messages.create`` lost ``temperature``: sending it is a TypeError before any call."""
        from anthropic.resources.messages import AsyncMessages

        request = shapes.anthropic_request("m", CONVERSATION, TOOLS, **SETTINGS)
        kept = _accepted(AsyncMessages.create, request)
        assert {"model", "messages", "max_tokens", "system", "tools", "tool_choice"} <= set(kept)
        assert set(kept) <= set(inspect.signature(AsyncMessages.create).parameters)

    def test_a_method_that_takes_anything_is_given_everything(self):
        def create(**kwargs: Any) -> None: ...

        assert _accepted(create, {"a": 1, "b": 2}) == {"a": 1, "b": 2}
        assert _accepted("not a method", {"a": 1}) == {"a": 1}


class TestGeminiShape:
    def test_the_conversation_as_generate_content_takes_it(self):
        request = shapes.gemini_request("m", CONVERSATION, TOOLS, **SETTINGS)
        contents = request["contents"]
        assert [c["role"] for c in contents] == ["user", "model", "user"]
        assert contents[1]["parts"][1] == {
            "function_call": {"name": "describe_table", "args": {"table": "db.orders"}}
        }
        assert contents[2]["parts"] == [
            {"function_response": {"name": "describe_table", "response": {"columns": ["id"]}}},
            {"function_response": {"name": "list_sources", "response": {"sources": []}}},
        ]
        config = request["config"]
        assert config["system_instruction"] == "You answer from the tools."
        assert config["tool_config"] == {"function_calling_config": {"mode": "AUTO"}}
        assert config["automatic_function_calling"] == {"disable": True}

    def test_the_sdk_reads_the_request_as_its_own_types(self):
        from google.genai import types
        from google.genai.models import AsyncModels

        request = shapes.gemini_request("m", CONVERSATION, TOOLS, **SETTINGS)
        assert set(request) <= set(inspect.signature(AsyncModels.generate_content).parameters)
        config = types.GenerateContentConfig(**request["config"])
        assert config.tools[0].function_declarations[1].name == "describe_table"
        assert [types.Content(**c).role for c in request["contents"]] == ["user", "model", "user"]

    def test_an_answer_that_is_not_an_object_is_wrapped_as_one(self):
        said = (ChatMessage("tool", "42", name="t"), ChatMessage("tool", "not json", name="t"))
        parts = shapes.gemini_request("m", said, (), **SETTINGS)["contents"][0]["parts"]
        assert [p["function_response"]["response"] for p in parts] == [
            {"result": 42},
            {"result": "not json"},
        ]

    def test_a_signature_comes_back_with_its_call(self):
        turn = shapes.gemini_turn(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "thinking", "thought": True},
                                {
                                    "function_call": {"name": "list_sources", "args": None},
                                    "thought_signature": b"abc",
                                },
                            ]
                        }
                    }
                ],
                "usage_metadata": {"prompt_token_count": 5, "candidates_token_count": 1},
            }
        )
        (call,) = turn.tool_calls
        assert (turn.text, call.name, call.signature) == ("", "list_sources", "YWJj")
        again = shapes.gemini_request(
            "m", (ChatMessage("user", "q"), turn.as_message()), TOOLS, **SETTINGS
        )
        assert again["contents"][1]["parts"][0]["thought_signature"] == b"abc"

    def test_no_tools_declared_when_none_are_given(self):
        config = shapes.gemini_request("m", CONVERSATION[:2], (), **SETTINGS)["config"]
        assert "tools" not in config and "tool_config" not in config


class TestOllamaShape:
    def test_the_conversation_as_api_chat_takes_it(self):
        request = shapes.ollama_request("m", CONVERSATION, TOOLS, **SETTINGS)
        assert request["stream"] is False
        assert request["options"] == {"temperature": 0.2, "num_predict": 64}
        assert request["messages"][2]["tool_calls"][0] == {
            "function": {"name": "describe_table", "arguments": {"table": "db.orders"}}
        }
        assert request["messages"][3] == {
            "role": "tool",
            "content": '{"columns": ["id"]}',
            "tool_name": "describe_table",
        }

    def test_an_answer_in_words_is_asked_without_the_tools(self):
        request = shapes.ollama_request(
            "m", CONVERSATION, TOOLS, **{**SETTINGS, "allow_tools": False}
        )
        assert "tools" not in request

    def test_calls_without_ids_are_numbered(self):
        turn = shapes.ollama_turn(
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "list_sources", "arguments": {}}},
                        {"function": {"name": "describe_table", "arguments": {"table": "a.b"}}},
                    ],
                },
                "prompt_eval_count": 7,
                "eval_count": 2,
            }
        )
        assert [c.id for c in turn.tool_calls] == ["call_0", "call_1"]
        assert (turn.input_tokens, turn.output_tokens) == (7, 2)


OPENAI_CALLS = {
    "id": "x",
    "object": "chat.completion",
    "created": 1,
    "model": "m",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "list_sources", "arguments": "{}"},
                    }
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}
ANTHROPIC_CALLS = {
    "id": "m",
    "type": "message",
    "role": "assistant",
    "model": "m",
    "content": [
        {"type": "text", "text": "Looking."},
        {"type": "tool_use", "id": "t1", "name": "list_sources", "input": {}},
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 4},
}
ANTHROPIC_TEXT = {**ANTHROPIC_CALLS, "content": [{"type": "text", "text": " Four. "}]}
GEMINI_CALLS = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {"name": "list_sources", "args": {}},
                        "thoughtSignature": "YWJj",
                    }
                ],
            }
        }
    ],
    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
}
GEMINI_TEXT = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Four."}]}}]}

ASK = (ChatMessage("system", "s"), ChatMessage("user", "How many?"))


def openai_provider(server: Server) -> OpenAIProvider:
    from openai import AsyncOpenAI

    provider = OpenAIProvider(api_key="k", model="m")
    provider.client = AsyncOpenAI(api_key="k", http_client=server.client(), max_retries=0)
    return provider


def anthropic_provider(server: Server) -> AnthropicProvider:
    from anthropic import AsyncAnthropic

    provider = AnthropicProvider(api_key="k", model="claude-sonnet-5-5")
    provider.client = AsyncAnthropic(api_key="k", http_client=server.client2(), max_retries=0)
    return provider


def gemini_provider(server: Server) -> GeminiProvider:
    from google import genai
    from google.genai import types

    provider = GeminiProvider(api_key="k", model="m")
    provider.client = genai.Client(
        api_key="k", http_options=types.HttpOptions(httpx_async_client=server.client())
    )
    return provider


def ollama_provider(server: Server) -> OllamaProvider:
    provider = OllamaProvider(model="m", base_url="http://ollama.test:11434")
    provider.client = server.client()
    return provider


@pytest.mark.asyncio
class TestThroughTheSdk:
    async def test_openai_asks_for_a_tool_and_is_answered(self):
        server = Server(OPENAI_CALLS)
        turn = await openai_provider(server).chat(ASK, TOOLS)
        assert turn.tool_calls == (ToolRequest("c1", "list_sources"),)
        assert (turn.input_tokens, turn.output_tokens) == (5, 2)
        assert server.requests[0][0].endswith("/chat/completions")
        assert [t["function"]["name"] for t in server.sent["tools"]] == [
            "list_sources",
            "describe_table",
        ]

    async def test_openai_quota_is_said_as_quota_after_one_retry(self):
        refused = httpx.Response(429, text='{"error": {"message": "rate limit"}}')
        server = Server(refused, refused, refused)
        with pytest.raises(LLMGenerationError, match="quota or rate limit"):
            await openai_provider(server).chat(ASK, TOOLS)
        assert len(server.requests) == 2  # the SDK's own default would be three

    async def test_openai_an_empty_turn_is_a_failure(self):
        empty = {
            **OPENAI_CALLS,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": ""},
                }
            ],
        }
        with pytest.raises(LLMGenerationError, match="Empty response from OpenAI"):
            await openai_provider(Server(empty)).chat(ASK, TOOLS)

    async def test_anthropic_asks_for_a_tool_and_is_answered(self):
        server = Server(ANTHROPIC_CALLS)
        turn = await anthropic_provider(server).chat(ASK, TOOLS)
        assert (turn.text, turn.tool_calls) == ("Looking.", (ToolRequest("t1", "list_sources"),))
        assert (turn.input_tokens, turn.output_tokens, turn.stop) == (3, 4, "tool_use")
        assert server.sent["system"] == "s"
        assert server.sent["tool_choice"] == {"type": "auto"}
        assert [t["name"] for t in server.sent["tools"]] == ["list_sources", "describe_table"]

    async def test_anthropic_gets_its_reasoning_back_with_the_tools_result(self):
        """Thinking is on by default: its blocks must return as they came, signature and all."""
        thought = {"type": "thinking", "thinking": "", "signature": "c2lnbmF0dXJl"}
        asked = {**ANTHROPIC_CALLS, "content": [thought, *ANTHROPIC_CALLS["content"]]}
        server = Server(asked, ANTHROPIC_TEXT)
        provider = anthropic_provider(server)
        turn = await provider.chat(ASK, TOOLS)
        result = ChatMessage("tool", '{"sources": []}', tool_call_id="t1", name="list_sources")
        answer = await provider.chat((*ASK, turn.as_message(), result), TOOLS)
        assert answer.text == "Four." and answer.provider_state == ""
        back = server.sent["messages"][1]
        assert back["role"] == "assistant" and back["content"][0] == thought
        assert [block["type"] for block in back["content"]] == ["thinking", "text", "tool_use"]
        assert server.sent["messages"][2]["content"] == [
            {"type": "tool_result", "tool_use_id": "t1", "content": '{"sources": []}'}
        ]

    @pytest.mark.parametrize(
        ("message", "said"),
        [
            (
                {
                    "stop_reason": "refusal",
                    "stop_details": {"type": "refusal", "category": "bio"},
                    "content": [],
                },
                r"Anthropic: The model declined this request \(bio\)",
            ),
            (
                {
                    "stop_reason": "max_tokens",
                    "content": [{"type": "thinking", "thinking": "", "signature": "s"}],
                },
                "Anthropic: The model ran out of tokens",
            ),
        ],
    )
    async def test_anthropic_says_why_there_is_no_answer(self, message, said):
        server = Server({**ANTHROPIC_CALLS, **message})
        with pytest.raises(LLMGenerationError, match=said):
            await anthropic_provider(server).chat(ASK, TOOLS)

    async def test_anthropic_generate_reaches_the_api(self):
        """It did not: the SDK refused ``temperature`` before sending anything."""
        server = Server(ANTHROPIC_TEXT)
        assert await anthropic_provider(server).generate("How many?", system_prompt="s") == "Four."
        assert server.sent["messages"] == [{"role": "user", "content": "How many?"}]

    async def test_gemini_asks_for_a_tool_then_answers_with_its_result(self):
        server = Server(GEMINI_CALLS, GEMINI_TEXT)
        provider = gemini_provider(server)
        turn = await provider.chat(ASK, TOOLS)
        (call,) = turn.tool_calls
        assert (call.name, call.signature, turn.input_tokens) == ("list_sources", "YWJj", 3)
        result = ChatMessage("tool", '{"sources": []}', tool_call_id=call.id, name=call.name)
        answer = await provider.chat((*ASK, turn.as_message(), result), TOOLS, allow_tools=False)
        assert answer.text == "Four."
        sent = server.sent
        assert sent["toolConfig"] == {"functionCallingConfig": {"mode": "NONE"}}
        assert sent["contents"][1]["parts"][0]["thoughtSignature"] == "YWJj"
        assert sent["contents"][2]["parts"][0]["functionResponse"] == {
            "name": "list_sources",
            "response": {"sources": []},
        }

    async def test_ollama_talks_to_api_chat(self):
        server = Server(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "list_sources", "arguments": {}}}],
                },
                "prompt_eval_count": 4,
                "eval_count": 1,
            }
        )
        turn = await ollama_provider(server).chat(ASK, TOOLS)
        assert turn.tool_calls == (ToolRequest("call_0", "list_sources"),)
        assert server.requests[0][0] == "http://ollama.test:11434/api/chat"
        assert len(server.sent["tools"]) == 2

    async def test_ollama_generate_stays_on_api_generate(self):
        server = Server({"response": " Four. "})
        assert await ollama_provider(server).generate("How many?") == "Four."
        assert server.requests[0][0].endswith("/api/generate")

    @pytest.mark.parametrize(
        ("answer", "said"),
        [
            (httpx.Response(404, text='{"error": "model not found"}'), "ollama pull m"),
            (
                httpx.Response(400, text='{"error": "m does not support tools"}'),
                "needs a model that does",
            ),
            (httpx.Response(500, text="boom"), "Ollama generation failed"),
        ],
    )
    async def test_ollama_says_what_is_wrong_with_the_model(self, answer, said):
        with pytest.raises(LLMGenerationError, match=said):
            await ollama_provider(Server(answer)).chat(ASK, TOOLS)

    async def test_the_client_sends_its_configured_temperature_and_length(self):
        server = Server(OPENAI_CALLS)
        client = LLMClient(openai_provider(server), temperature=0.7, max_tokens=99)
        await client.chat(ASK, TOOLS, allow_tools=False)
        assert (server.sent["temperature"], server.sent["max_tokens"]) == (0.7, 99)
        assert server.sent["tool_choice"] == "none"
