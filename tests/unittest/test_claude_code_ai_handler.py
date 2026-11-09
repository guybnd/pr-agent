import asyncio
import json
import stat
import sys
import textwrap

import pytest

from pr_agent.agent.pr_agent import PRAgent, default_ai_handler
from pr_agent.algo.ai_handlers.claude_code_ai_handler import (
    ClaudeCodeAIHandler,
    ClaudeCodeError,
    ClaudeCodeOutputError,
)
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.config_loader import get_settings
from pr_agent.config_security import is_repo_host_only_key

FAKE_CLI = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    state_dir = os.environ["FAKE_CLAUDE_DIR"]
    calls_path = os.path.join(state_dir, "calls.json")
    calls = json.load(open(calls_path)) if os.path.exists(calls_path) else []
    calls.append({{
        "argv": sys.argv[1:],
        "stdin": sys.stdin.read(),
        "cwd": os.getcwd(),
        "has_api_key": "ANTHROPIC_API_KEY" in os.environ,
    }})
    json.dump(calls, open(calls_path, "w"))
    responses = json.load(open(os.path.join(state_dir, "responses.json")))
    out = responses[min(len(calls), len(responses)) - 1]
    sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
    """
)


def _ok(result="review: ok", **extra):
    data = {
        "type": "result", "is_error": False, "result": result, "stop_reason": "end_turn",
        "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 5},
        "duration_ms": 10,
    }
    data.update(extra)
    return data


def _err(status, message="boom"):
    return {"type": "result", "is_error": True, "result": message, "api_error_status": status}


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    cli = tmp_path / "claude"
    cli.write_text(FAKE_CLI.format(python=sys.executable))
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    settings = get_settings()
    saved = {k: settings.get(k) for k in ("claude_code.cli_path", "claude_code.model", "claude_code.use_api_key",
                                          "claude_code.max_retries", "config.ai_handler")}
    settings.set("claude_code.cli_path", str(cli))
    settings.set("claude_code.model", "")
    settings.set("claude_code.use_api_key", False)
    settings.set("claude_code.max_retries", 2)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_: real_sleep(0))

    def set_responses(*responses):
        (tmp_path / "responses.json").write_text(json.dumps(list(responses)))

    def calls():
        path = tmp_path / "calls.json"
        return json.loads(path.read_text()) if path.exists() else []

    yield set_responses, calls
    for key, value in saved.items():
        settings.set(key, value)


def _run(handler, **kwargs):
    args = {"model": "anthropic/claude-sonnet-5-5", "system": "SYS", "user": "USER PROMPT"}
    args.update(kwargs)
    return asyncio.run(handler.chat_completion(**args))


def test_success_runs_text_only_cli(fake_claude):
    set_responses, calls = fake_claude
    set_responses(_ok("the review"))
    resp, finish = _run(ClaudeCodeAIHandler())
    assert (resp, finish) == ("the review", "stop")
    call = calls()[0]
    argv = call["argv"]
    assert argv[0] == "-p"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--system-prompt") + 1].startswith("SYS")
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert call["stdin"] == "USER PROMPT"
    assert call["has_api_key"] is False
    assert "pr-agent-claude-" in call["cwd"]


def test_model_override_and_api_key_opt_in(fake_claude):
    set_responses, calls = fake_claude
    set_responses(_ok())
    get_settings().set("claude_code.model", "opus")
    get_settings().set("claude_code.use_api_key", True)
    _run(ClaudeCodeAIHandler())
    argv = calls()[0]["argv"]
    assert argv[argv.index("--model") + 1] == "opus"
    assert calls()[0]["has_api_key"] is True


def test_max_tokens_stop_maps_to_length(fake_claude):
    set_responses, _ = fake_claude
    set_responses(_ok(stop_reason="max_tokens"))
    assert _run(ClaudeCodeAIHandler())[1] == "length"


def test_auth_error_is_not_retried(fake_claude):
    set_responses, calls = fake_claude
    set_responses(_err(None, "Authentication error"), _ok())
    with pytest.raises(ClaudeCodeError, match="Authentication error"):
        _run(ClaudeCodeAIHandler())
    assert len(calls()) == 1


def test_overload_is_retried(fake_claude):
    set_responses, calls = fake_claude
    set_responses(_err(529, "Overloaded"), _ok("second try"))
    assert _run(ClaudeCodeAIHandler())[0] == "second try"
    assert len(calls()) == 2


def test_retries_are_bounded(fake_claude):
    set_responses, calls = fake_claude
    set_responses(_err(503))
    with pytest.raises(ClaudeCodeError):
        _run(ClaudeCodeAIHandler())
    assert len(calls()) == 3  # first try + max_retries


def test_garbage_output_is_fallback_eligible(fake_claude):
    set_responses, _ = fake_claude
    set_responses("not json")
    with pytest.raises(ClaudeCodeOutputError):
        _run(ClaudeCodeAIHandler())


def test_empty_result_is_fallback_eligible(fake_claude):
    set_responses, _ = fake_claude
    set_responses(_ok("  "))
    with pytest.raises(ClaudeCodeOutputError):
        _run(ClaudeCodeAIHandler())


def test_missing_binary():
    settings = get_settings()
    saved = settings.get("claude_code.cli_path")
    settings.set("claude_code.cli_path", "definitely-not-a-real-claude-binary")
    try:
        with pytest.raises(ClaudeCodeError, match="not found"):
            _run(ClaudeCodeAIHandler())
    finally:
        settings.set("claude_code.cli_path", saved)


def test_ai_handler_setting_selects_backend():
    settings = get_settings()
    saved = settings.get("config.ai_handler")
    try:
        settings.set("config.ai_handler", "claude_code")
        assert default_ai_handler() is ClaudeCodeAIHandler
        assert PRAgent().ai_handler is ClaudeCodeAIHandler
        settings.set("config.ai_handler", "litellm")
        assert PRAgent().ai_handler is LiteLLMAIHandler
        assert PRAgent(ai_handler=ClaudeCodeAIHandler).ai_handler is ClaudeCodeAIHandler
    finally:
        settings.set("config.ai_handler", saved)


@pytest.mark.parametrize("section,key", [
    ("config", "ai_handler"),
    ("claude_code", "cli_path"),
    ("claude_code", "extra_args"),
    ("claude_code", "use_api_key"),
])
def test_backend_settings_are_host_only(section, key):
    assert is_repo_host_only_key(section, key)
