"""Configuration from environment variables (see .env.example)."""

from pydantic import SecretStr

from sre_agent.runtime.settings import BaseServiceSettings

PLACEHOLDER = "PLACEHOLDER-filled-in-phase-7"  # what the sealed secrets held before Phase 7


class Settings(BaseServiceSettings):
    service_name: str = "sre-agent"

    kafka_bootstrap_servers: str = "localhost:19092"

    cassandra_contact_points: str = "localhost"  # comma-separated
    cassandra_port: int = 9042
    cassandra_keyspace: str = "sre_agent"
    cassandra_username: str = "sre_agent_role"
    cassandra_password: SecretStr  # no default: a secret never lives in code

    # What the agent reads. No Prometheus URL (Docker Compose): the loop does not run, the API does.
    prometheus_url: str = ""
    loki_url: str = ""

    # The model (SRE agent design section 4). No key (or the Phase 6 placeholder): rules only.
    openrouter_api_key: SecretStr | None = None
    openrouter_url: str = "https://openrouter.ai/api/v1"
    sre_agent_model: str = "anthropic/claude-sonnet-5.5"
    sre_agent_fallback_models: str = ""  # comma-separated, tried by OpenRouter in order

    # The rollback's Git step (ADR-0016). Without the Argo CD token (make k8s-up, CI's slim profile)
    # there is nothing to reconcile; without the App, a bad release from Git is FAILED ("manual git
    # revert needed") and the rollback stays.
    argocd_url: str = ""
    argocd_token: SecretStr | None = None
    github_repository: str = ""  # owner/name
    github_app_id: str = ""
    github_app_installation_id: str = ""
    github_app_private_key: SecretStr | None = None

    # Approval mode's default before an admin first changes it (A-14): on in prod, off elsewhere.
    approval_required_by_default: bool | None = None
    poll_seconds: float = 5.0
    start_agent: bool = True  # False in unit tests: no background loop

    @property
    def approval_default(self) -> bool:
        if self.approval_required_by_default is not None:
            return self.approval_required_by_default
        return self.environment == "prod"

    @property
    def llm_key(self) -> str | None:
        key = self.openrouter_api_key.get_secret_value() if self.openrouter_api_key else ""
        return key if key and key != PLACEHOLDER else None

    @property
    def argo_token(self) -> str | None:
        token = self.argocd_token.get_secret_value() if self.argocd_token else ""
        return token if token and token != PLACEHOLDER else None

    @property
    def github_app(self) -> tuple[str, str, str] | None:
        """(app ID, installation ID, private key), or None while not configured."""
        key = self.github_app_private_key.get_secret_value() if self.github_app_private_key else ""
        values = (self.github_app_id, self.github_app_installation_id, key)
        if not all(values) or PLACEHOLDER in values or not self.github_repository:
            return None
        return values
