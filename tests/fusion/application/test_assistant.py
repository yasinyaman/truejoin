"""The built-in assistant: a model asking for tools, each run as the caller.

Real DuckDB, the real tools and rewriter, a fake source, and a model that says
what a script tells it to. The questions every test asks: does the model see
only what the caller may, does the run end, and does the trail say what
happened without saying what was read?
"""

import json

import pytest

from tests.fusion.application.test_access_scope import MASKING, RAW, TABLES, Audit, tenant
from tests.fusion.fakes import FakeSourceFactory, ManualScheduler, ScriptedChat, ask, say
from truejoin.application.olap.assistant import AssistantService
from truejoin.application.olap.settings import Settings
from truejoin.application.olap.tool_schemas import (
    ASSISTANT_TOOLS,
    TERM_TOOLS,
    TOOL_NAMES,
    assistant_tools,
)
from truejoin.bootstrap import build_app
from truejoin.domain.gateway.errors import LLMGenerationError
from truejoin.domain.governance.access import UNRESTRICTED, AccessScope
from truejoin.domain.olap.assistant import (
    AnswerEvent,
    AssistantLimits,
    ErrorEvent,
    Exchange,
    ModelTurn,
    StepEvent,
    ToolRequest,
    clip_result,
)

COUNT = "SELECT COUNT(*) AS n FROM db.customers"
EMAILS = "SELECT email, tc_no FROM db.customers ORDER BY id"
DPO = AccessScope(actor="dpo", roles=("dpo",), masking={"db": MASKING}, masked=False)


@pytest.fixture
def audit():
    return Audit()


@pytest.fixture
def app(tmp_path, audit):
    settings = Settings(threads=1, memory_limit="256MB", backup_path=str(tmp_path / "b"))
    a = build_app(
        settings, scheduler=ManualScheduler(), source_factory=FakeSourceFactory(), audit=audit
    )
    a.sources.connect("db", {"type": "fake", "tables": TABLES})
    yield a
    a.close()


def assistant(app, audit, *turns, **options) -> tuple[AssistantService, ScriptedChat]:
    chat = ScriptedChat(*turns)
    limits = options.pop("limits", None) or AssistantLimits(**options.pop("limit", {}))
    service = AssistantService(
        app.tools,
        chat,
        options.pop("specs", assistant_tools()),
        provider="scripted",
        model="m1",
        limits=limits,
        audit=audit,
        **options,
    )
    return service, chat


def counted(messages) -> ModelTurn:
    """An answer that quotes the count the last tool returned."""
    return say(f"n={json.loads(messages[-1].text)['rows'][0]['n']}")


def of(events, kind):
    return [e for e in events if isinstance(e, kind)]


def recorded(audit, event: str) -> list[dict]:
    return [e.as_dict() for e in audit.events if e.as_dict()["event"] == event]


class TestARun:
    def test_the_model_asks_a_tool_reads_its_result_and_answers(self, app, audit):
        service, chat = assistant(app, audit, ask("query_data", sql=COUNT), counted)
        events = list(service.run("How many customers?"))
        call, result = of(events, StepEvent)
        assert (call.phase, call.tool, call.arguments) == ("call", "query_data", {"sql": COUNT})
        assert (result.phase, result.outcome, result.row_count) == ("result", "answered", 1)
        (answer,) = of(events, AnswerEvent)
        assert (answer.text, answer.outcome) == ("n=4", "answered")
        assert answer.sql == (COUNT,)
        assert answer.result == {
            "columns": ["n"],
            "rows": [{"n": 4}],
            "row_count": 1,
            "truncated": False,
        }
        assert (answer.steps, answer.tool_calls) == (2, 1)
        # The second turn carried the call and its result back.
        assert [m.role for m in chat.sent] == ["system", "user", "assistant", "tool"]
        assert chat.sent[-1].tool_call_id == "call-query_data"

    def test_an_answer_with_no_tool_is_an_answer(self, app, audit):
        service, _ = assistant(app, audit, say("I can only answer about the data."))
        (answer,) = service.run("Tell me a joke")
        assert answer.text.startswith("I can only") and answer.result is None

    def test_tokens_are_added_up_when_the_model_reports_them(self, app, audit):
        first = ModelTurn(
            tool_calls=(ToolRequest("c", "list_sources"),), input_tokens=100, output_tokens=5
        )
        service, _ = assistant(app, audit, first, say("ok", 150, 7))
        (answer,) = of(service.run("q"), AnswerEvent)
        assert (answer.input_tokens, answer.output_tokens) == (250, 12)

    def test_a_tools_error_goes_back_to_the_model_and_the_run_goes_on(self, app, audit):
        service, chat = assistant(
            app, audit, ask("query_data", sql="DELETE FROM db.customers"), say("I cannot do that.")
        )
        events = list(service.run("Delete the customers"))
        failed = of(events, StepEvent)[1]
        assert failed.outcome == "failed" and "SELECT" in failed.error
        assert "error" in json.loads(chat.results()[0])
        assert of(events, AnswerEvent)[0].sql == ()

    def test_the_statement_kept_is_the_one_that_ran(self, app, audit):
        service, _ = assistant(
            app, audit, ask("query_data", sql=COUNT), ask("query_data", sql=COUNT), say("4")
        )
        (answer,) = of(service.run("q"), AnswerEvent)
        assert answer.sql == (COUNT,)  # once, though it ran twice


