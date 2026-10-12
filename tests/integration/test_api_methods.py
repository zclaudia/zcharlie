"""P3c: the offline-shadow replay guard — a saved history that lost its shape
is a 422 domain error at the model boundary, never a 500; and the route's
error mapping for broken requests."""

from uuid import uuid4

import pytest

from investment_agent.api.routes.methods import _frozen_progress_replay
from investment_agent.investor.foundations import InvestorError

KEY = {"X-API-Key": "secret"}


class _Part:
    def __init__(self, content: object, kind: str) -> None:
        self.content = content
        self.part_kind = kind


class _Message:
    def __init__(self, *parts: _Part) -> None:
        self.parts = parts


class _Info:
    class _Tool:
        name = "frozen_progress_tool"

    output_tools = [_Tool()]


def _history(progress: dict[str, object] | None) -> list[_Message]:
    import json

    prompt = json.dumps({"frozen_progress": progress or {"steps": []}})
    return [_Message(_Part("instructions", "system-prompt")), _Message(_Part(prompt, "user-prompt"))]


def test_well_shaped_history_replays_the_frozen_progress():
    progress = {"steps": ["read", "decided"]}
    response = _frozen_progress_replay(_history(progress), _Info())
    (part,) = response.parts
    assert part.tool_name == "frozen_progress_tool"
    assert part.args == progress


@pytest.mark.parametrize(
    "messages",
    [
        [],  # no history at all
        [_Message(_Part("instructions", "system-prompt"))],  # no user prompt
        [_Message(_Part("not json", "user-prompt"))],  # unparsable prompt
        [_Message(_Part('{"other": 1}', "user-prompt"))],  # missing frozen_progress
        [_Message(_Part('["not", "an object"]', "user-prompt"))],  # non-object JSON
        [_Message(_Part(b"\xff\xfe", "user-prompt"))],  # undecodable bytes
    ],
)
def test_broken_history_is_a_domain_error_not_a_crash(messages):
    with pytest.raises(InvestorError) as excinfo:
        _frozen_progress_replay(messages, _Info())
    assert excinfo.value.detail.code == "invalid_reference"


@pytest.mark.asyncio
async def test_offline_shadow_route_maps_domain_errors(api_client):
    """A request the service rejects surfaces as a domain error (409 here), the
    same mapping the guarded replay callback relies on for broken archives."""

    body = {
        "mutation_id": str(uuid4()),
        "proposal_id": str(uuid4()),
        "expected_revision": 1,
        "evaluation_id": str(uuid4()),
    }
    assert api_client.post("/investor/methods/offline-shadow", headers=KEY, json=body).status_code == 409
    assert api_client.post("/investor/methods/offline-shadow", json=body).status_code == 401
