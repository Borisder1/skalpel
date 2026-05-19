"""
SMC Agent v6 — HTML Report Generator
Генерує інтерактивний HTML-звіт з Plotly графіками.
"""
import os
import json
from datetime import datetime
from typing import List, Dict, Any

import numpy as np
import pandas as pd


def _safe_import_plotly():
    try:
        import plotly.graph_objects as go
        import plotly.subplots as sp
        return go, sp
    except ImportError:
        raise ImportError("Встановіть plotly: pip install plotly")


# ===========================================================================
# CHART BUILDERS
# ===========================================================================

def build_equity_chart(equity_curve: List[float], trades, symbol: str):
    go, sp = _safe_import_plotly()

    eq = np.array(equity_curve)
    x = list(range(len(eq)))

    # Drawdown
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / np.maximum(peak, 1.0) * 100

    fig = sp.make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3],
        subplot_titles=[f"Equity Curve — {symbol}", "Drawdown %"],
        vertical_spacing=0.05,
    )

    # Equity
    fig.add_trace(go.Scatter(
        x=x, y=eq.tolist(),
        mode="lines",
        name="Equity",
        line=dict(color="#00d4aa", width=2),
        fill="tozeroy",
        fillcolor="rgba(0,212,170,0.08)",
    ), row=1, col=1)

    # Trade markers
    for t in trades:
        color = "#00d4aa" if t.pnl_cash > 0 else "#ff4d6d"
        symbol_marker = "triangle-up" if t.direction == "Long" else "triangle-down"
        ei = min(t.entry_bar, len(eq) - 1)
        fig.add_trace(go.Scatter(
            x=[ei], y=[eq[ei]],
            mode="markers",
            marker=dict(color=color, size=8, symbol=symbol_marker),
            name=f"{t.direction} {'✓' if t.pnl_cash > 0 else '✗'}",
            showlegend=False,
            hovertext=f"{t.direction} | {t.exit_reason} | {t.pnl_cash:+.2f}$",
        ), row=1, col=1)

    # Drawdown
    fig.add_trace(go.Scatter(
        x=x, y=(-dd).tolist(),
        mode="lines",
        name="Drawdown",
        line=dict(color="#ff4d6d", width=1),
        fill="tozeroy",
        fillcolor="rgba(255,77,109,0.15)",
    ), row=2, col=1)

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0d1117",
        plot_bgcolor="#0d1117",
        font=dict(family="Inter, sans-serif", color="#c9d1d9"),
        height=600,
        showlegend=False,
        margin=dict(l=60, r=20, t=60, b=40),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_pnl_distribution(trades, symbol: str):
    go, _ = _safe_import_plotly()
    if not trades:
        return ""
    pnls = [t.pnl_cash for t in trades]
    colors = ["#00d4aa" if p > 0 else "#ff4d6d" for p in pnls]

    fig = go.Figure(go.Bar(
        x=list(range(len(pnls))),
        y=pnls,
        marker_color=colors,
        name="Trade PnL",
    ))
    fig.update_layout(
        title=f"Trade PnL Distribution — {symbol}",
        template="plotly_dark",
        paper_bgcolor="#0d1117",
        plot_bgcolor="#0d1117",
        font=dict(family="Inter, sans-serif", color="#c9d1d9"),
        height=300,
        margin=dict(l=60, r=20, t=50, b=40),
        xaxis_title="Trade #",
        yaxis_title="PnL ($)",
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_session_chart(stats: dict, symbol: str):
    go, _ = _safe_import_plotly()
    by_session = stats.get("by_session", {})
    if not by_session:
        return ""

    sessions = list(by_session.keys())
    wins = [by_session[s]["wins"] for s in sessions]
    totals = [by_session[s]["n"] for s in sessions]
    pnls = [by_session[s]["pnl"] for s in sessions]
    wr = [w / max(t, 1) * 100 for w, t in zip(wins, totals)]

    fig = go.Figure()
    fig.add_trace(go.Bar(x=sessions, y=totals, name="Trades", marker_color="#4f8ef7"))
    fig.add_trace(go.Bar(x=sessions, y=wins, name="Wins", marker_color="#00d4aa"))
    fig.add_trace(go.Scatter(x=sessions, y=wr, name="Win Rate %",
                              mode="lines+markers", yaxis="y2",
                              line=dict(color="#ffd700", width=2)))
    fig.update_layout(
        title=f"Sessions — {symbol}",
        template="plotly_dark",
        paper_bgcolor="#0d1117",
        plot_bgcolor="#0d1117",
        font=dict(family="Inter, sans-serif", color="#c9d1d9"),
        height=320,
        margin=dict(l=60, r=60, t=50, b=40),
        barmode="group",
        yaxis2=dict(overlaying="y", side="right", title="Win Rate %"),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


# ===========================================================================
# TRADES TABLE
# ===========================================================================

def _trades_table_html(trades) -> str:
    if not trades:
        return "<p style='color:#888'>Немає угод</p>"
    rows = []
    for i, t in enumerate(trades, 1):
        color = "#00d4aa" if t.pnl_cash > 0 else "#ff4d6d"
        entry_t = t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else "—"
        exit_t = t.exit_time.strftime("%Y-%m-%d %H:%M") if t.exit_time else "—"
        rows.append(f"""
        <tr>
          <td>{i}</td>
          <td>{entry_t}</td>
          <td>{exit_t}</td>
          <td style="color:{'#4f8ef7' if t.direction=='Long' else '#f7a04f'}">{t.direction}</td>
          <td>{getattr(t, 'grade', '—')}</td>
          <td>{getattr(t, 'session', '—')}</td>
          <td>{t.entry_price:.4f}</td>
          <td>{t.stop_price:.4f}</td>
          <td>{t.tp1:.4f}</td>
          <td>{t.exit_price:.4f}</td>
          <td style="color:{color}">{t.pnl_cash:+.2f}$</td>
          <td style="color:{color}">{t.pnl_pct:+.2f}%</td>
          <td>{t.exit_reason}</td>
          <td>{t.bars_held}</td>
          <td>{getattr(t, 'score', '—')}</td>
        </tr>""")
    return f"""
    <div style="overflow-x:auto">
    <table class="trades-table">
      <thead>
        <tr>
          <th>#</th><th>Entry Time</th><th>Exit Time</th><th>Dir</th>
          <th>Grade</th><th>Session</th><th>Entry</th><th>Stop</th>
          <th>TP1</th><th>Exit</th><th>PnL $</th><th>PnL %</th>
          <th>Reason</th><th>Bars</th><th>Score</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    </div>"""


# ===========================================================================
# MAIN REPORT GENERATOR
# ===========================================================================

def generate_report(
    results: List[Dict],
    output_path: str = None,
) -> str:
    """
    Генерує HTML-звіт для одного або кількох запусків.

    Args:
        results: список dict з ключами:
                 'symbol', 'period', 'stats', 'trades', 'equity_curve', 'params'
        output_path: шлях для збереження (якщо None — auto)

    Returns:
        Абсолютний шлях до HTML-файлу
    """
    go, _ = _safe_import_plotly()
    import plotly

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "reports",
            f"smc_report_{ts}.html",
        )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Build per-symbol sections
    sections_html = []
    for r in results:
        sym = r.get("symbol", "?")
        period = r.get("period", "?")
        stats = r.get("stats", {})
        trades = r.get("trades", [])
        eq_curve = r.get("equity_curve", [])
        params = r.get("params", {})

        equity_chart = build_equity_chart(eq_curve, trades, sym) if eq_curve else ""
        pnl_chart = build_pnl_distribution(trades, sym)
        session_chart = build_session_chart(stats, sym)
        trades_tbl = _trades_table_html(trades)

        # Stats cards
        def card(label, value, color="#c9d1d9"):
            return f"""<div class="stat-card">
              <div class="stat-label">{label}</div>
              <div class="stat-value" style="color:{color}">{value}</div>
            </div>"""

        pnl_color = "#00d4aa" if stats.get("net_pnl", 0) >= 0 else "#ff4d6d"
        cards = "".join([
            card("Total Trades", stats.get("total_trades", 0)),
            card("Win Rate", f"{stats.get('win_rate', 0):.1f}%",
                 "#00d4aa" if stats.get("win_rate", 0) >= 50 else "#ff4d6d"),
            card("Net PnL", f"{stats.get('net_pnl', 0):+.2f}$", pnl_color),
            card("Net PnL %", f"{stats.get('net_pnl_pct', 0):+.2f}%", pnl_color),
            card("Max DD", f"{stats.get('max_drawdown_pct', 0):.2f}%",
                 "#ffd700" if stats.get("max_drawdown_pct", 0) < 10 else "#ff4d6d"),
            card("Profit Factor", f"{stats.get('profit_factor', 0):.2f}",
                 "#00d4aa" if stats.get("profit_factor", 1) >= 1.5 else "#ffd700"),
            card("Sharpe", f"{stats.get('sharpe', 0):.2f}"),
            card("Avg Win", f"{stats.get('avg_win', 0):+.2f}$", "#00d4aa"),
            card("Avg Loss", f"{stats.get('avg_loss', 0):+.2f}$", "#ff4d6d"),
        ])

        param_rows = "".join(
            f"<tr><td>{k}</td><td>{v}</td></tr>"
            for k, v in params.items()
        )

        sections_html.append(f"""
        <section class="symbol-section">
          <div class="section-header">
            <h2>📊 {sym} &nbsp;·&nbsp; <span class="period-badge">{period} days</span></h2>
          </div>
          <div class="stats-grid">{cards}</div>
          <div class="chart-container">{equity_chart}</div>
          <div class="chart-row">
            <div class="chart-half">{pnl_chart}</div>
            <div class="chart-half">{session_chart}</div>
          </div>
          <details>
            <summary>⚙️ Parameters used</summary>
            <table class="param-table">
              <thead><tr><th>Parameter</th><th>Value</th></tr></thead>
              <tbody>{param_rows}</tbody>
            </table>
          </details>
          <details open>
            <summary>📋 All Trades ({len(trades)})</summary>
            {trades_tbl}
          </details>
        </section>
        """)

    plotly_js = f'<script src="https://cdn.plot.ly/plotly-{plotly.__version__}.min.js"></script>'

    html = f"""<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SMC Agent v6 — Backtest Report</title>
  {plotly_js}
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0d1117; color: #c9d1d9;
      font-family: 'Inter', sans-serif; font-size: 14px;
      min-height: 100vh;
    }}
    .header {{
      background: linear-gradient(135deg, #161b22 0%, #1f2937 100%);
      border-bottom: 1px solid #30363d;
      padding: 24px 40px;
      display: flex; align-items: center; gap: 16px;
    }}
    .header h1 {{ font-size: 22px; font-weight: 700; color: #f0f6fc; }}
    .header .badge {{
      background: linear-gradient(135deg, #00d4aa, #00a8cc);
      color: #0d1117; padding: 3px 10px; border-radius: 12px;
      font-size: 11px; font-weight: 700; letter-spacing: 1px;
    }}
    .timestamp {{ margin-left: auto; color: #6e7681; font-size: 12px; }}
    .container {{ max-width: 1400px; margin: 0 auto; padding: 32px 24px; }}
    .symbol-section {{
      background: #161b22; border: 1px solid #30363d;
      border-radius: 12px; padding: 28px; margin-bottom: 32px;
    }}
    .section-header {{ margin-bottom: 20px; }}
    .section-header h2 {{ font-size: 18px; font-weight: 600; color: #f0f6fc; }}
    .period-badge {{
      background: #1f2937; color: #7dd3fc;
      padding: 2px 8px; border-radius: 8px;
      font-size: 13px; font-weight: 400;
    }}
    .stats-grid {{
      display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 24px;
    }}
    .stat-card {{
      background: #0d1117; border: 1px solid #30363d;
      border-radius: 8px; padding: 14px 18px; min-width: 120px; flex: 1;
    }}
    .stat-label {{ color: #6e7681; font-size: 11px; text-transform: uppercase;
                   letter-spacing: 0.5px; margin-bottom: 6px; }}
    .stat-value {{ font-size: 22px; font-weight: 700; }}
    .chart-container {{ margin-bottom: 20px; border-radius: 8px; overflow: hidden; }}
    .chart-row {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
    .chart-half {{ flex: 1; min-width: 300px; border-radius: 8px; overflow: hidden; }}
    details {{ margin-top: 20px; }}
    summary {{
      cursor: pointer; color: #7dd3fc; font-weight: 600;
      padding: 8px 0; user-select: none;
      list-style: none; display: flex; align-items: center; gap: 8px;
    }}
    summary:hover {{ color: #00d4aa; }}
    .trades-table {{
      width: 100%; border-collapse: collapse; margin-top: 12px;
      font-size: 12px;
    }}
    .trades-table th {{
      background: #0d1117; color: #6e7681; padding: 8px 10px;
      text-align: left; border-bottom: 1px solid #30363d;
      white-space: nowrap;
    }}
    .trades-table td {{
      padding: 7px 10px; border-bottom: 1px solid #21262d;
      white-space: nowrap;
    }}
    .trades-table tr:hover td {{ background: #1f2937; }}
    .param-table {{
      border-collapse: collapse; margin-top: 12px; font-size: 13px;
    }}
    .param-table th, .param-table td {{
      padding: 6px 16px; border: 1px solid #30363d; text-align: left;
    }}
    .param-table th {{ background: #0d1117; color: #6e7681; }}
    @media (max-width: 768px) {{
      .chart-row {{ flex-direction: column; }}
      .stats-grid {{ flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>🤖 SMC Agent v6 — Backtest Report</h1>
    </div>
    <span class="badge">HARDCORE</span>
    <span class="timestamp">Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</span>
  </div>
  <div class="container">
    {''.join(sections_html)}
  </div>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n📄 Report saved: {output_path}")
    return output_path