class TestWhatTheModelMayCall:
    def test_only_the_tools_that_read(self):
        assert ASSISTANT_TOOLS < set(TOOL_NAMES)
        assert not ASSISTANT_TOOLS & {
            "create_view",
            "refresh_view",
            "load_table",
            "list_views",
            "cache_stats",
            "check_readiness",
        }
        assert {t.name for t in assistant_tools()} == ASSISTANT_TOOLS

    def test_the_term_layers_tools_only_when_it_is_on(self):
        assert {t.name for t in assistant_tools(terms=False)} == ASSISTANT_TOOLS - TERM_TOOLS

    def test_a_spec_is_its_own_copy(self):
        assistant_tools()[1].parameters["properties"].clear()
        assert assistant_tools()[1].parameters["properties"]

    @pytest.mark.parametrize("tool", ["load_table", "create_view", "drop_everything"])
    def test_a_tool_outside_the_list_is_named_back_never_run(self, app, audit, tool):
        service, chat = assistant(
            app, audit, ask(tool, table="db.customers", name="v", sql=COUNT), say("ok")
        )
        events = list(service.run("Load it"))
        refused = of(events, StepEvent)[1]
        assert refused.outcome == "failed" and refused.error.startswith(f"Unknown tool: {tool}")
        assert "query_data" in json.loads(chat.results()[0])["error"]
        assert recorded(audit, "tool_call") == []
        assert of(events, AnswerEvent)[0].tool_calls == 0
        assert app.tools.list_sources()["sources"][0]["tables"][0]["loaded"] is False

    def test_arguments_that_could_not_be_read_run_nothing(self, app, audit):
        garbled = ModelTurn(
            tool_calls=(ToolRequest("c", "query_data", error="The arguments were not valid JSON"),)
        )
        service, chat = assistant(app, audit, garbled, say("ok"))
        events = list(service.run("q"))
        assert of(events, StepEvent)[1].error == "The arguments were not valid JSON"
        assert recorded(audit, "tool_call") == []

    def test_the_model_is_told_the_tables_by_name_and_nothing_of_what_is_in_them(self, app, audit):
        service, chat = assistant(app, audit, say("ok"))
        list(service.run("q"))
        system = chat.sent[0].text
        assert system.endswith("Tables: db.customers, db.notes")
        assert "tc_no" not in system and not any(value in system for value in RAW)
        assert chat.calls[0]["tools"] == service.tool_names

    def test_the_term_hint_is_given_only_with_the_term_tools(self, app, audit):
        with_terms, chat = assistant(app, audit, say("ok"))
        list(with_terms.run("q"))
        assert "answer_question" in chat.sent[0].text
        without, chat = assistant(app, audit, say("ok"), specs=assistant_tools(terms=False))
        list(without.run("q"))
        assert "answer_question" not in chat.sent[0].text
        assert "answer_question" not in chat.calls[0]["tools"]

    def test_no_model_writes_sql_inside_the_assistants_calls(self, app):
        app.tools.write_sql = lambda prompt, system: "SELECT 1"
        bound = app.tools.for_assistant(UNRESTRICTED)
        assert bound.write_sql is None and app.tools.write_sql is not None


