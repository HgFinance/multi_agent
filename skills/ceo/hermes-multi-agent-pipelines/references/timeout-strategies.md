# Timeout Strategies for Multi-Agent Pipelines

## Default Timeout Behavior

The Hermes terminal tool has a default timeout of 60-90 seconds. This is sufficient for:
- Simple queries without external data fetching
- Local file operations
- Quick API calls with fast responses

It is NOT sufficient for:
- Stock price lookups via curl/API
- Web scraping or browser automation
- Large file downloads
- Complex multi-step API workflows

## Strategy 1: Increase Terminal Timeout

When running agents that fetch external data, always set a generous timeout:

```python
terminal(command="hermes chat -p qa-department -q 'Validate AAPL signal'", timeout=120)
```

Or in the terminal tool:
```
timeout=120
```

**Recommended timeouts by task type:**
- Stock/financial data lookup: 120s
- Web scraping: 120-180s
- Multiple API calls: 180s
- Browser automation: 180-300s

## Strategy 2: Pre-Fetch Data

Run data collection as a separate step before the agent pipeline:

```bash
# Step 1: Pre-fetch data
curl -s "https://api.example.com/aapl" > /tmp/aapl_data.json

# Step 2: Run agent with local data
hermes chat -p qa-department -q "Validate signal using /tmp/aapl_data.json"
```

**Benefits**: Eliminates network dependency during agent execution.

## Strategy 3: Local Data Sources

Store frequently accessed data locally:

```bash
# Create a local data directory
mkdir -p ./data

# Download reference data once
curl -s "https://api.example.com/historical_aapl" > ./data/aapl_history.json

# Agents read from local files
hermes chat -p risk-management -q "Assess risk using ./data/aapl_history.json"
```

## Strategy 4: Per-Profile Timeout Configuration

In the profile's YAML configuration, set agent-level parameters:

```yaml
agent:
  max_turns: 100
  reasoning_effort: high
  # The terminal timeout is set externally when invoking hermes chat
```

Note: The terminal timeout is controlled by the caller (terminal tool parameter), not by the profile YAML. The profile YAML controls agent-level settings like max_turns and reasoning_effort.

## Diagnosing Timeout Issues

1. **Check the exit code**: Exit code 124 means timeout.
2. **Check the output**: If the agent initialized and started reasoning but failed during data fetching, it's a timeout issue.
3. **Test the data source independently**: Run the same curl/API call outside the agent to verify it works.
4. **Check network connectivity**: Ensure the agent can reach the external API.

## Diagnosing HTTP 503 Errors

When the model provider returns HTTP 503 (capacity limits), the agent fails with:

```
API call failed after 3 retries: HTTP 503: The requested model is temporarily unavailable
```

**This is NOT a timeout issue** — the agent never gets to make external API calls. The model itself is unavailable.

### Solutions

1. **Wait and retry** — capacity often clears within 1-5 minutes.
2. **Switch model** — `hermes config set model.default gpt-4o-mini` or similar.
3. **Switch provider** — if using Nous/OpenRouter, try OpenAI or Anthropic.
4. **Use fallback model** — configure a model fallback chain.

### Retry Pattern

```bash
# Wait 2 minutes, then retry
sleep 120 && hermes chat -p qa-department -q "Validate the signal" --timeout 120
```

## Strategy 5: Local Data File Pattern (Recommended for Trading Pipelines)

For trading/risk management pipelines, create a local `market_data.json` file that agents read directly:

```bash
# Create the data file once
cat > market_data.json << 'EOF'
{
  "AAPL": {
    "current_price": 150.00,
    "beta": 1.29,
    "52_week_high": 198.23,
    "52_week_low": 124.17
  }
}
EOF

# Agents read from local file — no network calls needed
hermes chat -p qa-department -q "Validate AAPL signal using market_data.json" --timeout 60
```

**Benefits**: No network dependency, no external API rate limits, no timeout issues during agent execution.

### Personality Instructions for Local Data Priority

In the profile YAML, add instructions to the personality string:

```yaml
agent:
  personalities:
    qa-lead: "Always check local market_data.json first before making external API calls. If data is not available locally, note it but do not block on external API calls."
```
