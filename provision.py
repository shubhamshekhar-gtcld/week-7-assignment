"""Provision, use, and clean up AWS resources for the Week 7 assignment."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from threading import Event
from typing import Any, Callable

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, WaiterError

from config import (
    AMI_ID,
    AMI_NAME_FILTER,
    AMI_SSM_PARAMETER,
    BUCKET_PREFIX,
    INSTANCE_TYPE,
    KEY_NAME,
    PROJECT_TAG_KEY,
    PROJECT_TAG_VALUE,
    REGION,
    RUN_ID_TAG_KEY,
    STATE_FILE,
    SUBNET_CIDR,
    UPLOAD_FILE,
    UPLOAD_KEY,
    VPC_CIDR,
)


BOTO_CONFIG = Config(
    retries={"max_attempts": 10, "mode": "adaptive"},
    user_agent_extra="week7-cloud-resource-manager/1.0",
)

NOT_FOUND_CODES = {
    "InvalidAssociationID.NotFound",
    "InvalidGroup.NotFound",
    "InvalidInstanceID.NotFound",
    "InvalidInternetGatewayID.NotFound",
    "InvalidRouteTableID.NotFound",
    "InvalidSubnetID.NotFound",
    "InvalidVpcID.NotFound",
    "NoSuchBucket",
    "NoSuchKey",
}


def aws_call(action: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one boto3 call with useful error messages."""
    try:
        return func(*args, **kwargs)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "Unknown")
        message = error.response.get("Error", {}).get("Message", str(error))
        print(f"{action} failed: {code} - {message}")
        raise
    except BotoCoreError as error:
        print(f"{action} failed: {error}")
        raise


def ignored_client_error(error: ClientError) -> bool:
    """Return whether a missing resource error is safe to ignore during cleanup."""
    code = error.response.get("Error", {}).get("Code", "")
    return code in NOT_FOUND_CODES


def save_state(state: dict[str, Any]) -> None:
    """Persist identifiers for resources created during the current run."""
    Path(STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_state() -> dict[str, Any]:
    """Load saved resource identifiers, returning an empty mapping if absent."""
    path = Path(STATE_FILE)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def remove_state_file() -> None:
    """Remove temporary recovery state after resources have been cleaned up."""
    path = Path(STATE_FILE)
    if path.exists():
        try:
            path.unlink()
        except PermissionError as error:
            print(f"Could not remove {STATE_FILE}: {error}")


def make_clients(region: str = REGION) -> dict[str, Any]:
    """Create AWS clients and an EC2 resource interface for one region."""
    session = boto3.Session(region_name=region)
    return {
        "ec2": session.client("ec2", config=BOTO_CONFIG),
        "ec2_resource": session.resource("ec2", config=BOTO_CONFIG),
        "s3": session.client("s3", config=BOTO_CONFIG),
        "ssm": session.client("ssm", config=BOTO_CONFIG),
        "sts": session.client("sts", config=BOTO_CONFIG),
    }


def tag_specifications(run_id: str, resource_types: list[str]) -> list[dict[str, Any]]:
    """Build launch-time tag specifications for EC2-created resources."""
    tags = [
        {"Key": PROJECT_TAG_KEY, "Value": PROJECT_TAG_VALUE},
        {"Key": RUN_ID_TAG_KEY, "Value": run_id},
    ]
    return [{"ResourceType": resource_type, "Tags": tags} for resource_type in resource_types]


def create_tags(ec2: Any, resource_ids: list[str], run_id: str) -> None:
    """Tag existing EC2 network resources for later discovery and cleanup."""
    tags = [
        {"Key": PROJECT_TAG_KEY, "Value": PROJECT_TAG_VALUE},
        {"Key": RUN_ID_TAG_KEY, "Value": run_id},
    ]
    aws_call("Tagging EC2 resources", ec2.create_tags, Resources=resource_ids, Tags=tags)


def get_current_ip() -> str:
    """Discover the caller's public IP and format it as a single-host CIDR."""
    print("Detecting current public IP for SSH rule...")
    response = requests.get("https://api.ipify.org", timeout=10)
    response.raise_for_status()
    return f"{response.text.strip()}/32"


def resolve_ami_id(clients: dict[str, Any]) -> str:
    """Return the configured or latest suitable Amazon Linux 2 AMI ID."""
    if AMI_ID:
        return AMI_ID
    print(f"Resolving AMI ID from SSM parameter: {AMI_SSM_PARAMETER}")
    try:
        response = aws_call(
            "Resolving AMI ID",
            clients["ssm"].get_parameter,
            Name=AMI_SSM_PARAMETER,
        )
        return response["Parameter"]["Value"]
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code not in {"AccessDeniedException", "UnauthorizedOperation", "UnrecognizedClientException"}:
            raise

    print("SSM AMI lookup was not allowed; falling back to EC2 describe_images...")
    response = aws_call(
        "Finding latest Amazon Linux 2 AMI",
        clients["ec2"].describe_images,
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": [AMI_NAME_FILTER]},
            {"Name": "architecture", "Values": ["x86_64"]},
            {"Name": "root-device-type", "Values": ["ebs"]},
            {"Name": "virtualization-type", "Values": ["hvm"]},
            {"Name": "state", "Values": ["available"]},
        ],
    )
    images = sorted(response.get("Images", []), key=lambda image: image["CreationDate"], reverse=True)
    if not images:
        raise RuntimeError("No Amazon Linux 2 AMI found. Set AWS_AMI_ID to a valid AMI for your region.")
    return images[0]["ImageId"]