class TestTheCallersScope:
    def test_a_tenant_s_model_counts_the_tenants_rows(self, app, audit):
        service, _ = assistant(app, audit, ask("query_data", sql=COUNT), counted)
        (answer,) = of(service.run("How many?", scope=tenant("a")), AnswerEvent)
        assert answer.text == "n=2"
        assert recorded(audit, "data_access")[0]["row_filtered"] is True

    def test_what_is_masked_for_the_caller_is_masked_for_the_model(self, app, audit):
        service, chat = assistant(app, audit, ask("query_data", sql=EMAILS), say("done"))
        events = list(service.run("List them", scope=tenant("a", masked=True)))
        said = json.dumps([m.text for call in chat.calls for m in call["messages"]])
        assert "a***@x.com" in said and not any(value in said for value in RAW)
        (answer,) = of(events, AnswerEvent)
        assert answer.result["masked"] == ["email", "tc_no"]
        assert not any(value in json.dumps(answer.as_dict()) for value in RAW)

    def test_an_exempt_role_reads_raw_values_through_a_model_in_the_process(self, app, audit):
        service, chat = assistant(app, audit, ask("query_data", sql=EMAILS), say("done"))
        list(service.run("List them", scope=DPO))
        assert "alice@x.com" in chat.results()[0]

    def test_a_model_outside_the_process_is_shown_everyone_s_data_masked(self, app, audit):
        """Allowed to read a value is not allowed to send it: the default masks, whoever asks."""
        service, chat = assistant(
            app, audit, ask("query_data", sql=EMAILS), say("done"), mask_everyone=True
        )
        events = list(service.run("List them", scope=DPO))
        said = json.dumps([m.text for call in chat.calls for m in call["messages"]])
        assert "a***@x.com" in said and not any(value in said for value in RAW)
        # The answer's own table is what the model read, so it is masked too.
        (answer,) = of(events, AnswerEvent)
        assert not any(value in json.dumps(answer.as_dict()) for value in RAW)
        # The run is still the DPO's, in their name and roles.
        assert recorded(audit, "assistant_run")[0]["roles"] == ["dpo"]
        assert recorded(audit, "tool_call")[0]["roles"] == ["dpo"]

    def test_default_masks_keep_the_callers_row_rules_and_roles(self):
        forced = tenant("a", roles=("dpo",)).under_default_masks()
        assert forced.masked and forced.roles == ("dpo",)
        assert forced.conditions_for("customers") == tenant("a").conditions_for("customers")
        assert forced.masks_for("db", "customers") == {"email": "partial", "tc_no": "last4"}
        # Left alone, the same caller is exempt.
        assert (
            AccessScope(roles=("dpo",), masking={"db": MASKING}, masked=True).masks_for(
                "db", "customers"
            )
            == {}
        )


