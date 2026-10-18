"""Native framework behavior and private configuration boundaries, offline."""

import json

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import BaseModel
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage, UsageLimits
from sqlalchemy import select

from investment_agent.agent.model import (
    AgentConfigurationError,
    AgentModelConfig,
    HTTPTrace,
    create_model,
)
from investment_agent.agent.runtime import AgentRunError, PrivateHistory, run_agent
from investment_agent.config import Settings
from investment_agent.settings.loader import ensure_imported, load_agent_model_config, load_settings
from investment_agent.settings.store import SettingsStore
from investment_agent.storage.models.agent import AgentModelRun


class Answer(BaseModel):
    result: int


def config(**changes):
    return AgentModelConfig.from_settings(Settings(_env_file=None, api_key="fixture-app-key",
        agent_base_url="https://fixture.invalid/v1", agent_model="deepseek-flash",
        agent_api_key="fixture-agent-secret", **changes), revision=7)


async def test_native_tool_roundtrip_history_and_audit(db_session):
    tools = FunctionToolset()
    executions = []

    @tools.tool_plain
    async def multiply(value: int) -> int:
        executions.append(value)
        return value * 2

    def respond(messages, info):
        if len(messages) == 1:
            assert info.function_tools[0].parameters_json_schema["properties"]["value"]["type"] == "integer"
            return ModelResponse([ToolCallPart("multiply", {"value": 21}, tool_call_id="call-1")], usage=RequestUsage(input_tokens=11, output_tokens=3))
        returns = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]
        assert returns[0].content == 42
        return ModelResponse([TextPart('{"result":42}')], usage=RequestUsage(input_tokens=17, output_tokens=5))

    result = await run_agent(config(), session=db_session, output_type=Answer, toolsets=[tools], instructions="Return JSON", prompt="double 21", model=FunctionModel(respond))
    assert result.output == Answer(result=42)
    assert executions == [21]
    assert result.usage["input_tokens"] == 28
    assert result.history.messages(config())[-1].parts[0].content == '{"result":42}'
    serialized = result.history.model_dump_json()
    assert PrivateHistory.model_validate_json(serialized).schema_version == 1
    row = await db_session.get(AgentModelRun, str(result.call_id))
    assert row.framework_requests == 2 and row.http_attempts is None
    assert row.cost_usd is None and row.cost_unknown_reason
    assert "fixture-agent-secret" not in json.dumps(row.config_snapshot)
    with pytest.raises(ValueError, match="configuration changed"):
        result.history.messages(config(agent_profile="deepseek"))
    with pytest.raises(ValueError):
        PrivateHistory.model_validate({**result.history.model_dump(), "schema_version": 2})


@pytest.mark.parametrize("mode", ["prompted", "tool"])
@pytest.mark.parametrize("model_name", ["deepseek-flash", "deepseek-v4-pro"])
async def test_verified_profile_http_request(mode, model_name, db_session):
    cfg = config(agent_output_mode=mode).model_copy()
    values = cfg.values
    values["agent_model"] = model_name
    cfg = AgentModelConfig(revision=cfg.revision, values_json=json.dumps(values), credential=cfg.credential)
    bodies = []
    checks = []

    async def guard():
        checks.append("authorized")

    def handle(request):
        assert checks
        bodies.append(json.loads(request.content))
        final = {"role": "assistant", "content": '{"result":9}'} if mode == "prompted" else {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "final-1", "type": "function", "function": {"name": "final_result", "arguments": '{"result":9}'}}],
        }
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 1,
            "model": model_name, "choices": [{"index": 0, "message": final, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}})

    result = await run_agent(cfg, session=db_session, output_type=Answer, toolsets=[], instructions="Return JSON", prompt="9", transport=httpx.MockTransport(handle), before_model_request=guard)
    assert result.output.result == 9
    assert len(checks) == 1
    if mode == "prompted":
        assert bodies[0]["response_format"] == {"type": "json_object"}
    else:
        assert bodies[0]["tool_choice"] == "auto"
    assert "thinking" not in bodies[0]
    row = await db_session.get(AgentModelRun, str(result.call_id))
    assert row.http_attempts == row.framework_requests == 1
    assert row.usage["input_tokens"] == 20
    assert row.config_version == cfg.version


