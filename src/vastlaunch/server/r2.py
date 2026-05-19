"""Cloudflare R2 helpers (S3-compatible API via boto3).

Required environment variables:
  R2_ACCOUNT_ID        — Cloudflare account ID
  R2_ACCESS_KEY_ID     — R2 API token access key ID
  R2_SECRET_ACCESS_KEY — R2 API token secret
  R2_BUCKET            — bucket name
"""

from __future__ import annotations

import os

import boto3


def _client():
    account_id = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _bucket() -> str:
    return os.environ["R2_BUCKET"]


def upload(key: str, data: bytes) -> None:
    _client().put_object(Bucket=_bucket(), Key=key, Body=data)


def download(key: str) -> bytes:
    resp = _client().get_object(Bucket=_bucket(), Key=key)
    return resp["Body"].read()


def delete(key: str) -> None:
    _client().delete_object(Bucket=_bucket(), Key=key)
