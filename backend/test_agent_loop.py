import asyncio
import copy
import json
import unittest

from backend import agent_loop


def _chunk(delta, finish_reason=None, usage=None):
    payload = {
        "choices": [{"delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return "data: " + json.dumps(payload)


class _FakeResponse:
    status_code = 200

    def __init__(self, lines):
        self.lines = lines

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self.lines:
            yield line
        yield "data: [DONE]"


class _FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class _FakeClient:
    def __init__(self, responses, requests):
        self.responses = responses
        self.requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url, *, headers, json):
        self.requests.append(copy.deepcopy(json))
        return _FakeStream(_FakeResponse(self.responses.pop(0)))


class AgentLoopTests(unittest.TestCase):
    def test_thinking_react_round_trips_reasoning_with_tool_call(self):
        responses = [
            [
                _chunk({"reasoning_content": "Need the routing guide."}),
                _chunk({
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "reggia_index", "arguments": "{}"},
                    }],
                }, finish_reason="tool_calls"),
            ],
            [
                _chunk({"reasoning_content": "I have the observation."}),
                _chunk({"content": "Final answer."}, finish_reason="stop"),
            ],
        ]
        requests = []
        original_client = agent_loop.httpx.AsyncClient
        original_executor = agent_loop.TOOL_EXECUTORS["reggia_index"]

        async def fake_index():
            return "routing result"

        agent_loop.httpx.AsyncClient = lambda **_kwargs: _FakeClient(responses, requests)
        agent_loop.TOOL_EXECUTORS["reggia_index"] = fake_index
        try:
            async def collect():
                return [event async for event in agent_loop.run(
                    [], "Who am I?", thinking=True, reasoning_effort="max",
                )]

            events = asyncio.run(collect())
        finally:
            agent_loop.httpx.AsyncClient = original_client
            agent_loop.TOOL_EXECUTORS["reggia_index"] = original_executor

        parsed = [json.loads(event[6:]) for event in events]
        self.assertIn("reasoning_delta", [event["type"] for event in parsed])
        self.assertIn("tool_call", [event["type"] for event in parsed])
        self.assertIn("tool_result", [event["type"] for event in parsed])
        self.assertEqual(requests[0]["thinking"], {"type": "enabled"})
        self.assertEqual(requests[0]["reasoning_effort"], "max")

        assistant_tool_message = requests[1]["messages"][-2]
        self.assertEqual(assistant_tool_message["role"], "assistant")
        self.assertEqual(
            assistant_tool_message["reasoning_content"],
            "Need the routing guide.",
        )
        self.assertEqual(requests[1]["messages"][-1]["role"], "tool")

    def test_thinking_can_be_disabled(self):
        responses = [[_chunk({"content": "OK"}, finish_reason="stop")]]
        requests = []
        original_client = agent_loop.httpx.AsyncClient
        agent_loop.httpx.AsyncClient = lambda **_kwargs: _FakeClient(responses, requests)
        try:
            async def collect():
                return [event async for event in agent_loop.run(
                    [], "Reply OK", thinking=False, reasoning_effort="max",
                )]

            asyncio.run(collect())
        finally:
            agent_loop.httpx.AsyncClient = original_client

        self.assertEqual(requests[0]["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", requests[0])


if __name__ == "__main__":
    unittest.main()
