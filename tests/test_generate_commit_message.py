import unittest

from scripts.generate_commit_message import build_commit_message


class BuildCommitMessageTests(unittest.TestCase):
    def test_docs_changes_use_docs_prefix(self):
        message = build_commit_message(
            [
                {
                    "path": "README.md",
                    "status": "M",
                    "old_path": None,
                    "added": 8,
                    "deleted": 2,
                }
            ]
        )

        self.assertTrue(message.startswith("docs: "))
        self.assertIn("README", message)

    def test_new_source_file_uses_feat_prefix(self):
        message = build_commit_message(
            [
                {
                    "path": "src/webhooks.py",
                    "status": "A",
                    "old_path": None,
                    "added": 42,
                    "deleted": 0,
                }
            ]
        )

        self.assertTrue(message.startswith("feat: "))
        self.assertIn("webhooks", message)

    def test_github_workflow_changes_use_ci_prefix(self):
        message = build_commit_message(
            [
                {
                    "path": ".github/workflows/publish.yml",
                    "status": "M",
                    "old_path": None,
                    "added": 3,
                    "deleted": 1,
                }
            ]
        )

        self.assertTrue(message.startswith("ci: "))
        self.assertIn("publish workflow", message)


if __name__ == "__main__":
    unittest.main()
