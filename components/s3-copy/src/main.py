"""S3 Copy: copy or move one object, or everything under a folder, between buckets.

The bytes never pass through this pod. S3 copies each object server side,
in parts when it is large; the pod asks for the copies, checks every one
and, for a move, deletes the sources only after all copies are verified.
"""

import posixpath
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError
from botocore.utils import percent_encode_sequence

import patchbae

STORAGE_CLASSES = ("STANDARD", "STANDARD_IA", "ONEZONE_IA", "INTELLIGENT_TIERING", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE")
ARCHIVE_CLASSES = ("GLACIER", "DEEP_ARCHIVE")
# What a copy in parts does not take over from the source by itself.
CARRIED_HEADERS = ("ContentType", "CacheControl", "ContentDisposition", "ContentEncoding", "ContentLanguage")
MISSING_CODES = ("404", "NoSuchKey", "NotFound")
MAX_LISTED = 100


def run(inputs):
    src_bucket, src_key = split_s3_uri(inputs.get("source_uri"), "source_uri")
    dst_bucket, dst_key = split_s3_uri(inputs.get("destination_uri"), "destination_uri")
    if not src_key:
        patchbae.fail("source_uri needs an object or a folder, such as s3://bucket/in/show.mxf or s3://bucket/in/, not a whole bucket.")
    folder = src_key.endswith("/")
    to_folder = not dst_key or dst_key.endswith("/")
    if folder and not to_folder:
        patchbae.fail("source_uri is a folder, so destination_uri must be a folder too: end it with a slash.")
    if not folder and to_folder:
        dst_key += posixpath.basename(src_key)
    if src_bucket == dst_bucket:
        if src_key == dst_key:
            patchbae.fail("source_uri and destination_uri are the same. Nothing to copy.")
        if folder and (dst_key.startswith(src_key) or src_key.startswith(dst_key)):
            patchbae.fail("source_uri and destination_uri overlap. Copy to a folder outside the source folder.")
    storage_class = str(inputs.get("storage_class") or "STANDARD").strip().upper()
    if storage_class not in STORAGE_CLASSES:
        patchbae.fail(f"Unknown storage_class {storage_class!r}. Use one of {', '.join(STORAGE_CLASSES)}.")
    move = bool(inputs.get("move", False))
    max_objects = whole_number(inputs.get("max_objects"), 1000, "max_objects")

    s3 = boto3.client("s3")
    if folder:
        keys = [key for key, _size in list_objects(s3, src_bucket, src_key, max_objects + 1)]
        if len(keys) > max_objects:
            patchbae.fail(
                f"source_uri holds more than {max_objects} objects. Raise max_objects or copy a narrower folder. Nothing was copied."
            )
        if not keys:
            return nothing_left(s3, inputs, dst_bucket, dst_key, move, max_objects)
        pairs = [(key, dst_key + key[len(src_key) :]) for key in keys]
    else:
        pairs = [(src_key, dst_key)]

    copies, leftovers, media_type = [], [], None
    for source_key, target_key in pairs:
        head = find_object(s3, src_bucket, source_key)
        if head is None:
            # Safe to deliver twice: a source that is gone while its copy exists was moved by an earlier delivery.
            target = find_object(s3, dst_bucket, target_key) if move else None
            if target is None:
                patchbae.fail(f"s3://{src_bucket}/{source_key} does not exist. Check source_uri.")
            patchbae.warn("source is gone and its copy exists: moved by an earlier delivery", source=source_key)
            size, media_type = target["ContentLength"], target.get("ContentType")
        else:
            size = copy_verified(s3, src_bucket, source_key, head, dst_bucket, target_key, storage_class)
            media_type = head.get("ContentType")
            leftovers.append(source_key)
        copies.append((target_key, size))
    if move:
        # Only now, with every copy checked, are the sources removed.
        for source_key in leftovers:
            s3.delete_object(Bucket=src_bucket, Key=source_key)

    output = summary(dst_bucket, dst_key, copies, move)
    if not folder:
        patchbae.emit_asset("Copy", output["destination_uri"], media_type)
    patchbae.log("moved" if move else "copied", objects=output["copied"], bytes=output["bytes"], destination=output["destination_uri"])
    return output


def copy_verified(s3, src_bucket, src_key, head, dst_bucket, dst_key, storage_class):
    """Copies one object server side, checks the copy has the source's size and returns that size."""
    if head.get("ArchiveStatus") or (
        head.get("StorageClass") in ARCHIVE_CLASSES and 'ongoing-request="false"' not in (head.get("Restore") or "")
    ):
        patchbae.fail(
            f"s3://{src_bucket}/{src_key} is archived and cannot be read yet. "
            "Restore it first, for example with the S3 Glacier Restore component."
        )
    s3.copy(
        {"Bucket": src_bucket, "Key": src_key},
        dst_bucket,
        dst_key,
        ExtraArgs=copy_arguments(s3, src_bucket, src_key, head, storage_class),
    )
    size = head["ContentLength"]
    copied = s3.head_object(Bucket=dst_bucket, Key=dst_key)["ContentLength"]
    if copied != size:
        patchbae.fail(
            f"The copy s3://{dst_bucket}/{dst_key} is {copied} bytes but its source is {size} bytes. "
            "No source was deleted. Run the node again."
        )
    return size


def copy_arguments(s3, bucket, key, head, storage_class):
    """The arguments of the managed copy: the storage class plus the source's own metadata and tags.

    The managed copy sends a large object in parts, and a copy in parts starts
    a new object that keeps neither the content type, the metadata nor the
    tags of its source. Passing them makes a large copy equal to a small one.
    """
    arguments = {"StorageClass": storage_class, "MetadataDirective": "REPLACE", "Metadata": head.get("Metadata") or {}}
    for name in CARRIED_HEADERS:
        if head.get(name):
            arguments[name] = head[name]
    tags = s3.get_object_tagging(Bucket=bucket, Key=key).get("TagSet") or []
    if tags:
        arguments["Tagging"] = percent_encode_sequence([(tag["Key"], tag["Value"]) for tag in tags])
        arguments["TaggingDirective"] = "REPLACE"
    return arguments


def nothing_left(s3, inputs, dst_bucket, dst_key, move, max_objects):
    """A source folder without objects: finished by an earlier delivery of a move, or a mistake in source_uri."""
    moved = list_objects(s3, dst_bucket, dst_key, max_objects) if move else []
    if not moved:
        patchbae.fail(f"No objects found under {inputs['source_uri']}. Check source_uri.")
    patchbae.warn("the source folder is empty and the destination holds objects: moved by an earlier delivery")
    return summary(dst_bucket, dst_key, moved, move)


def summary(dst_bucket, dst_key, copies, move):
    """The node output for these (destination key, size) pairs."""
    return {
        "destination_uri": f"s3://{dst_bucket}/{dst_key}",
        "copied": len(copies),
        "bytes": sum(size for _key, size in copies),
        "moved": move,
        "objects": [f"s3://{dst_bucket}/{key}" for key, _size in copies[:MAX_LISTED]],
    }


def list_objects(s3, bucket, prefix, limit):
    """Up to `limit` objects under a prefix as (key, size) pairs, without the placeholders that stand for folders."""
    found, request = [], {"Bucket": bucket, "Prefix": prefix}
    while len(found) < limit:
        page = s3.list_objects_v2(**request)
        found += [(item["Key"], item["Size"]) for item in page.get("Contents", []) if not item["Key"].endswith("/")]
        if not page.get("IsTruncated"):
            break
        request["ContinuationToken"] = page["NextContinuationToken"]
    return found[:limit]


def find_object(s3, bucket, key):
    """The object's head, or None when there is no such object."""
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error_code(error) in MISSING_CODES:
            return None
        raise


def whole_number(value, default, name):
    try:
        result = int(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        result = 0
    if result < 1:
        patchbae.fail(f"{name} must be a whole number of 1 or more, got {value!r}")
    return result


def split_s3_uri(uri, name):
    parsed = urlparse(str(uri or ""))
    if parsed.scheme != "s3" or not parsed.netloc:
        patchbae.fail(f"{name} must be an S3 URI such as s3://bucket/path, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")
