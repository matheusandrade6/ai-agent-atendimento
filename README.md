# Datamind Agenda AI

Agente conversacional de atendimento e agendamento, multi-tenant, para prestadores de
servico com hora marcada. WhatsApp Cloud API + Google Calendar.

- Spec: `docs/SPEC.md` (fatiada por secao em `docs/spec/`)
- Plano de trabalho: `SESSIONS.md`
- Regras do projeto: `CLAUDE.md`

## Subir o ambiente

```bash
cp .env.example .env
make up
make install
make migrate
make test
```
