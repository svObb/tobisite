-- Роль admin_ro для админ-панели: чтение всего, кроме fsm_states.
--
-- Не миграция: у роли пароль, а он живёт в .env сервера, а не в репозитории.
-- Прогон (пароль тот же, что ADMIN_DB_PASSWORD в .env):
--   docker exec -i tobisite-db psql -U tobisite -d tobisite \
--     -v admin_password='…' -f - < admin/sql/admin_ro.sql
-- Скрипт идемпотентен: повторный прогон только переустанавливает пароль и права.
\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'admin_ro') THEN
        CREATE ROLE admin_ro LOGIN;
    END IF;
END
$$;

-- Подстановка psql не заходит внутрь $$-блока, поэтому пароль ставится отдельно
ALTER ROLE admin_ro WITH LOGIN PASSWORD :'admin_password';

GRANT CONNECT ON DATABASE tobisite TO admin_ro;
GRANT USAGE ON SCHEMA public TO admin_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO admin_ro;

-- Таблицы, созданные миграциями позже, тоже должны быть видны панели
ALTER DEFAULT PRIVILEGES FOR ROLE tobisite IN SCHEMA public
    GRANT SELECT ON TABLES TO admin_ro;

-- fsm_states — незаконченные формы работников: панели они не нужны,
-- а чужой черновик там лежит целиком
REVOKE ALL ON TABLE fsm_states FROM admin_ro;
