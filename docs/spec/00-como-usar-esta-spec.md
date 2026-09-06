## 0. Como usar esta spec

Esta spec é escrita para ser fatiada em tarefas de implementação. Ordem sugerida de trabalho:

1. Ler seções 1–3 (contexto e domínio) uma vez.
2. Implementar na ordem do roadmap da **seção 20**, não na ordem do documento.
3. Cada fase do roadmap referencia as seções técnicas que precisam ser lidas naquele momento.
4. Os requisitos são numerados (`RF-xx`, `RNF-xx`). Todo PR deve citar quais requisitos atende.
5. Os critérios de aceite da seção 20 são a definição de pronto de cada fase.

**Regra de ouro do projeto:** o produto é **um único codebase multi-tenant**. Nada específico de um cliente vai para o código — vai para configuração (seção 10) e base de conhecimento (seção 11.6). Se um pedido de cliente exigir mudança de código, ele vira feature genérica com flag, ou não é feito.

---
