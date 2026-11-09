"""Run model calls through a local Claude Code CLI (`claude -p`) instead of a provider API.

This lets a self-hosted PR-Agent use the Claude Code login on the host (for example a
`claude setup-token` OAuth token in CLAUDE_CODE_OAUTH_TOKEN) rather than an API key.
Select it with `config.ai_handler="claude_code"`; the `[claude_code]` section is host-only.
"""

import asyncio
import json
import os
import shutil
import tempfile

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.run_details import record_ai_call
from pr_agent.algo.token_budget import FallbackEligibleError
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

# Statuses worth retrying on the same model: overloaded and server-side errors.
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504, 529})
_PROVIDER_PREFIXES = ("anthropic/", "claude_code/")


class ClaudeCodeError(RuntimeError):
    """The Claude Code CLI could not produce a response (auth, usage limit, missing binary...)."""


class ClaudeCodeOutputError(FallbackEligibleError):
    """The CLI ran but returned something unusable; a fallback model may succeed."""


class ClaudeCodeAIHandler(BaseAiHandler):
    def __init__(self):
        super().__init__()
        settings = get_settings()
        self.cli_path = settings.get("claude_code.cli_path", "claude") or "claude"
        self.model_override = settings.get("claude_code.model", "") or ""
        self.extra_args = list(settings.get("claude_code.extra_args", []) or [])
        self.use_api_key = bool(settings.get("claude_code.use_api_key", False))
        self.max_retries = max(0, min(int(settings.get("claude_code.max_retries", 2)), 5))

    @property
    def deployment_id(self):
        return None

    def _cli_model(self, model: str) -> str:
        if self.model_override:
            return self.model_override
        for prefix in _PROVIDER_PREFIXES:
            if model.startswith(prefix):
                return model[len(prefix):]
        return model

    def _build_command(self, model: str, system: str) -> list[str]:
        binary = shutil.which(self.cli_path)
        if binary is None:
            raise ClaudeCodeError(
                f"Claude Code CLI not found ('{self.cli_path}'). Install it or set claude_code.cli_path."
            )
        return [
            binary, "-p",
            "--output-format", "json",
            "--model", self._cli_model(model),
            "--system-prompt", system,
            "--tools", "",                 # pure text completion: no file, shell or web access
            "--strict-mcp-config",         # ignore any MCP servers configured on the host
            "--disable-slash-commands",
            "--no-session-persistence",
            *self.extra_args,
        ]

    def _build_env(self) -> dict:
        env = dict(os.environ)
        if not self.use_api_key:
            # Claude Code prefers an API key when one is present; drop it so the
            # subscription login (CLAUDE_CODE_OAUTH_TOKEN or ~/.claude) is used.
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    async def _run_once(self, cmd: list[str], user: str, env: dict, timeout: float) -> dict:
        # Run from an empty directory so no CLAUDE.md or project settings leak into the prompt.
        with tempfile.TemporaryDirectory(prefix="pr-agent-claude-") as workdir:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(user.encode("utf-8")), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise
        try:
            return json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            err = stderr.decode("utf-8", errors="replace").strip()[-500:]
            raise ClaudeCodeOutputError(
                f"Claude Code returned non-JSON output (exit {proc.returncode}): {err or stdout[:200]!r}"
            ) from e

    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2, img_path: str = None):
        if img_path:
            get_logger().warning(f"Image input is not supported by ClaudeCodeAIHandler; ignoring {img_path}")
        # Reuse the base payload builder so protected system instructions are still appended.
        messages = self.build_request_messages(model, system, user)
        system_prompt = messages[0]["content"]
        user_prompt = messages[-1]["content"]

        cmd = self._build_command(model, system_prompt)
        env = self._build_env()
        timeout = float(get_settings().get("config.ai_timeout", 120))

        attempt = 0
        while True:
            data = await self._run_once(cmd, user_prompt, env, timeout)
            if not data.get("is_error"):
                break
            status = data.get("api_error_status")
            message = str(data.get("result") or data.get("terminal_reason") or "unknown error")
            if status in _RETRYABLE_STATUSES and attempt < self.max_retries:
                attempt += 1
                get_logger().warning(f"Claude Code API error {status}, retrying ({attempt}/{self.max_retries})")
                await asyncio.sleep(2 ** attempt)
                continue
            raise ClaudeCodeError(f"Claude Code call failed (status {status}): {message}")

        resp = data.get("result")
        if not isinstance(resp, str) or not resp.strip():
            raise ClaudeCodeOutputError("Claude Code returned an empty response")

        usage = data.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        record_ai_call({
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cache_read_input_tokens": usage.get("cache_read_input_tokens") or 0,
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens") or 0,
        })
        stop_reason = data.get("stop_reason")
        finish_reason = "length" if stop_reason == "max_tokens" else "stop"
        get_logger().info("AI response", response=resp, model=self._cli_model(model),
                          finish_reason=finish_reason, duration_ms=data.get("duration_ms"))
        return resp, finish_reason
