"""AI analyst — decides what to buy, sell, or hold.

Supports two providers:
  - anthropic      : Claude via Anthropic SDK with web-search beta
  - openai_compat  : Any OpenAI-compatible API (Ollama, LM Studio, Jan, OpenAI, etc.)
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import anthropic
import httpx
import structlog

from app.config import get_settings

log = structlog.get_logger()

_SYSTEM = (
    "You are a JSON-only API endpoint for a crypto trading simulator. "
    "Your entire response must be a single valid JSON object with no text, "
    "explanation, or markdown before or after it."
)


def _extract_json(text: str) -> dict:
    """Pull the JSON object from an AI response, trying from the last '{' first."""
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Scan from each `{`, last-first — handles preamble text before JSON
    for match in reversed(list(re.finditer(r'\{', text))):
        try:
            candidate = json.loads(text[match.start():])
            if isinstance(candidate, dict) and "actions" in candidate:
                return candidate
        except Exception:
            pass
    return {"actions": [], "market_view": text[:500]}


def _build_prompt(
    portfolio_cash: float,
    positions: list[dict],
    market_snapshot: dict,
    settings_dict: dict,
    has_web_search: bool,
) -> str:
    total_value = portfolio_cash + sum(
        p["quantity"] * p.get("current_price", p["avg_cost"]) for p in positions
    )
    basis = settings_dict.get("total_deposited", settings_dict["initial_capital"])
    pnl = total_value - basis
    pnl_pct = pnl / basis * 100 if basis > 0 else 0

    top_movers = market_snapshot.get("top_movers", [])[:20]
    trending = market_snapshot.get("trending", [])[:7]
    fg = market_snapshot.get("fear_greed", {"value": 50, "classification": "Neutral"})

    positions_text = "\n".join(
        f"  {p['symbol']}: {p['quantity']:.4f} units @ avg ${p['avg_cost']:,.2f} | "
        f"now ${p.get('current_price', p['avg_cost']):,.2f} | "
        f"P&L {((p.get('current_price', p['avg_cost']) - p['avg_cost']) / p['avg_cost'] * 100):+.1f}%"
        for p in positions
    ) or "  None — all cash"

    movers_text = "\n".join(
        f"  {m['symbol']:6} ${m['price']:>12,.4f} | 1h:{m['change_1h']:+5.1f}% | "
        f"24h:{m['change_24h']:+5.1f}% | 7d:{m['change_7d']:+6.1f}% | "
        f"Vol:${m['volume_24h']/1e6:,.0f}M"
        for m in top_movers
    )

    trending_text = ", ".join(f"{t['symbol']}({t['name']})" for t in trending)

    if has_web_search:
        task_section = """1. Use web_search to research the market RIGHT NOW:
   - Search "crypto market today momentum breakout [current top coins]"
   - Search news on any positions you hold or plan to trade
   - Check for volume anomalies, breakouts, whale activity"""
    else:
        task_section = """1. Analyze the live market data provided above:
   - The CoinGecko data includes real-time prices, 1h/24h/7d changes, and volume
   - Focus on strong movers: 1h breakouts, volume spikes, 7d trend continuations
   - Cross-reference Fear & Greed with price action for conviction"""

    return f"""You are an elite autonomous crypto trading agent. Your goal: maximize returns aggressively.

═══ PORTFOLIO STATUS ═══
  Net deposited (basis):  ${basis:,.2f}
  Current cash:           ${portfolio_cash:,.2f}
  Total portfolio value:  ${total_value:,.2f}
  Total P&L:              ${pnl:+,.2f} ({pnl_pct:+.1f}%)
  Weekly target:          {settings_dict['target_weekly_pct']}%

═══ OPEN POSITIONS ═══
{positions_text}

═══ MARKET SENTIMENT ═══
  Fear & Greed Index: {fg['value']}/100 — {fg['classification']}
  (0=Extreme Fear, 100=Extreme Greed | Buy fear, sell greed)

═══ TOP MOVERS RIGHT NOW (CoinGecko live data) ═══
{movers_text}

Trending (most searched): {trending_text}

═══ RISK RULES (strictly enforced by execution engine) ═══
  • Max {settings_dict['max_positions']} simultaneous positions
  • Max {settings_dict['max_position_pct']*100:.0f}% of portfolio per position (≈ ${total_value * settings_dict['max_position_pct']:,.0f})
  • Keep ≥{settings_dict['min_cash_reserve_pct']*100:.0f}% cash reserve (≈ ${total_value * settings_dict['min_cash_reserve_pct']:,.0f})
  • Stop-loss: auto-sell if position down >{settings_dict['stop_loss_pct']*100:.0f}%
  • Take-profit: auto-sell if position up >{settings_dict['take_profit_pct']*100:.0f}%

