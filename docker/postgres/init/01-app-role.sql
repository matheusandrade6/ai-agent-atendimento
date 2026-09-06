-- Papel de aplicacao, sem privilegio (RNF-03).
--
-- Por que isto existe: o usuario criado pela imagem do Postgres (POSTGRES_USER) e
-- SUPERUSER, e superusuario **ignora RLS por completo** — inclusive com
-- FORCE ROW LEVEL SECURITY, que so alcanca o dono da tabela. Se a aplicacao conectasse
-- com ele, toda a politica de isolamento seria decorativa.
--
-- Divisao de papeis:
--   datamind      superusuario e dono das tabelas. Roda migrations. Nunca serve request.
--   datamind_app  NOSUPERUSER, NOBYPASSRLS. E quem a aplicacao e os workers usam.
--
-- Este script roda uma unica vez, na criacao do volume. Em producao o papel e criado
-- pelo provisionamento da infraestrutura, com senha vinda do secret manager.

CREATE ROLE datamind_app WITH
    LOGIN
    PASSWORD 'datamind_app'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOBYPASSRLS;

GRANT USAGE ON SCHEMA public TO datamind_app;

-- As tabelas ainda nao existem neste ponto (as migrations vem depois), entao os GRANTs
-- de tabela ficam na migration 0004. Isto aqui garante que qualquer tabela criada
-- daqui em diante pelo dono ja nasca acessivel ao papel de aplicacao.
ALTER DEFAULT PRIVILEGES FOR ROLE datamind IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO datamind_app;
ALTER DEFAULT PRIVILEGES FOR ROLE datamind IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO datamind_app;
