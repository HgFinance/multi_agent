---
name: hermes-multi-agent-pipelines
description: "Run multi-agent Hermes pipelines with profiles."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, multi-agent, profiles, pipeline, workflow, timeout, delegation]
    related_skills: [hermes-agent]
---

# Hermes Multi-Agent Pipelines

This skill covers configuring, executing, and debugging multi-agent workflows in Hermes using profile-based departments (e.g., QA department, risk management) that collaborate through cross-profile delegation.

## When to Use This Skill

- You have YAML workflow configuration files defining multi-step agent pipelines
- You need to run agents with different personas/departments in sequence
- You're debugging timeout issues in multi-agent pipelines
- You need to manage profile-based isolation for different agent roles
- You're setting up cross-profile delegation between departments

## Core Concepts

### Profiles as Departments

Each department in a multi-agent pipeline is a Hermes profile with its own:
- Model configuration (provider + model name)
- Environment variables (API keys, secrets)
- Agent personality/personas
- Skills list
- Usage examples

Profiles are listed with:
```bash
hermes profile list
```

### Workflow Configuration

Multi-agent workflows are defined in YAML files that specify:
1. The model to use
2. Sequential steps, each assigned to a department
3. The task for each step
4. Usage examples for single-department and multi-agent modes

Example structure:
```yaml
workflow:
  name: "AI Trading Signal Validation Process"
  steps:
    - step: 1
      department: qa-department
      task: "Validate AI signal hallucinations and logical consistency"
    - step: 2
      department: risk-management
      task: "Assess position risk and portfolio exposure"
    - step: 3
      department: ceo-agent
      task: "Final decision"
```

## Running Pipelines

### Single Department Mode

Run a single department with a specific query:
```bash
hermes chat -p <department-profile> -q "Your query here"
```

### Multi-Agent Collaboration Mode

Run cross-profile delegation where one agent delegates to another:
```bash
hermes chat -p qa-department -q "Validate the signal, then delegate risk assessment to risk-management"
```

### Profile Management

- List profiles: `hermes profile list`
- Create a profile: `hermes profile create <name>`
- Switch active profile: `hermes profile use <name>`

## Timeout Management

### Problem

Multi-agent pipelines that fetch external data (stock prices, API responses, web data) frequently hit the default terminal timeout (60-90 seconds), causing the agent to fail mid-execution even though the pipeline structure is correct.

### Symptoms

- Agent initializes correctly and begins reasoning
- Agent starts fetching external data (curl, API calls, web requests)
- Command times out with exit code 124
- The timeout occurs during data-fetching phase, not during agent initialization

### Solutions

1. **Increase terminal timeout**: When calling `terminal()` or `hermes chat`, set `timeout` to 120+ seconds for agents that make external API calls.

2. **Use local data sources**: Instead of fetching live data, provide data locally (CSV files, pre-downloaded JSON, local databases) to avoid network latency.

3. **Pre-fetch data**: Run data collection as a separate step before the agent pipeline, storing results locally for the agents to consume.

4. **Configure per-profile timeouts**: In the profile's YAML configuration, you can set agent-level timeout parameters.

### Timeout Configuration in YAML

```yaml
agent:
  max_turns: 100
  reasoning_effort: high
  # Add timeout configuration for external data calls
```

## Cross-Profile Delegation

When an agent in one profile needs to delegate to another profile, the task description should explicitly mention the target profile:

```
"Validate the signal, then delegate risk assessment to risk-management"
```

The delegating agent will use `hermes chat -p <target-profile> -q "..."` internally.

## Debugging Pipeline Issues

### Step 1: Verify Profile Configuration

```bash
hermes profile list
```
Ensure all department profiles are listed and active.

### Step 2: Test Individual Departments

Run each department independently to verify it initializes correctly:
```bash
hermes chat -p qa-department -q "Test query"
hermes chat -p risk-management -q "Test query"
```

### Step 3: Check Agent Initialization

If an agent fails to initialize:
- Verify the profile's YAML configuration is valid
- Check that required skills are installed (`hermes skills list`)
- Verify API keys are set in `.env` or environment variables

### Step 4: Check External Data Access

If an agent times out during data fetching:
- Test the external API call independently (e.g., `curl` the endpoint)
- Check network connectivity
- Consider using local data sources instead

### Step 5: Verify Cross-Profile Communication

If delegation between profiles fails:
- Ensure both profiles are properly configured
- Check that the delegating agent has the `hermes-agent` skill loaded
- Verify the target profile name matches exactly