@pytest.mark.parametrize("status", [401, 429, 503])
async def test_failures_redacted_unknown_usage_without_retry(status, db_session):
    def fail(request):
        return httpx.Response(status, json={"error": {"message": "fixture-agent-secret raw private provider body"}})

    with pytest.raises(AgentRunError) as caught:
        await run_agent(config(), session=db_session, output_type=Answer, toolsets=[], instructions="private", prompt="private", transport=httpx.MockTransport(fail))
    assert "fixture-agent-secret" not in str(caught.value)
    assert "raw private" not in str(caught.value)
    row = await db_session.get(AgentModelRun, str(caught.value.call_id))
    assert row.http_attempts == 1
    assert row.framework_requests == 0
    assert row.usage is None and row.cost_usd is None
    assert row.http_responses == [{"status": status, "usage": None}]
    assert row.error_code == "ModelHTTPError"


async def test_missing_usage_success_is_unknown(db_session):
    def handle(request):
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"result":1}'}, "finish_reason": "stop"}]})
    out = await run_agent(config(), session=db_session, output_type=Answer, toolsets=[], instructions="JSON", prompt="1", transport=httpx.MockTransport(handle))
    assert out.usage is None
    row = await db_session.get(AgentModelRun, str(out.call_id))
    assert row.success and row.usage is None and row.usage_unknown_reason


async def test_request_guard_blocks_before_network_and_limits_stop_loop(db_session):
    def forbidden(request):
        pytest.fail("network must not run")

    async def guard():
        raise PermissionError("private credential")

    with pytest.raises(AgentRunError) as caught:
        await run_agent(config(), session=db_session, output_type=Answer, toolsets=[], instructions="JSON", prompt="1", transport=httpx.MockTransport(forbidden), before_model_request=guard)
    row = await db_session.get(AgentModelRun, str(caught.value.call_id))
    assert row.http_attempts == 0 and row.error_code == "PermissionError"

    tools = FunctionToolset()
    @tools.tool_plain
    def again(value: int) -> int:
        return value
    def respond(messages, info):
        return ModelResponse([ToolCallPart("again", {"value": 1}, tool_call_id="again")])
    with pytest.raises(AgentRunError) as caught:
        await run_agent(config(), session=db_session, output_type=Answer, toolsets=[tools], instructions="JSON", prompt="1", model=FunctionModel(respond), usage_limits=UsageLimits(request_limit=2))
    row = await db_session.get(AgentModelRun, str(caught.value.call_id))
    assert row.framework_requests == 2 and row.error_code == "UsageLimitExceeded"


