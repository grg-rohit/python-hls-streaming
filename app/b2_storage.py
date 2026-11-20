# #python-hls-streaming\app\b2_storage.py

# """Backblaze B2 helpers using its S3-compatible API."""

# from __future__ import annotations

# import json
# import mimetypes
# import os
# from pathlib import Path
# from typing import Any

# import boto3
# from botocore.config import Config
# from botocore.exceptions import ClientError


# def _required_env(name: str) -> str:
#     value = os.getenv(name)
#     if not value:
#         raise RuntimeError(f"Missing environment variable: {name}")
#     return value


# def get_client():
#     return boto3.client(
#         "s3",
#         endpoint_url=_required_env("B2_ENDPOINT_URL"),
#         aws_access_key_id=_required_env("B2_KEY_ID"),
#         aws_secret_access_key=_required_env("B2_APPLICATION_KEY"),
#         config=Config(
#             signature_version="s3v4",
#             retries={"max_attempts": 5, "mode": "standard"},
#             connect_timeout=20,
#             read_timeout=120,
#         ),
#     )


# def originals_bucket() -> str:
#     return _required_env("B2_ORIGINALS_BUCKET")


# def streams_bucket() -> str:
#     return _required_env("B2_STREAMS_BUCKET")


# def upload_file(
#     local_path: str | Path,
#     bucket: str,
#     object_key: str,
#     content_type: str | None = None,
#     *,
#     client=None,
# ) -> None:
#     path = Path(local_path)
#     if not path.is_file():
#         raise FileNotFoundError(f"Upload source does not exist: {path}")
#     if content_type is None:
#         content_type, _ = mimetypes.guess_type(str(path))

#     extra_args: dict[str, Any] = {}
#     if content_type:
#         extra_args["ContentType"] = content_type
#     if object_key.endswith(".m3u8"):
#         extra_args["CacheControl"] = "no-store"
#     elif object_key.endswith(".ts"):
#         extra_args["CacheControl"] = "public, max-age=31536000, immutable"

#     (client or get_client()).upload_file(
#         str(path), bucket, object_key, ExtraArgs=extra_args or None
#     )


# def upload_fileobj(fileobj, bucket: str, object_key: str, content_type: str | None = None, *, client=None) -> None:
#     extra_args: dict[str, Any] = {}
#     if content_type:
#         extra_args["ContentType"] = content_type
#     if object_key.endswith(".m3u8"):
#         extra_args["CacheControl"] = "no-store"
#     elif object_key.endswith(".ts"):
#         extra_args["CacheControl"] = "public, max-age=31536000, immutable"
#     (client or get_client()).upload_fileobj(
#         fileobj, bucket, object_key, ExtraArgs=extra_args or None
#     )


# def download_file(bucket: str, object_key: str, destination: str | Path) -> Path:
#     destination = Path(destination)
#     destination.parent.mkdir(parents=True, exist_ok=True)
#     get_client().download_file(bucket, object_key, str(destination))
#     return destination


# def get_object(bucket: str, object_key: str):
#     return get_client().get_object(Bucket=bucket, Key=object_key)


# def put_json(bucket: str, object_key: str, value: dict[str, Any], *, client=None) -> None:
#     payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
#     (client or get_client()).put_object(
#         Bucket=bucket,
#         Key=object_key,
#         Body=payload,
#         ContentType="application/json; charset=utf-8",
#         CacheControl="no-store",
#     )


# def get_json(bucket: str, object_key: str, default=None):
#     """Read a JSON object. Return default only when the object is absent."""
#     try:
#         response = get_object(bucket, object_key)
#     except ClientError as exc:
#         code = str(exc.response.get("Error", {}).get("Code", ""))
#         status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
#         if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
#             return default
#         raise
#     body = response["Body"]
#     try:
#         raw = body.read()
#     finally:
#         body.close()
#     try:
#         value = json.loads(raw.decode("utf-8"))
#     except (UnicodeDecodeError, json.JSONDecodeError) as exc:
#         raise RuntimeError(f"B2 metadata object is not valid JSON: {object_key}") from exc
#     if not isinstance(value, dict):
#         raise RuntimeError(f"B2 metadata object must contain a JSON object: {object_key}")
#     return value


# def list_files(bucket: str, prefix: str = "") -> list[dict]:
#     paginator = get_client().get_paginator("list_objects_v2")
#     result: list[dict] = []
#     for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
#         result.extend(page.get("Contents", []))
#     return result


# def object_exists(bucket: str, object_key: str) -> bool:
#     try:
#         get_client().head_object(Bucket=bucket, Key=object_key)
#         return True
#     except ClientError as exc:
#         code = str(exc.response.get("Error", {}).get("Code", ""))
#         status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
#         if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
#             return False
#         raise


# def delete_object(bucket: str, object_key: str) -> None:
#     get_client().delete_object(Bucket=bucket, Key=object_key)


# def delete_prefix(bucket: str, prefix: str) -> None:
#     """Delete all objects under a prefix, in B2's 1,000-key batches."""
#     client = get_client()
#     paginator = client.get_paginator("list_objects_v2")
#     for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
#         contents = page.get("Contents", [])
#         for start in range(0, len(contents), 1000):
#             batch = contents[start : start + 1000]
#             if batch:
#                 client.delete_objects(
#                     Bucket=bucket,
#                     Delete={"Objects": [{"Key": item["Key"]} for item in batch], "Quiet": True},
#                 )


# def public_url(object_key: str) -> str:
#     """Compatibility helper; private HLS playback uses the API proxy."""
#     base_url = _required_env("B2_PUBLIC_URL").rstrip("/")
#     return f"{base_url}/{object_key.lstrip('/')}"
