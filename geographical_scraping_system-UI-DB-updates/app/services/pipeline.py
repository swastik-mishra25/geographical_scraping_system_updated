"""Autonomous geographical discovery and qualification pipeline entry point."""
from __future__ import annotations

from typing import Any, Optional

from app.services.orchestrator import execute_autonomous_run


def run_pipeline(
    city: str,
    category: str = "Digital Marketing",
    limit: int = 25,
    state: str = "",
    country: str = "India",
    locality: str = "",
    phone_required: bool = False,
    multi_locality: bool = False,
    webhook_url: Optional[str] = None,
    batch_size: int = 10,
) -> dict[str, Any]:
    """Execute autonomous business discovery pipeline maintaining backward-compatible response contract."""
    return execute_autonomous_run(
        city=city,
        category=category,
        limit=limit,
        state=state,
        country=country,
        locality=locality,
        trigger_type="api",
        phone_required=phone_required,
        multi_locality=multi_locality,
        webhook_url=webhook_url,
        batch_size=batch_size,
    )
