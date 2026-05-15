"""Claude analyst with web search — decides what to buy, sell, or hold."""
from __future__ import annotations

import json
import re
import time
from typing import Any

import anthropic
import structlog

from app.config import get_settings

log = structlog.get_logger()


def _extract_json(text: str) -> dict:
    """Pull the JSON object from a Claude response, trying from the last '{' first."""
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
    # Try parsing from each `{`, starting from the last — handles text before JSON
    for match in reversed(list(re.finditer(r'\{', text))):
        try:
            candidate = json.loads(text[match.start():])
            if isinstance(candidate, dict) and "actions" in candidate:
                return candidate
        except Exception:
            pass
    return {"actions": [], "market_view": text[:500]}


async def analyze_and_decide(
    portfolio_cash: float,
    positions: list[dict],
    market_snapshot: dict[str, Any],
    settings_dict: dict,
) -> dict:
    """Ask Claude (with web search) to decide what trades to make."""
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

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

    prompt = f"""You are an elite autonomous crypto trading agent. Your goal: maximize returns aggressively.

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
1. Use web_search to research the market RIGHT NOW:
   - Search "crypto market today momentum breakout [current top coins]"
   - Search news on any positions you hold or plan to trade
   - Check for volume anomalies, breakouts, whale activity

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

    _SYSTEM = (
        "You are a JSON-only API endpoint for a crypto trading simulator. "
        "Your entire response must be a single valid JSON object with no text, "
        "explanation, or markdown before or after it."
    )

    t0 = time.time()
    try:
        try:
            response = await client.beta.messages.create(
                model=settings.claude_model,
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
                model=settings.claude_model,
                max_tokens=4096,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt + no_search_note}],
            )

        full_text = "\n".join(
            b.text for b in response.content if hasattr(b, "text") and b.text
        )
        result = _extract_json(full_text)
        result.setdefault("actions", [])
        result.setdefault("market_view", "")
        result.setdefault("cash_advice", {"action": "NONE"})

        log.info("analyst.done", actions=len(result["actions"]), elapsed=round(time.time() - t0, 1))
        return result

    except Exception as e:
        log.exception("analyst.failed", error=str(e))
        return {
            "actions": [],
            "market_view": f"Analysis failed: {e}",
            "cash_advice": {"action": "NONE"},
            "error": str(e),
        }