## Pitfalls

- **Default timeout too short**: The terminal tool's default timeout (60-90s) is often insufficient for agents that fetch external data. Always increase it for data-dependent pipelines.
- **Profile name mismatch**: Cross-profile delegation requires exact profile name matching. Verify with `hermes profile list`.
- **Missing skills**: Each profile must have the `hermes-agent` skill loaded for cross-profile delegation to work.
- **API key scope**: Each profile's `.env` must contain the API keys needed for that department's tasks.
- **Network-dependent agents**: Agents that rely on external APIs (stock data, web scraping) will fail if the network is unavailable or slow. Design pipelines with local fallbacks.
- **HTTP 503 from model provider**: When the model returns HTTP 503 (capacity limits), the agent fails mid-execution. This is a transient error — retry after a short wait or switch to a different model. The pipeline structure itself is correct; only the model availability is the issue.
- **Personality doesn't enforce local data**: If the agent personality doesn't explicitly instruct to "check local data first" and "avoid external API calls," the agent will attempt network calls that cause timeouts. Always include local-data-priority instructions in the personality string.
- **Missing timeout_seconds in profile YAML**: While the terminal tool controls the actual timeout, adding `timeout_seconds` to the profile's `agent:` section documents intent and can be read by custom wrapper scripts.

## Handling Model Capacity Errors (HTTP 503)

When the model provider returns HTTP 503 (service temporarily unavailable due to capacity limits), the agent will fail with an error like:

```
API call failed after 3 retries: HTTP 503: The requested model is temporarily unavailable
```

**This is NOT a pipeline configuration error.** The pipeline structure is correct. The model is simply overloaded.

### Solutions

1. **Retry after a short wait** (1-5 minutes) — capacity often clears quickly.
2. **Switch to a different model** — use `hermes config set model.default <model-name>` to switch to a less-loaded model.
3. **Switch provider** — if using Nous/OpenRouter, try switching to OpenAI or Anthropic.
4. **Use a fallback model** — configure a fallback chain in the profile YAML.

### Retry Pattern

```bash
# Retry the same command after waiting
sleep 120 && hermes chat -p qa-department -q "Validate the signal" --timeout 120
```

## Tavily API Integration for News/Article Fetching

Tavily provides a search API useful for fetching news articles, press releases, and financial documents.

### Setup

1. Add the API key to `.env`:
```bash
echo 'TAVILY_API_KEY=tvly-dev-YOUR_KEY_HERE' >> .env
```

2. Configure in Hermes:
```bash
hermes config set tavily.api_key "tvly-dev-YOUR_KEY_HERE"
```

3. Instruct agents to use Tavily for research tasks by mentioning it in the task description.

### Usage in Pipelines

Agents with Tavily access can fetch relevant news and articles as part of validation or risk assessment:

```bash
hermes chat -p qa-department -q "Validate AAPL signal. Use Tavily to find recent news about AAPL that might affect this position." --timeout 120
```

### Profile Configuration

Add Tavily to the profile's environment:
```yaml
env:
  TAVILY_API_KEY: «redacted:tvly-…»
```

## Local Data Source Pattern

When external APIs are slow, unreliable, or rate-limited, create a local data file that agents can read directly.

### Creating a Local Data File

Create a `market_data.json` file with current market information:

```json
{
  "AAPL": {
    "symbol": "AAPL",
    "current_price": 150.00,
    "beta": 1.29,
    "52_week_high": 198.23,
    "52_week_low": 124.17,
    "sector": "Technology",
    "last_updated": "2024-01-15T10:30:00Z"
  }
}
```

### Agent Personality Instructions

In the profile YAML, instruct the agent to use local data first:

```yaml
agent:
  personalities:
    qa-lead: "You are a QA Lead. Always check local market_data.json first before making external API calls. If data is not available locally, note it but do not block on external API calls."
```

### Workflow Configuration

Reference the local data file in the workflow YAML:

```yaml
workflow:
  data_source: "local"
  data_file: "market_data.json"
  steps:
    - step: 1
      department: qa-department
      task: "Validate signal using market_data.json for current prices"
```

## References

- `references/pipeline-patterns.md` — Common multi-agent pipeline patterns and anti-patterns
- `references/timeout-strategies.md` — Detailed timeout management strategies for different pipeline types
- `references/profile-configuration.md` — Profile YAML configuration reference with examples
