"""Webhook Batch Streaming and Deduplicating Pipeline Service.

Collects items yielded by scrapers, deduplicates them by unique keys (phone, domain, ID, CIN),
accumulates unique items until batch size N (default 10) is reached, immediately dispatches
the batch via HTTP POST (Content-Type: application/json) to target webhook_url, validates delivery
(checking 2xx status, handling network and non-2xx errors cleanly), and logs validation results.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from app.config import DATABASE_PATH, DEFAULT_WEBHOOK_BATCH_SIZE, DEFAULT_WEBHOOK_URL
from app.services.dedupe import domain, normalize_cin, normalize_name
from app.services.extraction import normalize_indian_phone
from app.services.persistence import log_crm_export

logger = logging.getLogger("dakini.webhook_batch")


def extract_dedupe_keys(item: Dict[str, Any]) -> List[str]:
    """
    Extract all authoritative unique identity keys for a lead or company item.
    Enables multi-coordinate deduplication (e.g., matching on phone OR domain OR CIN OR name+city).
    """
    keys: List[str] = []
    if not item or not isinstance(item, dict):
        return keys

    # 1. Phone number (highest value direct contact coordinate)
    raw_phone = str(item.get("phone") or item.get("telephone") or "").strip()
    if raw_phone:
        norm_phone = normalize_indian_phone(raw_phone)
        digits = re.sub(r"\D", "", norm_phone or raw_phone)[-10:]
        if len(digits) == 10:
            keys.append(f"phone:{digits}")

    # 2. Canonical Website Domain
    raw_web = str(item.get("website") or item.get("url") or item.get("domain") or "").strip()
    if raw_web:
        clean_domain = domain(raw_web)
        if clean_domain:
            keys.append(f"domain:{clean_domain}")

    # 3. Corporate Identification Number (CIN)
    raw_cin = str(item.get("cin") or "").strip()
    if raw_cin:
        clean_cin = normalize_cin(raw_cin)
        if clean_cin:
            keys.append(f"cin:{clean_cin}")

    # 4. Explicit ID or Unique Key
    for id_field in ("id", "lead_id", "company_id", "record_id", "_id"):
        val = str(item.get(id_field) or "").strip()
        if val:
            keys.append(f"id:{val}")

    # 5. Normalized Name + City
    raw_name = str(item.get("company_name") or item.get("name") or "").strip()
    raw_city = str(item.get("city") or "").strip()
    if raw_name:
        clean_name = normalize_name(raw_name, raw_city)
        if clean_name and len(clean_name) >= 3:
            keys.append(f"name_city:{clean_name}_{raw_city.lower()}")

    if not keys:
        serialized = json.dumps(item, sort_keys=True, default=str)
        keys.append(f"hash:{hash(serialized)}")

    return keys


def generate_dedupe_key(item: Dict[str, Any]) -> str:
    """
    Generate the primary authoritative unique key for deduplicating lead/company items.
    """
    keys = extract_dedupe_keys(item)
    return keys[0] if keys else ""


class WebhookBatchCollector:
    """
    Stateful batch accumulator that deduplicates streaming scraper items,
    dispatches batches of size N immediately to webhook_url, and validates delivery.
    """

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        batch_size: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
        max_retries: int = 1,
        db_path: str = DATABASE_PATH,
        customer_name: str = "Webhook Batch Pipeline",
    ):
        # Fallback hierarchy for webhook_url: parameter -> .env / config
        raw_url = (webhook_url or "").strip()
        self.webhook_url = raw_url if raw_url else DEFAULT_WEBHOOK_URL

        # User-specified batch size N (defaulting to 10 when 10 or invalid is entered)
        if batch_size is not None:
            try:
                b_int = int(batch_size)
                self.batch_size = b_int if b_int > 0 else 10
            except (ValueError, TypeError):
                self.batch_size = 10
        else:
            self.batch_size = DEFAULT_WEBHOOK_BATCH_SIZE or 10

        self.custom_headers = headers or {}
        self.timeout = timeout
        self.max_retries = max_retries
        self.db_path = db_path
        self.customer_name = customer_name

        # State tracking
        self.seen_keys: Set[str] = set()
        self.current_batch: List[Dict[str, Any]] = []
        self.total_received: int = 0
        self.unique_count: int = 0
        self.duplicate_count: int = 0
        self.dispatched_batches: List[List[Dict[str, Any]]] = []
        self.delivery_results: List[Dict[str, Any]] = []

    def add_item(self, item: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Ingest a scraper item.
        Deduplicates against seen keys across all identity coordinates (phone, domain, CIN, name+city).
        Accumulates until len(current_batch) == N.
        Once N unique items are collected, immediately dispatches the batch via HTTP POST.

        Returns:
            Tuple[bool, Optional[Dict[str, Any]]]:
                (is_unique, delivery_result_if_batch_fired)
        """
        self.total_received += 1
        keys = extract_dedupe_keys(item)

        if not keys or any(k in self.seen_keys for k in keys):
            self.duplicate_count += 1
            logger.debug("Duplicate item skipped (keys=%s): %s", keys, item.get("company_name"))
            return False, None

        # Unique item accepted: register all identity keys to prevent any cross-attribute duplicates
        for k in keys:
            self.seen_keys.add(k)
        self.unique_count += 1
        self.current_batch.append(item)
        logger.info(
            "Unique item added (%d/%d): %s (keys=%s)",
            len(self.current_batch),
            self.batch_size,
            item.get("company_name", "Unknown"),
            keys,
        )

        # Trigger batch dispatch immediately when threshold N is met
        delivery_result = None
        if len(self.current_batch) >= self.batch_size:
            delivery_result = self._dispatch_current_batch()

        return True, delivery_result

    def _dispatch_current_batch(self) -> Dict[str, Any]:
        """Send the accumulated N items via HTTP POST to webhook_url and validate delivery."""
        batch_to_send = list(self.current_batch)
        self.current_batch = []  # Reset batch buffer immediately
        self.dispatched_batches.append(batch_to_send)

        return self.dispatch_batch_http(batch_to_send)

    def dispatch_batch_http(self, batch_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Dispatch a batch of items to webhook_url via HTTP POST.
        Validates 2xx response status, handles non-2xx and network errors cleanly,
        and logs the validation result.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        batch_count = len(batch_items)

        if not batch_items:
            logger.debug("[webhook-batch] dispatch_batch_http called with empty batch. Skipping.")
            return {
                "delivered": False,
                "status_code": 0,
                "batch_count": 0,
                "timestamp": now_iso,
                "target_url": self.webhook_url or "",
                "error_message": "Empty batch items list.",
                "response_summary": "",
            }

        # Validate URL presence and scheme
        validation_error = None
        clean_url = (self.webhook_url or "").strip()
        if not clean_url:
            validation_error = "No webhook_url configured (missing in request and .env fallback)."
        elif not (clean_url.startswith("http://") or clean_url.startswith("https://")):
            validation_error = f"Invalid webhook_url scheme: '{clean_url}' (must start with http:// or https://)."

        if validation_error:
            logger.warning("[webhook-batch] Delivery aborted: %s", validation_error)
            val_result = {
                "delivered": False,
                "status_code": 0,
                "batch_count": batch_count,
                "timestamp": now_iso,
                "target_url": clean_url,
                "error_message": validation_error,
                "response_summary": "",
            }
            try:
                log_crm_export(
                    destination_id="webhook_batch_pipeline",
                    customer_name=self.customer_name,
                    city=str(batch_items[0].get("city") or "") if batch_items else "",
                    crm_type="webhook_batch",
                    leads_count=batch_count,
                    status="failed",
                    status_code=0,
                    response_summary="",
                    error_message=validation_error,
                    trigger_type="webhook_batch_threshold",
                    db_path=self.db_path,
                )
            except Exception as log_exc:
                logger.debug("Failed to record export log in persistence: %s", log_exc)

            self.delivery_results.append(val_result)
            return val_result

        # Build payload envelope
        payload = {
            "event": "scraper_batch_delivery",
            "source": "Dakini AI Geographical Scraping System",
            "batch_size": batch_count,
            "timestamp": now_iso,
            "items": batch_items,
        }

        # Request headers
        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Dakini-AI-Webhook-Batch-Pipeline/2.5",
        }
        if self.custom_headers:
            req_headers.update(self.custom_headers)

        status_code = 0
        response_summary = ""
        error_msg = ""
        delivered = False

        # Attempt HTTP POST with clean error handling and retries
        for attempt in range(self.max_retries + 1):
            try:
                logger.info(
                    "[webhook-batch] Sending batch of %d items to %s (attempt %d)...",
                    batch_count,
                    clean_url,
                    attempt + 1,
                )
                resp = requests.post(
                    clean_url,
                    json=payload,
                    headers=req_headers,
                    timeout=self.timeout,
                )
                status_code = resp.status_code
                response_summary = resp.text[:500] if resp.text else ""

                # Check 2xx HTTP success status
                if 200 <= status_code < 300:
                    delivered = True
                    error_msg = ""
                    logger.info(
                        "[webhook-batch] SUCCESS: Batch of %d delivered to %s (HTTP %d)",
                        batch_count,
                        clean_url,
                        status_code,
                    )
                    break
                else:
                    error_msg = f"Non-2xx HTTP status received: HTTP {status_code} ({response_summary[:120]})"
                    logger.warning("[webhook-batch] Non-2xx delivery response: %s", error_msg)

                    # Retry on 429 or 5xx if retries remaining
                    if status_code in (429, 502, 503, 504) and attempt < self.max_retries:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    break

            except requests.exceptions.Timeout as exc:
                error_msg = f"Network Timeout after {self.timeout}s: {exc}"
                logger.error("[webhook-batch] %s", error_msg)
                if attempt < self.max_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                break

            except requests.exceptions.ConnectionError as exc:
                error_msg = f"Connection Error (Endpoint unreachable): {exc}"
                logger.error("[webhook-batch] %s", error_msg)
                if attempt < self.max_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                break

            except requests.exceptions.RequestException as exc:
                error_msg = f"HTTP Request Error: {exc}"
                logger.error("[webhook-batch] %s", error_msg)
                break

            except Exception as exc:
                error_msg = f"Unexpected Dispatch Exception: {exc}"
                logger.exception("[webhook-batch] %s", error_msg)
                break

        validation_result = {
            "delivered": delivered,
            "status_code": status_code,
            "batch_count": batch_count,
            "timestamp": now_iso,
            "target_url": self.webhook_url,
            "error_message": error_msg or None,
            "response_summary": response_summary,
        }

        # Record in SQLite audit log table
        try:
            log_crm_export(
                destination_id="webhook_batch_pipeline",
                customer_name=self.customer_name,
                city=str(batch_items[0].get("city") or "") if batch_items else "",
                crm_type="webhook_batch",
                leads_count=batch_count,
                status="success" if delivered else "failed",
                status_code=status_code,
                response_summary=response_summary,
                error_message=error_msg,
                trigger_type="webhook_batch_threshold",
                db_path=self.db_path,
            )
        except Exception as log_exc:
            logger.debug("Failed to record export log in persistence: %s", log_exc)

        self.delivery_results.append(validation_result)
        return validation_result

    def flush(self) -> Optional[Dict[str, Any]]:
        """Flush any remaining items in current_batch buffer regardless of batch_size threshold."""
        if self.current_batch:
            return self._dispatch_current_batch()
        return None

    def get_summary(self) -> Dict[str, Any]:
        """Return operational summary of the batch collector lifecycle."""
        return {
            "target_webhook_url": self.webhook_url,
            "batch_size_N": self.batch_size,
            "total_received": self.total_received,
            "unique_accumulated": self.unique_count,
            "duplicates_filtered": self.duplicate_count,
            "batches_dispatched": len(self.dispatched_batches),
            "pending_in_buffer": len(self.current_batch),
            "delivery_results": self.delivery_results,
        }


def process_items_through_batch_pipeline(
    items: List[Dict[str, Any]],
    webhook_url: Optional[str] = None,
    batch_size: int = 10,
    auto_flush: bool = True,
    headers: Optional[Dict[str, str]] = None,
    db_path: str = DATABASE_PATH,
) -> Dict[str, Any]:
    """
    Convenience helper to feed a stream or list of items through the WebhookBatchCollector,
    deduplicating each item and immediately dispatching batches of size N.
    """
    collector = WebhookBatchCollector(
        webhook_url=webhook_url,
        batch_size=batch_size,
        headers=headers,
        db_path=db_path,
    )

    for item in items:
        collector.add_item(item)

    if auto_flush and collector.current_batch:
        collector.flush()

    return collector.get_summary()
