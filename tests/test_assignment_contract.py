import ast
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROVISION = ROOT / "provision.py"
CONFIG = ROOT / "config.py"
GITIGNORE = ROOT / ".gitignore"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_required_deliverables_exist():
    for filename in ("provision.py", "cleanup.py", "config.py", "README.md"):
        assert (ROOT / filename).exists(), f"{filename} is missing"


def test_config_contains_named_non_secret_constants():
    config_source = read(CONFIG)
    tree = ast.parse(config_source)
    assigned_names = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert {"REGION", "INSTANCE_TYPE", "AMI_ID"}.issubset(assigned_names)
    assert "AKIA" not in config_source
    assert "AWS_SECRET_ACCESS_KEY" not in config_source


def test_no_hardcoded_aws_credentials_in_python_files():
    for path in ROOT.glob("*.py"):
        source = read(path)
        assert "AKIA" not in source
        assert "AWS_ACCESS_KEY_ID" not in source
        assert "AWS_SECRET_ACCESS_KEY" not in source


def test_gitignore_excludes_sensitive_local_files():
    gitignore = read(GITIGNORE)
    assert ".env" in gitignore
    assert "*.pem" in gitignore
    assert "credentials" in gitignore
    assert "state.json" in gitignore


def test_provision_uses_required_boto3_apis_and_waiters():
    source = read(PROVISION)
    required_snippets = [
        'get_waiter("instance_running")',
        'get_waiter("instance_terminated")',
        "s3.create_bucket",
        "s3.put_object",
        "s3.list_objects_v2",
        "s3.delete_objects",
        "s3.delete_bucket",
        "ec2.terminate_instances",
        "try:",
        "finally:",
        "BucketAlreadyExists",
        "error.response.get",
    ]

    for snippet in required_snippets:
        assert snippet in source


def test_no_sleep_loop_for_instance_polling():
    source = read(PROVISION)
    assert "time.sleep" not in source
    assert "describe_instance_status" not in source


def test_ssh_ingress_uses_current_public_ip_not_open_world():
    source = read(PROVISION)
    assert 'requests.get("https://api.ipify.org", timeout=10)' in source
    assert 'return f"{response.text.strip()}/32"' in source
    assert '"FromPort": 22' in source
    assert '"ToPort": 22' in source
    assert '"CidrIp": ssh_cidr' in source
    assert '"CidrIp": "0.0.0.0/0"' not in source


def test_bucket_name_uses_uuid_for_global_uniqueness():
    source = read(PROVISION)
    assert "uuid.uuid4()" in source
    assert "BUCKET_PREFIX" in source


def test_get_current_ip_formats_single_host_cidr(monkeypatch):
    import provision

    response = Mock()
    response.text = "203.0.113.42\n"
    response.raise_for_status = Mock()
    get = Mock(return_value=response)
    monkeypatch.setattr(provision.requests, "get", get)

    assert provision.get_current_ip() == "203.0.113.42/32"
    get.assert_called_once_with("https://api.ipify.org", timeout=10)
    response.raise_for_status.assert_called_once()


def test_ignored_client_error_recognizes_expected_not_found_codes():
    import provision

    error = Mock()
    error.response = {"Error": {"Code": "InvalidVpcID.NotFound"}}

    assert provision.ignored_client_error(error)


@pytest.mark.parametrize(
    "filename",
    ["provision.py", "cleanup.py", "config.py"],
)
def test_python_files_parse(filename):
    ast.parse(read(ROOT / filename), filename=filename)
