#python-hls-streaming\app\s3_storage.py
"""Amazon S3 / S3-compatible storage helpers for media and JSON metadata."""
from __future__ import annotations
import json
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def get_client():
    region = os.getenv("S3_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    endpoint = os.getenv("S3_ENDPOINT_URL", "").strip() or None
    access_key = os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("S3_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY") or os.getenv("S3_SECRET_ACCESS_KEY")
    session_token = os.getenv("AWS_SESSION_TOKEN") or None
    kwargs: dict[str, Any] = {
        "service_name": "s3", "region_name": region,
        "config": Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=20, read_timeout=120),
    }
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if access_key and secret_key:
        kwargs.update(aws_access_key_id=access_key, aws_secret_access_key=secret_key)
    if session_token:
        kwargs["aws_session_token"] = session_token
    return boto3.client(**kwargs)


def originals_bucket() -> str:
    return _required_env("S3_ORIGINALS_BUCKET")


def streams_bucket() -> str:
    return _required_env("S3_STREAMS_BUCKET")


def upload_file(local_path: str | Path, bucket: str, object_key: str, content_type: str | None = None, *, client=None) -> None:
    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(f"Upload source does not exist: {path}")
    if content_type is None:
        content_type, _ = mimetypes.guess_type(str(path))
    extra: dict[str, Any] = {}
    if content_type:
        extra["ContentType"] = content_type
    if object_key.endswith(".m3u8"):
        extra["CacheControl"] = "no-store"
    elif object_key.endswith(".ts"):
        extra["CacheControl"] = "public, max-age=31536000, immutable"
    (client or get_client()).upload_file(str(path), bucket, object_key, ExtraArgs=extra or None)


def upload_fileobj(fileobj, bucket: str, object_key: str, content_type: str | None = None, *, client=None) -> None:
    extra: dict[str, Any] = {}
    if content_type:
        extra["ContentType"] = content_type
    if object_key.endswith(".m3u8"):
        extra["CacheControl"] = "no-store"
    elif object_key.endswith(".ts"):
        extra["CacheControl"] = "public, max-age=31536000, immutable"
    (client or get_client()).upload_fileobj(fileobj, bucket, object_key, ExtraArgs=extra or None)


def download_file(bucket: str, object_key: str, destination: str | Path) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    get_client().download_file(bucket, object_key, str(destination))
    return destination


def get_object(bucket: str, object_key: str, *, byte_range: str | None = None):
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": object_key}
    if byte_range:
        kwargs["Range"] = byte_range
    return get_client().get_object(**kwargs)


def put_json(bucket: str, object_key: str, value: dict[str, Any], *, client=None) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    (client or get_client()).put_object(Bucket=bucket, Key=object_key, Body=payload, ContentType="application/json; charset=utf-8", CacheControl="no-store")


def get_json(bucket: str, object_key: str, default=None):
    try:
        response = get_object(bucket, object_key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return default
        raise
    body = response["Body"]
    try:
        raw = body.read()
    finally:
        body.close()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"S3 metadata object is not valid JSON: {object_key}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"S3 metadata object must contain a JSON object: {object_key}")
    return value


def list_files(bucket: str, prefix: str = "") -> list[dict]:
    paginator = get_client().get_paginator("list_objects_v2")
    result: list[dict] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        result.extend(page.get("Contents", []))
    return result


def object_exists(bucket: str, object_key: str) -> bool:
    try:
        get_client().head_object(Bucket=bucket, Key=object_key)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return False
        raise


def delete_object(bucket: str, object_key: str) -> None:
    get_client().delete_object(Bucket=bucket, Key=object_key)


def delete_prefix(bucket: str, prefix: str) -> None:
    client = get_client()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        contents = page.get("Contents", [])
        for start in range(0, len(contents), 1000):
            batch = contents[start:start + 1000]
            if batch:
                client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": item["Key"]} for item in batch], "Quiet": True})


def public_url(object_key: str) -> str:
    bucket = streams_bucket()
    region = os.getenv("S3_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    base = os.getenv("S3_PUBLIC_URL", "").rstrip("/")
    if base:
        return f"{base}/{quote(object_key.lstrip('/'), safe='/')}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{quote(object_key.lstrip('/'), safe='/')}"
