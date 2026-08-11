# Multi-Agent Pipeline Patterns

## Common Patterns

### Sequential Department Pipeline

The most common pattern: each department processes output from the previous one.

```yaml
workflow:
  name: "Sequential Validation"
  steps:
    - step: 1
      department: qa-department
      task: "Validate AI signal"
    - step: 2
      department: risk-management
      task: "Assess risk"
    - step: 3
      department: ceo-agent
      task: "Final decision"
```

**When to use**: Linear workflows where each step depends on the previous.

### Parallel Department Pipeline

Multiple departments process the same input simultaneously.

```bash
# Run in parallel
hermes chat -p qa-department -q "Validate: [signal]" &
hermes chat -p risk-management -q "Assess: [signal]" &
wait
```

**When to use**: Independent assessments that don't depend on each other.

### Feedback Loop Pipeline

Departments iterate on each other's output.

```bash
# QA validates, Risk adjusts, QA re-validates
hermes chat -p qa-department -q "Validate: [signal]"
hermes chat -p risk-management -q "Adjust: [qa_output]"
hermes chat -p qa-department -q "Re-validate: [risk_output]"
```

**When to use**: When refinement is needed after initial assessment.

## Anti-Patterns

### Monolithic Agent

Using a single agent for all tasks instead of department specialization.

**Why it's bad**: Loses the benefit of role-specific personas and expertise.

### No Timeout Management

Running agents that fetch external data without increasing the terminal timeout.

**Why it's bad**: Pipeline fails mid-execution with exit code 124, even when the structure is correct.

### Hardcoded Profile Names

Embedding profile names directly in workflow YAML instead of parameterizing.

**Why it's bad**: Makes the pipeline non-portable across environments.

### Missing Local Fallbacks

Relying solely on external APIs without local data sources.

**Why it's bad**: Pipeline fails when network is unavailable or slow.

### No Local Data Priority in Personality

Not instructing agents to check local data first in their personality strings.

**Why it's bad**: Even with local data available, the agent will attempt external API calls that cause timeouts. Always include "check local market_data.json first" in the personality string.

### Ignoring HTTP 503 Errors

Treating HTTP 503 (model capacity) as a pipeline configuration error.

**Why it's bad**: The pipeline is correct; only the model is temporarily unavailable. Retry after waiting or switch models instead of debugging the pipeline structure.

## Additional Patterns

### Tavily API Integration Pattern

For pipelines that need news articles or research documents:

1. Add Tavily API key to `.env`:
```bash
echo 'TAVILY_API_KEY=tvly-dev-YOUR_KEY' >> .env
```

2. Configure in Hermes:
```bash
hermes config set tavily.api_key "tvly-dev-YOUR_KEY"
```

3. Instruct agents to use Tavily in the task:
```bash
hermes chat -p qa-department -q "Validate AAPL signal. Use Tavily to find recent news." --timeout 120
```

### Local Data File Pattern

For trading/risk pipelines that need market data:

1. Create `market_data.json` with current prices, beta, volatility
2. Instruct agents via personality to "check local market_data.json first"
3. Run agents with shorter timeouts (60s) since no external calls are needed
