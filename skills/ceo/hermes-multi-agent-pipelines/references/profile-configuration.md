# Profile YAML Configuration Reference

## Basic Structure

```yaml
# Department Name Configuration
# Description

model:
  provider: nous
  default: poolside/laguna-s-2.1:free

env:
  API_KEY: «redacted:sk-…»

agent:
  max_turns: 100
  reasoning_effort: medium

agent_personalities:
  role-name: "You are a Role specializing in X. Do Y."

skills:
  - hermes-agent
  - other-skill

usage:
  run: "hermes chat -p profile-name -q 'Query'"
  collaborate: "hermes chat -p profile-name -q 'Task, then delegate to other-profile'"
```

## Fields

### model
- `provider`: The LLM provider (e.g., `nous`, `openai`, `anthropic`)
- `default`: The model name (e.g., `poolside/laguna-s-2.1:free`)

### env
Environment variables for the profile. API keys and secrets go here.

### agent
- `max_turns`: Maximum conversation turns before the agent stops
- `reasoning_effort`: `low`, `medium`, or `high` — controls reasoning depth
- `timeout_seconds`: Optional. Documents the intended terminal timeout for this profile. The actual timeout is set by the caller (terminal tool parameter), but this field serves as documentation and can be read by custom wrapper scripts.

### agent_personalities
Defines role-specific system prompts. The key is the role name, the value is the persona description.

**Local Data Priority Instructions**: When agents fetch external data, always include instructions to check local data first:

```yaml
agent:
  personalities:
    qa-lead: "You are a QA Lead. Always check local market_data.json first before making external API calls. If data is not available locally, note it but do not block on external API calls."
```

### skills
List of skills to load for this profile. Always include `hermes-agent` for cross-profile delegation.

### usage
Example commands for running and collaborating with this profile.

## Example: QA Department Profile (with Local Data Priority)

```yaml
# QA Department Agent Configuration
# AI 검증 및 환할 모니터링 부서
# 타임아웃 문제 해결: 로컬 데이터 우선 사용

model:
  provider: nous
  default: poolside/laguna-s-2.1:free

env:
  ANTHROPIC_API_KEY: «redacted:sk-…»

agent:
  max_turns: 100
  reasoning_effort: high
  timeout_seconds: 60

agent_personalities:
  qa-lead: "You are a QA Lead specializing in AI system validation. Detect hallucinations, verify factual accuracy, check logical consistency. Always check local market_data.json first before making external API calls. If data is not available locally, note it but do not block on external API calls."
  qa-analyst: "You are a QA Analyst testing AI outputs. Create test cases, validate responses, document discrepancies. Use local data sources whenever possible."

skills:
  - hermes-agent
  - dogfood

usage:
  run: "hermes chat -p qa-department -q 'Validate this trading signal' --timeout 60"
  collaborate: "hermes chat -p qa-department -q 'Validate the signal, then delegate risk assessment to risk-management' --timeout 60"
```

## Example: Risk Management Profile (with Local Data Priority)

```yaml
# Risk Management Department Configuration
# 실시간 리스크 관리 및 포트폴리오 모니터링 부서
# 타임아웃 문제 해결: 로컬 데이터 우선 사용

model:
  provider: nous
  default: poolside/laguna-s-2.1:free

env:
  OPENAI_API_KEY: «redacted:sk-…»

agent:
  max_turns: 100
  reasoning_effort: medium
  timeout_seconds: 60

agent_personalities:
  risk-manager: "You are a Risk Manager monitoring trading positions. Assess potential losses, evaluate market volatility, make real-time decisions to protect capital. Always use local market_data.json for price and volatility data. Do not make external API calls that could cause timeouts."
  risk-analyst: "You are a Risk Analyst specializing in quantitative risk assessment. Monitor portfolio exposure, calculate VaR, identify risk factors. Use local data files for calculations. Reference market_data.json for beta, volatility, and price information."

skills:
  - hermes-agent

usage:
  run: "hermes chat -p risk-management -q 'Assess risk of AAPL long position' --timeout 60"
  collaborate: "hermes chat -p risk-management -q 'Evaluate portfolio risk with 25% volatility' --timeout 60"
```

## Common Issues

### Profile Not Found

If `hermes chat -p <name>` fails with "profile not found":
- Check `hermes profile list` for exact profile name
- Ensure the profile directory exists under `~/.hermes/profiles/`

### Skills Not Loading

If skills don't load:
- Check `hermes skills list` for installed skills
- Ensure the skill name in the YAML matches exactly
- Verify the skill is not corrupted or pruned

### API Key Not Found

If the agent can't access API keys:
- Check the `env` section in the profile YAML
- Verify the `.env` file at `~/.hermes/.env` or `$HERMES_HOME/.env`
- Ensure keys are not expired
