"""Persistent Feishu delivery workflow. Requires Python 3.10+ and lark-cli."""

import argparse
from contextlib import closing, contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


def digest(data):
    return hashlib.sha256(data).hexdigest()


def state_dir():
    base = os.getenv("LOCALAPPDATA") if os.name == "nt" else os.getenv("XDG_CONFIG_HOME")
    default = Path(base or Path.home() / ".config") / "knowledge-library"
    return Path(os.getenv("KNOWLEDGE_LIBRARY_STATE_DIR", default)).resolve()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, data):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False) as output:
        temporary = Path(output.name)
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def lock(root):
    # State is committed separately. A killed process releases the SQLite lock.
    with closing(sqlite3.connect(root / "workflow.lock", timeout=120)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        yield


def targets(data):
    values = data["targets"] if "targets" in data else [data]
    keys = set()
    for target in values:
        if target.get("mode") not in (None, "entry", "direct"):
            raise ValueError("Unknown binding mode")
        key = target_key(target)
        address = urlsplit(target["url"])
        if (key in keys or not target["name"].strip() or address.scheme != "https"
                or address.username or address.password or address.port
                or not address.hostname or not address.hostname.endswith((".feishu.cn", ".larksuite.com"))
                or address.path.rstrip("/") != "/wiki/" + target["parent_node_token"]):
            raise ValueError("Invalid or repeated Feishu target")
        keys.add(key)
    if not values:
        raise ValueError("At least one Feishu target is required")
    return values


def target_key(target):
    return target["space_id"] + ":" + target["parent_node_token"]


class LarkRejected(RuntimeError):
    """The command could not leave a completed remote write."""


def error_envelope(output):
    # The CLI can emit a rollback warning before its final JSON error on stderr.
    for match in re.finditer(r"(?m)^\{", output):
        try:
            result = json.loads(output[match.start():])
        except ValueError:
            continue
        if isinstance(result, dict) and result.get("ok") is False and isinstance(result.get("error"), dict):
            return result["error"]
    return {}


def lark(*args, cwd=None):
    launcher = shutil.which("lark-cli")
    if not launcher:
        raise LarkRejected("lark-cli is missing from PATH")
    command = [launcher]
    if os.name == "nt" and Path(launcher).suffix.lower() in {".cmd", ".bat"}:
        script = Path(launcher).parent / "node_modules/@larksuite/cli/scripts/run.js"
        node = shutil.which("node.exe")
        if not script.is_file() or not node:
            raise LarkRejected("Install the official lark-cli npm launcher or use its native executable")
        command = [node, str(script)]
    try:
        completed = subprocess.run([*command, *args, "--as", "user"], cwd=cwd,
                                   capture_output=True, encoding="utf-8", errors="replace", timeout=90)
    except OSError as error:
        raise LarkRejected(f"Could not launch lark-cli: {error}") from error
    if completed.returncode:
        error = error_envelope(completed.stderr) or error_envelope(completed.stdout)
        rejected = error.get("type") in {"validation", "authentication", "authorization", "config"}
        if args[1] == "+media-insert":
            hint = error.get("hint", "")
            # v1.0.98 reports failed rollback in stderr or in the bind recovery hint.
            if "warning: rollback failed for block " in completed.stderr or "rollback=failed" in hint:
                rejected = False
            elif "phase=bind_media" in hint and "rollback=succeeded" in hint:
                rejected = True
        outcome = LarkRejected if rejected else RuntimeError
        raise outcome(error.get("message") or completed.stderr.strip() or f"lark-cli exit {completed.returncode}")
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        raise RuntimeError(completed.stderr.strip() or f"lark-cli exit {completed.returncode}") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Unexpected lark-cli success response")
    return result["data"]


def source_key(request, path):
    if request.get("source_file"):
        return "sha256:" + digest((path.parent / request["source_file"]).read_bytes())
    return request["source_id"]


def check_legacy(root, source, selected):
    key = source.removeprefix("sha256:")
    if not re.fullmatch(r"[\da-f]{64}", key):
        return
    record = root / "deliveries" / (key + ".json")
    if record.exists() and target_key(read(record)["target"]) in set(map(target_key, selected)):
        raise RuntimeError(f"Existing legacy delivery needs reconciliation before writing: {record}")


def snapshot(root, path):
    data = path.read_bytes()
    checksum = digest(data)
    folder = root / "attachments" / checksum
    folder.mkdir(parents=True, exist_ok=True)
    stored = folder / path.name
    if not stored.exists():
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.replace(temporary, stored)
        finally:
            temporary.unlink(missing_ok=True)
    if digest(stored.read_bytes()) != checksum:
        raise RuntimeError(f"Attachment snapshot mismatch: {path.name}")
    return {"name": path.name, "sha256": checksum}


def scope(node, target):
    if (node["space_id"] != target["space_id"]
            or node["parent_node_token"] != target["parent_node_token"]
            or node["obj_type"] != "docx" or node["node_type"] != "origin"):
        raise RuntimeError("Existing page is outside the bound parent or is not an original document")


def locate(record, target, marker, title, checkpoint):
    if record.get("node_token"):
        node = lark("wiki", "+node-get", "--node-token", record["node_token"],
                    "--space-id", target["space_id"])
        scope(node, target)
        return node
    response = lark("wiki", "+node-list", "--space-id", target["space_id"],
                    "--parent-node-token", target["parent_node_token"], "--page-all", "--page-limit", "0")
    matches = [node for node in response["nodes"] if node["title"].endswith(f"[{marker}]")]
    if response.get("has_more") or len(matches) > 1:
        raise RuntimeError("Cannot establish a unique existing node")
    if matches:
        node = matches[0]
    elif record.get("creating"):
        raise RuntimeError("Previous creation outcome is unknown. Locate the page before retrying creation")
    else:
        record["creating"] = True
        checkpoint()
        try:
            node = lark("wiki", "+node-create", "--space-id", target["space_id"],
                        "--parent-node-token", target["parent_node_token"], "--title", f"{title} [{marker}]")
        except LarkRejected:
            record.pop("creating")
            checkpoint()
            raise
    scope(node, target)
    record["node_token"] = node["node_token"]
    record.pop("creating", None)
    checkpoint()
    return node


def fetch(node, format, *options):
    return lark("docs", "+fetch", "--doc", node["node_token"], "--doc-format", format,
                *options)["document"]


def normalized(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def attachment_token(element):
    if element.tag == "figure" and len(element) == 1 and element[0].tag == "source":
        return element[0].get("token")
    return None


def document_blocks(node, revision):
    xml = fetch(node, "xml", "--detail", "with-ids", "--revision-id", str(revision))
    return list(ET.fromstring("<doc>" + xml["content"] + "</doc>"))


def markdown_body(node, document):
    lines = normalized(document["content"]).split("\n")
    if lines and lines[0].startswith("# "):
        lines.pop(0)
    body = normalized("\n".join(lines))
    last = body.rsplit("\n", 1)[-1]
    if not re.fullmatch(r"<figure\b[^\n]*><source [^\n]*?/></figure>", last):
        return body
    # Only remove trailing rendered attachments confirmed by the XML block
    # list for the same revision. Preserve lookalike text in the card body.
    blocks = document_blocks(node, document["revision_id"])
    for block in reversed(blocks):
        token = attachment_token(block)
        body = body.rstrip("\n")
        prefix, _, last = body.rpartition("\n")
        if not token or not re.fullmatch(r"<figure\b[^\n]*><source [^\n]*?/></figure>", last):
            break
        try:
            if attachment_token(ET.fromstring(last)) != token:
                break
        except ET.ParseError:
            break
        body = prefix
    return normalized(body)


def update_body(node, record, desired, *, base_version=None, legacy_revision=False):
    document = fetch(node, "markdown")
    actual = markdown_body(node, document)
    if actual == desired:
        return
    if legacy_revision:
        raise RuntimeError("Legacy revision needs reconciliation before writing")
    if base_version is not None and record.get("version", 0) != base_version:
        raise RuntimeError("Revision base is no longer current. Prepare against the verified version")
    baseline = record.get("baseline")
    if (baseline is not None and actual != baseline) or (baseline is None and actual):
        diff = "\n".join(difflib.unified_diff((baseline or desired).split("\n"), actual.split("\n"),
                                               fromfile="verified", tofile="Feishu", lineterm=""))
        raise RuntimeError("Feishu body differs from the verified baseline\n" + diff)
    options = ["--command", "append"]
    if baseline is not None:
        children = document_blocks(node, document["revision_id"])
        blocks = [element for element in children if element.tag != "title"]
        text_blocks = [element for element in blocks if attachment_token(element) is None]
        if not text_blocks or blocks[:len(text_blocks)] != text_blocks:
            raise RuntimeError("Cannot replace the managed body without touching attachment blocks")
        options = ["--command", "block_replace", "--start-block-id", text_blocks[0].attrib["id"],
                   "--end-block-id", text_blocks[-1].attrib["id"]]
    with tempfile.TemporaryDirectory() as folder:
        Path(folder, "body.md").write_text(desired, encoding="utf-8")
        lark("docs", "+update", "--doc", node["obj_token"], *options, "--doc-format", "markdown",
             "--revision-id", str(document["revision_id"]), "--content", "@body.md", cwd=folder)
    if markdown_body(node, fetch(node, "markdown")) != desired:
        raise RuntimeError("Body readback did not match")


def media_hash(token):
    with tempfile.TemporaryDirectory() as folder:
        lark("docs", "+media-download", "--token", token, "--output", "file", cwd=folder)
        files = list(Path(folder).iterdir())
        if len(files) != 1:
            raise RuntimeError("Attachment download did not produce a unique file")
        return digest(files[0].read_bytes())


def attach(root, node, record, attachment, checkpoint):
    name, checksum = attachment["name"], attachment["sha256"]
    path = root / "attachments" / checksum / name
    document = fetch(node, "xml")
    sources = ET.fromstring("<doc>" + document["content"] + "</doc>").findall(".//source")
    key = checksum + ":" + name
    attachments = record.setdefault("attachments", {})
    previous = attachments.get(key)
    matching = [item.attrib["token"] for item in sources
                if item.get("name") == name and item.get("token")
                and media_hash(item.attrib["token"]) == checksum]
    if len(matching) > 1 or (previous and previous not in matching):
        raise RuntimeError(f"Attachment state conflict: {name}")
    if matching:
        token = matching[0]
    else:
        if key in attachments:
            raise RuntimeError(f"Previous upload outcome is unknown: {name}")
        data = path.read_bytes()
        if digest(data) != checksum:
            raise RuntimeError(f"Attachment snapshot mismatch: {name}")
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, name).write_bytes(data)
            attachments[key] = None
            checkpoint()
            try:
                uploaded = lark("docs", "+media-insert", "--doc", node["obj_token"], "--file",
                                name, "--type", "file", cwd=folder)
            except LarkRejected:
                attachments.pop(key)
                checkpoint()
                raise
        token = uploaded["file_token"]
        document = fetch(node, "xml")
        if token not in {item.get("token") for item in
                         ET.fromstring("<doc>" + document["content"] + "</doc>").findall(".//source")}:
            raise RuntimeError(f"Attachment missing from readback: {name}")
        if media_hash(token) != checksum:
            raise RuntimeError(f"Attachment readback mismatch: {name}")
    attachments[key] = token
    checkpoint()


def run_delivery(root, source, job, checkpoint):
    results = []
    for target in job["targets"]:
        for index, card in delivery_cards(job):
            card_id = digest(f"{source}\n{index}".encode())
            record = job["pages"].setdefault(target_key(target) + ":" + card_id, {})
            desired = normalized(f'{card["body"]}\n\n投递标识 {card_id}\n\n版本 {card["version"]}')
            result = {"target": target["name"], "card": index + 1}
            try:
                legacy_revision = "card_index" not in job and card["version"] > 1
                if (legacy_revision or "card_index" in job) and not record.get("node_token"):
                    raise RuntimeError("Revision requires a recorded, verified page at this target")
                node = locate(record, target, card_id[:16], card["title"], checkpoint)
                if record.get("version", 0) > card["version"]:
                    if markdown_body(node, fetch(node, "markdown")) != record["baseline"]:
                        raise RuntimeError("Feishu body differs from the verified newer version")
                    if "card_index" in job or legacy_revision:
                        raise RuntimeError(
                            f"Requested revision {card['version']} was superseded by verified version {record['version']}")
                    result["status"] = "newer_version"
                    result["version"] = record["version"]
                else:
                    update_body(node, record, desired, base_version=job.get("base_version"),
                                legacy_revision=legacy_revision)
                    record.update(baseline=desired, version=card["version"])
                    checkpoint()
                address = urlsplit(target["url"])
                result.update(url=f"{address.scheme}://{address.netloc}/wiki/{node['node_token']}",
                              body_status="verified", version=record["version"])
                result["parent_status"] = "unverified"
                current = lark("wiki", "+node-get", "--node-token", node["node_token"],
                               "--space-id", target["space_id"])
                scope(current, target)
                result["parent_status"] = "verified"
                if card["attachments"]:
                    verified = result["attachments_verified"] = []
                    pending = result["attachments_pending"] = [item["name"] for item in card["attachments"]]
                    for attachment in card["attachments"]:
                        attach(root, node, record, attachment, checkpoint)
                        verified.append(pending.pop(0))
            except (OSError, ValueError, KeyError, RuntimeError, ET.ParseError, subprocess.TimeoutExpired) as error:
                result["error"] = str(error)
                if result.get("attachments_pending"):
                    result["status"] = "partial"
            results.append(result)
    return results


def delivery_cards(delivery):
    return enumerate(delivery["cards"], start=delivery.get("card_index", 0))


def original_cards(job):
    # Older releases mutated job.cards in place. Each card's first v1 snapshot
    # remains available in its original delivery.
    cards = list(job["cards"])
    for delivery in job["deliveries"].values():
        for index, card in delivery_cards(delivery):
            if card["version"] == 1:
                cards[index] = card
    if any(card["version"] != 1 for card in cards):
        raise RuntimeError("Original card snapshots are missing from the legacy ledger")
    return cards


def prepare_revision(job, previous, index, request):
    candidates = dict(delivery_cards(previous))
    if index not in candidates:
        raise ValueError("Choose an existing card from the base delivery")
    base = candidates[index]
    selected = previous["targets"]
    if "targets" in request:
        bound = {target_key(target): target for target in selected}
        requested = targets({"targets": request["targets"]})
        keys = [target_key(target) for target in requested]
        if any(key not in bound for key in keys):
            raise ValueError("Revision targets must belong to the base delivery")
        selected = [bound[key] for key in keys]
    body = request["body"]
    latest = max(card["version"] for delivery in job["deliveries"].values()
                 for number, card in delivery_cards(delivery) if number == index)
    for delivery_id, delivery in job["deliveries"].items():
        if (delivery.get("card_index") == index and delivery.get("base_version") == base["version"]
                and set(map(target_key, delivery["targets"])) == set(map(target_key, selected))
                and delivery["cards"][0]["body"] == body):
            return delivery_id, delivery
    return None, {
        "targets": selected, "cards": [dict(base, body=body, version=latest + 1)],
        "card_index": index, "base_version": base["version"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["config", "configure", "prepare", "revise", "deliver", "retry"])
    parser.add_argument("argument", nargs="?")
    parser.add_argument("--card", type=int)
    args = parser.parse_args()
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    config = root / "config.json"
    ledger = root / "ledger.json"
    with lock(root):
        if args.action == "config":
            return {"targets": targets(read(config))}
        if args.action == "configure":
            selected = targets(read(args.argument))
            for target in selected:
                node = lark("wiki", "+node-get", "--node-token", target["url"],
                            "--space-id", target["space_id"])
                if (node["node_token"] != target["parent_node_token"]
                        or node["space_id"] != target["space_id"]):
                    raise RuntimeError("Parent lookup did not match selected target")
            save(config, {"targets": selected})
            return read(config)
        jobs = read(ledger) if ledger.exists() else {}
        if args.action in {"prepare", "revise"}:
            path = Path(args.argument).resolve()
            request = read(path)
            source = source_key(request, path)
            if not isinstance(source, str) or not source.strip():
                raise ValueError("A stable source ID is required")
            job_id = digest(source.encode())
            job = jobs.get(job_id)
            if args.action == "revise":
                if not job or args.card is None or args.card < 1:
                    raise ValueError("Choose a valid existing card with --card")
                previous = job["deliveries"][request["delivery_id"]]
                delivery_id, delivery = prepare_revision(job, previous, args.card - 1, request)
            elif not job:
                job = jobs[job_id] = {"cards": [
                    {"title": card["title"], "body": card["body"], "version": 1, "attachments": [
                        snapshot(root, path.parent / name) for name in card.get("attachments", [])]}
                    for card in request["cards"]], "pages": {}, "deliveries": {}}
            if args.action == "prepare":
                selected = targets({"targets": request["targets"]} if "targets" in request else read(config))
                if any(t.get("mode") == "entry" for t in selected):
                    raise ValueError("Resolve each library entry to a filing destination before prepare")
                if "targets" not in request and any(t.get("mode") != "direct" for t in selected):
                    raise ValueError("Check the legacy binding and supply the actual filing targets")
                check_legacy(root, source, selected)
                cards = original_cards(job)
                versions = [card["version"] for card in cards]
                delivery_id = digest(json.dumps([job_id, sorted(map(target_key, selected)), versions]).encode())
                delivery = {"targets": selected, "cards": cards}
            if delivery_id is None:
                delivery_id = digest(json.dumps([job_id, delivery], sort_keys=True).encode())
            delivery = job["deliveries"].setdefault(delivery_id, delivery)
            save(ledger, jobs)
            return {"delivery_id": delivery_id, "targets": delivery["targets"],
                    "cards": [{"card": index + 1, "title": item["title"], "version": item["version"]}
                              for index, item in delivery_cards(delivery)]}
        for job_id, job in jobs.items():
            if args.argument in job["deliveries"]:
                delivery = job["deliveries"][args.argument]
                return {"results": run_delivery(root, job_id, {**job, **delivery},
                                                 lambda: save(ledger, jobs))}
        raise ValueError("Unknown delivery ID")


if __name__ == "__main__":
    try:
        result = main()
        print(json.dumps(result, ensure_ascii=True))
        if any("error" in item for item in result.get("results", [])):
            raise SystemExit(1)
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True))
        raise SystemExit(1) from None
