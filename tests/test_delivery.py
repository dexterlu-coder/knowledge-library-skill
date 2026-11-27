import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/knowledge-library/scripts/delivery.py"
SPEC = importlib.util.spec_from_file_location("delivery", SCRIPT)
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


def target(name):
    return {"name": name, "space_id": "space", "parent_node_token": name,
            "url": f"https://example.feishu.cn/wiki/{name}"}


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(TemporaryDirectory())
        self.root = Path(temp)
        self.enterContext(patch.dict(os.environ, {"KNOWLEDGE_LIBRARY_STATE_DIR": temp}))
        self.pages = {}
        self.create_failure = None
        self.upload_failure = None
        self.enterContext(patch.object(delivery, "lark", self.mock_lark))

    def invoke(self, *args):
        with patch.object(sys, "argv", ["delivery.py", *map(str, args)]):
            return delivery.main()

    def request(self, data):
        path = self.root / "request.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def prepare(self, names, attachment=False):
        cards = [{"title": f"card {i}", "body": f"original {i}"} for i in (1, 2)]
        if attachment:
            (self.root / "source.txt").write_bytes(b"original attachment")
            cards[0]["attachments"] = ["source.txt"]
        return self.invoke("prepare", self.request({"source_id": "fixture", "cards": cards,
                                                   "targets": [target(n) for n in names]}))["delivery_id"]

    def revise(self, base, number, body):
        request = self.request({"source_id": "fixture", "delivery_id": base, "body": body})
        return self.invoke("revise", request, "--card", number)["delivery_id"]

    def mock_lark(self, service, command, *args, cwd=None):
        flags = dict(zip(args[::2], args[1::2]))
        if command == "+node-list":
            return {"nodes": [page for page in self.pages.values()
                              if page["parent_node_token"] == flags["--parent-node-token"]]}
        if command == "+node-get":
            return self.pages[flags["--node-token"]]
        if command == "+node-create":
            parent = flags["--parent-node-token"]
            if self.create_failure and parent == "B":
                raise self.create_failure
            token = str(len(self.pages) + 1)
            self.pages[token] = {"node_token": token, "obj_token": token, "title": flags["--title"],
                                 "parent_node_token": parent, "space_id": "space", "obj_type": "docx",
                                 "node_type": "origin", "body": "", "revision": 1, "files": {}}
            return self.pages[token]
        if command == "+media-download":
            token = flags["--token"]
            for page in self.pages.values():
                if token in page["files"]:
                    Path(cwd, "file").write_bytes(page["files"][token])
                    return {}
            raise AssertionError(f"No file for {token}")
        page = self.pages[flags["--doc"]]
        if command == "+fetch":
            if flags["--doc-format"] == "markdown":
                content = "# " + page["title"] + "\n\n" + page["body"]
                for token in page["files"]:
                    content += f'\n\n<figure><source name="source.txt" token="{token}"/></figure>'
            else:
                content = '<title/>' + ('<p id="body"/>' if page["body"] else "")
                for token in page["files"]:
                    content += f'<figure><source name="source.txt" token="{token}"/></figure>'
            return {"document": {"content": content, "revision_id": page["revision"]}}
        if command == "+update":
            self.assertEqual(int(flags["--revision-id"]), page["revision"])
            page["body"] = Path(cwd, "body.md").read_text(encoding="utf-8")
            page["revision"] += 1
            return {}
        if command == "+media-insert":
            if self.upload_failure:
                raise self.upload_failure
            token = "attachment-" + page["obj_token"]
            page["files"][token] = Path(cwd, flags["--file"]).read_bytes()
            return {"file_token": token}
        raise AssertionError(command)

    def test_new_delivery_distinguishes_entry_direct_and_legacy_bindings(self):
        entry = dict(target("home"), mode="entry", filing_rules=[{"parent_node_token": "methods"}])
        self.pages[entry["url"]] = {"node_token": "home", "space_id": "space", "parent_node_token": ""}
        self.invoke("configure", self.request({"targets": [entry]}))
        self.assertEqual(self.invoke("config")["targets"], [entry])
        req = {"source_id": "slides", "cards": [{"title": "method", "body": "conditions"}]}
        with self.assertRaisesRegex(ValueError, "Resolve"):
            self.invoke("prepare", self.request(req))
        chosen = dict(target("methods"), mode="direct")
        ready = self.invoke("prepare", self.request(dict(req, targets=[chosen])))
        delivery.save(self.root / "config.json", target("old"))
        with self.assertRaisesRegex(ValueError, "legacy"):
            self.invoke("prepare", self.request(req))
        receipt, = self.invoke("retry", ready["delivery_id"])["results"]
        self.assertEqual((receipt["parent_status"], receipt["target"]), ("verified", "methods"))
        delivery.save(self.root / "config.json", dict(target("inbox"), mode="direct"))
        ready = self.invoke("prepare", self.request(req))
        self.assertEqual(ready["targets"][0]["parent_node_token"], "inbox")

    def test_revision_only_writes_the_selected_card_and_target(self):
        a, b = self.prepare("A"), self.prepare("B")
        self.invoke("deliver", a)
        self.invoke("deliver", b)
        original_a = self.pages["1"]["body"]
        self.pages["3"]["body"] = "remote hand edit"
        b_draft = self.revise(b, 1, "B-only")
        self.assertIn("baseline", self.invoke("deliver", b_draft)["results"][0]["error"])
        a_revision = self.revise(a, 2, "A-only")
        self.assertEqual([2], [result["card"] for result in self.invoke("deliver", a_revision)["results"]])
        self.assertEqual(original_a, self.pages["1"]["body"])
        self.assertIn("A-only", self.pages["2"]["body"])
        self.assertEqual("remote hand edit", self.pages["3"]["body"])

    def test_confirmed_create_rejection_can_be_retried(self):
        job = self.prepare("AB")
        self.create_failure = delivery.LarkRejected("permission rejected")
        self.assertIn("error", self.invoke("deliver", job)["results"][2])
        self.create_failure = None
        self.assertTrue(all("url" in row for row in self.invoke("retry", job)["results"]))
        self.assertEqual(4, len(self.pages))
        self.assertEqual(2, self.pages["1"]["revision"])

    def test_explicit_revision_subset_cannot_expand_scope(self):
        base = self.prepare("AB")
        self.invoke("deliver", base)
        request = {"source_id": "fixture", "delivery_id": base, "body": "A-only",
                   "targets": [target("A")]}
        revision = self.invoke("revise", self.request(request), "--card", 1)["delivery_id"]
        self.assertEqual(["A"], [row["target"] for row in self.invoke("deliver", revision)["results"]])
        self.assertIn("A-only", self.pages["1"]["body"])
        self.assertNotIn("A-only", self.pages["3"]["body"])
        request["targets"] = [target("C")]
        with self.assertRaisesRegex(ValueError, "must belong"):
            self.invoke("revise", self.request(request), "--card", 1)

    def test_superseded_revision_does_not_report_success(self):
        base = self.prepare("A")
        self.invoke("deliver", base)
        older = self.revise(base, 1, "older draft")
        newer = self.revise(base, 1, "newer draft")
        self.assertIn("url", self.invoke("deliver", newer)["results"][0])
        result = self.invoke("deliver", older)["results"][0]
        self.assertIn("superseded", result["error"])
        self.assertNotIn("url", result)
        self.assertIn("newer draft", self.pages["1"]["body"])
        self.assertEqual("newer_version", self.invoke("retry", base)["results"][0]["status"])

    def test_revision_preserves_manual_first_line_indentation(self):
        base = self.prepare("A")
        self.invoke("deliver", base)
        page = self.pages["1"]
        page["body"] = "    " + page["body"]
        revision = self.revise(base, 1, "new body")
        result = self.invoke("deliver", revision)["results"][0]
        self.assertIn("differs from the verified baseline", result["error"])
        self.assertTrue(page["body"].startswith("    original 1"))

    def test_revision_preserves_manual_markdown_hard_break(self):
        base = self.prepare("A")
        self.invoke("deliver", base)
        page = self.pages["1"]
        page["body"] = page["body"].replace("original 1\n", "original 1  \n", 1)
        revision = self.revise(base, 1, "new body")
        result = self.invoke("deliver", revision)["results"][0]
        self.assertIn("differs from the verified baseline", result["error"])
        self.assertTrue(page["body"].startswith("original 1  \n"))

    def test_revision_preserves_attachment_lookalike_text(self):
        request = {"source_id": "fixture", "targets": [target("A")],
                   "cards": [{"title": "example", "body": "Example\n\n~~~html\n\n~~~"}]}
        base = self.invoke("prepare", self.request(request))["delivery_id"]
        self.invoke("deliver", base)
        page = self.pages["1"]
        literal = '<figure><source name="custom"/></figure>'
        page["body"] = page["body"].replace("~~~html\n\n", f"~~~html\n{literal}\n")
        revision = self.revise(base, 1, "new body")
        result = self.invoke("deliver", revision)["results"][0]
        self.assertIn("differs from the verified baseline", result["error"])
        self.assertIn(literal, page["body"])

    def test_readback_and_conflict_preserve_unicode_line_separator(self):
        request = {"source_id": "fixture", "targets": [target("A")],
                   "cards": [{"title": "example", "body": "code\u2028text"}]}
        base = self.invoke("prepare", self.request(request))["delivery_id"]
        self.assertIn("url", self.invoke("deliver", base)["results"][0])
        page = self.pages["1"]
        page["body"] = page["body"].replace("\u2028", "\n")
        revision = self.revise(base, 1, "new body")
        result = self.invoke("deliver", revision)["results"][0]
        self.assertIn("differs from the verified baseline", result["error"])
        self.assertIn("-code\u2028text", result["error"])
        self.assertTrue(page["body"].startswith("code\ntext"))

    def test_retry_upload_uses_snapshot_after_confirmed_rejection(self):
        job = self.prepare("A", attachment=True)
        self.upload_failure = delivery.LarkRejected("permission rejected")
        partial = self.invoke("deliver", job)["results"][0]
        self.assertIn("error", partial)
        self.assertIn("url", partial)
        self.assertEqual("partial", partial["status"])
        self.assertEqual("verified", partial["body_status"])
        self.assertEqual(["source.txt"], partial["attachments_pending"])
        (self.root / "source.txt").unlink()
        self.upload_failure = None
        resumed = self.invoke("retry", job)["results"][0]
        self.assertIn("url", resumed)
        self.assertEqual([], resumed["attachments_pending"])
        self.assertEqual(["source.txt"], resumed["attachments_verified"])
        self.assertEqual([b"original attachment"], list(self.pages["1"]["files"].values()))

    def test_unknown_create_is_reconciled_without_blind_retry(self):
        job = self.prepare("AB")
        self.create_failure = subprocess.TimeoutExpired("mock", 1)
        self.invoke("deliver", job)
        self.create_failure = None
        self.assertIn("outcome is unknown", self.invoke("retry", job)["results"][2]["error"])
        self.assertEqual(2, len(self.pages))

    def test_unknown_upload_is_reconciled_when_remote_file_exists(self):
        job = self.prepare("A", attachment=True)

        def lose_response(service, command, *args, **kwargs):
            result = self.mock_lark(service, command, *args, **kwargs)
            if command == "+media-insert":
                raise subprocess.TimeoutExpired("mock", 1)
            return result

        with patch.object(delivery, "lark", lose_response):
            self.assertIn("error", self.invoke("deliver", job)["results"][0])
        with patch.object(delivery, "lark", wraps=self.mock_lark) as cli:
            self.assertIn("url", self.invoke("retry", job)["results"][0])
            self.assertNotIn("+media-insert", [call.args[1] for call in cli.call_args_list])
        self.assertEqual(1, len(self.pages["1"]["files"]))


class LarkErrorTest(unittest.TestCase):
    def test_cli_classifies_stderr_and_respects_failed_rollback(self):
        error = json.dumps({"ok": False, "error": {"type": "authorization", "message": "denied"}})
        with patch.object(delivery.shutil, "which", return_value="lark-cli.exe"):
            with patch.object(delivery.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 3, "", error)):
                with self.assertRaises(delivery.LarkRejected):
                    delivery.lark("wiki", "+node-create")
            with patch.object(delivery.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 3, "", "warning: rollback failed for block sample: interrupted\n" + error)):
                with self.assertRaises(RuntimeError) as result:
                    delivery.lark("docs", "+media-insert")
                self.assertNotIsInstance(result.exception, delivery.LarkRejected)


if __name__ == "__main__":
    unittest.main()