def create_vpc(clients: dict[str, Any], state: dict[str, Any]) -> None:
    """Create and record a VPC with one internet-routable public subnet."""
    ec2 = clients["ec2"]
    ec2_resource = clients["ec2_resource"]
    run_id = state["run_id"]

    print("Creating VPC...")
    vpc = aws_call("Creating VPC", ec2_resource.create_vpc, CidrBlock=VPC_CIDR)
    state["vpc_id"] = vpc.id
    save_state(state)
    create_tags(ec2, [vpc.id], run_id)

    aws_call(
        "Enabling VPC DNS support",
        ec2.modify_vpc_attribute,
        VpcId=vpc.id,
        EnableDnsSupport={"Value": True},
    )
    aws_call(
        "Enabling VPC DNS hostnames",
        ec2.modify_vpc_attribute,
        VpcId=vpc.id,
        EnableDnsHostnames={"Value": True},
    )
    aws_call("Waiting for VPC availability", vpc.wait_until_available)

    print("Creating public subnet...")
    subnet = aws_call("Creating subnet", vpc.create_subnet, CidrBlock=SUBNET_CIDR)
    state["subnet_id"] = subnet.id
    save_state(state)
    create_tags(ec2, [subnet.id], run_id)
    aws_call(
        "Enabling public IPv4 assignment for subnet",
        ec2.modify_subnet_attribute,
        SubnetId=subnet.id,
        MapPublicIpOnLaunch={"Value": True},
    )

    print("Creating and attaching internet gateway...")
    internet_gateway = aws_call("Creating internet gateway", ec2_resource.create_internet_gateway)
    state["internet_gateway_id"] = internet_gateway.id
    save_state(state)
    create_tags(ec2, [internet_gateway.id], run_id)
    aws_call("Attaching internet gateway", vpc.attach_internet_gateway, InternetGatewayId=internet_gateway.id)

    print("Creating public route table...")
    route_table = aws_call("Creating route table", vpc.create_route_table)
    state["route_table_id"] = route_table.id
    save_state(state)
    create_tags(ec2, [route_table.id], run_id)
    aws_call(
        "Creating internet route",
        route_table.create_route,
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=internet_gateway.id,
    )
    association = aws_call("Associating route table", route_table.associate_with_subnet, SubnetId=subnet.id)
    state["route_table_association_id"] = association.id
    save_state(state)