═══ YOUR TASK ═══
{task_section}

2. STRATEGY PLAYBOOK (use whichever fits):
   - MOMENTUM: coins up 5%+ in 1h on rising volume → buy the breakout
   - REVERSAL: Fear&Greed < 25 → oversold bounce likely → buy dips in quality coins
   - TREND: 7d positive, 24h pullback < 3% → add to rising trend
   - NEWS CATALYST: regulatory approval, mainnet launch, partnership → immediate entry
   - TAKE PROFITS: position up 8%+ → consider partial sell to lock gains
   - ROTATE: underperforming position + better opportunity → sell/rebuy

3. Be decisive. Cash sitting idle earns 0%. Missing a big move is a loss.

4. CASH ADVICE: If you see a strong multi-coin setup but insufficient capital, recommend adding cash.
   If portfolio is up significantly and market looks risky, suggest withdrawing profits.

5. Return ONLY this JSON (no other text before or after):
{{
  "actions": [
    {{"action": "BUY",  "symbol": "SOL", "amount_usd": 350, "reason": "...concise reason..."}},
    {{"action": "SELL", "symbol": "XRP", "quantity": null, "reason": "...sell all..."}},
    {{"action": "HOLD", "symbol": "ETH", "reason": "..."}}
  ],
  "market_view": "2-3 sentence market summary with your conviction level",
  "cash_advice": {{
    "action": "ADD",
    "amount": 200,
    "reason": "Strong setup forming in BTC and SOL — more capital = more profit potential"
  }}
}}

cash_advice.action must be "ADD", "WITHDRAW", or "NONE".
Only include HOLD if you want to record it. Only include cash_advice if conviction is strong."""


async def _call_anthropic(api_key: str, model: str, prompt: str) -> dict:
    client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        response = await client.beta.messages.create(
            model=model,
            max_tokens=4096,
            system=_SYSTEM,
            betas=["web-search-2025-03-05"],
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        no_search_note = (
            "\n\nIMPORTANT: Web search is unavailable. "
            "Analyze the live market data provided above and return ONLY the JSON object."
        )
        response = await client.messages.create(
            model=model,
            max_tokens=4096,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt + no_search_note}],
        )
    full_text = "\n".join(b.text for b in response.content if hasattr(b, "text") and b.text)
    return full_text


async def _call_openai_compat(api_key: str, base_url: str, model: str, prompt: str) -> str:
    url = (base_url or "http://localhost:11434/v1").rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model or "llama3.2",
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        # max_completion_tokens: required by Groq reasoning models (openai/gpt-oss-*)
        # max_tokens: required by Ollama, LM Studio, and most other providers
        # Sending both is safe — providers use whichever they understand
        "max_completion_tokens": 4096,
        "max_tokens": 4096,
        # temperature omitted: reasoning models (Groq, OpenAI o-series) reject values != 1
        # and most providers default to something sensible without it
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def analyze_and_decide(
    portfolio_cash: float,
    positions: list[dict],
    market_snapshot: dict[str, Any],
    settings_dict: dict,
    ai_config: dict | None = None,
) -> dict:
    """Decide what trades to make using the configured AI provider."""
    cfg = ai_config or {}
    app_settings = get_settings()

    provider = cfg.get("provider") or "anthropic"
    api_key = cfg.get("api_key") or app_settings.anthropic_api_key
    base_url = cfg.get("base_url") or ""
    model = cfg.get("model_name") or app_settings.claude_model

    has_web_search = provider == "anthropic"
    prompt = _build_prompt(portfolio_cash, positions, market_snapshot, settings_dict, has_web_search)

    t0 = time.time()
    try:
        if provider == "anthropic":
            full_text = await _call_anthropic(api_key, model, prompt)
        else:
            full_text = await _call_openai_compat(api_key, base_url, model, prompt)

        result = _extract_json(full_text)
        result.setdefault("actions", [])
        result.setdefault("market_view", "")
        result.setdefault("cash_advice", {"action": "NONE"})

        log.info("analyst.done", provider=provider, model=model,
                 actions=len(result["actions"]), elapsed=round(time.time() - t0, 1))
        return result

    except Exception as e:
        log.exception("analyst.failed", provider=provider, error=str(e))
        return {
            "actions": [],
            "market_view": f"Analysis failed ({provider}): {e}",
            "cash_advice": {"action": "NONE"},
            "error": str(e),
        }
