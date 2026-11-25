"""S3 Glacier Restore: make an archived object on S3 readable again.

The object never passes through this pod. S3 restores it in place; the pod
asks for the restore, optionally waits until the object can be read, and
returns the same reference it was given.
"""

import random
import re
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

import patchbae

TIERS = ("Standard", "Bulk", "Expedited")
ARCHIVE_CLASSES = ("GLACIER", "DEEP_ARCHIVE")
MISSING_CODES = ("404", "NoSuchKey", "NotFound")
RESTORE_FIELD = re.compile(r'([a-z-]+)="([^"]*)"')
# Restores take minutes to hours, so the wait is slower than for other services.
POLL_START_SECONDS = 60
POLL_MAX_SECONDS = 300


def run(inputs):
    bucket, key = split_s3_uri(inputs.get("media_uri"), "media_uri")
    if not key or key.endswith("/"):
        patchbae.fail("media_uri must point to one object such as s3://bucket/archive/show.mxf, not to a folder or a bucket.")
    tier = str(inputs.get("tier") or "Standard").strip().capitalize()
    if tier not in TIERS:
        patchbae.fail(f"Unknown tier {inputs.get('tier')!r}. Use Standard, Bulk or Expedited.")
    days = whole_number(inputs.get("days"), 7, "days")
    wait = bool(inputs.get("wait", True))

    media_uri = f"s3://{bucket}/{key}"
    s3 = boto3.client("s3")
    head = head_object(s3, bucket, key, media_uri)
    storage_class = head.get("StorageClass") or "STANDARD"
    if not archived(head):
        patchbae.log("not archived, nothing to restore", media=media_uri, storage_class=storage_class)
        return result(media_uri, "available", False, storage_class, "")

    in_progress, expires_at = restore_state(head)
    if in_progress is None:
        deep = storage_class == "DEEP_ARCHIVE" or head.get("ArchiveStatus") == "DEEP_ARCHIVE_ACCESS"
        if tier == "Expedited" and deep:
            patchbae.fail("Expedited is not available for objects in Deep Archive. Set tier to Standard or Bulk.")
        request_restore(s3, bucket, key, storage_class, tier, days)
        in_progress = True
    elif in_progress:
        # Safe to deliver twice: the restore an earlier delivery asked for is waited for, not asked for again.
        patchbae.log("restore already in progress", media=media_uri)
    else:
        patchbae.log("already restored", media=media_uri, expires_at=expires_at)

    if in_progress and wait:
        expires_at = wait_for(s3, bucket, key)
        in_progress = False
        patchbae.log("restored", media=media_uri, expires_at=expires_at)
    return result(media_uri, "in_progress" if in_progress else "available", True, storage_class, expires_at)


def archived(head):
    """True when the object has to be restored before it can be read.

    That is the Glacier Flexible Retrieval and Deep Archive storage classes,
    and the two archive tiers of Intelligent-Tiering, which S3 reports as an
    archive status. Glacier Instant Retrieval is readable as it is.
    """
    return head.get("StorageClass") in ARCHIVE_CLASSES or bool(head.get("ArchiveStatus"))


def restore_state(head):
    """What the object's Restore header says, as (in progress, expiry time).

    No header means no restore was asked for: (None, ""). While S3 works on
    it the header is `ongoing-request="true"`: (True, ""). When the copy is
    readable it is `ongoing-request="false", expiry-date="Fri, 21 Dec 2029
    00:00:00 GMT"`: (False, that time in ISO 8601).
    """
    fields = dict(RESTORE_FIELD.findall(head.get("Restore") or ""))
    if "ongoing-request" not in fields:
        return None, ""
    if fields["ongoing-request"].lower() == "true":
        return True, ""
    return False, iso_time(fields.get("expiry-date", ""))


def request_restore(s3, bucket, key, storage_class, tier, days):
    """Asks S3 to restore the object. A restore that is already running is fine."""
    request = {"GlacierJobParameters": {"Tier": tier}}
    if storage_class != "INTELLIGENT_TIERING":
        # Intelligent-Tiering moves the object back for good and does not accept a number of days.
        request["Days"] = days
    try:
        s3.restore_object(Bucket=bucket, Key=key, RestoreRequest=request)
        patchbae.log("restore requested", media=f"s3://{bucket}/{key}", tier=tier, days=request.get("Days"))
    except ClientError as error:
        code = error_code(error)
        if code == "GlacierExpeditedRetrievalNotAvailable":
            patchbae.fail("S3 has no Expedited capacity right now. Run the node again later, or set tier to Standard.")
        if code != "RestoreAlreadyInProgress":
            raise
        patchbae.log("restore already in progress", media=f"s3://{bucket}/{key}")


def wait_for(s3, bucket, key):
    """Polls until the object can be read and returns when the restored copy expires.

    Starts at one request a minute and backs off to one every five minutes.
    """
    delay = POLL_START_SECONDS
    while True:
        time.sleep(delay + random.uniform(0, 5))
        delay = min(delay * 1.5, POLL_MAX_SECONDS)
        head = s3.head_object(Bucket=bucket, Key=key)
        if not archived(head):
            # Intelligent-Tiering: the object is back in a tier that can be read, with no expiry.
            return ""
        in_progress, expires_at = restore_state(head)
        if in_progress is False:
            return expires_at


def head_object(s3, bucket, key, media_uri):
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error_code(error) in MISSING_CODES:
            patchbae.fail(f"{media_uri} does not exist. Check media_uri.")
        raise


def result(media_uri, status, was_archived, storage_class, expires_at):
    return {
        "media_uri": media_uri,
        "status": status,
        "was_archived": was_archived,
        "storage_class": storage_class,
        "expires_at": expires_at,
    }


def iso_time(http_date):
    """An HTTP date such as "Fri, 21 Dec 2029 00:00:00 GMT" as ISO 8601 in UTC, or "" when it cannot be read."""
    try:
        moment = parsedate_to_datetime(http_date)
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def whole_number(value, default, name):
    try:
        number = int(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        number = 0
    if number < 1:
        patchbae.fail(f"{name} must be a whole number of 1 or more, got {value!r}")
    return number


def split_s3_uri(uri, name):
    parsed = urlparse(str(uri or ""))
    if parsed.scheme != "s3" or not parsed.netloc:
        patchbae.fail(f"{name} must be an S3 URI such as s3://bucket/path, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")
