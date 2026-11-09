# PR-Agent on Claude Code (self-hosted)

This fork adds a `claude_code` AI handler: every model call runs `claude -p` (tools disabled, empty
working directory) instead of calling a provider API, so reviews use a Claude Code subscription login.

What changed versus upstream:

| File | Change |
| --- | --- |
| `pr_agent/algo/ai_handlers/claude_code_ai_handler.py` | New handler |
| `pr_agent/agent/pr_agent.py` | `config.ai_handler` selects the handler (default `litellm`, unchanged behaviour) |
| `pr_agent/settings/configuration.toml` | `config.ai_handler` + `[claude_code]` section |
| `pr_agent/config_security.py` | Both are host-only (a repo's `.pr_agent.toml` or a PR comment can't change them) |
| `docker/Dockerfile.claude_code`, `docker/claude_code_healthcheck.sh` | GitHub App image with Claude Code installed |
| `.github/workflows/claude-code-image.yml` | Builds and pushes `ghcr.io/guybnd/pr-agent-claude-code` |
| `tests/unittest/test_claude_code_ai_handler.py` | Tests |

## 1. Test on your PC (PowerShell)

Needs Python 3.12+, `uv`, and Claude Code signed in (`claude` works in a terminal).

```powershell
git clone https://github.com/guybnd/pr-agent
cd pr-agent
uv sync

$env:CONFIG__AI_HANDLER   = "claude_code"
$env:CONFIG__MODEL        = "claude-sonnet-5-5"
$env:CONFIG__FALLBACK_MODELS = '["claude-opus-5-5"]'
$env:CONFIG__AI_TIMEOUT   = "300"
$env:GITHUB__USER_TOKEN   = "<a GitHub PAT with repo access>"
$env:CONFIG__PUBLISH_OUTPUT = "false"   # print to the terminal instead of commenting on the PR

uv run python -m pr_agent.cli --pr_url https://github.com/guybnd/<repo>/pull/<n> review
uv run python -m pr_agent.cli --pr_url https://github.com/guybnd/<repo>/pull/<n> describe
uv run python -m pr_agent.cli --pr_url https://github.com/guybnd/<repo>/pull/<n> improve
```

Happy with the output? Remove `CONFIG__PUBLISH_OUTPUT` to have it post to the PR.

No PR handy? Review a local branch against `main` instead (output goes to `review.md`):

```powershell
cd <some repo with a feature branch checked out>
$env:CONFIG__GIT_PROVIDER = "local"
uv run --project <path to pr-agent> python -m pr_agent.cli --pr_url main review
```

## 2. Claude login for the server

On your PC: `claude setup-token`, approve in the browser, and save the printed token in Bitwarden.
It goes into the container as `CLAUDE_CODE_OAUTH_TOKEN`. Renew it when it expires (the command shows the expiry).

## 3. Image

Every push to `main` builds `ghcr.io/guybnd/pr-agent-claude-code:latest` (Actions tab → "Claude Code image",
or run it manually). The package is private by default: either make it public under
GitHub → Packages → pr-agent-claude-code → Package settings, or `docker login ghcr.io` on Unraid with a PAT
that has `read:packages`.

## 4. Public URL for webhooks

GitHub must reach the container. Easiest: a Cloudflare Tunnel container on Unraid with a public hostname such as
`pr-agent.<your-domain>` → `http://<unraid-ip>:3000`. (Tailscale Funnel or an existing SWAG/NPM proxy also work.)

## 5. GitHub App

GitHub → Settings → Developer settings → GitHub Apps → New GitHub App:

- **Webhook URL:** `https://pr-agent.<your-domain>/api/v1/github_webhooks`
- **Webhook secret:** generate one (`python -c "import secrets; print(secrets.token_hex(20))"`) and keep it.
- **Repository permissions:** Pull requests: Read & write · Issues: Read & write · Contents: Read & write ·
  Metadata: Read-only · Checks: Read & write (optional)
- **Subscribe to events:** Pull request · Issue comment · Pull request review comment · Pull request review
- **Where can it be installed:** Only on this account

Create it, note the **App ID**, and generate + download a **private key** (.pem).

## 6. Container on Unraid

Docker → Add Container:

| Field | Value |
| --- | --- |
| Repository | `ghcr.io/guybnd/pr-agent-claude-code:latest` |
| Port | host `3000` → container `3000` |
| `CLAUDE_CODE_OAUTH_TOKEN` | token from step 2 |
| `GITHUB__APP_ID` | App ID |
| `GITHUB__WEBHOOK_SECRET` | webhook secret |
| `GITHUB__PRIVATE_KEY` | full contents of the .pem, including the BEGIN/END lines |
| `NOTIFY_WEBHOOK_URL` (optional) | Home Assistant webhook for health-check failures |

Defaults baked into the image (override with env vars if you like): `CONFIG__AI_HANDLER=claude_code`,
`CONFIG__MODEL=claude-sonnet-5-5`, `CONFIG__FALLBACK_MODELS=["claude-opus-5-5"]`, `CONFIG__AI_TIMEOUT=300`.

Start it and check the log for gunicorn listening on port 3000.

## 7. Go live

1. Install the GitHub App on one test repo (App page → Install App).
2. Open a small PR. By default PR-Agent runs `/describe`, `/review` and `/improve` on every new PR; comment
   `/ask "..."` (or re-run any command) on the PR for more. To save usage, trim the automatic set with the
   env var `GITHUB_APP__PR_COMMANDS='["/describe", "/review"]'`.
3. Add a `.pr_agent.toml` to repos that need project-specific rules, e.g.

   ```toml
   [pr_reviewer]
   extra_instructions = "Unity project: flag allocations in Update(), missing null checks on serialized fields."
   [pr_code_suggestions]
   extra_instructions = "Prefer small, focused suggestions."
   ```
4. Install the app on your other repos.

## 8. Health check

`docker exec pr-agent claude-code-healthcheck` makes one tiny Haiku call and exits non-zero if the login is
broken (expired token, usage limit). Schedule it daily with the Unraid **User Scripts** plugin:

```sh
#!/bin/bash
docker exec pr-agent claude-code-healthcheck
```

With `NOTIFY_WEBHOOK_URL` set to a Home Assistant webhook (`http://<ha>:8123/api/webhook/<id>`), failures are POSTed
as `{"title": ..., "message": ...}` so an automation can push a phone notification.

## Notes

- Each model call starts a CLI process, so expect a few extra seconds per call; `/improve` makes several.
- `temperature` is ignored and image inputs are unsupported.
- Every review counts against the subscription's Claude Code usage limits.
- Keep the fork in sync with upstream via GitHub's "Sync fork" button; the change is isolated, so conflicts
  should be rare (most likely in `configuration.toml` or `configuration_reference.md`).