def create_security_group(clients: dict[str, Any], state: dict[str, Any]) -> None:
    """Create a security group allowing SSH access only from the current IP."""
    ec2_resource = clients["ec2_resource"]
    run_id = state["run_id"]
    ssh_cidr = get_current_ip()

    print(f"Creating security group with SSH limited to {ssh_cidr}...")
    vpc = ec2_resource.Vpc(state["vpc_id"])
    security_group = aws_call(
        "Creating security group",
        vpc.create_security_group,
        GroupName=f"week7-ssh-{run_id[:8]}",
        Description="Week 7 SSH access from current public IP only",
    )
    state["security_group_id"] = security_group.id
    save_state(state)
    create_tags(clients["ec2"], [security_group.id], run_id)

    aws_call(
        "Authorizing SSH ingress",
        security_group.authorize_ingress,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": ssh_cidr, "Description": "Current public IP only"}],
            }
        ],
    )


def launch_instance(clients: dict[str, Any], state: dict[str, Any]) -> tuple[str, str]:
    """Launch the EC2 instance, wait for it to run, and return its identity."""
    ec2 = clients["ec2"]
    ec2_resource = clients["ec2_resource"]
    run_id = state["run_id"]
    image_id = resolve_ami_id(clients)

    print("Launching EC2 instance...")
    launch_args: dict[str, Any] = {
        "ImageId": image_id,
        "InstanceType": INSTANCE_TYPE,
        "MinCount": 1,
        "MaxCount": 1,
        "NetworkInterfaces": [
            {
                "SubnetId": state["subnet_id"],
                "DeviceIndex": 0,
                "AssociatePublicIpAddress": True,
                "Groups": [state["security_group_id"]],
            }
        ],
        "TagSpecifications": tag_specifications(run_id, ["instance", "volume"]),
    }
    if KEY_NAME:
        launch_args["KeyName"] = KEY_NAME

    instances = aws_call("Launching EC2 instance", ec2_resource.create_instances, **launch_args)
    instance = instances[0]
    state["instance_id"] = instance.id
    save_state(state)

    print(f"Waiting for instance to reach running state: {instance.id}")
    try:
        aws_call(
            "Waiting for instance_running",
            ec2.get_waiter("instance_running").wait,
            InstanceIds=[instance.id],
    )
    except WaiterError as error:
        print(f"Instance did not reach running state: {error}")
        raise

    aws_call("Reloading instance", instance.reload)
    public_ip = instance.public_ip_address or ""
    print(f"Instance running: {instance.id}")
    print(f"Public IP: {public_ip}")
    return instance.id, public_ip


def create_bucket(clients: dict[str, Any], state: dict[str, Any]) -> str:
    """Create and tag a globally unique S3 bucket for the current run."""
    s3 = clients["s3"]
    account_id = aws_call("Reading AWS account ID", clients["sts"].get_caller_identity)["Account"]
    account_suffix = account_id[-6:]

    for _ in range(5):
        bucket_name = f"{BUCKET_PREFIX}-{REGION}-{account_suffix}-{uuid.uuid4().hex[:12]}".lower()
        print(f"Creating S3 bucket: {bucket_name}")
        create_args: dict[str, Any] = {"Bucket": bucket_name}
        if REGION != "us-east-1":
            create_args["CreateBucketConfiguration"] = {"LocationConstraint": REGION}
        try:
            aws_call("Creating S3 bucket", s3.create_bucket, **create_args)
            state["bucket_name"] = bucket_name
            save_state(state)
            aws_call(
                "Tagging S3 bucket",
                s3.put_bucket_tagging,
                Bucket=bucket_name,
                Tagging={
                    "TagSet": [
                        {"Key": PROJECT_TAG_KEY, "Value": PROJECT_TAG_VALUE},
                        {"Key": RUN_ID_TAG_KEY, "Value": state["run_id"]},
                    ]
                },
            )
            return bucket_name
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
                print(f"Bucket name collision ({code}); retrying with a new UUID.")
                continue
            raise

    raise RuntimeError("Could not create a unique S3 bucket name after 5 attempts.")


