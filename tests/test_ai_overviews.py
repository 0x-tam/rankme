import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from rankme.ai_overviews import AIOCollector, _BROWSER_SNAPSHOT_JS, _browse_google, _browser_session, _parse_browser_snapshot, build_ai_overview_brief


def result(overview=None, ident="s1", status="Success"):
    response = {"search_metadata": {"id": ident, "status": status},
                "search_parameters": {"engine": "google", "q": "best panels", "gl": "us", "hl": "en",
                                      "location_used": "Austin, Texas, United States", "device": "desktop"}}
    if overview is not None:
        response["ai_overview"] = overview
    return response


PRESENT = {"text_blocks": [{"type": "paragraph", "snippet": "Solar panels need regular cleaning.",
                            "reference_indexes": [0]},
                           {"type": "list", "list": [{"snippet": "Check output monthly."}]}],
           "references": [{"index": 0, "link": "https://example.com/guide?utm_source=x", "title": "Panel guide",
                           "source": "Example", "snippet": "Maintenance guide."},
                          {"index": 1, "link": "http://127.0.0.1/internal", "title": "Unsafe"}]}

BROWSER_SNAPSHOT = {"http_status": 200, "final_url": "https://www.google.com/search?q=best+panels&gl=us&hl=en",
                    "returned_query": "best panels", "heading_count": 1, "container_verified": True,
                    "container_text": "AI Overview\nSolar panels need regular cleaning and an output check each month.",
                    "anchors": [{"url": "https://example.com/guide", "title": "Guide"},
                                {"url": "https://www.google.com/url?url=https%3A%2F%2Fpublisher.example%2Farticle", "title": "Article"},
                                {"url": "https://support.google.com/privacy", "title": "Privacy"}],
                    "organic_result_count": 3, "search_root_present": True,
                    "challenge_text": False, "generating": False}


class AIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.moment = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        self.collector = AIOCollector(self.temp.name, clock=lambda: self.moment)

    def configure(self, cap=4):
        path = Path(self.temp.name) / "ai_overviews_config.json"
        path.write_text(json.dumps({"provider": "serpapi", "api_key": "test-private-key",
                                    "daily_call_cap": cap, "enabled": True}))
        os.chmod(path, 0o600)
        return path

    def collect(self, client="client1", query="best panels"):
        return self.collector.collect(client, query, country="us", language="en",
                                      location="Austin, Texas, United States", device="desktop")

    def summary(self, client="client1", query="best panels"):
        return self.collector.summarize(client, query, country="us", language="en",
                                        location="Austin, Texas, United States", device="desktop")

    def test_disabled_without_private_config_no_request(self):
        with patch("rankme.ai_overviews._request_json") as fetch:
            record = self.collect()
        fetch.assert_not_called()

    def test_symlink_and_hardlink_config_are_rejected(self):
        path = self.configure()
        alias = Path(self.temp.name) / "alias.json"
        os.link(path, alias)
        self.assertEqual(self.collector.readiness()["status"], "blocked")
        alias.unlink()
        path.unlink()
        target = Path(self.temp.name) / "other.json"
        target.write_text('{}')
        os.chmod(target, 0o600)
        path.symlink_to(target)
        self.assertEqual(self.collector.readiness()["status"], "blocked")

    def test_reserved_call_survives_restart_without_network_result(self):
        self.configure(cap=1)
        reservation = self.collector._reserve("google", 1)
        self.assertIsNotNone(reservation)
        restarted = AIOCollector(self.temp.name, clock=lambda: self.moment)
        self.assertEqual(restarted.readiness()["calls_today"], 1)
        with patch("rankme.ai_overviews._request_json") as fetch:
            record = restarted.collect("client1", "best panels")
        fetch.assert_not_called()
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(self.collector.readiness()["status"], "blocked")
        path = self.configure()
        os.chmod(path, 0o644)
        with patch("rankme.ai_overviews._request_json") as fetch:
            self.assertEqual(self.collect()["status"], "blocked")
        fetch.assert_not_called()

    def test_inline_present_normalized_with_provenance_and_no_secret(self):
        self.configure()
        with patch("rankme.ai_overviews._request_json", return_value=result(PRESENT)) as fetch:
            record = self.collect()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[1]["q"], "best panels")
        self.assertEqual(record["status"], "present")
        self.assertEqual(record["answer"], "Solar panels need regular cleaning.\nCheck output monthly.")
        self.assertEqual([c["url"] for c in record["citations"]], ["https://example.com/guide"])
        self.assertEqual(record["provider_search_ids"], ["s1"])
        self.assertEqual(record["source"], "provider")
        self.assertNotIn("test-private-key", json.dumps(record))
        self.assertNotIn("test-private-key", json.dumps(self.collector.history("client1", "best panels", location="Austin, Texas, United States")))
        self.assertEqual(self.collector.readiness()["calls_today"], 1)

    def test_followup_token_counts_two_calls_and_never_stores_token(self):
        self.configure(cap=2)
        first = result({"page_token": "private-token"})
        second = result(PRESENT, ident="s2")
        with patch("rankme.ai_overviews._request_json", side_effect=[first, second]) as fetch:
            record = self.collect()
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.args[1]["engine"], "google_ai_overview")
        self.assertEqual(fetch.call_args.args[1]["page_token"], "private-token")
        self.assertEqual(record["status"], "present")
        self.assertEqual(len(record["request_ids"]), 2)
        self.assertNotIn("private-token", json.dumps(record))
        self.assertEqual(self.collector.readiness()["calls_today"], 2)
        with patch("rankme.ai_overviews._request_json") as more:
            self.assertEqual(self.collect()["status"], "blocked")
        more.assert_not_called()

    def test_followup_budget_exhaustion_is_blocked_not_absent(self):
        self.configure(cap=1)
        with patch("rankme.ai_overviews._request_json", return_value=result({"page_token": "token"})) as fetch:
            record = self.collect()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["reason"], "daily_cap_reached_before_followup")

    def test_provider_cohort_mismatch_is_error_without_followup(self):
        self.configure(cap=2)
        response = result({"page_token": "token"})
        response["search_parameters"]["gl"] = "gb"
        with patch("rankme.ai_overviews._request_json", return_value=response) as fetch:
            record = self.collect()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(record["status"], "error")
        self.assertEqual(self.collector.readiness()["calls_today"], 1)

    def test_absent_error_and_transport_failure_are_distinct(self):
        self.configure(cap=4)
        with patch("rankme.ai_overviews._request_json", side_effect=[result(), result(status="Error"), ValueError("secret-in-exception")]):
            absent, provider_error, transport_error = self.collect(), self.collect(), self.collect()
        self.assertEqual([x["status"] for x in (absent, provider_error, transport_error)],
                         ["absent", "error", "error"])
        self.assertNotIn("secret-in-exception", json.dumps([provider_error, transport_error]))
        self.assertEqual(self.summary()["sample_days"], 1)

    def test_manual_requires_provenance_and_deduplicates_exact_cohort(self):
        payload = {"query": "best panels", "country": "us", "language": "en",
                   "location": "Austin, Texas, United States", "device": "desktop",
                   "observed_at": "2026-10-07T12:00:00Z", "status": "present",
                   "answer": "An observed overview.", "citations": [{"url": "https://source.example/"}],
                   "evidence_url": "https://www.google.com/search?q=best+panels"}
        first = self.collector.import_observation("client1", payload)
        second = self.collector.import_observation("client1", payload)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["source"], "manual")
        self.assertEqual(first["citations"][0]["verification"], "manually_reported")
        self.assertEqual(self.summary()["sample_days"], 1)
        with self.assertRaises(ValueError):
            self.collector.import_observation("client1", {**payload, "evidence_url": "", "evidence_note": ""})
        with self.assertRaises(ValueError):
            self.collector.import_observation("client1", {**payload, "observed_at": "2026-10-06T12:00:00Z",
                                                          "evidence_url": "https://example.com/?api_key=private",
                                                          "evidence_note": ""})

    def test_credential_shaped_citation_urls_are_dropped(self):
        self.configure()
        overview = {**PRESENT, "references": [
            {"link": "https://source.example/article?access_token=private", "title": "Token"},
            {"link": "https://source.example/article?X-Amz-Signature=private", "title": "Signed"},
            {"link": "https://source.example/article?author=Jane", "title": "Author"},
            {"link": "https://source.example/article?part=1", "title": "Safe"}]}
        with patch("rankme.ai_overviews._request_json", return_value=result(overview)):
            record = self.collect()
        self.assertEqual([c["url"] for c in record["citations"]],
                         ["https://source.example/article?author=Jane", "https://source.example/article?part=1"])

    def test_seven_distinct_days_frequency_changes_and_client_isolation(self):
        for index in range(7):
            self.moment = datetime(2026, 10, index + 1, 12, tzinfo=timezone.utc)
            payload = {"query": "best panels", "country": "us", "language": "en",
                       "location": "Austin, Texas, United States", "device": "desktop",
                       "observed_at": self.moment.isoformat(), "status": "present" if index < 6 else "absent",
                       "answer": "Solar panels need regular cleaning." if index < 6 else "",
                       "citations": [{"url": "https://source.example/a"}] if index < 6 else [],
                       "evidence_note": "Browser observation on the dated query"}
            self.collector.import_observation("client1", payload)
            self.collector.import_observation("client2", payload)
        self.moment = datetime(2026, 10, 7, 13, tzinfo=timezone.utc)
        summary = self.summary()
        self.assertTrue(summary["enough_history"])
        self.assertEqual(summary["sample_days"], 7)
        self.assertEqual(summary["basis_source"], "manual")
        self.assertEqual(summary["present_days"], 6)
        self.assertEqual(summary["citations"][0]["days_cited"], 6)
        self.assertEqual(summary["citations"][0]["frequency"], 1.0)
        self.assertEqual(summary["citation_changes"][-1]["lost"], ["https://source.example/a"])
        self.assertEqual(build_ai_overview_brief(summary)["status"], "observed_history")
        self.collector.delete_client("client1")
        self.assertEqual(self.summary()["sample_days"], 0)
        self.assertEqual(self.summary(client="client2")["sample_days"], 7)

    def test_mixed_sources_do_not_form_false_seven_day_history(self):
        for index in range(7):
            self.moment = datetime(2026, 10, index + 1, 12, tzinfo=timezone.utc)
            if index < 4:
                self.configure(cap=10)
                with patch("rankme.ai_overviews._request_json", return_value=result(PRESENT)):
                    self.collect()
            else:
                self.collector.import_observation("client1", {
                    "query": "best panels", "country": "us", "language": "en",
                    "location": "Austin, Texas, United States", "device": "desktop",
                    "observed_at": self.moment.isoformat(), "status": "present",
                    "answer": "Observed browser overview.", "evidence_note": "Dated browser check"})
        self.moment = datetime(2026, 10, 7, 13, tzinfo=timezone.utc)
        summary = self.summary()
        self.assertEqual(summary["total_observed_days"], 7)
        self.assertEqual(summary["sample_days"], 4)
        self.assertEqual(summary["basis_source"], "provider")
        self.assertFalse(summary["enough_history"])
        self.assertEqual(summary["source_breakdown"]["manual"]["sample_days"], 3)

    def test_deep_nested_blocks_are_bounded(self):
        nested = {"snippet": "deep"}
        for _ in range(100):
            nested = {"snippet": "outer", "list": [nested]}
        self.configure()
        with patch("rankme.ai_overviews._request_json", return_value=result({"text_blocks": [nested]})):
            record = self.collect()
        self.assertEqual(record["status"], "present")
        self.assertLessEqual(record["answer"].count("outer"), 9)

    def test_browser_snapshot_classification_and_source_filtering(self):
        # Google's visible Search combobox is a textarea on the observed page.
        self.assertIn('textarea[name="q"],input[name="q"]', _BROWSER_SNAPSHOT_JS)
        status, reason, answer, citations, evidence = _parse_browser_snapshot(BROWSER_SNAPSHOT, "best panels")
        self.assertEqual(status, "present")
        self.assertIn("Solar panels", answer)
        self.assertEqual([c["url"] for c in citations], ["https://example.com/guide", "https://publisher.example/article"])
        self.assertEqual(citations[0]["verification"], "browser_observed")
        self.assertEqual(evidence["heading_count"], 1)
        absent = {**BROWSER_SNAPSHOT, "heading_count": 0, "container_text": "", "anchors": []}
        self.assertEqual(_parse_browser_snapshot(absent, "best panels")[0], "absent")
        self.assertEqual(_parse_browser_snapshot({**absent, "body_mentions_overview": True}, "best panels")[0], "error")
        self.assertEqual(_parse_browser_snapshot({**absent, "organic_result_count": 0}, "best panels")[0], "error")
        self.assertEqual(_parse_browser_snapshot({**absent, "challenge_text": True}, "best panels")[0], "blocked")
        self.assertEqual(_parse_browser_snapshot({**absent, "generating": True}, "best panels")[0], "error")
        self.assertEqual(_parse_browser_snapshot({**BROWSER_SNAPSHOT, "returned_query": "other"}, "best panels")[0], "error")
        organic_named_overview = {**BROWSER_SNAPSHOT, "container_verified": False}
        self.assertEqual(_parse_browser_snapshot(organic_named_overview, "best panels")[0], "error")
        oversized_link = {**BROWSER_SNAPSHOT, "anchors": [{"url": "https://example.com/" + "a" * 3000, "title": "Too long"}]}
        self.assertEqual(_parse_browser_snapshot(oversized_link, "best panels")[3], [])

    def test_free_browser_collection_cap_and_separate_history(self):
        with patch("rankme.ai_overviews._browser_runtime", return_value=("/fake/chrome", "ready")), \
             patch("rankme.ai_overviews._browse_google", return_value=BROWSER_SNAPSHOT) as browse:
            self.assertEqual(self.collector.browser_readiness()["status"], "ready")
            records = [self.collector.collect_browser("client1", "best panels") for _ in range(6)]
            blocked = self.collector.collect_browser("client1", "best panels")
        self.assertEqual(browse.call_count, 6)
        self.assertTrue(all(r["status"] == "present" and r["source"] == "browser" for r in records))
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "daily_cap_reached")
        self.assertEqual(self.collector.browser_readiness()["calls_today"], 6)
        self.assertEqual(self.collector.readiness()["calls_today"], 0)
        self.assertEqual(self.collector.summarize("client1", "best panels")["basis_source"], "browser")

    def test_browser_rejects_unsupported_cohort_without_launch(self):
        with patch("rankme.ai_overviews._browser_runtime") as runtime, \
             patch("rankme.ai_overviews._browse_google") as browse:
            location = self.collector.collect_browser("client1", "best panels", location="Beirut")
            device = self.collector.collect_browser("client1", "best panels", device="mobile")
            language = self.collector.collect_browser("client1", "best panels", language="ar")
        runtime.assert_not_called()
        browse.assert_not_called()
        self.assertTrue(all(r["reason"] == "unsupported_browser_cohort" for r in (location, device, language)))

    def test_browser_busy_and_exception_never_claim_absence(self):
        with patch("rankme.ai_overviews._browser_runtime", return_value=("/fake/chrome", "ready")), \
             patch("rankme.ai_overviews.fcntl.flock", side_effect=BlockingIOError), \
             patch("rankme.ai_overviews._browse_google") as browse:
            busy = self.collector.collect_browser("client1", "best panels")
        self.assertEqual(busy["reason"], "browser_busy")
        browse.assert_not_called()
        self.assertEqual(self.collector.browser_readiness()["calls_today"], 0)
        with patch("rankme.ai_overviews._browser_runtime", return_value=("/fake/chrome", "ready")), \
             patch("rankme.ai_overviews._browse_google", side_effect=RuntimeError("private page data")):
            failed = self.collector.collect_browser("client1", "best panels")
        self.assertEqual(failed["status"], "error")
        self.assertNotIn("private page data", json.dumps(failed))
        self.assertEqual(self.collector.browser_readiness()["calls_today"], 1)

    def test_browser_launch_uses_sandbox_fixed_search_and_scrubbed_environment(self):
        page = Mock()
        page.goto.return_value.status = 200
        page.get_by_role.return_value.count.return_value = 0
        page.evaluate.return_value = dict(BROWSER_SNAPSHOT)
        context = Mock(new_page=Mock(return_value=page))
        manager = Mock()
        manager.__enter__ = Mock(return_value=Mock(chromium=Mock(launch_persistent_context=Mock(return_value=context))))
        manager.__exit__ = Mock(return_value=False)
        package, api = types.ModuleType("playwright"), types.ModuleType("playwright.sync_api")
        api.sync_playwright = Mock(return_value=manager)
        with patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": api}), \
             patch.dict(os.environ, {"TYPESAFE_API_KEY": "fake-secret"}):
            snapshot = _browser_session("best panels", "us", "en", Path(self.temp.name) / "profile", "/fake/chrome")
        self.assertEqual(snapshot["http_status"], 200)
        launch = manager.__enter__.return_value.chromium.launch_persistent_context
        self.assertTrue(launch.call_args.kwargs["chromium_sandbox"])
        self.assertFalse(launch.call_args.kwargs["accept_downloads"])
        self.assertNotIn("TYPESAFE_API_KEY", launch.call_args.kwargs["env"])
        self.assertNotIn("--no-sandbox", launch.call_args.kwargs["args"])
        url = page.goto.call_args.args[0]
        self.assertTrue(url.startswith("https://www.google.com/search?"))
        self.assertNotIn("udm=50", url)
        route_handler = context.route.call_args.args[1]
        external, google = Mock(request=Mock(url="https://evil.example/")), Mock(request=Mock(url="https://www.google.com/search"))
        route_handler(external)
        route_handler(google)
        external.abort.assert_called_once()
        google.continue_.assert_called_once()

    def test_browser_worker_has_hard_deadline_and_no_query_in_argv(self):
        process = Mock(returncode=0, pid=12345)
        process.communicate.return_value = (json.dumps(BROWSER_SNAPSHOT), "")
        process.poll.return_value = 0
        with patch("rankme.ai_overviews.subprocess.Popen", return_value=process) as launch, \
             patch("rankme.ai_overviews.os.killpg"), \
             patch.dict(os.environ, {"TYPESAFE_API_KEY": "fake-secret"}):
            snapshot = _browse_google("best panels", "us", "en", Path(self.temp.name) / "profile", "/fake/chrome")
        self.assertEqual(snapshot["http_status"], 200)
        self.assertNotIn("best panels", " ".join(launch.call_args.args[0]))
        self.assertIn("best panels", process.communicate.call_args.args[0])
        self.assertEqual(process.communicate.call_args.kwargs["timeout"], 55)
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        self.assertNotIn("TYPESAFE_API_KEY", launch.call_args.kwargs["env"])

    def test_browser_worker_timeout_kills_process_group(self):
        from subprocess import TimeoutExpired
        process = Mock(returncode=None, pid=12345)
        process.communicate.side_effect = [TimeoutExpired("browser", 55), ("", "")]
        process.poll.return_value = 0
        with patch("rankme.ai_overviews.subprocess.Popen", return_value=process), \
             patch("rankme.ai_overviews.os.killpg") as kill_group:
            with self.assertRaises(ValueError):
                _browse_google("best panels", "us", "en", Path(self.temp.name) / "profile", "/fake/chrome")
        self.assertGreaterEqual(kill_group.call_count, 1)


if __name__ == "__main__":
    unittest.main()
