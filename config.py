"""Non-secret configuration for the Week 7 AWS lifecycle assignment."""

import os

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


if load_dotenv:
    load_dotenv()


REGION = os.getenv("AWS_REGION", "us-east-1")
INSTANCE_TYPE = os.getenv("AWS_INSTANCE_TYPE", "t2.micro")

# Set AWS_AMI_ID to a region-specific AMI if your evaluator requires a fixed ID.
# When left blank, provision.py resolves the latest Amazon Linux 2 AMI automatically.
AMI_ID = os.getenv("AWS_AMI_ID", "")
AMI_SSM_PARAMETER = os.getenv(
    "AWS_AMI_SSM_PARAMETER",
    "/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2",
)
AMI_NAME_FILTER = "amzn2-ami-hvm-*-x86_64-gp2"

KEY_NAME = os.getenv("AWS_KEY_NAME", "")
PROJECT_TAG_KEY = "Project"
PROJECT_TAG_VALUE = "week7-cloud-resource-manager"
RUN_ID_TAG_KEY = "RunId"

VPC_CIDR = "10.42.0.0/16"
SUBNET_CIDR = "10.42.1.0/24"
BUCKET_PREFIX = "week7-crm"

STATE_FILE = "state.json"
UPLOAD_FILE = "sample_upload.txt"
UPLOAD_KEY = "week7-upload.txt"
