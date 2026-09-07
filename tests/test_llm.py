from types import SimpleNamespace

from memory_manager.llm import OpenAIJsonClient


class FakeCompletions:
    def __init__(self) -> None:
        self.request: dict[str, object] | None = None

    def create(self, **request: object) -> object:
        self.request = request
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
        )


class FakeClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions())


def test_llm_request_omits_temperature_by_default():
    client = FakeClient()
    adapter = OpenAIJsonClient("gpt-5.6-luna", "dummy", client=client)

    assert adapter.complete_json(system_prompt="system", payload={}) == {"ok": True}
    assert client.chat.completions.request is not None
    assert "temperature" not in client.chat.completions.request


def test_llm_request_can_send_temperature_when_configured():
    client = FakeClient()
    adapter = OpenAIJsonClient("compatible-model", "dummy", temperature=0, client=client)

    adapter.complete_json(system_prompt="system", payload={})

    assert client.chat.completions.request is not None
    assert client.chat.completions.request["temperature"] == 0