def ensure_upload_file() -> Path:
    """Return the sample input file, creating its harmless content if missing."""
    path = Path(UPLOAD_FILE)
    if not path.exists():
        path.write_text(
            "Week 7 Cloud Resource Manager upload test file.\n",
            encoding="utf-8",
        )
    return path


def upload_and_list_objects(clients: dict[str, Any], state: dict[str, Any]) -> None:
    """Upload the demonstration file and print objects found in its S3 bucket."""
    s3 = clients["s3"]
    bucket_name = state["bucket_name"]
    upload_path = ensure_upload_file()

    print(f"Uploading {upload_path} to s3://{bucket_name}/{UPLOAD_KEY}")
    aws_call(
        "Uploading object",
        s3.put_object,
        Bucket=bucket_name,
        Key=UPLOAD_KEY,
        Body=upload_path.read_bytes(),
    )

    print("Listing objects with list_objects_v2...")
    response = aws_call("Listing S3 objects", s3.list_objects_v2, Bucket=bucket_name)
    contents = response.get("Contents", [])
    if not contents:
        print("No objects found.")
        return
    for item in contents:
        print(f"S3 object: {item['Key']} ({item['Size']} bytes)")


def delete_bucket_objects(s3: Any, bucket_name: str) -> None:
    """Empty an S3 bucket, including any object versions and delete markers."""
    print(f"Deleting all objects from bucket: {bucket_name}")

    version_paginator = aws_call("Creating S3 version paginator", s3.get_paginator, "list_object_versions")
    try:
        for page in version_paginator.paginate(Bucket=bucket_name):
            objects = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for item in page.get("Versions", []) + page.get("DeleteMarkers", [])
            ]
            for index in range(0, len(objects), 1000):
                aws_call(
                    "Deleting versioned S3 objects",
                    s3.delete_objects,
                    Bucket=bucket_name,
                    Delete={"Objects": objects[index : index + 1000], "Quiet": True},
                )
    except ClientError as error:
        if ignored_client_error(error):
            return
        raise

    object_paginator = aws_call("Creating S3 object paginator", s3.get_paginator, "list_objects_v2")
    for page in object_paginator.paginate(Bucket=bucket_name):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        for index in range(0, len(objects), 1000):
            aws_call(
                "Deleting S3 objects",
                s3.delete_objects,
                Bucket=bucket_name,
                Delete={"Objects": objects[index : index + 1000], "Quiet": True},
            )


