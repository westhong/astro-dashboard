import json
import tempfile
import unittest
from pathlib import Path

from backend.ai_analysis import merge_analysis


class AiAnalysisMergeTests(unittest.TestCase):
    def setUp(self):
        self.report = {
            "generated_utc": "2026-07-31T07:00:00+00:00",
            "locations": [
                {"location_id": "a", "error": False},
                {"location_id": "b", "error": False},
            ],
        }
        self.analysis = {
            "schema_version": 1,
            "source_generated_utc": "2026-07-31T07:00:00+00:00",
            "analyzed_at": "2026-07-31T07:05:00+00:00",
            "analyst": "愛 / Hermes",
            "status": "verified",
            "headline": "今晚不建議追銀河",
            "summary": "月光與低空銀心共同限制畫面。",
            "confidence": "high",
            "best_location_id": "a",
            "key_factors": ["月亮整夜重疊"],
            "uncertainties": [],
            "location_notes": {
                "a": {"verdict": "不建議", "photographic_meaning": "月光削弱銀河對比。"},
                "b": {"verdict": "備選", "photographic_meaning": "雲較少但構圖仍受月光限制。"},
            },
            "verification": {
                "score_recalculated": True,
                "best_window_recalculated": True,
                "source_fields_checked": True,
            },
        }

    def test_merges_verified_analysis_into_matching_report(self):
        with tempfile.TemporaryDirectory() as td:
            report_path = Path(td) / "report.json"
            analysis_path = Path(td) / "analysis.json"
            report_path.write_text(json.dumps(self.report), encoding="utf-8")
            analysis_path.write_text(json.dumps(self.analysis), encoding="utf-8")

            merge_analysis(report_path, analysis_path)

            merged = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(merged["ai_analysis"]["headline"], "今晚不建議追銀河")
            self.assertEqual(merged["ai_analysis"]["source_generated_utc"], merged["generated_utc"])

    def test_rejects_analysis_for_stale_report(self):
        self.analysis["source_generated_utc"] = "2026-07-31T06:00:00+00:00"
        with tempfile.TemporaryDirectory() as td:
            report_path = Path(td) / "report.json"
            analysis_path = Path(td) / "analysis.json"
            report_path.write_text(json.dumps(self.report), encoding="utf-8")
            analysis_path.write_text(json.dumps(self.analysis), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source_generated_utc"):
                merge_analysis(report_path, analysis_path)

    def test_rejects_missing_location_note(self):
        del self.analysis["location_notes"]["b"]
        with tempfile.TemporaryDirectory() as td:
            report_path = Path(td) / "report.json"
            analysis_path = Path(td) / "analysis.json"
            report_path.write_text(json.dumps(self.report), encoding="utf-8")
            analysis_path.write_text(json.dumps(self.analysis), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "location_notes"):
                merge_analysis(report_path, analysis_path)


class FrontendContractTests(unittest.TestCase):
    def test_template_uses_fixed_formula_without_ai_analysis(self):
        html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("雲 45%", html)
        self.assertIn("固定公式更新", html)
        self.assertNotIn("data.ai_analysis", html)
        self.assertNotIn("locAnalysis", html)


class WorkflowContractTests(unittest.TestCase):
    def test_github_workflow_is_manual_fallback_not_scheduled_publisher(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("cron:", workflow)


if __name__ == "__main__":
    unittest.main()
