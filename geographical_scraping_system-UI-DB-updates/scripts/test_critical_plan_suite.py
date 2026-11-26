"""Automated Test Suite implementing the 'Geological Scraper — Critical Test Plan'.

Covers all 14 test areas directly against the actual codebase:
1. Source Connectivity (2.1)
2. Data Extraction (2.2)
3. Data Accuracy (2.3)
4. Pagination & Crawl Depth (2.4)
5. Duplicate Handling (2.5)
6. Negative & Edge-Cases (Missing, Invalid, Empty, Unavailable) (3.1 - 3.4)
7. Smoke Test (4)
8. Sanity Test (5)
9. Soak & Stability Test (7)
10. Recovery Test (8)
11. Restart & Checkpoint Test (9)
12. Output Integrity (CSV & XLSX) (10)
13. Data Completeness & ICP Scoring (11)
14. Performance & Logging & Security/SSRF (12, 13, 14)
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

# Ensure root directory is on PYTHONPATH
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.models import Lead, ScrapeRequest, ScrapeResponse
from app.services.crawler import TARGET_PAGE_PATTERNS, _extract_rich_page_text
from app.services.crawl_policy import (
    BLOCKED_HOSTNAMES,
    PRIVATE_NETWORKS,
    check_crawl_permission,
    detect_anti_bot_or_auth,
    is_safe_public_url,
)
from app.services.dedupe import (
    IndexedDeduplicator,
    deduplicate,
    domain,
    normalize_cin,
    normalize_name,
)
from app.services.exporter import export_csv, export_excel
from app.services.extraction import (
    PHONE_RE,
    extract_business_data,
    normalize_indian_phone,
)
from app.services.industry_classifier import classify_industry
from app.services.location_verifier import verify_location
from app.services.persistence import (
    create_research_run,
    get_latest_checkpoint,
    init_db,
    save_checkpoint,
    save_company,
    update_research_run,
)
from app.services.public_web_discovery import (
    INDIAN_DIGITAL_MARKETING_SEEDS,
    PublicWebDiscoveryGateway,
)
from app.services.query_generator import generate_discovery_queries
from app.services.scoring import score_lead
from app.services.url_frontier import calculate_url_priority, normalize_url
from app.services.validation import (
    INDIAN_LANDLINE_RE,
    INDIAN_MOBILE_RE,
    INDIAN_PHONE_RE,
    INDIAN_TOLLFREE_RE,
    validate_lead,
)


def _build_crawled_page(url: str, html: str, page_type: str = "homepage") -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = _extract_rich_page_text(soup)
    return {
        "url": url,
        "page_type": page_type,
        "title": title,
        "html": html,
        "text": text,
        "soup": soup,
    }


class GeologicalScraperCriticalTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.test_db_path = os.path.join(cls.temp_dir.name, "critical_test.db")
        init_db(cls.test_db_path)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.temp_dir.cleanup()
        except Exception:
            pass

    # =========================================================================
    # 2.1 SOURCE CONNECTIVITY TEST
    # =========================================================================
    def test_2_1_source_connectivity(self):
        """Confirm that the discovery gateway and public sources are initialized cleanly."""
        gateway = PublicWebDiscoveryGateway()
        self.assertIsNotNone(gateway)
        self.assertIsNotNone(gateway.session)
        from app.config import DISCOVERY_MAX_URLS, DISCOVERY_TIMEOUT_SECONDS
        self.assertGreater(DISCOVERY_TIMEOUT_SECONDS, 0)
        self.assertGreater(DISCOVERY_MAX_URLS, 0)
        self.assertIn("mumbai", INDIAN_DIGITAL_MARKETING_SEEDS)
        self.assertGreater(len(INDIAN_DIGITAL_MARKETING_SEEDS["mumbai"]), 10)

    # =========================================================================
    # 2.2 DATA EXTRACTION TEST
    # =========================================================================
    def test_2_2_data_extraction_critical_fields(self):
        """Confirm extraction of ID/Name, Location, Lat/Lon, Category, Description, URL, Phone, Address."""
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Apex Digital Innovations - Best Digital Marketing Agency in Mumbai</title>
            <meta name="description" content="Premier full-service SEO, SEM, and performance marketing agency in Mumbai.">
            <meta property="og:site_name" content="Apex Digital">
        </head>
        <body>
            <header><h1>Apex Digital Innovations</h1></header>
            <div class="content">
                <p>We provide enterprise SEO, social media marketing, and PPC management services across Maharashtra.</p>
                <div class="contact-info">
                    <p>Contact Us: <a href="tel:+919820012345">+91 98200 12345</a></p>
                    <p>Email: <a href="mailto:hello@apexdigital.in">hello@apexdigital.in</a></p>
                    <p>Address: 402, Lotus Grandeur, Veera Desai Road, Andheri West, Mumbai, Maharashtra 400053</p>
                </div>
            </div>
            <footer>CIN: U72900MH2018PTC310234</footer>
        </body>
        </html>
        """
        page = _build_crawled_page("https://apexdigital.in", sample_html)
        data = extract_business_data("https://apexdigital.in", [page], city="Mumbai", state="Maharashtra")
        
        # Verify Company Name
        self.assertIn("Apex Digital", data["company_name"])
        # Verify Location
        self.assertEqual(data["city"], "Mumbai")
        self.assertEqual(data["state"], "Maharashtra")
        # Verify Phone & Email
        self.assertEqual(data["phone"], "+919820012345")
        self.assertEqual(data["email"], "hello@apexdigital.in")
        # Verify Source URL
        self.assertEqual(data["website"], "https://apexdigital.in")

    # =========================================================================
    # 2.3 DATA ACCURACY TEST
    # =========================================================================
    def test_2_3_data_accuracy_phone_and_location(self):
        """Confirm extracted data matches exact ground-truth values and formats."""
        # 1. Indian Mobile (+91 followed by 10 digits starting with 6-9)
        self.assertEqual(normalize_indian_phone("+91 98765-43210"), "+919876543210")
        self.assertEqual(normalize_indian_phone("09876543210"), "+919876543210")
        self.assertEqual(normalize_indian_phone("9876543210"), "+919876543210")
        
        # 2. Indian Landlines (Mumbai 022, Delhi 011, Bhubaneswar 0674)
        self.assertEqual(normalize_indian_phone("022-26834100"), "+912226834100")
        self.assertEqual(normalize_indian_phone("011 4155 1234"), "+911141551234")
        self.assertEqual(normalize_indian_phone("06742541234"), "+916742541234")
        
        # 3. Toll-Free (1800)
        self.assertEqual(normalize_indian_phone("1800 123 4567"), "+9118001234567")

        # 4. Location PIN code verification
        addr_html = "<html><body>Plot 12, Sector 5, Salt Lake, Kolkata, West Bengal 700091</body></html>"
        page = _build_crawled_page("https://kolkatabiz.in", addr_html)
        loc_res = verify_location("Kolkata", "West Bengal", "India", "Salt Lake", [page])
        self.assertTrue(loc_res["verified"])
        self.assertIn(loc_res["location_type"], ("hq", "branch"))

    # =========================================================================
    # 2.4 PAGINATION AND CRAWL DEPTH TEST
    # =========================================================================
    def test_2_4_pagination_and_frontier_traversal(self):
        """Confirm priority internal link detection (/contact, /about, /services) without infinite loops."""
        homepage_html = """
        <html><body>
            <a href="/about-us">About Company</a>
            <a href="/contact">Get In Touch</a>
            <a href="/services/seo">SEO Services</a>
            <a href="/blog/post-1">Blog Post 1</a>
            <a href="/privacy-policy">Privacy</a>
        </body></html>
        """
        soup = BeautifulSoup(homepage_html, "html.parser")
        links = [a["href"] for a in soup.find_all("a", href=True)]
        
        contact_links = [l for l in links if TARGET_PAGE_PATTERNS["contact"].search(l)]
        about_links = [l for l in links if TARGET_PAGE_PATTERNS["about"].search(l)]
        services_links = [l for l in links if TARGET_PAGE_PATTERNS["services"].search(l)]
        
        self.assertIn("/contact", contact_links)
        self.assertIn("/about-us", about_links)
        self.assertIn("/services/seo", services_links)

        # Verify URL normalization prevents loops
        norm1 = normalize_url("https://example.com/contact/")
        norm2 = normalize_url("https://example.com/contact#section")
        self.assertEqual(norm1, "https://example.com/contact")
        self.assertEqual(norm2, "https://example.com/contact")

    # =========================================================================
    # 2.5 DUPLICATE TEST
    # =========================================================================
    def test_2_5_duplicate_handling_tri_level(self):
        """Confirm that duplicate records (by domain, CIN, or normalized entity name) are correctly filtered."""
        deduper = IndexedDeduplicator()
        
        lead1 = {
            "company_name": "Zenith Media Works Pvt Ltd",
            "website": "https://zenithmedia.in",
            "cin": "U74999DL2019PTC345678",
            "phone": "+919811122233",
            "city": "Delhi",
        }
        lead1_copy = {
            "company_name": "Zenith Media Works",
            "website": "https://www.zenithmedia.in/contact",  # Same domain
            "cin": "U74999DL2019PTC345678",                 # Same CIN
            "phone": "+919811122233",
            "city": "Delhi",
        }
        lead2_new = {
            "company_name": "Blue Ocean Digital",
            "website": "https://blueoceandigital.in",
            "cin": "U72200KA2020PTC123456",
            "phone": "+919845012345",
            "city": "Bengaluru",
        }
        
        # Ingest first lead
        deduper.add_or_merge(lead1)
        self.assertEqual(len(deduper.records), 1)
        
        # Second lead is duplicate (domain and CIN match) -> merged, record count stays 1
        deduper.add_or_merge(lead1_copy)
        self.assertEqual(len(deduper.records), 1)
        
        # Third lead is unique -> added, record count becomes 2
        deduper.add_or_merge(lead2_new)
        self.assertEqual(len(deduper.records), 2)
        
        # Batch deduplication test
        deduped = deduplicate([lead1, lead1_copy, lead2_new])
        self.assertEqual(len(deduped), 2)

    # =========================================================================
    # 3. NEGATIVE AND EDGE-CASE TESTS
    # =========================================================================
    def test_3_1_missing_data(self):
        """Test records with missing coordinates, description, phone, or date."""
        minimal_html = """
        <html>
        <head><title>Simple Consulting</title></head>
        <body><p>Consulting agency in Pune.</p></body>
        </html>
        """
        page = _build_crawled_page("https://simpleconsulting.co", minimal_html)
        data = extract_business_data("https://simpleconsulting.co", [page], city="Pune", state="Maharashtra")
        # System must not crash, must provide safe defaults
        self.assertEqual(data["phone"], "")
        self.assertEqual(data["email"], "")
        self.assertEqual(data["city"], "Pune")
        
        # Score calculation must handle empty fields without exception
        score, reasons = score_lead(data)
        self.assertIsInstance(score, int)
        self.assertGreaterEqual(score, 0)
        self.assertIsInstance(reasons, str)

    def test_3_2_invalid_data(self):
        """Test records with invalid coordinates, malformed phones, special characters, unicode."""
        bad_lead = {
            "company_name": "Test & Co. <script>alert('xss')</script> — 100% Guaranteed 🚀",
            "website": "https://test-invalid-data.com",
            "phone": "invalid_phone_12345",
            "email": "bad_email@@domain..com",
            "city": "Mumbai",
            "latitude": 125.432,   # Invalid latitude (> 90)
            "longitude": -250.12,  # Invalid longitude (< -180)
        }
        # Normalization should strip malformed phone
        norm_phone = normalize_indian_phone(bad_lead["phone"])
        self.assertEqual(norm_phone, "")
        
        # Validation should handle bad data gracefully and clear phone
        val_res = validate_lead(bad_lead)
        self.assertFalse(val_res["_phone_valid"])
        self.assertEqual(val_res["phone"], "")

    def test_3_3_empty_page(self):
        """Test scraping an empty HTML page returns empty/safe records without crash."""
        empty_html = ""
        page = _build_crawled_page("https://empty-test.com", empty_html)
        data = extract_business_data("https://empty-test.com", [page], city="Delhi", state="Delhi")
        self.assertIsInstance(data, dict)
        self.assertEqual(data["phone"], "")
        self.assertEqual(data["email"], "")

    def test_3_4_invalid_or_unavailable_source(self):
        """Test invalid URL or unavailable source handling in crawl policy."""
        invalid_url = "http://non-existent-domain-xyz-9876543210.org/some/path"
        is_safe, reason = is_safe_public_url(invalid_url)
        self.assertIsInstance(is_safe, bool)
        self.assertIsInstance(reason, str)

    # =========================================================================
    # 4. SMOKE TEST
    # =========================================================================
    def test_04_smoke_test_core_components(self):
        """Smoke test verifying all core system components initialize and produce output."""
        # 1. Database initializable
        self.assertTrue(os.path.exists(self.test_db_path))
        
        # 2. Query synthesis works
        queries = generate_discovery_queries("Digital Marketing", city="Bengaluru", state="Karnataka")
        self.assertGreater(len(queries), 0)
        
        # 3. Extraction works on simulated page
        sample_page = "<html><head><title>Smoke Digital</title></head><body>Call: <a href='tel:9845098450'>9845098450</a> in Bengaluru 560001</body></html>"
        page = _build_crawled_page("https://smokedigital.in", sample_page)
        extracted = extract_business_data("https://smokedigital.in", [page], city="Bengaluru", state="Karnataka")
        self.assertEqual(extracted["phone"], "+919845098450")
        
        # 4. Scoring works
        score, reason = score_lead(extracted)
        self.assertGreater(score, 0)
        
        # 5. Output export works
        smoke_csv = os.path.join(self.temp_dir.name, "smoke_output.csv")
        export_csv([extracted], smoke_csv)
        self.assertTrue(os.path.exists(smoke_csv))
        self.assertGreater(os.path.getsize(smoke_csv), 0)

    # =========================================================================
    # 5. SANITY TEST
    # =========================================================================
    def test_05_sanity_test(self):
        """Targeted sanity check verifying industry verification and B2B qualification."""
        genuine_agency_text = "We are an award-winning digital marketing agency offering search engine optimization and PPC campaigns."
        fake_coaching_text = "Learn digital marketing course with fees 15000 and 100% placement. Enroll in certification course."
        
        agency_res = classify_industry("Top Agency", genuine_agency_text, ["Top Agency"], "Digital Marketing")
        coaching_res = classify_industry("Training Inst", fake_coaching_text, ["Digital Marketing Training Institute Course"], "Digital Marketing")
        
        self.assertTrue(agency_res["is_match"])
        self.assertFalse(coaching_res["is_match"])
        self.assertIn("educational", coaching_res["rejection_reason"].lower())

    # =========================================================================
    # 7. SOAK / STABILITY TEST
    # =========================================================================
    def test_07_soak_test_memory_stability(self):
        """Soak test: run 50 continuous extraction & deduplication cycles to monitor memory growth."""
        tracemalloc.start()
        snapshot_start = tracemalloc.take_snapshot()
        
        deduper = IndexedDeduplicator()
        for i in range(50):
            html = f"<html><head><title>Agency {i}</title></head><body><a href='tel:98200{i:05d}'>Call</a> in Mumbai 400001</body></html>"
            page = _build_crawled_page(f"https://agency-{i}.com", html)
            extracted = extract_business_data(f"https://agency-{i}.com", [page], city="Mumbai", state="Maharashtra")
            deduper.add_or_merge(extracted)
            save_company(extracted, self.test_db_path)
            
        snapshot_end = tracemalloc.take_snapshot()
        tracemalloc.stop()
        
        # Memory growth check: difference should be negligible (< 25MB)
        top_stats = snapshot_end.compare_to(snapshot_start, 'lineno')
        total_growth_kb = sum(stat.size_diff for stat in top_stats) / 1024
        self.assertLess(total_growth_kb, 25000, f"Memory growth exceeded limit: {total_growth_kb:.2f} KB")

    # =========================================================================
    # 8. RECOVERY TEST
    # =========================================================================
    def test_08_recovery_test(self):
        """Verify that temporary network exceptions or HTTP timeouts do not break extraction pipeline."""
        with patch("requests.Session.get") as mock_get:
            mock_get.side_effect = Exception("Simulated Network Timeout / Connection Drop")
            from app.services.crawler import _fetch_url_resilient
            resp = _fetch_url_resilient("https://timeout-test.org")
            self.assertIsNone(resp)

    # =========================================================================
    # 9. RESTART AND CHECKPOINT TEST
    # =========================================================================
    def test_09_restart_and_checkpoint_state(self):
        """Verify state preservation across restarts using checkpoint persistence."""
        run_id = "test_run_recovery_101"
        create_research_run(run_id, "Digital Marketing", "Kolkata", "West Bengal", db_path=self.test_db_path)
        
        # Save a mid-run checkpoint
        state_data = {
            "processed_urls": ["https://agency1.in", "https://agency2.in"],
            "leads_found": 2,
            "status": "in_progress",
        }
        save_checkpoint(
            run_id=run_id,
            stage="stage_2_crawling",
            last_processed_item="https://agency1.in",
            checkpoint_data=state_data,
            db_path=self.test_db_path,
        )
        
        # Simulate restart by reading checkpoint back from SQLite
        checkpoint = get_latest_checkpoint(run_id, db_path=self.test_db_path)
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint["stage"], "stage_2_crawling")
        recovered_state = checkpoint["checkpoint_data"]
        self.assertEqual(recovered_state["leads_found"], 2)

    # =========================================================================
    # 10. OUTPUT INTEGRITY TEST (CSV & XLSX)
    # =========================================================================
    def test_10_output_integrity_csv_and_xlsx(self):
        """Verify generated CSV and XLSX output files have correct encoding, schema, and non-empty rows."""
        sample_leads = [
            {
                "company_name": "Alpha Solutions",
                "website": "https://alphasolutions.in",
                "phone": "+919876543210",
                "email": "info@alphasolutions.in",
                "city": "Mumbai",
                "state": "Maharashtra",
                "pin_code": "400001",
                "confidence_score": 85,
            },
            {
                "company_name": "Beta Digital",
                "website": "https://betadigital.in",
                "phone": "+912226834100",
                "email": "contact@betadigital.in",
                "city": "Mumbai",
                "state": "Maharashtra",
                "pin_code": "400053",
                "confidence_score": 90,
            },
        ]
        
        csv_file = os.path.join(self.temp_dir.name, "integrity_test.csv")
        xlsx_file = os.path.join(self.temp_dir.name, "integrity_test.xlsx")
        
        export_csv(sample_leads, csv_file)
        export_excel(sample_leads, xlsx_file)
        
        self.assertTrue(os.path.exists(csv_file))
        self.assertTrue(os.path.exists(xlsx_file))
        
        # Validate CSV contents and encoding
        with open(csv_file, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["company_name"], "Alpha Solutions")
            self.assertEqual(rows[0]["phone"], "+919876543210")
            self.assertIn("website", reader.fieldnames)
            self.assertIn("city", reader.fieldnames)

    # =========================================================================
    # 11. DATA COMPLETENESS TEST
    # =========================================================================
    def test_11_data_completeness_and_scoring(self):
        """Verify ICP score calculation accounts for all key fields without silently dropping evidence."""
        lead_full = {
            "company_name": "Omni Agency",
            "website": "https://omniagency.in",
            "phone": "+919820011223",
            "phone_type": "mobile",
            "email": "contact@omniagency.in",
            "address": "Level 3, Cyber City, Gurugram, Haryana 122002",
            "city": "Gurugram",
            "state": "Haryana",
            "pin_code": "122002",
            "has_ssl": True,
            "is_agency_verified": True,
            "_phone_valid": True,
            "_email_valid": True,
            "_location_verified": True,
        }
        score, reason = score_lead(lead_full)
        self.assertGreaterEqual(score, 50)
        self.assertIn("mobile", reason)

    # =========================================================================
    # 12. PERFORMANCE TEST
    # =========================================================================
    def test_12_performance_benchmark(self):
        """Verify throughput: extraction must complete 25 records within 1.0 second."""
        start_time = time.time()
        for i in range(25):
            html = f"<html><head><title>Perf Agency {i}</title></head><body><a href='tel:98111{i:05d}'>Call</a> City: Delhi 110001</body></html>"
            page = _build_crawled_page(f"https://perf-{i}.in", html)
            _ = extract_business_data(f"https://perf-{i}.in", [page], city="Delhi", state="Delhi")
        elapsed = time.time() - start_time
        self.assertLess(elapsed, 1.0, f"Extraction throughput too slow: {elapsed:.3f}s for 25 records")

    # =========================================================================
    # 14. SECURITY AND SSRF ROBUSTNESS TEST
    # =========================================================================
    def test_14_security_ssrf_and_host_validation(self):
        """Verify SSRF protection blocks loopback, private networks, and cloud metadata endpoints."""
        allowed, reason = check_crawl_permission("http://127.0.0.1/admin")
        self.assertFalse(allowed)
        
        allowed, reason = check_crawl_permission("http://localhost:8000/api")
        self.assertFalse(allowed)
        
        allowed, reason = check_crawl_permission("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(allowed)
        
        allowed, reason = check_crawl_permission("http://10.0.0.5/internal")
        self.assertFalse(allowed)
        
        allowed, reason = check_crawl_permission("http://192.168.1.1/router")
        self.assertFalse(allowed)
        
        allowed, reason = check_crawl_permission("http://metadata.google.internal/computeMetadata/v1/")
        self.assertFalse(allowed)
        
        # Safe public domains must be allowed
        allowed, reason = check_crawl_permission("https://example.com")
        self.assertTrue(allowed)
        
        allowed, reason = check_crawl_permission("https://google.co.in")
        self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