class TestItEnds:
    def test_out_of_steps_the_model_is_asked_once_more_for_words(self, app, audit):
        service, chat = assistant(
            app,
            audit,
            ask("list_sources"),
            ask("list_sources"),
            say("From what I read: two tables."),
            limit={"max_steps": 2},
        )
        events = list(service.run("q"))
        (answer,) = of(events, AnswerEvent)
        assert (answer.outcome, answer.steps, answer.tool_calls) == ("exhausted", 3, 2)
        last = chat.calls[-1]
        assert last["allow_tools"] is False
        assert last["messages"][-1].role == "user" and "no tool calls left" in chat.sent[-1].text
        assert recorded(audit, "assistant_run")[0]["outcome"] == "exhausted"

    def test_a_model_that_never_stops_asking_is_stopped(self, app, audit):
        service, chat = assistant(app, audit, ask("list_sources"), limit={"max_steps": 3})
        events = list(service.run("q"))
        assert len(chat.calls) == 4  # three turns, then the one that must be words
        (error,) = of(events, ErrorEvent)
        assert error.code == "model" and "ran out of steps" in error.message
        run = recorded(audit, "assistant_run")[0]
        assert (run["outcome"], run["tool_calls"]) == ("failed", 3)

    @staticmethod
    def clock(*times: float):
        """A clock that reads these times in turn, then stays at the last."""
        left = list(times)
        return lambda: left.pop(0) if len(left) > 1 else left[0]

    def test_each_wait_for_the_model_is_given_what_is_left_of_the_time(self, app, audit):
        service, chat = assistant(
            app,
            audit,
            ask("list_sources"),
            say("in time"),
            limit={"timeout": 10},
            clock=self.clock(0.0, 0.0, 4.0),  # started, first turn, second turn
        )
        assert of(service.run("q"), AnswerEvent)
        assert [call["timeout"] for call in chat.calls] == [10.0, 6.0]

    def test_past_the_deadline_the_model_is_not_asked_again(self, app, audit):
        service, chat = assistant(
            app,
            audit,
            ask("list_sources"),
            say("too late"),
            limit={"timeout": 10},
            clock=self.clock(0.0, 0.0, 11.0),
        )
        events = list(service.run("q"))
        assert len(chat.calls) == 1
        (error,) = of(events, ErrorEvent)
        assert (error.code, error.message) == (
            "timeout",
            "The assistant did not finish within 10 seconds.",
        )
        assert recorded(audit, "assistant_run")[0]["outcome"] == "failed"

    def test_a_model_that_does_not_answer_in_time_is_a_timeout(self, app, audit):
        service, _ = assistant(app, audit, TimeoutError())
        (error,) = service.run("q")
        assert error.code == "timeout"

    @pytest.mark.parametrize(
        ("failure", "said"),
        [
            (
                LLMGenerationError("OpenAI: API quota or rate limit exceeded."),
                "quota or rate limit",
            ),
            (RuntimeError("the loop is closed"), "The model did not answer: the loop is closed"),
        ],
    )
    def test_a_model_that_fails_ends_the_run_with_why(self, app, audit, failure, said):
        service, _ = assistant(app, audit, ask("list_sources"), failure)
        events = list(service.run("q"))
        (error,) = of(events, ErrorEvent)
        assert error.code == "model" and said in error.message
        run = recorded(audit, "assistant_run")[0]
        assert run["outcome"] == "failed" and said in run["reason"]

    def test_no_more_runs_at_once_than_there_are_slots(self, app, audit):
        service, _ = assistant(
            app, audit, ask("list_sources"), say("ok"), limit={"max_concurrent": 1}
        )
        first = service.run("q")
        next(first)  # holds the only slot
        (busy,) = service.run("another")
        assert busy.code == "busy"
        assert recorded(audit, "assistant_run")[0]["outcome"] == "refused"
        list(first)
        assert of(service.run("again"), AnswerEvent)  # the slot came back

    def test_a_caller_that_stops_listening_frees_the_slot_and_is_recorded(self, app, audit):
        service, _ = assistant(
            app, audit, ask("list_sources"), say("ok"), limit={"max_concurrent": 1}
        )
        run = service.run("q")
        next(run)
        run.close()
        stopped = recorded(audit, "assistant_run")[0]
        assert (stopped["outcome"], stopped["reason"]) == ("failed", "stopped before it ended")
        assert of(service.run("again"), AnswerEvent)


class TestTheConversation:
    def exchanges(self, n: int) -> list[Exchange]:
        return [Exchange(f"question {i}", f"answer {i}") for i in range(n)]

    def test_earlier_questions_and_answers_go_first_oldest_first(self, app, audit):
        service, chat = assistant(app, audit, say("ok"))
        list(service.run("and by branch?", history=self.exchanges(2)))
        assert [(m.role, m.text) for m in chat.sent[1:]] == [
            ("user", "question 0"),
            ("assistant", "answer 0"),
            ("user", "question 1"),
            ("assistant", "answer 1"),
            ("user", "and by branch?"),
        ]

    def test_only_the_newest_turns_are_kept(self, app, audit):
        service, chat = assistant(app, audit, say("ok"), limit={"history_turns": 2})
        list(service.run("q", history=self.exchanges(10)))
        assert [m.text for m in chat.sent[1:-1]] == [
            "question 8",
            "answer 8",
            "question 9",
            "answer 9",
        ]

    def test_and_only_as_much_of_them_as_fits(self, app, audit):
        long = [Exchange("q" * 30, "a" * 30) for _ in range(5)]
        service, chat = assistant(app, audit, say("ok"), limit={"history_chars": 130})
        list(service.run("q", history=long))
        assert len(chat.sent) == 2 + 2 * 2  # two exchanges of 60 fit in 130

    def test_no_history_when_none_is_kept(self, app, audit):
        service, chat = assistant(app, audit, say("ok"), limit={"history_turns": 0})
        list(service.run("q", history=self.exchanges(3)))
        assert [m.role for m in chat.sent] == ["system", "user"]

    def test_a_long_question_is_cut(self, app, audit):
        service, chat = assistant(app, audit, say("ok"), limit={"question_chars": 10})
        list(service.run("x" * 50))
        assert chat.sent[-1].text == "x" * 10


