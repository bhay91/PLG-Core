from pathlib import Path
import unittest

from tests.test_workflow_action_cleanup_batch2 import WorkflowActionCleanupBatch2Tests


ROOT = Path(__file__).resolve().parents[1]


class VisualResponsiveCleanupBatch4Tests(WorkflowActionCleanupBatch2Tests):
    def test_command_center_contrast_rules_are_scoped_and_theme_safe(self):
        css = (ROOT / "static" / "pps_erp.css").read_text()
        self.assertIn(".erp-app .job-command-header :is(h1,h2,h3,h4,h5,h6,strong)", css)
        self.assertIn(".erp-app .job-command-header .job-command-next > strong", css)
        self.assertIn("var(--erp-text)", css)
        self.assertNotIn("body h1", css)

    def test_secondary_groups_collapse_when_empty_and_active_group_stays_open(self):
        empty_job, _ = self.job()
        empty_html = self.render(empty_job)
        group_start = empty_html.index('<details class="cc-card job-command-group" id="parts-purchasing-group"')
        group_tag = empty_html[group_start:empty_html.index(">", group_start) + 1]
        self.assertNotIn(" open", group_tag)

        self.part(empty_job)
        active_html = self.render(empty_job)
        active_start = active_html.index('<details class="cc-card job-command-group" id="parts-purchasing-group"')
        active_tag = active_html[active_start:active_html.index(">", active_start) + 1]
        self.assertIn(" open", active_tag)

    def test_anchor_reveal_and_secondary_context_are_preserved(self):
        template = (ROOT / "templates" / "job_command_center_advanced.html").read_text()
        self.assertIn('"#technical-context"', template)
        self.assertIn('"#commercial-history"', template)
        self.assertIn("const revealTarget = (target)", template)
        self.assertIn("node instanceof HTMLDetailsElement", template)
        self.assertIn("window.location.hash", template)
        self.assertEqual(self.render(self.job()[0]).count('class="job-command-next"'), 1)

    def test_responsive_rules_cover_command_center_intake_and_follow_up(self):
        css = (ROOT / "static" / "pps_erp.css").read_text()
        self.assertIn("@media (max-width: 780px)", css)
        self.assertIn("@media (max-width: 480px)", css)
        smart = (ROOT / "templates" / "smart_intake.html").read_text()
        follow = (ROOT / "templates" / "follow_up.html").read_text()
        self.assertIn("smart-intake", smart)
        self.assertIn("@media(max-width:760px)", follow)
        self.assertIn("follow-view-tabs", follow)


if __name__ == "__main__":
    unittest.main()
