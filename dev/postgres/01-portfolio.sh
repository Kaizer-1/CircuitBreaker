#!/usr/bin/env bash
# Runs once, on first start of an empty data directory (the postgres image's /docker-entrypoint-initdb.d).
# Only portfolio-service uses Postgres: its own role and database; nobody else can connect to it.
# The role's password comes from PORTFOLIO_DB_PASSWORD (a Kubernetes Secret on the cluster); without it, the
# local-development placeholder (Docker Compose). Used by Compose and the Helm chart (charts/circuitbreaker/charts/
# postgres links to this file).
set -euo pipefail
password="${PORTFOLIO_DB_PASSWORD:-cb_dev_portfolio}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -v password="$password" <<'SQL'
CREATE ROLE portfolio_service LOGIN PASSWORD :'password';
CREATE DATABASE portfolio OWNER portfolio_service;
REVOKE ALL ON DATABASE portfolio FROM PUBLIC;
SQL