class TestTheRecord:
    def test_one_run_and_its_calls_under_one_request(self, app, audit):
        service, _ = assistant(
            app, audit, ask("query_data", sql=COUNT), ask("describe_table", table="db.customers"),
            say("4", 30, 2),
        )  # fmt: skip
        list(service.run("How many?", scope=tenant("a"), conversation="conv-9"))
        (run,) = recorded(audit, "assistant_run")
        calls = recorded(audit, "tool_call")
        assert [c["tool"] for c in calls] == ["query_data", "describe_table"]
        assert {c["via"] for c in calls} == {"assistant"}
        assert {e.as_dict()["request_id"] for e in audit.events} == {"req-1"}
        assert (run["actor"], run["tenant"], run["conversation"]) == ("key-a", "a", "conv-9")
        assert (run["provider"], run["model"], run["steps"], run["tool_calls"]) == (
            "scripted",
            "m1",
            3,
            2,
        )
        assert (run["outcome"], run["input_tokens"], run["output_tokens"]) == ("answered", 30, 2)

    def test_nothing_that_was_read_or_said_is_in_the_trail(self, app, audit):
        service, _ = assistant(
            app, audit, ask("query_data", sql=EMAILS), say("alice@x.com is the first one.")
        )
        list(service.run("Who is alice@x.com?", scope=DPO))
        trail = json.dumps([e.as_dict() for e in audit.events])
        assert "alice@x.com" not in trail and "SELECT" not in trail and "first one" not in trail

    def test_the_question_is_kept_only_where_questions_are(self, app, audit):
        service, _ = assistant(app, audit, say("ok"))
        list(service.run("How many customers?"))
        assert "question" not in recorded(audit, "assistant_run")[0]
        app.tools.keep_questions = True
        list(service.run("How many customers?"))
        assert recorded(audit, "assistant_run")[1]["question"] == "How many customers?"

    def test_a_caller_with_no_request_id_gets_one_for_the_run(self, app, audit):
        service, _ = assistant(app, audit, ask("list_sources"), say("ok"))
        list(service.run("q"))
        ids = {e.as_dict()["request_id"] for e in audit.events}
        assert len(ids) == 1 and len(ids.pop()) == 32

    def test_a_sink_that_fails_does_not_fail_the_run(self, app):
        class Broken:
            def record(self, event) -> None:
                raise OSError("disk full")

        service, _ = assistant(app, Broken(), say("ok"))
        assert of(service.run("q"), AnswerEvent)


class TestClippingAResult:
    def test_one_that_fits_is_given_whole(self):
        result = {"columns": ["n"], "rows": [{"n": 1}], "row_count": 1}
        assert json.loads(clip_result(result, 1000)) == result

    def test_rows_are_dropped_until_it_fits_and_it_says_so(self):
        result = {"columns": ["v"], "rows": [{"v": "x" * 50} for _ in range(100)], "row_count": 100}
        text = clip_result(result, 1500)
        clipped = json.loads(text)
        assert len(text) <= 1500 and 0 < len(clipped["rows"]) < 100
        assert clipped["clipped"].startswith(f"Only the first {len(clipped['rows'])} of 100 rows")

    def test_one_with_no_rows_is_given_by_its_beginning(self):
        text = clip_result({"columns": [{"name": "c" * 40} for _ in range(200)]}, 1000)
        clipped = json.loads(text)
        assert len(text) <= 1000 and clipped["beginning"].startswith('{"columns"')

    def test_the_model_is_given_the_clipped_one_and_the_caller_the_rows(self, app, audit):
        service, chat = assistant(
            app, audit, ask("query_data", sql=EMAILS), say("ok"), limit={"result_chars": 200}
        )
        (answer,) = of(service.run("q"), AnswerEvent)
        assert "clipped" in json.loads(chat.results()[0])
        assert answer.result["row_count"] == 4 and len(answer.result["rows"]) == 4
