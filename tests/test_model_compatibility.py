import json

import httpx
import pytest
from langchain_openai import ChatOpenAI

from app.config import Settings
from app.model_errors import model_failure
from tests.test_app import settings, signed_in


@pytest.mark.parametrize('feature', ['coach', 'routine'])
@pytest.mark.parametrize('model', ['gpt-6-luna', 'gpt-5.6-luna'])
def test_actual_provider_payload_omits_unsupported_sampling_and_keeps_budget(tmp_path, monkeypatch, feature, model):
    """Exercise real LangChain request serialization, stopping at a fake HTTP boundary."""
    from app import coach, routine
    module = coach if feature == 'coach' else routine
    captured = []

    def handler(request):
        payload = json.loads(request.content)
        captured.append((request.url.path, payload))
        return httpx.Response(400, json={'error': {'message': 'Intentional test boundary',
                'type': 'invalid_request_error', 'code': 'test_boundary'}})

    def provider(name, **kwargs):
        kwargs.pop('model_provider')
        return ChatOpenAI(model=name, http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0, **kwargs)

    monkeypatch.setattr(module, 'init_chat_model', provider)
    config = settings(tmp_path, openai_api_key='synthetic-test-key', openai_model=model)
    instance = module._default_factory(config)()
    # Including a tool reproduces the real coaching API selection.
    bound = instance.bind_tools([{'name': 'training_profile', 'description': 'Read profile', 'parameters': {'type': 'object', 'properties': {}}}])
    with pytest.raises(Exception): bound.invoke('Use the profile tool.')
    assert len(captured) == 1
    path, payload = captured[0]
    assert 'temperature' not in payload and 'top_p' not in payload
    assert payload['model'] == model
    assert payload.get('max_output_tokens', payload.get('max_completion_tokens')) == 700
    if model.startswith('gpt-6'):
        assert path.endswith('/responses')  # The installed adapter selects tool-compatible Responses.


@pytest.mark.parametrize(('status', 'code', 'expected'), [
    (400, 'unsupported_parameter', 'model_configuration'),
    (401, 'invalid_api_key', 'authentication'),
    (429, 'insufficient_quota', 'api_budget'),
    (429, 'rate_limit_exceeded', 'rate_limit'),
    (500, '', 'provider_unavailable'),
])
def test_provider_errors_are_actionable_and_do_not_log_raw_payload(caplog, status, code, expected):
    class ProviderError(Exception):
        status_code = status
    exc = ProviderError('synthetic-sensitive-prompt-and-credential')
    exc.code = code
    result = model_failure(exc)
    assert result['error_type'] == expected and result['status'] == 'failed'
    assert 'synthetic-sensitive' not in caplog.text
    assert 'synthetic-sensitive' not in json.dumps(result)


def test_configuration_error_surfaces_specific_message_on_page(tmp_path):
    client, app = signed_in(tmp_path, openai_api_key='synthetic-provider-key')
    class ProviderError(Exception): status_code = 400
    def fail(): raise ProviderError('Private provider details must stay hidden')
    app.state.model_factory = fail
    response = client.post('/coach', data={'question': 'What do I do today?'})
    assert 'rejected an app setting' in response.text
    assert 'Private provider details' not in response.text
    assert app.state.db.latest_coach_reply()['error_type'] == 'model_configuration'


@pytest.mark.parametrize(('zone', 'day'), [('America/Toronto', 'Sunday'), ('Asia/Tokyo', 'Monday')])
def test_today_context_uses_the_app_timezone(tmp_path, monkeypatch, zone, day):
    from datetime import datetime, timezone
    from app import coach
    class Clock:
        @staticmethod
        def now(tz): return datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(coach, 'datetime', Clock)
    _, app = signed_in(tmp_path, app_timezone=zone)
    profile = next(tool for tool in coach._tools(app.state.db, app.state.settings) if tool.name == 'training_profile')
    clock = json.loads(profile.invoke({}))['app_clock']
    assert clock['weekday'] == day and clock['timezone'] == zone
