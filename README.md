# Week 7 Cloud Resource Manager

This project provisions, uses, and cleans up a small AWS environment with boto3:

1. Creates a VPC, public subnet, internet gateway, route table, and SSH security group.
2. Launches a Free Tier sized EC2 instance.
3. Waits for the instance to reach `running`.
4. Prints the instance ID and public IP.
5. Creates a globally unique S3 bucket, uploads a file, and lists objects with `list_objects_v2`.
6. Terminates the instance, waits for `terminated`, deletes all S3 objects, deletes the bucket, and removes the VPC resources.

## Prerequisites

- Python 3.10+
- An AWS account with permissions for EC2, S3, SSM Parameter Store, and STS
- AWS credentials configured outside the code

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Run the local test suite:

```powershell
python -m pytest
```

The tests do not create AWS resources. They verify the assignment contract, credential hygiene, waiters, S3 APIs, `.gitignore`, and helper behavior.

Configure credentials with one of these safe options:

```powershell
aws configure
```

or copy `.env.example` to `.env` and put your local credentials there. Do not commit credentials, `.env` files, private keys, or `state.json`.

```powershell
$env:AWS_ACCESS_KEY_ID="your-access-key-id"
$env:AWS_SECRET_ACCESS_KEY="your-secret-access-key"
```

The scripts load `.env` automatically when `python-dotenv` is installed through `requirements.txt`.

## Configuration

Non-secret settings live in `config.py`.

- `REGION`: defaults to `us-east-1`
- `INSTANCE_TYPE`: defaults to `t2.micro`
- `AMI_ID`: optional fixed AMI ID. If blank, the script resolves the latest Amazon Linux 2 AMI from AWS SSM, then falls back to EC2 `describe_images` if SSM is not allowed.
- `KEY_NAME`: optional EC2 key pair name through `AWS_KEY_NAME`

Optional overrides:

```powershell
$env:AWS_REGION="us-east-1"
$env:AWS_INSTANCE_TYPE="t2.micro"
$env:AWS_AMI_ID="ami-xxxxxxxxxxxxxxxxx"
$env:AWS_KEY_NAME="my-key-pair"
```

## Run

Run the full lifecycle:

```powershell
python provision.py
```

If the script is interrupted or fails midway, run:

```powershell
python cleanup.py
```

## Expected Output

The exact IDs and IP address will be different:

```text
Using AWS region: us-east-1
Creating VPC...
Creating public subnet...
Creating and attaching internet gateway...
Creating public route table...
Detecting current public IP for SSH rule...
Creating security group with SSH limited to 203.0.113.10/32...
Resolving AMI ID from SSM parameter: /aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2
Launching EC2 instance...
Waiting for instance to reach running state: i-0123456789abcdef0
Instance running: i-0123456789abcdef0
Public IP: 54.210.12.34
Creating S3 bucket: week7-crm-us-east-1-123456-a1b2c3d4e5f6
Uploading sample_upload.txt to s3://week7-crm-us-east-1-123456-a1b2c3d4e5f6/week7-upload.txt
Listing objects with list_objects_v2...
S3 object: week7-upload.txt (48 bytes)
Starting cleanup...
Terminating EC2 instance: i-0123456789abcdef0
Waiting for instance termination: i-0123456789abcdef0
Instance terminated: i-0123456789abcdef0
Deleting all objects from bucket: week7-crm-us-east-1-123456-a1b2c3d4e5f6
S3 bucket deleted: week7-crm-us-east-1-123456-a1b2c3d4e5f6
Disassociating route table: rtbassoc-0123456789abcdef0
Deleting route table: rtb-0123456789abcdef0
Deleting security group: sg-0123456789abcdef0
Detaching internet gateway: igw-0123456789abcdef0
Deleting internet gateway: igw-0123456789abcdef0
Deleting subnet: subnet-0123456789abcdef0
Deleting VPC: vpc-0123456789abcdef0
Cleanup complete. No assignment resources should remain.
```

## Notes

- The EC2 lifecycle uses `instance_running` and `instance_terminated` waiters.
- SSH ingress is restricted to the current public IP from `https://api.ipify.org`, never `0.0.0.0/0`.
- S3 bucket names include the region, account suffix, and a UUID.
- Bucket cleanup deletes normal objects, versions, and delete markers before deleting the bucket.
- All assignment resources are tagged with `Project=week7-cloud-resource-manager` and a per-run `RunId`, so `cleanup.py` can recover most resources even if `state.json` is missing.
