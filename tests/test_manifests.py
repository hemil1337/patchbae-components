"""Every component in the library has a manifest Patchbae will accept.

Patchbae validates a library when it syncs it. These checks catch the same
problems before a pull request is opened.
"""

import json
import pathlib
import re

import pytest
import yaml

LIBRARY = pathlib.Path(__file__).resolve().parents[1]
COMPONENTS = sorted(path.parent for path in LIBRARY.glob("components/*/component.yaml"))

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
PINNED = re.compile(r"^[A-Za-z0-9._-]+==[A-Za-z0-9._-]+$")
CATEGORIES = {"ingest", "transcode", "speech", "vision", "language", "qc", "live", "render", "storage", "delivery", "utility"}
TYPES = {"string", "text", "integer", "number", "boolean", "enum", "list", "json", "s3_uri"}
README_SECTIONS = ["What it does", "Inputs", "Outputs", "AWS access", "How it runs", "Troubleshooting"]


def manifest_of(root):
    return yaml.safe_load((root / "component.yaml").read_text(encoding="utf-8"))


def test_the_library_has_components():
    assert yaml.safe_load((LIBRARY / "library.yaml").read_text(encoding="utf-8"))["kind"] == "ComponentLibrary"
    assert COMPONENTS, "no components found"


@pytest.mark.parametrize("root", COMPONENTS, ids=lambda root: root.name)
def test_manifest(root):
    manifest = manifest_of(root)

    assert manifest["apiVersion"] == "patchb.ae/v1"
    assert manifest["kind"] == "Component"
    assert manifest["name"] == root.name, "name must equal the folder name"
    assert NAME.match(manifest["name"])
    assert manifest["displayName"].strip()
    assert 0 < len(manifest["summary"]) <= 140, "summary: one sentence, at most 140 characters"
    assert SEMVER.match(str(manifest["version"]))
    assert manifest["category"] in CATEGORIES

    runtime = manifest["runtime"]
    assert runtime["language"] == "python"
    assert (root / runtime["entrypoint"]).is_file(), "runtime.entrypoint must exist"
    assert re.search(rf"^def {re.escape(runtime.get('handler', 'run'))}\(", (root / runtime["entrypoint"]).read_text(), re.M)
    assert 30 <= int(runtime["timeoutSeconds"]) <= 21600
    for package in runtime.get("packages", []):
        assert PINNED.match(package), f"pin {package!r} to an exact version"

    names = set()
    for item in manifest.get("inputs", []):
        assert isinstance(item, dict), "an input is a mapping: check the YAML quoting"
        assert SNAKE.match(item["name"]) and item["name"] not in names
        names.add(item["name"])
        assert item["type"] in TYPES
        assert item.get("description", "").strip(), f"input {item['name']} needs a description"
        if item["type"] == "enum":
            assert item.get("options"), f"enum input {item['name']} needs options"
            if "default" in item:
                assert item["default"] in item["options"]
        if item.get("required"):
            assert "default" not in item, f"required input {item['name']} cannot have a default"
    for item in manifest.get("outputs", []):
        assert isinstance(item, dict), "an output is a mapping: check the YAML quoting"
        assert SNAKE.match(item["name"])
        assert item["type"] in TYPES
        assert item.get("description", "").strip()


@pytest.mark.parametrize("root", COMPONENTS, ids=lambda root: root.name)
def test_aws_access_is_documented(root):
    manifest = manifest_of(root)
    readme = (root / "README.md").read_text(encoding="utf-8")
    for section in README_SECTIONS:
        assert f"## {section}" in readme, f"README is missing the section {section!r}"

    aws = manifest.get("aws")
    if not aws:
        return
    assert aws["services"] and all(service == service.lower() for service in aws["services"])
    policy = json.loads((root / aws["policy"]).read_text(encoding="utf-8"))
    assert policy["Version"] == "2012-10-17"
    for statement in policy["Statement"]:
        assert statement["Sid"] and statement["Effect"] == "Allow"
        actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        assert all("*" not in action for action in actions), "list the actions the code calls; no wildcards"
    assert "sts:AssumeRoleWithWebIdentity" in readme, "the README must show the role's trust policy"
    assert "Set up access" in readme


@pytest.mark.parametrize("root", COMPONENTS, ids=lambda root: root.name)
def test_logo_is_a_plain_svg(root):
    logo = manifest_of(root).get("logo")
    if not logo:
        return
    svg = (root / logo).read_text(encoding="utf-8")
    assert svg.lstrip().startswith("<svg")
    assert "<script" not in svg.lower() and "href=" not in svg.lower()
