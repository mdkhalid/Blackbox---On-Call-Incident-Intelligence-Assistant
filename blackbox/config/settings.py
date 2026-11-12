"""Configuration: environment settings (pydantic-settings) + YAML tunables (AppConfig)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from blackbox.iputils import Network, parse_trusted_networks

# ---------- YAML tunables ----------

class RetrievalConfig(BaseModel):
    top_k: int = 8
    candidate_pool: int = 30
    fusion_method: str = "rrf"
    fusion_k: int = 60
    dense_weight: float = 0.75
    sparse_weight: float = 0.25
    rerank: bool = False
    rerank_provider: str = "auto"
    rerank_candidates: int = 20
    rewrite_cap: int = 2
    retrieval_timeout_ms: int = 5000
    min_relevance_score: float | None = None
    collection_name: str = "blackbox_chunks"
    embedding_provider: str = "auto"
    # Which adapter serves the dense leg. `chroma` is the development adapter;
    # `qdrant` keeps vectors in the cluster.
    dense_backend: str = "chroma"
    # Optional shadow dense backend run alongside the primary for dual-read
    # comparison during a migration. None disables shadowing.
    shadow_dense_backend: str | None = None
    # Which adapter serves the sparse leg. `bm25` keeps the index in the pod's
    # memory and is the development adapter; `opensearch` keeps it in the cluster.
    sparse_backend: str = "bm25"

    @field_validator("top_k", "candidate_pool", "fusion_k", "rewrite_cap", "retrieval_timeout_ms")
    @classmethod
    def positive(cls, v):
        if v < 1:
            raise ValueError("must be >= 1")
        return v

    @field_validator("dense_weight", "sparse_weight")
    @classmethod
    def weight_range(cls, v):
        if not (0.0 <= v <= 1.0):
            raise ValueError("weight must be in [0,1]")
        return v

    @field_validator("min_relevance_score")
    @classmethod
    def score_range(cls, v):
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("min_relevance_score must be in [0,1]")
        return v

    @field_validator("fusion_method")
    @classmethod
    def fusion_method_known(cls, v):
        if v not in ("rrf", "weighted"):
            raise ValueError("fusion_method must be 'rrf' or 'weighted'")
        return v

    @field_validator("rerank_provider")
    @classmethod
    def rerank_provider_known(cls, v):
        if v not in ("auto", "cross_encoder", "lexical", "llm", "none"):
            raise ValueError("rerank_provider must be auto|cross_encoder|lexical|llm|none")
        return v

    @field_validator("sparse_backend")
    @classmethod
    def sparse_backend_known(cls, v):
        if v not in ("bm25", "opensearch"):
            raise ValueError("sparse_backend must be 'bm25' or 'opensearch'")
        return v

    @field_validator("dense_backend")
    @classmethod
    def dense_backend_known(cls, v):
        if v not in ("chroma", "qdrant"):
            raise ValueError("dense_backend must be 'chroma' or 'qdrant'")
        return v

    @field_validator("shadow_dense_backend")
    @classmethod
    def shadow_dense_backend_known(cls, v):
        if v is not None and v not in ("chroma", "qdrant"):
            raise ValueError("shadow_dense_backend must be None, 'chroma' or 'qdrant'")
        return v

    @model_validator(mode="after")
    def pool_covers_top_k(self):
        if self.candidate_pool < self.top_k:
            raise ValueError("candidate_pool must be >= top_k")
        return self


class GraphConfig(BaseModel):
    ground_check_min_overlap: float = 0.34
    direct_answers: bool = True

    @field_validator("ground_check_min_overlap")
    @classmethod
    def overlap_range(cls, v):
        if not (0.0 <= v <= 1.0):
            raise ValueError("ground_check_min_overlap must be in [0,1]")
        return v


class MemoryConfig(BaseModel):
    working_turns: int = 6
    summary_trigger_turns: int = 8
    summary_max_tokens: int = 600
    entity_max_tokens: int = 400

    @field_validator("working_turns", "summary_trigger_turns", "summary_max_tokens", "entity_max_tokens")
    @classmethod
    def positive(cls, v):
        if v < 1:
            raise ValueError("must be >= 1")
        return v

    @model_validator(mode="after")
    def compression_can_trigger(self):
        # Compression only runs when the buffer exceeds the verbatim window.
        if self.summary_trigger_turns <= self.working_turns:
            raise ValueError("summary_trigger_turns must be greater than working_turns")
        return self


class BudgetConfig(BaseModel):
    system_prompt_tokens: int = 800
    entity_memory_tokens: int = 400
    summary_tokens: int = 600
    recent_turns_tokens: int = 1200
    retrieved_chunks_tokens: int = 2000
    output_headroom_tokens: int = 1024
    total_cap_tokens: int = 6024

    @field_validator(
        "system_prompt_tokens",
        "entity_memory_tokens",
        "summary_tokens",
        "recent_turns_tokens",
        "retrieved_chunks_tokens",
        "output_headroom_tokens",
        "total_cap_tokens",
    )
    @classmethod
    def non_negative(cls, v):
        if v < 0:
            raise ValueError("token budgets must be >= 0")
        return v


class ChunkingConfig(BaseModel):
    postmortem_max_tokens: int = 800
    runbook_max_tokens: int = 600
    arch_max_tokens: int = 700
    alert_max_tokens: int = 300
    embedding_max_tokens: int = 8000


class GuardrailsConfig(BaseModel):
    max_message_chars: int = 4000
    max_request_bytes: int = 32768
    rate_limit_rps: int = 5
    max_llm_calls_per_turn: int = 6
    max_answer_tokens: int = 1024
    max_concurrent_turns_per_tenant: int = 8
    strict_injection_block: bool = False
    secret_scrub: bool = True

    @field_validator(
        "max_message_chars",
        "max_request_bytes",
        "rate_limit_rps",
        "max_llm_calls_per_turn",
        "max_answer_tokens",
        "max_concurrent_turns_per_tenant",
    )
    @classmethod
    def positive(cls, v):
        if v < 1:
            raise ValueError("must be >= 1")
        return v


class RedisConfig(BaseModel):
    enabled: bool = True
    timeout_ms: int = 200
    ttl_llm_response_s: int = 86400
    ttl_embedding_s: int = 604800
    ttl_retrieval_s: int = 60
    ttl_rerank_s: int = 300
    ttl_conversation_state_s: int = 3600
    ttl_idempotency_s: int = 3600

    @field_validator("timeout_ms")
    @classmethod
    def positive_timeout(cls, v):
        if v < 1:
            raise ValueError("timeout_ms must be >= 1")
        return v

    @field_validator(
        "ttl_llm_response_s",
        "ttl_embedding_s",
        "ttl_retrieval_s",
        "ttl_rerank_s",
        "ttl_conversation_state_s",
        "ttl_idempotency_s",
    )
    @classmethod
    def non_negative_ttl(cls, v):
        if v < 0:
            raise ValueError("ttl must be >= 0")
        return v


class IngestionConfig(BaseModel):
    max_upload_size_mb: int = 50
    max_total_corpus_size_mb: int = 2048
    spool_threshold_bytes: int = 1048576
    batch_size_chunks: int = 64
    batch_size_tokens: int = 100000
    max_concurrent_jobs: int = 2


class AppConfig(BaseModel):
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    pricing: dict[str, Any] = Field(default_factory=dict)


def load_app_config(settings: Settings) -> AppConfig:
    path = Path(settings.config_path)
    if not path.exists():
        # try relative to package
        alt = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
        if alt.exists():
            path = alt
        else:
            return AppConfig()
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"invalid config yaml: {e}") from e
    try:
        return AppConfig(**raw)
    except ValidationError as e:
        raise ValueError(str(e)) from e


# ---------- Env settings ----------

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    env: str = Field(default="dev", alias="ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    llm_provider: str = Field(default="openai", alias="LLM_PROVIDER")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    redis_url: str | None = Field(default=None, alias="REDIS_URL")
    chroma_persist_dir: str = Field(default="./data/chroma", alias="CHROMA_PERSIST_DIR")
    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    qdrant_api_key: str | None = Field(default=None, alias="QDRANT_API_KEY")
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")
    embedding_provider_override: str | None = Field(
        default=None, alias="EMBEDDING_PROVIDER"
    )
    generation_model: str = Field(default="gpt-4o", alias="GENERATION_MODEL")
    grading_model: str = Field(default="gpt-4o-mini", alias="GRADING_MODEL")
    rewrite_model: str = Field(default="gpt-4o-mini", alias="REWRITE_MODEL")
    reranker_model: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L-6-v2", alias="RERANKER_MODEL"
    )
    config_path: str = Field(default="blackbox/config/config.yaml", alias="CONFIG_PATH")
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")
    llm_timeout_s: float = Field(default=30.0, alias="LLM_TIMEOUT_S")
    llm_max_retries: int = Field(default=2, alias="LLM_MAX_RETRIES")
    llm_max_concurrency: int = Field(default=8, alias="LLM_MAX_CONCURRENCY")
    llm_circuit_threshold: int = Field(default=5, alias="LLM_CIRCUIT_THRESHOLD")
    llm_circuit_reset_s: float = Field(default=30.0, alias="LLM_CIRCUIT_RESET_S")

    # Worker threads for the blocking parts of the request path (Chroma queries, BM25
    # scoring, SQLAlchemy reads). The default executor is `min(32, cpu_count + 4)`,
    # which silently ties concurrency to the machine size; this makes it explicit.
    max_worker_threads: int = Field(default=32, alias="MAX_WORKER_THREADS")

    # Authentication. Disabled by default only for local development; required in prod.
    auth_enabled: bool = Field(default=False, alias="AUTH_ENABLED")
    auth_jwt_secret: str | None = Field(default=None, alias="AUTH_JWT_SECRET")
    auth_jwks_url: str | None = Field(default=None, alias="AUTH_JWKS_URL")
    auth_jwt_issuer: str | None = Field(default=None, alias="AUTH_JWT_ISSUER")
    auth_jwt_audience: str | None = Field(default=None, alias="AUTH_JWT_AUDIENCE")
    auth_default_tenant: str = Field(default="default", alias="AUTH_DEFAULT_TENANT")

    # Trusted reverse proxies. `X-Forwarded-For` is only honoured when the request
    # arrives from one of these networks; anything else is client-controlled and is
    # ignored, so the rate limiter cannot be bypassed with a spoofed header.
    trusted_proxy_cidrs: str = Field(default="", alias="TRUSTED_PROXY_CIDRS")

    # OpenSearch, the sparse retrieval backend that scales past a single pod.
    opensearch_hosts: str = Field(default="", alias="OPENSEARCH_HOSTS")
    opensearch_index: str = Field(default="blackbox_chunks", alias="OPENSEARCH_INDEX")
    opensearch_username: str = Field(default="", alias="OPENSEARCH_USERNAME")
    opensearch_password: str = Field(default="", alias="OPENSEARCH_PASSWORD")
    opensearch_verify_certs: bool = Field(default=True, alias="OPENSEARCH_VERIFY_CERTS")

    @field_validator("llm_timeout_s")
    @classmethod
    def positive_timeout(cls, v):
        if v <= 0:
            raise ValueError("LLM_TIMEOUT_S must be > 0")
        return v

    @field_validator("llm_max_retries")
    @classmethod
    def non_negative_retries(cls, v):
        if v < 0:
            raise ValueError("LLM_MAX_RETRIES must be >= 0")
        return v

    @field_validator("llm_max_concurrency", "llm_circuit_threshold")
    @classmethod
    def positive_int(cls, v):
        if v < 1:
            raise ValueError("must be >= 1")
        return v

    @field_validator("max_worker_threads")
    @classmethod
    def positive_threads(cls, v):
        if v < 1:
            raise ValueError("MAX_WORKER_THREADS must be >= 1")
        return v

    @field_validator("llm_circuit_reset_s")
    @classmethod
    def non_negative_reset(cls, v):
        if v < 0:
            raise ValueError("LLM_CIRCUIT_RESET_S must be >= 0")
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def trusted_proxy_networks(self) -> list[Network]:
        """Parsed trusted proxy networks."""
        return parse_trusted_networks(self.trusted_proxy_cidrs.split(","))

    @model_validator(mode="after")
    def validate_provider_key(self):
        if self.env == "prod":
            if self.llm_provider == "openai" and not self.openai_api_key:
                raise ValueError("OPENAI_API_KEY required in prod")
            if self.llm_provider == "anthropic" and not self.anthropic_api_key:
                raise ValueError("ANTHROPIC_API_KEY required in prod")
            if "*" in self.cors_origin_list:
                raise ValueError("CORS_ORIGINS must not contain '*' in prod")
            if not self.auth_enabled:
                raise ValueError("AUTH_ENABLED must be true in prod")
        if self.auth_enabled and not (self.auth_jwt_secret or self.auth_jwks_url):
            raise ValueError(
                "AUTH_JWT_SECRET or AUTH_JWKS_URL is required when AUTH_ENABLED=true"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def get_config() -> AppConfig:
    return load_app_config(get_settings())
