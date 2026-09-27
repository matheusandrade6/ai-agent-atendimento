# Defeitos conhecidos

Defeito encontrado e ainda não corrigido, um por entrada. Cada um tem um cenário da suíte
conversacional que o reproduz, registrado como `known_failure` em
`tests/conversational/placar.yaml` — enquanto o defeito existe, o cenário falha de
propósito; no dia em que for corrigido, a suíte manda promover a linha para `pass`
(`docs/DECISOES.md`, D-34).

Defeito corrigido sai daqui e vira teste verde. Isto não é changelog.

---

## Nenhum defeito aberto

Os dois que a suíte da S11 encontrou — placeholder não resolvido na mensagem de emergência
e escalada que não respondia nada ao cliente — foram corrigidos na S25. O que garante que
eles não voltem são os cenários `emergencia_placeholders` e `pedido_humano_com_aviso`, hoje
`pass` no placar, mais os testes de `tests/unit/test_templates.py`. As decisões que a
correção tomou estão em `docs/DECISOES.md`, D-38 e D-39.
