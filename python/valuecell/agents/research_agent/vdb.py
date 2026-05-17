"""
Vector database configuration for Research Agent.

Fault-tolerant, lazy initialization:
- Attempts to create an embedder and LanceDb only when requested.
- If no embedding provider/API key is available, returns None instead of raising.
- Respects environment variable overrides (e.g., EMBEDDER_MODEL_ID).

This prevents import-time failures and allows the ResearchAgent to run in a
"tools-only" mode without knowledge search when embeddings are not configured.
"""

from typing import Any, Optional

from loguru import logger

try:
    from agno.vectordb.lancedb import LanceDb
    from agno.vectordb.search import SearchType
except ImportError:
    LanceDb = None  # type: ignore[misc, assignment]
    SearchType = None  # type: ignore[misc, assignment]

import valuecell.utils.model as model_utils_mod
from valuecell.utils.db import resolve_lancedb_uri


def get_vector_db() -> Optional[Any]:
    """Create and return the LanceDb instance, or None if embeddings are unavailable.

    This function is safe to call at runtime; it will not raise during normal
    missing-configuration scenarios. Unexpected errors are logged and result in
    a None return to enable graceful degradation.
    """
    if LanceDb is None or SearchType is None:
        logger.warning(
            "lancedb is not installed (optional extra). "
            "Install with: uv sync --extra research"
        )
        return None

    try:
        embedder = model_utils_mod.get_embedder_for_agent("research_agent")
    except Exception as e:
        logger.warning(
            "ResearchAgent embeddings unavailable; disabling knowledge search. Error: {}",
            e,
        )
        return None

    try:
        return LanceDb(
            table_name="research_agent_knowledge_base",
            uri=resolve_lancedb_uri(),
            embedder=embedder,
            # reranker=reranker,  # Optional: can be configured later
            search_type=SearchType.hybrid,
            use_tantivy=False,
        )
    except Exception as e:
        logger.warning(
            "Failed to initialize LanceDb for ResearchAgent; disabling knowledge. Error: {}",
            e,
        )
        return None
