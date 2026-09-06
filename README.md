# Datamind Agenda AI

Agente conversacional de atendimento e agendamento, multi-tenant, para prestadores de
servico com hora marcada. WhatsApp Cloud API + Google Calendar.

- Spec: `docs/SPEC.md` (fatiada por secao em `docs/spec/`)
- Plano de trabalho: `SESSIONS.md`
- Regras do projeto: `CLAUDE.md`

## Subir o ambiente

Windows (PowerShell):

```powershell
Copy-Item .env.example .env
.\tasks.ps1 install
.\tasks.ps1 up
.\tasks.ps1 check
```

Linux/macOS:

```bash
cp .env.example .env && make install && make up && make migrate && make test
```

O Postgres sobe em `localhost:5433` e o Redis em `localhost:6380` — portas deslocadas
para nao brigar com instancias locais.
