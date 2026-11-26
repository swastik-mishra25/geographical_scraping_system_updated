"""Unit and integration tests for Webhook Streaming and Batch Deduplication Pipeline.

Verifies:
1. Deduplication of items using unique keys (phone, domain, CIN, name+city).
2. Accumulation of unique items until batch size N (N=10) is reached.
3. Immediate HTTP POST dispatch with Content-Type: application/json upon reaching N=10.
4. Delivery validation checking for 2xx status, handling non-2xx/network errors cleanly.
5. In-process HTTP test endpoint verification confirming exactly 10 unique items arrive.
6. REST API endpoint /api/webhook/stream-batch integration.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List
from unittest.mock import patch

from app.models import WebhookStreamBatchRequest
from app.services.webhook_batch_pipeline import (
    WebhookBatchCollector,
    generate_dedupe_key,
    process_items_through_batch_pipeline,
)


class MockWebhookReceiver(BaseHTTPRequestHandler):
    """In-process mock webhook receiver capturing incoming POST payloads and headers."""

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Quiet logger for clean test output

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        
        parsed_body = {}
        try:
            parsed_body = json.loads(raw_body)
        except Exception:
            parsed_body = {"raw": raw_body}

        req_record = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": parsed_body,
            "content_type": self.headers.get("Content-Type", ""),
        }

        # Check if the test requested a failure simulation
        if "/fail_500" in self.path:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Internal Server Error", "status": 500}).encode("utf-8"))
            self.server.captured_requests.append(req_record)  # type: ignore[attr-defined]
            return

        if "/fail_400" in self.path:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Bad Request", "status": 400}).encode("utf-8"))
            self.server.captured_requests.append(req_record)  # type: ignore[attr-defined]
            return

        # Default success 200 OK
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"received": True, "status": "accepted"}).encode("utf-8"))
        self.server.captured_requests.append(req_record)  # type: ignore[attr-defined]


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestWebhookBatchPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = get_free_port()
        cls.server = HTTPServer(("127.0.0.1", cls.port), MockWebhookReceiver)
        cls.server.captured_requests = []  # type: ignore[attr-defined]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:
            pass

    def setUp(self):
        self.server.captured_requests = []  # type: ignore[attr-defined]

    def test_01_dedupe_key_generation(self):
        """Verify unique key generation across phone, domain, CIN, and name+city."""
        lead_phone = {"company_name": "Apex Digital", "phone": "+91 98200 12345", "website": "https://apex.in"}
        lead_domain = {"company_name": "Brand Boost", "phone": "", "website": "https://www.brandboost.co.in/contact"}
        lead_cin = {"company_name": "Zenith Tech", "cin": "U72900MH2018PTC310234"}
        lead_name_city = {"company_name": "Alpha Media Works", "city": "Mumbai"}

        self.assertEqual(generate_dedupe_key(lead_phone), "phone:9820012345")
        self.assertEqual(generate_dedupe_key(lead_domain), "domain:brandboost.co.in")
        self.assertEqual(generate_dedupe_key(lead_cin), "cin:U72900MH2018PTC310234")
        self.assertIn("name_city:", generate_dedupe_key(lead_name_city))

    def test_02_batch_pipeline_n10_with_duplicates(self):
        """
        Critical Test:
        - Stream items with duplicates into WebhookBatchCollector with N = 10.
        - Verify exactly 10 unique items accumulate.
        - Verify immediate HTTP POST delivery to webhook endpoint when 10th unique item arrives.
        - Verify delivery validation passes with 2xx status, batch count = 10, and timestamp.
        - Verify endpoint receives exactly 10 unique items and no duplicates.
        """
        webhook_target = f"{self.base_url}/webhook/leads"
        collector = WebhookBatchCollector(
            webhook_url=webhook_target,
            batch_size=10,
        )

        # Mock scraper stream containing 16 items: 10 unique leads and 6 duplicate items
        stream_items = [
            # 1. Unique 1
            {"company_name": "Agency 1", "phone": "+919800000001", "website": "https://agency1.in", "city": "Mumbai"},
            # 2. Unique 2
            {"company_name": "Agency 2", "phone": "+919800000002", "website": "https://agency2.in", "city": "Mumbai"},
            # Duplicate of Unique 1 (identical phone)
            {"company_name": "Agency 1 Copy", "phone": "+91-98000-00001", "website": "https://mirror1.in", "city": "Mumbai"},
            # 3. Unique 3
            {"company_name": "Agency 3", "phone": "+919800000003", "website": "https://agency3.in", "city": "Mumbai"},
            # Duplicate of Unique 2 (identical domain)
            {"company_name": "Agency 2 Subpage", "phone": "", "website": "https://www.agency2.in/contact", "city": "Mumbai"},
            # 4. Unique 4
            {"company_name": "Agency 4", "phone": "+919800000004", "website": "https://agency4.in", "city": "Mumbai"},
            # 5. Unique 5
            {"company_name": "Agency 5", "phone": "+919800000005", "website": "https://agency5.in", "city": "Mumbai"},
            # Duplicate of Unique 4 (identical name and city, no phone)
            {"company_name": "Agency 4", "phone": "", "website": "", "city": "Mumbai"},
            # 6. Unique 6
            {"company_name": "Agency 6", "phone": "+919800000006", "website": "https://agency6.in", "city": "Mumbai"},
            # 7. Unique 7
            {"company_name": "Agency 7", "phone": "+919800000007", "website": "https://agency7.in", "city": "Mumbai"},
            # Duplicate of Unique 7 (identical phone)
            {"company_name": "Agency 7 Duplicate", "phone": "09800000007", "website": "https://agency7-alt.in", "city": "Mumbai"},
            # 8. Unique 8
            {"company_name": "Agency 8", "phone": "+919800000008", "website": "https://agency8.in", "city": "Mumbai"},
            # 9. Unique 9
            {"company_name": "Agency 9", "phone": "+919800000009", "website": "https://agency9.in", "city": "Mumbai"},
            # Duplicate of Unique 5
            {"company_name": "Agency 5 Duplicate", "phone": "+919800000005", "website": "https://agency5.in", "city": "Mumbai"},
            # Duplicate of Unique 8
            {"company_name": "Agency 8 Alt", "phone": "", "website": "https://agency8.in/about", "city": "Mumbai"},
            # 10. Unique 10 -> AT THIS MOMENT, N = 10 IS REACHED! Batch MUST dispatch immediately!
            {"company_name": "Agency 10", "phone": "+919800000010", "website": "https://agency10.in", "city": "Mumbai"},
        ]

        immediate_dispatch_result = None

        # Feed items one by one as a real scraper stream yields them
        for idx, item in enumerate(stream_items):
            is_unique, dispatch_res = collector.add_item(item)
            if dispatch_res is not None:
                immediate_dispatch_result = dispatch_res
                # Verify that batch dispatched EXACTLY on the 10th unique item (which is index 15)
                self.assertEqual(idx, 15, "Batch should dispatch immediately when 10th unique item is added")

        # 1. Verification of collector statistics
        self.assertEqual(collector.total_received, 16)
        self.assertEqual(collector.unique_count, 10)
        self.assertEqual(collector.duplicate_count, 6)
        self.assertEqual(len(collector.dispatched_batches), 1)

        # 2. Verification of delivery validation result
        self.assertIsNotNone(immediate_dispatch_result)
        self.assertTrue(immediate_dispatch_result["delivered"])
        self.assertEqual(immediate_dispatch_result["status_code"], 200)
        self.assertEqual(immediate_dispatch_result["batch_count"], 10)
        self.assertTrue(bool(immediate_dispatch_result["timestamp"]))
        self.assertEqual(immediate_dispatch_result["target_url"], webhook_target)
        self.assertIsNone(immediate_dispatch_result["error_message"])

        # 3. Verification of payload arrived at the mock webhook receiver
        captured = self.server.captured_requests  # type: ignore[attr-defined]
        self.assertEqual(len(captured), 1, "Exactly one HTTP POST request should arrive at webhook target")
        
        req = captured[0]
        self.assertEqual(req["path"], "/webhook/leads")
        self.assertIn("application/json", req["content_type"])

        body = req["body"]
        self.assertEqual(body.get("event"), "scraper_batch_delivery")
        self.assertEqual(body.get("batch_size"), 10)
        
        delivered_items = body.get("items", [])
        self.assertEqual(len(delivered_items), 10, "Target endpoint MUST receive exactly 10 unique items")

        # Verify all delivered items are distinct
        delivered_phones = [item.get("phone") for item in delivered_items]
        self.assertEqual(len(set(delivered_phones)), 10, "No duplicate items must exist in the delivered payload")

        # Verify company names match 1 through 10
        expected_names = [f"Agency {i}" for i in range(1, 11)]
        delivered_names = [item.get("company_name") for item in delivered_items]
        self.assertEqual(delivered_names, expected_names)

    def test_03_non_2xx_error_handling(self):
        """Verify clean error handling and validation logging when endpoint returns non-2xx (HTTP 500)."""
        webhook_target = f"{self.base_url}/fail_500"
        collector = WebhookBatchCollector(
            webhook_url=webhook_target,
            batch_size=2,
            max_retries=0,
        )

        collector.add_item({"company_name": "Err 1", "phone": "+919999900001"})
        is_unique, result = collector.add_item({"company_name": "Err 2", "phone": "+919999900002"})

        self.assertIsNotNone(result)
        self.assertFalse(result["delivered"])
        self.assertEqual(result["status_code"], 500)
        self.assertEqual(result["batch_count"], 2)
        self.assertIn("Non-2xx HTTP status", result["error_message"])

    def test_04_network_connection_error_handling(self):
        """Verify clean error handling when target server is down or unreachable."""
        dead_port = get_free_port()
        webhook_target = f"http://127.0.0.1:{dead_port}/dead-endpoint"
        collector = WebhookBatchCollector(
            webhook_url=webhook_target,
            batch_size=1,
            max_retries=0,
            timeout=1.0,
        )

        is_unique, result = collector.add_item({"company_name": "Dead Test", "phone": "+919999900003"})
        self.assertIsNotNone(result)
        self.assertFalse(result["delivered"])
        self.assertEqual(result["status_code"], 0)
        self.assertTrue(
            any(k in result["error_message"] for k in ("Connection Error", "Network Timeout", "ConnectTimeout", "unreachable")),
            f"Expected connection/timeout error message, got: {result['error_message']}",
        )

    def test_05_default_env_fallback(self):
        """Verify fallback to DEFAULT_WEBHOOK_URL when webhook_url is not passed."""
        with patch("app.services.webhook_batch_pipeline.DEFAULT_WEBHOOK_URL", f"{self.base_url}/env_webhook"):
            collector = WebhookBatchCollector(webhook_url="", batch_size=10)
            self.assertEqual(collector.webhook_url, f"{self.base_url}/env_webhook")
            self.assertEqual(collector.batch_size, 10)

    def test_06_fastapi_rest_endpoint_stream_batch(self):
        """Verify the /api/webhook/stream-batch REST API endpoint."""
        from app.main import stream_batch_to_webhook
        
        stream_items = [
            {"company_name": "API Lead 1", "phone": "+919700000001", "city": "Delhi"},
            {"company_name": "API Lead 1 Duplicate", "phone": "+919700000001", "city": "Delhi"},
            {"company_name": "API Lead 2", "phone": "+919700000002", "city": "Delhi"},
        ]
        
        req = WebhookStreamBatchRequest(
            webhook_url=f"{self.base_url}/api_test_receiver",
            batch_size=2,
            items=stream_items,
            auto_flush=True,
        )
        
        resp = stream_batch_to_webhook(req)
        self.assertTrue(resp["success"])
        self.assertEqual(resp["total_received"], 3)
        self.assertEqual(resp["unique_accumulated"], 2)
        self.assertEqual(resp["duplicates_filtered"], 1)
        self.assertEqual(resp["batches_dispatched"], 1)
        self.assertTrue(resp["delivery_results"][0]["delivered"])
        self.assertEqual(resp["delivery_results"][0]["status_code"], 200)

    def test_07_empty_webhook_url_graceful_handling(self):
        """Verify clean error reporting and persistence logging when webhook_url is empty."""
        with patch("app.services.webhook_batch_pipeline.DEFAULT_WEBHOOK_URL", ""):
            collector = WebhookBatchCollector(webhook_url="", batch_size=2)
            collector.add_item({"company_name": "NoUrl 1", "phone": "+919911100001"})
            is_unique, result = collector.add_item({"company_name": "NoUrl 2", "phone": "+919911100002"})

            self.assertIsNotNone(result)
            self.assertFalse(result["delivered"])
            self.assertEqual(result["status_code"], 0)
            self.assertIn("No webhook_url configured", result["error_message"])

    def test_08_invalid_url_scheme_graceful_handling(self):
        """Verify clean error reporting and persistence logging for invalid schemes (e.g. ftp://)."""
        collector = WebhookBatchCollector(webhook_url="ftp://invalid.endpoint/stream", batch_size=2)
        collector.add_item({"company_name": "Scheme 1", "phone": "+919922200001"})
        is_unique, result = collector.add_item({"company_name": "Scheme 2", "phone": "+919922200002"})

        self.assertIsNotNone(result)
        self.assertFalse(result["delivered"])
        self.assertEqual(result["status_code"], 0)
        self.assertIn("Invalid webhook_url scheme", result["error_message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