def retry_dependency(action: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Retry deletions briefly while AWS releases dependent network resources."""
    delay = 2
    for attempt in range(1, 8):
        try:
            aws_call(action, func, *args, **kwargs)
            return
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if ignored_client_error(error):
                return
            if code != "DependencyViolation" or attempt == 7:
                raise
            print(f"{action} hit DependencyViolation; retrying after AWS finishes detaching resources.")
            Event().wait(delay)
            delay = min(delay * 2, 15)


def terminate_instance(clients: dict[str, Any], state: dict[str, Any]) -> None:
    """Terminate a created EC2 instance and wait until AWS confirms termination."""
    instance_id = state.get("instance_id")
    if not instance_id:
        return

    ec2 = clients["ec2"]
    print(f"Terminating EC2 instance: {instance_id}")
    try:
        response = aws_call("Describing instance before termination", ec2.describe_instances, InstanceIds=[instance_id])
    except ClientError as error:
        if ignored_client_error(error):
            return
        raise

    reservations = response.get("Reservations", [])
    instances = [item for reservation in reservations for item in reservation.get("Instances", [])]
    if not instances or instances[0]["State"]["Name"] == "terminated":
        return

    aws_call("Terminating EC2 instance", ec2.terminate_instances, InstanceIds=[instance_id])
    print(f"Waiting for instance termination: {instance_id}")
    try:
        aws_call(
            "Waiting for instance_terminated",
            ec2.get_waiter("instance_terminated").wait,
            InstanceIds=[instance_id],
    )
        print(f"Instance terminated: {instance_id}")
    except WaiterError as error:
        print(f"Instance did not terminate in time: {error}")
        raise


def cleanup_bucket(clients: dict[str, Any], state: dict[str, Any]) -> None:
    """Delete the run's S3 objects and bucket when a bucket was created."""
    bucket_name = state.get("bucket_name")
    if not bucket_name:
        return
    s3 = clients["s3"]
    try:
        delete_bucket_objects(s3, bucket_name)
        aws_call("Deleting S3 bucket", s3.delete_bucket, Bucket=bucket_name)
        print(f"S3 bucket deleted: {bucket_name}")
    except ClientError as error:
        if ignored_client_error(error):
            return
        raise


def cleanup_network(clients: dict[str, Any], state: dict[str, Any]) -> None:
    """Remove created VPC dependencies in an order accepted by AWS."""
    ec2 = clients["ec2"]
    ec2_resource = clients["ec2_resource"]

    association_id = state.get("route_table_association_id")
    if not association_id and state.get("route_table_id"):
        try:
            response = aws_call(
                "Describing route table associations",
                ec2.describe_route_tables,
                RouteTableIds=[state["route_table_id"]],
            )
            for route_table in response.get("RouteTables", []):
                for association in route_table.get("Associations", []):
                    route_table_association_id = association.get("RouteTableAssociationId")
                    if not association.get("Main") and route_table_association_id:
                        print(f"Disassociating route table: {route_table_association_id}")
                        aws_call(
                            "Disassociating route table",
                            ec2.disassociate_route_table,
                            AssociationId=route_table_association_id,
                        )
        except ClientError as error:
            if not ignored_client_error(error):
                raise

    if association_id:
        print(f"Disassociating route table: {association_id}")
        try:
            aws_call("Disassociating route table", ec2.disassociate_route_table, AssociationId=association_id)
        except ClientError as error:
            if not ignored_client_error(error):
                raise

    route_table_id = state.get("route_table_id")
    if route_table_id:
        print(f"Deleting route table: {route_table_id}")
        retry_dependency("Deleting route table", ec2.delete_route_table, RouteTableId=route_table_id)

    security_group_id = state.get("security_group_id")
    if security_group_id:
        print(f"Deleting security group: {security_group_id}")
        retry_dependency("Deleting security group", ec2.delete_security_group, GroupId=security_group_id)

    internet_gateway_id = state.get("internet_gateway_id")
    vpc_id = state.get("vpc_id")
    if internet_gateway_id and vpc_id:
        print(f"Detaching internet gateway: {internet_gateway_id}")
        try:
            aws_call(
                "Detaching internet gateway",
                ec2_resource.InternetGateway(internet_gateway_id).detach_from_vpc,
                VpcId=vpc_id,
            )
        except ClientError as error:
            if not ignored_client_error(error):
                raise
        print(f"Deleting internet gateway: {internet_gateway_id}")
        retry_dependency("Deleting internet gateway", ec2.delete_internet_gateway, InternetGatewayId=internet_gateway_id)

    subnet_id = state.get("subnet_id")
    if subnet_id:
        print(f"Deleting subnet: {subnet_id}")
        retry_dependency("Deleting subnet", ec2.delete_subnet, SubnetId=subnet_id)

    if vpc_id:
        print(f"Deleting VPC: {vpc_id}")
        retry_dependency("Deleting VPC", ec2.delete_vpc, VpcId=vpc_id)


def find_tagged_resources(clients: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    """Find assignment-owned resources by project tags for recovery cleanup."""
    ec2 = clients["ec2"]
    s3 = clients["s3"]
    filters = [{"Name": f"tag:{PROJECT_TAG_KEY}", "Values": [PROJECT_TAG_VALUE]}]
    if run_id:
        filters.append({"Name": f"tag:{RUN_ID_TAG_KEY}", "Values": [run_id]})

    found: dict[str, Any] = {}
    instances = aws_call("Finding tagged instances", ec2.describe_instances, Filters=filters).get("Reservations", [])
    for reservation in instances:
        for instance in reservation.get("Instances", []):
            if instance["State"]["Name"] != "terminated":
                found.setdefault("instance_id", instance["InstanceId"])

    for key, api_name, id_name in [
        ("security_group_id", "describe_security_groups", "GroupId"),
        ("subnet_id", "describe_subnets", "SubnetId"),
        ("route_table_id", "describe_route_tables", "RouteTableId"),
        ("internet_gateway_id", "describe_internet_gateways", "InternetGatewayId"),
        ("vpc_id", "describe_vpcs", "VpcId"),
    ]:
        response = aws_call(f"Finding tagged {key}", getattr(ec2, api_name), Filters=filters)
        collection_name = next(name for name in response if name.endswith("s"))
        if response.get(collection_name):
            found.setdefault(key, response[collection_name][0][id_name])

    try:
        buckets = aws_call("Listing S3 buckets for tagged cleanup", s3.list_buckets).get("Buckets", [])
    except ClientError:
        buckets = []

    for bucket in buckets:
        bucket_name = bucket["Name"]
        try:
            location_response = aws_call("Reading S3 bucket location", s3.get_bucket_location, Bucket=bucket_name)
            bucket_region = location_response.get("LocationConstraint") or "us-east-1"
            if bucket_region != REGION:
                continue
            tag_response = aws_call("Reading S3 bucket tags", s3.get_bucket_tagging, Bucket=bucket_name)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchBucket", "NoSuchTagSet", "AccessDenied"}:
                continue
            raise

        tags = {tag["Key"]: tag["Value"] for tag in tag_response.get("TagSet", [])}
        if tags.get(PROJECT_TAG_KEY) != PROJECT_TAG_VALUE:
            continue
        if run_id and tags.get(RUN_ID_TAG_KEY) != run_id:
            continue
        found.setdefault("bucket_name", bucket_name)
        break

    return found


def cleanup_resources(clients: dict[str, Any], state: dict[str, Any], remove_state: bool = False) -> None:
    """Attempt all cleanup stages using saved state plus tagged discovery."""
    if not state:
        state = find_tagged_resources(clients)
    else:
        try:
            discovered = find_tagged_resources(clients, state.get("run_id"))
        except (ClientError, BotoCoreError) as error:
            print(f"Tagged resource discovery failed; continuing with saved state: {error}")
            discovered = {}
        state = {**discovered, **state}

    cleanup_errors: list[Exception] = []
    for cleanup_step in (terminate_instance, cleanup_bucket, cleanup_network):
        try:
            cleanup_step(clients, state)
        except Exception as error:
            cleanup_errors.append(error)
            print(f"Cleanup step {cleanup_step.__name__} failed: {error}")

    if remove_state and not cleanup_errors:
        remove_state_file()
    if cleanup_errors:
        raise RuntimeError("One or more cleanup steps failed.") from cleanup_errors[0]


def run_lifecycle() -> None:
    """Run the full AWS provision, demonstration, and guaranteed cleanup flow."""
    state = {
        "run_id": uuid.uuid4().hex,
        "region": REGION,
    }
    save_state(state)
    clients = make_clients(REGION)

    try:
        print(f"Using AWS region: {REGION}")
        create_vpc(clients, state)
        create_security_group(clients, state)
        launch_instance(clients, state)
        create_bucket(clients, state)
        upload_and_list_objects(clients, state)
    finally:
        print("Starting cleanup...")
        cleanup_resources(clients, load_state(), remove_state=True)
        print("Cleanup complete. No assignment resources should remain.")


def main() -> int:
    """Execute the lifecycle command and return a terminal-friendly status code."""
    try:
        run_lifecycle()
        return 0
    except (ClientError, BotoCoreError, WaiterError, requests.RequestException, RuntimeError) as error:
        print(f"Script failed: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