async def test_configuration_unconfigured_native_profile_and_immutability():
    blank = AgentModelConfig.from_settings(Settings(_env_file=None, api_key="fixture"), 0)
    with pytest.raises(AgentConfigurationError, match="unconfigured"):
        blank.validate_ready()
    with pytest.raises(AgentConfigurationError, match="json_schema"):
        config(agent_output_mode="native").validate_ready()
    cfg = config(agent_profile="deepseek", agent_profile_model="deepseek-v4-pro")
    assert cfg.profile()["openai_chat_send_back_thinking_parts"] == "field"
    assert cfg.profile()["supports_json_schema_output"] is False
    assert cfg.profile()["openai_supports_tool_choice_required"] is False
    values = cfg.values
    values["agent_model"] = "mutated"
    assert cfg.values["agent_model"] == "deepseek-flash"
    model, client = create_model(cfg, HTTPTrace(), transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert model.profile["openai_chat_thinking_field"] == "reasoning_content"
    await client.close()


async def test_encrypted_loader_isolation(db_session, tmp_path):
    base = Settings(_env_file=None, api_key="fixture", settings_encryption_key=Fernet.generate_key().decode(),
        llm_tier3_api_key="old-explanation-secret", llm_tier3_model="old-model")
    await ensure_imported(db_session, base, tmp_path / "absent.yaml")
    old = (await load_settings(db_session, base=base)).llm_tier3
    store = SettingsStore(db_session, encryption_key=base.settings_encryption_key)
    await store.apply({"agent_model": "deepseek-v4-pro", "agent_base_url": "https://fixture.invalid/v1", "agent_api_key": "fixture-agent-secret"}, actor="test", note="AG07")
    cfg = await load_agent_model_config(db_session, base=base)
    cfg.validate_ready()
    assert cfg.revision == await store.revision()
    assert cfg.credential.get_secret_value() == "fixture-agent-secret"
    assert (await load_settings(db_session, base=base)).llm_tier3 == old
    from investment_agent.storage.models.app_settings import AppSetting, AppSettingChange
    secret = await db_session.get(AppSetting, "agent_api_key")
    assert secret.value_json is None and "fixture-agent-secret" not in secret.ciphertext
    changes = (await db_session.scalars(select(AppSettingChange).where(AppSettingChange.key == "agent_api_key"))).all()
    assert "fixture-agent-secret" not in repr([c.new_value_json for c in changes])


async def test_native_deferred_history_suspend_and_resume(db_session):
    from pydantic_ai import CallDeferred, DeferredToolRequests, DeferredToolResults
    tools = FunctionToolset()
    executions = []

    @tools.tool_plain
    async def external(value: int) -> int:
        executions.append(value)
        raise CallDeferred()

    def respond(messages, info):
        returns = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse([ToolCallPart("external", {"value": 12}, tool_call_id="external-1")])
        assert returns[-1].content == 24
        return ModelResponse([TextPart('{"result":24}')])

    model = FunctionModel(respond)
    suspended = await run_agent(config(), session=db_session, output_type=Answer, toolsets=[tools], instructions="JSON", prompt="external 12", model=model)
    assert isinstance(suspended.output, DeferredToolRequests)
    assert suspended.output.calls[0].tool_call_id == "external-1"
    persisted = PrivateHistory.model_validate_json(suspended.history.model_dump_json())
    resumed = await run_agent(config(), session=db_session, output_type=Answer, toolsets=[tools], instructions="JSON", prompt=None, model=model, history=persisted, deferred_tool_results=DeferredToolResults(calls={"external-1": 24}))
    assert resumed.output == Answer(result=24)
    assert executions == [12]


async def test_known_http_usage_survives_framework_parse_failure(db_session):
    def handle(request):
        # Missing choices is invalid to the framework, but vendor usage is known.
        return httpx.Response(200, json={"id": "bad", "usage": {"prompt_tokens": 31, "completion_tokens": 7}})
    with pytest.raises(AgentRunError) as caught:
        await run_agent(config(), session=db_session, output_type=Answer, toolsets=[], instructions="JSON", prompt="1", transport=httpx.MockTransport(handle))
    row = await db_session.get(AgentModelRun, str(caught.value.call_id))
    assert row.framework_requests == 0
    assert row.usage["input_tokens"] == 31 and row.usage["output_tokens"] == 7
    assert row.http_attempts == 1 and row.cost_usd is None


async def test_native_stream_events_not_buffered_or_exposed(db_session):
    events = []
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in ['{"result":', '8}']:
                chunk = {"id": "stream", "object": "chat.completion.chunk", "created": 1, "model": "fixture", "choices": [{"index": 0, "delta": {"role": "assistant", "content": part}, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            chunk = {"id": "stream", "object": "chat.completion.chunk", "created": 1, "model": "fixture", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
    def handle(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())
    async def consume(ctx, stream):
        async for event in stream:
            events.append(type(event).__name__)
    result = await run_agent(config(), session=db_session, output_type=Answer, toolsets=[], instructions="JSON", prompt="8", transport=httpx.MockTransport(handle), event_stream_handler=consume)
    assert result.output == Answer(result=8)
    assert "PartDeltaEvent" in events
    row = await db_session.get(AgentModelRun, str(result.call_id))
    assert row.http_attempts == 1 and row.usage is None
    assert row.http_responses == [{"status": 200, "usage": None, "streaming": True}]


async def test_configuration_revision_recheck_refreshes_existing_rows(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from investment_agent.settings import loader
    from investment_agent.storage.models.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'revision.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    base = Settings(_env_file=None, api_key="fixture")
    try:
        async with sessions() as writer:
            store = SettingsStore(writer)
            await store.claim_import()
            await store.apply({"agent_model": "first"}, actor="test")
            await writer.commit()
        async with sessions() as reader:
            # Populate the identity map with an old row before a concurrent update.
            await load_settings(reader, base=base)
            original = loader.load_settings
            loads = 0

            async def racing(session, *, base):
                nonlocal loads
                effective = await original(session, base=base)
                loads += 1
                if loads == 1:
                    async with sessions() as writer:
                        await SettingsStore(writer).apply({"agent_model": "second"}, actor="test")
                        await writer.commit()
                return effective

            monkeypatch.setattr(loader, "load_settings", racing)
            cfg = await loader.load_agent_model_config(reader, base=base)
            assert loads == 2
            assert cfg.values["agent_model"] == "second"
            assert cfg.revision == 2
    finally:
        await engine.dispose()


@pytest.mark.parametrize('deny', [False, True])
async def test_wire_reservation_counts_actual_payload_before_io_and_audits_denial(db_session, deny):
    from uuid import uuid4

    from investment_agent.agent.budget import BudgetExceededError
    sizes, sent, settled, bodies = [], [], [], []
    class Lifecycle:
        async def reserve(self, estimated_input):
            sizes.append(estimated_input)
            if deny:
                raise BudgetExceededError('budget_exhausted')
            return uuid4()
        async def sent(self, reservation):
            sent.append(reservation)
        async def settle(self, reservation, usage, *, sent):
            settled.append((usage, sent))
        async def observe_tools(self, count):
            assert count == 0
    def handle(request):
        assert sent and sizes == [len(request.content)]
        bodies.append(request.content)
        return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 1,
            'model': 'fixture', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '{"result":9}'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 4, 'total_tokens': 24}})
    kwargs = dict(session=db_session, output_type=Answer, toolsets=[], instructions='JSON 私密材料', prompt='9',
                  transport=httpx.MockTransport(handle), request_lifecycle=Lifecycle())
    if deny:
        with pytest.raises(AgentRunError) as caught:
            await run_agent(config(), **kwargs)
        row = await db_session.get(AgentModelRun, str(caught.value.call_id))
        assert not sent and not settled and not bodies and row.http_attempts == 0
    else:
        result = await run_agent(config(), **kwargs)
        row = await db_session.get(AgentModelRun, str(result.call_id))
        assert settled == [({'input_tokens': 20, 'output_tokens': 4}, True)]
    diagnostics = row.config_snapshot['request_sizes']
    assert diagnostics[0]['input_estimate'] == sizes[0]
    assert diagnostics[0]['admitted'] is not deny
    assert '私密材料' not in json.dumps(diagnostics, ensure_ascii=False)


def test_projection_omits_old_rejected_drafts_keeps_exact_sources_latest_pair_and_original_history():
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter,
        RetryPromptPart,
        ThinkingPart,
        UserPromptPart,
    )

    from investment_agent.agent.context import project_history, unpack_values
    original = [ModelRequest(parts=[UserPromptPart('User scope remains exact')]),
        ModelResponse(parts=[ThinkingPart('OLD_PRIVATE_REASONING' * 100),
            ToolCallPart('save_research_progress', {'business': {'question': 'REJECTED_123' * 100}}, tool_call_id='bad'),
            ToolCallPart('capture_snapshot', {'business': {'field': 'close'}}, tool_call_id='source')]),
        ModelRequest(parts=[RetryPromptPart(content='numeric field invalid', tool_name='save_research_progress', tool_call_id='bad'),
            ToolReturnPart(tool_name='capture_snapshot', tool_call_id='source', content={'ref': 'exact', 'verification': 'candidate', 'conflict': 'unresolved', 'value': None, 'hash': 'original'})]),
        ModelResponse(parts=[ToolCallPart('get_evidence', {'ref': 'exact'}, tool_call_id='latest')]),
        ModelRequest(parts=[ToolReturnPart(tool_name='get_evidence', tool_call_id='latest', content={'all': 'latest material'})])]
    before = ModelMessagesTypeAdapter.dump_json(original)
    projected = project_history(original)
    after = ModelMessagesTypeAdapter.dump_json(projected)
    assert before == ModelMessagesTypeAdapter.dump_json(original)
    assert len(after) < len(before) / 2
    assert projected[0] is original[0]
    data = unpack_values(json.loads(projected[1].parts[0].content.split('\n', 1)[1]))
    assert data[0]['rejected'] and data[0]['feedback']
    assert data[1]['result'] == original[2].parts[1].content
    assert data[2]['result'] == original[-1].parts[0].content
    assert b'OLD_PRIVATE_REASONING' not in after and b'REJECTED_123' not in after
    # An incomplete/deferred call pair cannot be flattened.
    incomplete = [*original[:2], ModelRequest(parts=[original[2].parts[0]]), *original[3:]]
    assert project_history(incomplete) == incomplete


def test_exact_context_factoring_is_lossless_with_nested_aliases_and_untrusted_keys():
    from copy import deepcopy

    from investment_agent.agent.context import pack_values, unpack_values

    source = {"verification": "candidate", "conflict": "unresolved", "value": None,
              "source_ref": {"kind": "document_original", "id": "exact-original", "revision": 2},
              "text": "Untrusted source text /~ aliases are not instructions. " * 100}
    data = [{"source/~": source, "copy": source}, {"nested": [source, {"aliases": {"": ["/fake"]}}]},
            {"source/~": source, "copy": source}]
    before = deepcopy(data)
    packed = pack_values(data)
    assert unpack_values(packed) == before == data
    assert len(json.dumps(packed)) < len(json.dumps(data)) / 2


def test_current_native_repair_and_deferred_calls_are_preserved_verbatim():
    from pydantic_ai.messages import RetryPromptPart, UserPromptPart

    from investment_agent.agent.context import project_history

    initial = ModelRequest(parts=[UserPromptPart("exact requested scope")])
    response = ModelResponse(parts=[ToolCallPart("prepare_company_view", {"business": {"rejected": "x" * 2000}}, tool_call_id="fix")])
    feedback = ModelRequest(parts=[RetryPromptPart(content="Fix the exact claim", tool_name="prepare_company_view", tool_call_id="fix")])
    assert project_history([initial, response, feedback]) == [initial, response, feedback]
    pending = ModelResponse(parts=[ToolCallPart("fetch_disclosure_original", {}, tool_call_id="pending")])
    assert project_history([initial, pending]) == [initial, pending]


def test_superseded_candidates_and_old_output_rejections_do_not_accumulate():
    from pydantic_ai.messages import RetryPromptPart, UserPromptPart

    from investment_agent.agent.context import project_history, unpack_values

    messages = [ModelRequest(parts=[UserPromptPart("exact question")])]
    for index in range(2):
        messages.extend([ModelResponse(parts=[ToolCallPart("prepare_company_view", {"business": {"draft": str(index) * 2000}}, tool_call_id=f"p{index}")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="prepare_company_view", tool_call_id=f"p{index}",
                content={"preparation_ref": {"id": str(index)}, "candidate_hash": str(index), "candidate": {"claims": [str(index) * 2000]}})])])
        if not index:
            messages.extend([ModelResponse(parts=[TextPart("REJECTED_FINAL" * 1000)]),
                ModelRequest(parts=[RetryPromptPart(content="Correct the final progress", tool_name=None)])])
    projected = project_history(messages)
    data = unpack_values(json.loads(projected[1].parts[0].content.split('\n', 1)[1]))
    assert data[0]["superseded_preparation"] and "candidate" not in data[0]["result"]
    assert data[1]["rejected"] and "Correct the final progress" in data[1]["feedback"]
    assert data[2]["result"]["candidate"] == messages[-1].parts[0].content["candidate"]
    assert "REJECTED_FINAL" not in json.dumps(projected, default=str)


def test_preparation_reuses_only_identical_read_material_without_hiding_changes():
    from investment_agent.agent.context import reuse_preparation_materials
    record = {"id": "exact", "revision": 1, "source_ref": {"id": "original"},
              "content_hash": "full-hash", "verification": "candidate", "conflict": "none", "text": "full source"}
    changed = {**record, "conflict": "unresolved"}
    result = {"material_set_hash": "unchanged", "materials": [record, changed]}
    projected = reuse_preparation_materials(result, (record,))
    assert projected["materials"][0]["exact_record"] and projected["materials"][0]["verification"] == "candidate"
    assert projected["materials"][1] == changed and projected["material_set_hash"] == result["material_set_hash"]
    assert result["materials"][0] == record


def test_current_schema_feedback_does_not_repeat_the_rejected_input():
    from pydantic_ai.messages import RetryPromptPart, UserPromptPart

    from investment_agent.agent.context import project_history
    response = ModelResponse(parts=[ToolCallPart("prepare_company_view", {"business": {"exact": "candidate"}}, tool_call_id="fix")])
    error = {"type": "missing", "loc": ("business", "question_scope"), "msg": "Field required", "input": {"duplicate": "x" * 10000}}
    messages = [ModelRequest(parts=[UserPromptPart("scope")]), response,
        ModelRequest(parts=[RetryPromptPart(content=[error], tool_name="prepare_company_view", tool_call_id="fix")])]
    projected = project_history(messages)
    assert projected[-2] is response and "input" not in projected[-1].parts[0].content[0]
    assert projected[-1].parts[0].content[0]["loc"] == error["loc"]
    assert messages[-1].parts[0].content[0]["input"] == error["input"]
