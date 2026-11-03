-- Runs once, on first start of an empty data directory. Only portfolio-service uses Postgres.
-- Its own role and database; nobody else can connect to it.
CREATE ROLE portfolio_service LOGIN PASSWORD 'cb_dev_portfolio';  -- local-development placeholder
CREATE DATABASE portfolio OWNER portfolio_service;
REVOKE ALL ON DATABASE portfolio FROM PUBLIC;
