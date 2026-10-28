"""Transforma o JSON de um episodio da loja em um relatorio HTML legivel."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path
from string import Template


STATUS = {
    "confirmed": ("Confirmado", "success", "Estoque reservado e pagamento autorizado"),
    "inventory_unavailable": ("Falha no estoque", "failure", "O serviço de estoque não respondeu normalmente"),
    "stock_unavailable": ("Sem estoque", "failure", "Não havia unidades suficientes"),
    "payment_unavailable": ("Falha no pagamento", "failure", "Serviço de pagamento indisponível; reserva liberada"),
    "compensation_pending": ("Liberação pendente", "pending", "A liberação do estoque ainda não foi confirmada"),
    "pending_reconciliation": ("Conferência pendente", "pending", "A resposta de uma etapa se perdeu; o resultado precisa ser conferido"),
    "pending": ("Em andamento", "pending", "O pedido ainda está em processamento"),
}
TARGET = {"instavel": "Estoque", "carga": "Pagamento", "normal": "Pedidos"}
PATTERN = {"intermittent": "Falha intermitente", "continuous": "Falha contínua", "normal": "Operação normal"}
ACTION = {"disable_simulated_fault": "Desativar a falha intermitente do estoque",
          "restore_inventory_dependency": "Restabelecer a dependência do estoque",
          "release_reservation_lock": "Liberar o bloqueio simulado de reservas",
          "reset_overload": "Restabelecer o pagamento após sobrecarga",
          "restore_dependency": "Restabelecer a dependência do pagamento",
          "reset_circuit_breaker": "Reiniciar o circuito do pagamento", "no_action": "Nenhuma ação"}
STRATEGY = {"rf": "Random Forest", "memory": "Random Forest com memória", "qwen_memory": "Qwen com memória"}
DECISION_SOURCE = {"rf_fallback": "regra padrão", "memory": "memória", "qwen": "Qwen",
                   "qwen_unavailable_or_invalid": "Qwen indisponível ou resposta inválida"}
CAUSE = {"overload": "Sobrecarga no pagamento", "dependency_timeout": "Tempo esgotado no pagamento",
         "dependency_unavailable": "Dependência do estoque indisponível",
         "reservation_locked": "Reserva de estoque bloqueada",
         "circuit_open": "Circuito do pagamento preso em aberto"}
PRODUCT = {"camiseta-basica": "Camiseta básica", "moletom-capuz": "Moletom com capuz"}


def safe(value) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def local_time(iso_value: str) -> str:
    try:
        moment = datetime.fromisoformat(iso_value).astimezone(timezone(timedelta(hours=-3)))
        return moment.strftime("%d/%m/%Y às %H:%M") + " (Brasília)"
    except (ValueError, TypeError):
        return "Horário não informado"


def render_html(report: dict) -> str:
    orders = report.get("orders", [])
    clothing = report.get("shop_type") == "clothing"
    decisions = sorted(report.get("decisions", []), key=lambda item: item.get("timestamp", ""))
    counts = Counter(item.get("status") for item in orders)
    confirmed = counts["confirmed"]
    pending = counts["compensation_pending"] + counts["pending_reconciliation"] + counts["pending"]
    failed = len(orders) - confirmed - pending
    target = report.get("target")
    verified = (any(item.get("target") == target and item.get("verified") for item in decisions) if target
                else {item.get("target") for item in decisions if item.get("verified")} >= {"instavel", "carga"})
    recovered = verified and len(orders) >= 3 and all(item.get("status") == "confirmed" for item in orders[-3:])
    verdict = ("Os serviços voltaram a confirmar pedidos após o reparo." if recovered
               else "O episódio terminou com pedidos que ainda precisam de atenção." if pending
               else "O episódio terminou sem confirmar a recuperação completa dos pedidos.")
    incident_orders = orders[:-3] if len(orders) >= 3 else orders
    check_orders = orders[-3:] if len(orders) >= 3 else []

    def resolution(target: str, failures: int, stage: str) -> str:
        decision = next((item for item in decisions if item.get("target") == target), None)
        opening = f"{stage}: {failures} {'pedido falhou' if failures == 1 else 'pedidos falharam'} nesta etapa. "
        if decision is None:
            return opening + "O agente não registrou uma ação para esse serviço neste episódio."
        pattern = PATTERN.get(decision.get("predicted_type"), "um padrão de falha")
        action = ACTION.get(decision.get("action"), "uma ação de reparo")
        ending = ("A chamada de verificação confirmou que o próprio serviço voltou a responder."
                  if decision.get("verified") else "A chamada de verificação não confirmou a recuperação.")
        return opening + f"O agente identificou {pattern.lower()} e executou: {action.lower()}. " + ending

    stock_failures = counts["inventory_unavailable"] + counts["stock_unavailable"]
    payment_failures = counts["payment_unavailable"] + counts["compensation_pending"]
    check_confirmed = sum(item.get("status") == "confirmed" for item in check_orders)
    story = "".join(f"<li>{safe(sentence)}</li>" for sentence in [
        f"O script ativou as falhas simuladas e enviou {len(incident_orders)} pedidos automaticamente.",
        resolution("instavel", stock_failures, "Estoque"),
        resolution("carga", payment_failures, "Pagamento") +
        (f" A reserva de estoque foi liberada em {counts['payment_unavailable']} pedidos com erro de pagamento."
         if counts["payment_unavailable"] else ""),
        f"Após aguardar as decisões do agente, o script enviou {len(check_orders)} pedidos de verificação; "
        f"{check_confirmed} foram confirmados.",
    ])

    sequence = "".join(
        f'<span class="step {STATUS.get(item.get("status"), ("Outro resultado", "pending", ""))[1]}" '
        f'title="Pedido {safe(item.get("number", index))}: {safe(STATUS.get(item.get("status"), ("Outro resultado", "pending", ""))[0])}" '
        f'aria-label="Pedido {safe(item.get("number", index))}: {safe(STATUS.get(item.get("status"), ("Outro resultado", "pending", ""))[0])}"></span>'
        for index, item in enumerate(orders, 1)
    ) or '<p class="empty">Nenhum pedido foi registrado neste episódio.</p>'

    decision_rows = "".join(
        "<tr>"
        f"<td>{safe(TARGET.get(item.get('target'), item.get('target', 'Serviço desconhecido')))}</td>"
        f"<td>{safe(PATTERN.get(item.get('predicted_type'), item.get('predicted_type', 'Não informado')))}</td>"
        f"<td>{safe(ACTION.get(item.get('action'), item.get('action', 'Não informado')))}</td>"
        f"<td><span class='pill {'success' if item.get('verified') else 'failure'}'>{'Recuperação confirmada' if item.get('verified') else 'Não confirmado'}</span></td>"
        "</tr>"
        for item in decisions
    ) or '<tr><td colspan="4" class="empty">O agente não registrou decisões neste episódio.</td></tr>'

    def item_markup(item: dict) -> str:
        if not clothing:
            return ""
        name = PRODUCT.get(item.get("sku"), item.get("sku") or "Peça não informada")
        size = item.get("size") or "?"
        return f"<td>{safe(name)}<span class='explanation'>Tamanho {safe(size)}</span></td>"

    order_rows = "".join(
        "<tr>"
        f"<td class='number'>{safe(item.get('number', index))}</td>"
        f"{item_markup(item)}"
        f"<td><span class='pill {STATUS.get(item.get('status'), ('', 'pending', ''))[1]}'>{safe(STATUS.get(item.get('status'), ('Outro resultado', 'pending', ''))[0])}</span>"
        f"<span class='explanation'>{safe(STATUS.get(item.get('status'), ('', '', 'Resultado não classificado'))[2])}</span></td>"
        f"<td class='code'>{safe(item.get('request_id') or '—')}</td>"
        "</tr>"
        for index, item in enumerate(orders, 1)
    ) or f'<tr><td colspan="{4 if clothing else 3}" class="empty">Nenhum pedido foi registrado neste episódio.</td></tr>'
    id_note = ("Use o ID de correlação para localizar os eventos do pedido no painel de observabilidade."
               if any(item.get("request_id") for item in orders)
               else "Esta execução anterior não registrou IDs de correlação no relatório. As próximas execuções incluirão esse dado.")

    template = Template("""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>$title</title>
  <style>
    :root { --ink:#173047; --muted:#4d6576; --paper:#f7fafc; --line:#ccd9e2; --blue:#1763a6; --green:#176d4e; --red:#a23d3a; --amber:#91630e; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--paper); color:var(--ink); font:16px/1.5 "Segoe UI", Arial, sans-serif; }
    main { max-width:1040px; margin:auto; padding:40px 24px 72px; }
    h1 { font-size:clamp(2rem,4.3vw,3.2rem); letter-spacing:-.045em; line-height:1.08; margin:0 0 14px; }
    h2 { font-size:1.35rem; letter-spacing:-.02em; margin:0 0 14px; }
    p { margin:0 0 12px; max-width:72ch; }
    .intro { color:var(--muted); font-size:1.05rem; }
    .meta { margin:16px 0 28px; color:var(--muted); display:flex; flex-wrap:wrap; gap:6px 20px; font-size:.9rem; }
    .verdict { border-left:7px solid var(--blue); background:#e9f2f9; padding:22px 26px; margin:28px 0; }
    .verdict.recovered { border-color:var(--green); background:#e9f5ee; }
    .verdict h2 { font-size:clamp(1.45rem,3vw,2.1rem); margin-bottom:7px; }
    .verdict p { margin:0; }
    .totals { display:grid; grid-template-columns:repeat(3,1fr); gap:1px; background:var(--line); border:1px solid var(--line); margin:0 0 32px; }
    .total { background:#fff; padding:17px 20px; }
    .total strong { display:block; font-size:2rem; line-height:1.1; font-variant-numeric:tabular-nums; }
    .total span { color:var(--muted); }
    section { margin-top:34px; }
    .section-note { color:var(--muted); margin-bottom:14px; }
    .flow { margin:0 0 30px; padding:0; list-style:none; display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }
    .flow li { background:#fff; border:1px solid var(--line); padding:14px; font-size:.92rem; }
    .flow strong { display:block; margin-bottom:5px; }
    .story { padding-left:24px; max-width:78ch; }
    .story li { padding-left:5px; margin:0 0 10px; }
    .sequence { display:flex; flex-wrap:wrap; gap:5px; padding:17px; background:#fff; border:1px solid var(--line); }
    .step { display:block; width:24px; height:24px; border-radius:3px; }
    .step.success { background:var(--green); } .step.failure { background:var(--red); } .step.pending { background:var(--amber); }
    .legend { display:flex; flex-wrap:wrap; gap:8px 20px; margin-top:10px; color:var(--muted); font-size:.9rem; }
    .key { display:inline-block; width:11px; height:11px; border-radius:2px; margin-right:6px; }
    .key.success { background:var(--green); } .key.failure { background:var(--red); } .key.pending { background:var(--amber); }
    .table-wrap { overflow-x:auto; background:#fff; border:1px solid var(--line); }
    table { width:100%; border-collapse:collapse; font-size:.94rem; }
    th, td { text-align:left; vertical-align:top; padding:12px 14px; border-bottom:1px solid #e5edf2; }
    th { background:#edf3f7; font-weight:650; }
    tr:last-child td { border-bottom:0; }
    .pill { display:inline-block; padding:2px 8px; border-radius:3px; font-size:.86rem; font-weight:650; white-space:nowrap; }
    .pill.success { color:var(--green); background:#e3f2e8; } .pill.failure { color:var(--red); background:#f9e9e8; } .pill.pending { color:var(--amber); background:#fbf1d8; }
    .explanation { display:block; color:var(--muted); font-size:.86rem; margin-top:4px; }
    .number { font-variant-numeric:tabular-nums; font-weight:650; width:58px; }
    .code { font: .82rem/1.45 Consolas, "Courier New", monospace; overflow-wrap:anywhere; max-width:310px; }
    .empty { padding:22px; color:var(--muted); }
    footer { border-top:1px solid var(--line); margin-top:40px; padding-top:18px; color:var(--muted); font-size:.87rem; }
    @media print { body { background:#fff; } main { padding:0; } .verdict,.totals,.sequence,.table-wrap { print-color-adjust:exact; } tr { break-inside:avoid; } }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>$heading</h1>
      <p class="intro">$intro</p>
      <div class="meta"><span>$date</span><span>$strategy</span><span>$cause</span></div>
    </header>
    <section class="verdict $verdict_class" aria-labelledby="result-title">
      <h2 id="result-title">$headline</h2>
      <p>$verdict</p>
    </section>
    <div class="totals" aria-label="Resumo dos pedidos">
      <div class="total"><strong>$confirmed</strong><span>Confirmados</span></div>
      <div class="total"><strong>$failed</strong><span>Com falha</span></div>
      <div class="total"><strong>$pending</strong><span>Pendentes de conferência</span></div>
    </div>
    $process
    <section aria-labelledby="progress-title">
      <h2 id="progress-title">Pedidos na ordem em que ocorreram</h2>
      <p class="section-note">Cada quadrado representa um pedido. A sequência ajuda a ver quando a operação voltou a funcionar.</p>
      <div class="sequence" role="img" aria-label="Sequência de $total pedidos">$sequence</div>
      <div class="legend"><span><i class="key success"></i>Confirmado</span><span><i class="key failure"></i>Falha</span><span><i class="key pending"></i>Pendente</span></div>
    </section>
    <section aria-labelledby="story-title">
      <h2 id="story-title">Como o episódio aconteceu</h2>
      <ol class="story">$story</ol>
    </section>
    <section aria-labelledby="agent-title">
      <h2 id="agent-title">O que o agente fez</h2>
      <p class="section-note">Uma ação aparece como confirmada apenas quando o próprio serviço voltou a responder.</p>
      <div class="table-wrap"><table id="agent-table"><thead><tr><th>Serviço</th><th>Diagnóstico</th><th>Ação</th><th>Resultado</th></tr></thead><tbody>$decisions</tbody></table></div>
    </section>
    <section aria-labelledby="orders-title">
      <h2 id="orders-title">Resultado de cada pedido</h2>
      <p class="section-note">$id_note</p>
      <div class="table-wrap"><table><thead><tr><th>Nº</th>$item_header<th>Resultado</th><th>ID de correlação</th></tr></thead><tbody>$orders</tbody></table></div>
    </section>
    <footer>Relatório gerado a partir dos dados do episódio. O pagamento é fictício e nenhum valor é cobrado.</footer>
  </main>
</body>
</html>""")
    return template.substitute(
        title="Relatório da loja de roupas" if clothing else "Relatório da loja virtual",
        heading="O que aconteceu com os pedidos de roupas" if clothing else "O que aconteceu com os pedidos",
        intro=("O cliente escolhe uma peça e um tamanho. Serviços separados reservam o estoque e autorizam "
               "um pagamento fictício; o agente observa as falhas e tenta restabelecer o fluxo."
               if clothing else "Um episódio da loja virtual que usa serviços separados para estoque e pagamento. "
               "As falhas são simuladas para mostrar como o agente identifica e repara problemas."),
        process=("<section aria-labelledby='flow-title'><h2 id='flow-title'>Como funciona a compra</h2>"
                 "<ol class='flow'><li><strong>1. Escolha</strong>Peça, tamanho e quantidade.</li>"
                 "<li><strong>2. Reserva</strong>O serviço de estoque separa as unidades.</li>"
                 "<li><strong>3. Pagamento</strong>Um serviço autoriza o valor fictício.</li>"
                 "<li><strong>4. Confirmação</strong>O pedido é confirmado; se o pagamento falha, o sistema tenta liberar a reserva.</li>"
                 "</ol></section>" if clothing else ""),
        item_header="<th>Peça e tamanho</th>" if clothing else "",
        date=safe(local_time(report.get("episode_started_at", ""))),
        strategy=safe("Estratégia: " + STRATEGY.get(report.get("strategy"), str(report.get("strategy", "não informada")))),
        cause=safe("Causa: " + CAUSE.get(report.get("cause"), str(report.get("cause", "não informada")))),
        verdict_class="recovered" if recovered else "",
        confirmed=confirmed, failed=failed, pending=pending, total=len(orders),
        headline=f"{confirmed} {'pedido confirmado' if confirmed == 1 else 'pedidos confirmados'} entre {len(orders)}",
        verdict=safe(verdict),
        sequence=sequence, story=story, decisions=decision_rows, orders=order_rows,
        id_note=safe(id_note),
    )


def render_comparison_html(report: dict) -> str:
    """Mostra os 15 episodios separadamente, com um quadro comparativo no inicio."""
    episodes = report.get("episodes", [])
    strategies = report.get("strategies", ["rf", "memory", "qwen_memory"])
    causes = report.get("causes", list(CAUSE))
    by_pair = {(item.get("strategy"), item.get("cause")): item for item in episodes}

    def outcome(episode: dict) -> tuple[bool, int, int, str]:
        orders = episode.get("orders", [])
        target = episode.get("target")
        decision = next((item for item in episode.get("decisions", [])
                         if item.get("target") == target and item.get("verified")), None)
        check = orders[-3:] if len(orders) >= 3 else []
        recovered = bool(decision and len(check) == 3 and all(item.get("status") == "confirmed" for item in check))
        confirmed = sum(item.get("status") == "confirmed" for item in orders)
        delay = "—"
        if decision:
            try:
                seconds = (datetime.fromisoformat(decision["timestamp"]) -
                           datetime.fromisoformat(episode["episode_started_at"])).total_seconds()
                delay = f"{max(0, seconds):.1f} s"
            except (ValueError, TypeError, KeyError):
                pass
        return recovered, confirmed, len(orders), delay

    matrix_rows = []
    for cause in causes:
        cells = []
        for strategy in strategies:
            episode = by_pair.get((strategy, cause))
            if episode is None:
                cells.append("<td>Não executado</td>")
                continue
            recovered, confirmed, total, delay = outcome(episode)
            label = "Reparado" if recovered else "Não reparado"
            css = "good" if recovered else "bad"
            cells.append(f'<td><strong class="{css}">{label}</strong><small>{confirmed}/{total} pedidos · {delay}</small></td>')
        matrix_rows.append(f"<tr><th scope='row'>{safe(CAUSE.get(cause, cause))}</th>{''.join(cells)}</tr>")

    strategy_sections = []
    for strategy in strategies:
        own = [by_pair[(strategy, cause)] for cause in causes if (strategy, cause) in by_pair]
        successes = sum(outcome(item)[0] for item in own)
        cases = []
        for episode in own:
            cause = episode.get("cause", "")
            target = episode.get("target")
            orders = episode.get("orders", [])
            decisions = sorted((item for item in episode.get("decisions", []) if item.get("target") == target),
                               key=lambda item: item.get("timestamp", ""))
            recovered, confirmed, total, delay = outcome(episode)
            incident = orders[:-3] if len(orders) >= 3 else orders
            incident_ok = sum(item.get("status") == "confirmed" for item in incident)
            checks_ok = sum(item.get("status") == "confirmed" for item in orders[-3:]) if len(orders) >= 3 else 0
            action_text = "; ".join(
                f"{ACTION.get(item.get('action'), item.get('action', 'Nenhuma ação'))} "
                f"(origem: {DECISION_SOURCE.get(item.get('strategy'), item.get('strategy', 'desconhecida'))}; "
                f"{'verificado' if item.get('verified') else 'sem verificação positiva'})"
                for item in decisions
            ) or "O agente não registrou uma decisão para esta causa."
            action_time = None
            if decisions:
                try:
                    action_time = datetime.fromisoformat(decisions[0]["timestamp"])
                except (KeyError, ValueError, TypeError):
                    pass
            marks = []
            action_marked = False
            for index, item in enumerate(orders, 1):
                try:
                    order_time = datetime.fromisoformat(item["timestamp"])
                except (KeyError, ValueError, TypeError):
                    order_time = None
                if action_time and order_time and not action_marked and order_time >= action_time:
                    marks.append('<span class="action-marker" title="Primeira ação do agente">Ação do agente</span>')
                    action_marked = True
                marks.append(
                    f'<span class="mark {"good" if item.get("status") == "confirmed" else "pending" if item.get("status") in ("pending", "pending_reconciliation", "compensation_pending") else "bad"}" '
                    f'title="Pedido {safe(item.get("number", index))}: {safe(STATUS.get(item.get("status"), ("Outro resultado", "", ""))[0])}">'
                    f'{safe(item.get("number", index))}</span>'
                )
            if action_time and not action_marked:
                marks.append('<span class="action-marker" title="Primeira ação do agente">Ação do agente</span>')
            order_marks = "".join(marks)
            badge = "Recuperado" if recovered else "Sem recuperação confirmada"
            cases.append(
                f'<article class="case" id="{safe(strategy)}-{safe(cause)}">'
                f'<div class="case-head"><h3>{safe(CAUSE.get(cause, cause))}</h3><span class="badge {"good" if recovered else "bad"}">{badge}</span></div>'
                f'<p class="episode-meta">{safe(TARGET.get(target, target))} · {confirmed}/{total} pedidos confirmados · tempo até o reparo: {delay}</p>'
                f'<div class="orders" aria-label="Pedidos na ordem em que ocorreram">{order_marks}</div>'
                f'<p class="episode-note">Primeiros {len(incident)} pedidos: {incident_ok} confirmados. '
                f'Pedidos finais de verificação: {checks_ok}/3 confirmados.</p>'
                f'<p><strong>O que aconteceu:</strong> a falha foi ativada no serviço de {safe(TARGET.get(target, target)).lower()}; '
                f'os pedidos acima seguiram na ordem indicada. <strong>Ação do agente:</strong> {safe(action_text)}. '
                f'<strong>Resultado:</strong> {"o reparo foi confirmado no serviço e nos três pedidos finais" if recovered else "a recuperação completa não foi confirmada"}.</p>'
                '</article>'
            )
        strategy_sections.append(
            f'<section class="strategy" id="{safe(strategy)}"><div class="strategy-head">'
            f'<h2>{safe(STRATEGY.get(strategy, strategy))}</h2><p>{successes}/{len(own)} causas recuperadas</p></div>'
            f'{"".join(cases)}</section>'
        )

    table_head = "".join(f"<th scope='col'>{safe(STRATEGY.get(item, item))}</th>" for item in strategies)
    return Template("""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Comparação das estratégias — loja de roupas</title><style>
:root{--ink:#1b3141;--muted:#526675;--line:#ced9df;--paper:#f5f8f9;--good:#17694c;--bad:#a33e3e;--pending:#986912}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.48 "Segoe UI",Arial,sans-serif}
main{max-width:1250px;margin:auto;padding:42px 36px 90px}h1{font-size:2.8rem;line-height:1.08;letter-spacing:-.035em;margin:0 0 16px}
h2{font-size:1.65rem;margin:0}h3{font-size:1.14rem;margin:0}p{max-width:82ch}.intro{font-size:1.12rem;color:var(--muted);margin:0 0 24px}
.method{padding:16px 22px;border-left:5px solid #327b85;background:#e7f0f1;max-width:none;margin:26px 0 36px}
.summary{border-top:2px solid var(--ink);padding-top:18px;margin-bottom:50px}.summary h2{margin-bottom:14px}
table{width:100%;border-collapse:collapse;background:white}th,td{text-align:left;padding:13px 15px;border-bottom:1px solid var(--line);vertical-align:top}
thead th{background:#dce8ec}tbody th{width:27%;font-weight:600}td small{display:block;color:var(--muted);margin-top:3px}
.good{color:var(--good)}.bad{color:var(--bad)}.pending{color:var(--pending)}
.strategy{margin-top:54px}.strategy-head{display:flex;justify-content:space-between;align-items:baseline;border-bottom:3px solid var(--ink);padding-bottom:8px}
.strategy-head p{margin:0;font-weight:700;font-variant-numeric:tabular-nums}.case{background:white;border-bottom:1px solid var(--line);padding:22px 26px}
.case-head{display:flex;justify-content:space-between;gap:20px;align-items:center}.badge{font-size:.84rem;font-weight:700;white-space:nowrap}
.episode-meta{color:var(--muted);font-size:.9rem;margin:5px 0 15px}.orders{display:flex;flex-wrap:wrap;gap:5px;margin:15px 0 8px}
.mark{width:27px;height:27px;border-radius:3px;display:inline-grid;place-items:center;font-size:.75rem;font-weight:700;color:white}
.mark.good{background:var(--good)}.mark.bad{background:var(--bad)}.mark.pending{background:var(--pending)}
.action-marker{border-left:3px solid #327b85;padding:4px 7px;color:#24616b;font-size:.75rem;font-weight:700;align-self:center;white-space:nowrap}
.episode-note{color:var(--muted);font-size:.9rem;margin:0 0 13px}.case p:last-child{margin-bottom:0}
footer{border-top:1px solid var(--line);padding-top:18px;color:var(--muted);font-size:.9rem;margin-top:54px}
@media print{body{background:white}main{padding:0}.case{break-inside:avoid}.strategy{break-before:page}}
</style></head><body><main><header><h1>Como cada estratégia lidou com as falhas</h1>
<p class="intro">Cinco causas na loja de roupas, executadas separadamente com três formas de escolher o reparo.</p>
<p class="method">O Random Forest detecta o padrão da falha nas três versões. O que muda é a escolha do reparo: regra padrão, memória de reparos verificados ou memória seguida de consulta ao Qwen. Cada episódio recebeu a mesma memória inicial e o estoque foi reiniciado. Verde: pedido confirmado; vermelho: falha; amarelo: pendente de conferência.</p></header>
<section class="summary"><h2>Resultado por causa</h2><table><thead><tr><th scope="col">Causa simulada</th>$table_head</tr></thead><tbody>$matrix</tbody></table></section>
$sections
<footer>Comparação de laboratório com pagamento fictício. Cinco causas distintas não substituem repetições estatísticas de cada causa. O Qwen pode não ser consultado quando a memória já conhece o reparo.</footer>
</main></body></html>""").substitute(table_head=table_head, matrix="".join(matrix_rows), sections="".join(strategy_sections))


def write_report(source: Path) -> Path:
    report = json.loads(source.read_text(encoding="utf-8"))
    output = source.with_suffix(".html")
    output.write_text(render_comparison_html(report) if report.get("kind") == "shop_comparison"
                      else render_html(report), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_file", type=Path, help="Arquivo loja_*.json produzido por demo_loja.py")
    args = parser.parse_args()
    output = write_report(args.json_file)
    print(f"Relatório visual: {output}")


if __name__ == "__main__":
    main()
