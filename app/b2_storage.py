
import os
import mimetypes
from pathlib import Path

import boto3
from botocore.config import Config


def _required_env(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"Missing environment variable: {name}"
        )

    return value


def get_client():
    return boto3.client(
        "s3",
        endpoint_url=_required_env("B2_ENDPOINT_URL"),
        aws_access_key_id=_required_env("B2_KEY_ID"),
        aws_secret_access_key=_required_env("B2_APPLICATION_KEY"),
        config=Config(signature_version="s3v4"),
    )


def originals_bucket() -> str:
    return _required_env("B2_ORIGINALS_BUCKET")


def streams_bucket() -> str:
    return _required_env("B2_STREAMS_BUCKET")


def upload_file(
    local_path: str | Path,
    bucket: str,
    object_key: str,
    content_type: str | None = None,
):
    local_path = Path(local_path)

    if content_type is None:
        content_type, _ = mimetypes.guess_type(str(local_path))

    extra_args = {}

    if content_type:
        extra_args["ContentType"] = content_type

    if object_key.endswith(".m3u8"):
        extra_args["CacheControl"] = "public, max-age=60"

    elif object_key.endswith(".ts"):
        extra_args["CacheControl"] = (
            "public, max-age=31536000, immutable"
        )

    get_client().upload_file(
        str(local_path),
        bucket,
        object_key,
        ExtraArgs=extra_args or None,
    )


def download_file(
    bucket: str,
    object_key: str,
    destination: str | Path,
):
    destination = Path(destination)
    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    get_client().download_file(
        bucket,
        object_key,
        str(destination),
    )

    return destination


def get_object(bucket: str, object_key: str):
    """
    Retrieve an object from a private Backblaze B2 bucket.

    Returns the boto3 response, including its StreamingBody.
    """
    return get_client().get_object(
        Bucket=bucket,
        Key=object_key,
    )


def list_files(
    bucket: str,
    prefix: str = "",
) -> list[dict]:
    paginator = get_client().get_paginator(
        "list_objects_v2"
    )

    files = []

    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=prefix,
    ):
        files.extend(page.get("Contents", []))

    return files


def delete_prefix(bucket: str, prefix: str):
    files = list_files(bucket, prefix)
    client = get_client()

    for start in range(0, len(files), 1000):
        batch = files[start:start + 1000]

        if batch:
            client.delete_objects(
                Bucket=bucket,
                Delete={
                    "Objects": [
                        {"Key": item["Key"]}
                        for item in batch
                    ],
                    "Quiet": True,
                },
            )


def public_url(object_key: str) -> str:
    """
    Kept for compatibility with any other existing code.
    Private HLS playback should use the FastAPI proxy instead.
    """
    base_url = _required_env(
        "B2_PUBLIC_URL"
    ).rstrip("/")

    return f"{base_url}/{object_key.lstrip('/')}"
