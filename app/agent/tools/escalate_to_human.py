"""Tool de escrita: escala a conversa para um humano (secao 11.4, RF-26).

Por que esta tool nao abre o handoff sozinha
---------------------------------------------
O handler so **sinaliza** a escalada no `ToolResult`; quem abre o `handoff` de verdade —
grava a linha, silencia a conversa e notifica o responsavel — e `app.agent.runner`, depois
que o turno inteiro termina (`app.agent.handoff.HandoffService`). Isso mantem uma unica
porta de entrada para o handoff, que trata do mesmo jeito uma escalada vinda desta tool,
de um gatilho de entrada (`app.agent.guardrails`) ou do disjuntor de custo/mensagens
(secao 11.5) — sem isso, cada caminho reinventaria a abertura, a notificacao e o silencio.

Cobertura da RF-26
------------------
Gatilho de risco ja e pego pelo guardrail de entrada, antes do modelo ser chamado. Esta
tool cobre o resto: pedido explicito que o guardrail nao casou, falha de ferramenta sem
contorno, topico fora do que a base cobre, e insistencia sem avanco.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.tools.base import ToolContext, ToolResult, ToolSpec

__all__ = ["EscalateToHumanArgs", "escalate_to_human_tool"]

NAME = "escalate_to_human"

EscalationCategory = Literal[
    "pedido_explicito", "falha_de_ferramenta", "fora_de_escopo", "baixa_confianca", "outro"
]

_DESCRIPTION = (
    "Escala o atendimento para um humano e faz voce entrar em silencio ate alguem da "
    "equipe assumir ou o prazo configurado passar. Use quando: o cliente pede para falar "
    "com uma pessoa e isso ainda nao foi tratado; uma ferramenta de escrita falhou de um "
    "jeito que voce nao consegue contornar reformulando; a pergunta esta fora do que a "
    "base de conhecimento e o catalogo cobrem; ou voce ja tentou ajudar varias vezes sem "
    "avancar. Depois de chamar esta ferramenta, feche o turno com uma mensagem curta "
    "avisando que alguem da equipe vai continuar — nao prometa horario, preco ou "
    "qualquer coisa que dependa de outra ferramenta."
)

_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": [
                "pedido_explicito",
                "falha_de_ferramenta",
                "fora_de_escopo",
                "baixa_confianca",
                "outro",
            ],
            "description": "Motivo estruturado da escalada.",
        },
        "reason": {
            "type": "string",
            "description": "Explicacao breve, em portugues, do que levou a escalada.",
        },
    },
    "required": ["category", "reason"],
}


class EscalateToHumanArgs(BaseModel):
    category: EscalationCategory
    reason: str = Field(min_length=3, max_length=300)


@dataclass(frozen=True, slots=True)
class _Handler:
    async def __call__(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        category = str(arguments["category"])
        reason = str(arguments["reason"]).strip()
        return ToolResult(
            content={"status": "escalated", "category": category, "reason": reason},
            entity="conversation",
            entity_id=ctx.conversation_id,
        )


def _idempotency_key(ctx: ToolContext, arguments: Mapping[str, Any]) -> str:
    """Uma so escalada por turno, qualquer que seja a categoria ou o motivo.

    Duas chamadas na mesma conversa nao devem abrir dois handoffs — a chave ignora os
    argumentos de proposito (invariante 5).
    """
    return f"{NAME}:{ctx.conversation_id}"


def escalate_to_human_tool() -> ToolSpec:
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=_Handler(),
        kind="write",
        args_model=EscalateToHumanArgs,
        idempotency_key=_idempotency_key,
    )
