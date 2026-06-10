# TA-First OpenClaw Orchestration

This local workflow is designed for a discretionary or semi-systematic trading process where:

1. human TA defines the setup
2. the agent implements that TA in Freqtrade first
3. hyperopt tunes execution around the TA anchors
4. FreqAI is used later as a filter or confidence layer

## Principle

Do not optimize the thesis before the thesis exists in code.

The human TA should define:
- entry zone or trigger
- invalidation
- target zones
- higher-timeframe regime filter
- no-trade conditions

The agent should implement those rules first as deterministic strategy logic.

## What gets optimized

By default, optimization is constrained to execution around the TA anchors:

- stoploss buffer around invalidation
- TP1 / TP2 / TP3 buffers around target levels
- break-even trigger
- trailing trigger and trailing offset
- partial take-profit fractions
- max trade duration / timeout

By default, these are locked:

- entry zone definition
- invalidation source
- target source
- regime filter
- core no-trade logic

## Expected inputs

The orchestration flow can take any mix of:

- TradingView screenshots
- paper-trading or open-trades CSV exports
- Pine Script
- plain-text TA notes

The agent should normalize them into a fixed order map before changing code.

## Fixed order map template

```md
Setup name:
Market: spot | futures
Pairs:
Timeframe:
Direction: long | short | both

Entry thesis:
- Trigger:
- Entry zone:
- Confirmation:

Invalidation:
- Hard invalidation level:
- Invalidation logic:

Targets:
- TP1:
- TP2:
- TP3:

Higher-timeframe context:
- Regime filter:
- Structure requirement:

No-trade conditions:
- 

Execution notes:
- Break-even behavior:
- Trailing behavior:
- Partial exits:
- Timeout:
```

## Recommended implementation pattern

Use two layers:

1. TA layer
   - computes entry anchors
   - computes invalidation anchor
   - computes TP anchors
   - decides whether the setup is valid
2. execution layer
   - applies optimized stoploss / TP buffers
   - applies break-even and trailing
   - applies scale-out and timeout behavior

This keeps hyperopt focused on execution quality instead of curve-fitting the thesis.

## FreqAI role

FreqAI should usually be introduced only after the anchored deterministic strategy works.

Default use:
- filter weak setups
- rank valid setups
- add confidence thresholds

Avoid using FreqAI to replace the human TA thesis unless the goal is explicit thesis discovery.

## OpenClaw skill

The corresponding OpenClaw skill is installed at:

- `/Users/bits/.openclaw/acpx/codex-home/skills/freqtrade-orchestrator`

Important files:

- skill definition: `/Users/bits/.openclaw/acpx/codex-home/skills/freqtrade-orchestrator/SKILL.md`
- operator prompt: `/Users/bits/.openclaw/acpx/codex-home/skills/freqtrade-orchestrator/references/operator-prompt.md`
- TA anchor workflow: `/Users/bits/.openclaw/acpx/codex-home/skills/freqtrade-orchestrator/references/ta-anchor-workflow.md`
- scaffold script: `/Users/bits/.openclaw/acpx/codex-home/skills/freqtrade-orchestrator/scripts/init_freqtrade_run.py`

## Compact scaffold command

Use the scaffold first to keep the agent context and file output small:

```bash
python3 /Users/bits/.openclaw/acpx/codex-home/skills/freqtrade-orchestrator/scripts/init_freqtrade_run.py \
  --title "ETH reclaim setup" \
  --pairs "ETH/USDT" \
  --timeframe 15m \
  --market spot \
  --direction long
```

It generates:

- a compact run folder in `user_data/openclaw_runs/`
- a TA map template to fill
- a strategy stub
- a config stub
- a compose stub

## Stop rule

The orchestration run should end when one of these is true:

- active trade is closed and no valid re-entry appears for 3 candles
- higher-timeframe regime flips against the thesis
- challenger underperforms incumbent for 2 evaluation cycles
- time or compute budget is exhausted
- the user stops the run
