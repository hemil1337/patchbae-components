"""Amazon Kinesis Video Streams: export a time range to S3 as an MP4 clip or as still images.

Kinesis Video Streams cannot write to S3 itself, so this is the one component
in the library where media passes through the pod. A clip is streamed from
Kinesis straight into an S3 multipart upload, a few megabytes at a time, and
is never held in memory or written to disk as a whole.
"""

import base64
import datetime
import re
from urllib.parse import urlparse

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

import patchbae

ACTIONS = ("export_clip", "extract_images")
STREAM_NAME = re.compile(r"^[a-zA-Z0-9_.-]{1,256}$")
FILE_NAME = re.compile(r"^[a-zA-Z0-9_.-]{1,200}$")
TIMESTAMP_TYPES = ("PRODUCER_TIMESTAMP", "SERVER_TIMESTAMP")
IMAGE_FORMATS = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png")}
MAX_CLIP_RANGE = datetime.timedelta(hours=24)
MAX_IMAGE_RANGE = datetime.timedelta(seconds=300)
MIN_SAMPLING_SECONDS = 0.2
PAGE_SIZE = 100
MAX_PAGES = 50
# Two parts of 8 MB in flight keep the pod's memory small whatever the clip's size.
UPLOAD = TransferConfig(multipart_chunksize=8 * 1024 * 1024, max_concurrency=2)
# What Kinesis answers when the stream's media cannot be turned into a clip.
UNUSABLE_MEDIA = (
    "InvalidArgumentException",
    "UnsupportedStreamMediaTypeException",
    "MissingCodecPrivateDataException",
    "InvalidCodecPrivateDataException",
    "InvalidMediaFrameException",
    "NoDataRetentionException",
)


def run(inputs):
    stream = text(inputs, "stream_name")
    if not STREAM_NAME.match(stream):
        patchbae.fail("stream_name must be the stream's name: letters, digits, dots, dashes and underscores.")
    action = text(inputs, "action")
    if action not in ACTIONS:
        patchbae.fail(f"Unknown action {action!r}. Use export_clip or extract_images.")
    start = moment(inputs, "start_time")
    end = moment(inputs, "end_time")
    if end <= start:
        patchbae.fail("end_time must be after start_time.")
    longest = MAX_CLIP_RANGE if action == "export_clip" else MAX_IMAGE_RANGE
    if end - start > longest:
        limit = "24 hours for a clip" if action == "export_clip" else "300 seconds for images. Use several nodes for a longer range"
        patchbae.fail(f"The range from start_time to end_time is too long: at most {limit}.")
    bucket, prefix = split_s3_uri(inputs.get("output_uri"), "output_uri")
    if prefix and not prefix.endswith("/"):
        patchbae.fail("output_uri must be a folder: end it with a slash, for example s3://bucket/camera-clips/")
    name = text(inputs, "file_name") or f"patchbae-{patchbae.task.id}"
    if not FILE_NAME.match(name):
        patchbae.fail("file_name may hold letters, digits, dots, dashes and underscores. Put folders in output_uri.")
    timestamp_type = text(inputs, "timestamp_type") or "PRODUCER_TIMESTAMP"
    if timestamp_type not in TIMESTAMP_TYPES:
        patchbae.fail(f"Unknown timestamp_type {timestamp_type!r}. Use PRODUCER_TIMESTAMP or SERVER_TIMESTAMP.")

    if action == "export_clip":
        return export_clip(stream, start, end, timestamp_type, bucket, f"{prefix}{name}.mp4")
    return extract_images(inputs, stream, start, end, timestamp_type, bucket, f"{prefix}{name}/")


def export_clip(stream, start, end, timestamp_type, bucket, key):
    archive = archive_client(stream, "GET_CLIP")
    try:
        response = archive.get_clip(
            StreamName=stream,
            ClipFragmentSelector={
                "FragmentSelectorType": timestamp_type,
                "TimestampRange": {"StartTimestamp": start, "EndTimestamp": end},
            },
        )
    except ClientError as error:
        fail_if_no_media(error, stream, start, end)
        if error_code(error) in UNUSABLE_MEDIA:
            patchbae.fail(f"Kinesis Video Streams could not make a clip of this range: {error_message(error)}")
        raise

    # The same key on a second delivery: the clip is written again with the same content.
    body = CountingReader(response["Payload"])
    content_type = response.get("ContentType") or "video/mp4"
    boto3.client("s3").upload_fileobj(body, bucket, key, ExtraArgs={"ContentType": content_type}, Config=UPLOAD)
    if body.count == 0:
        patchbae.fail(f"Stream {stream} returned an empty clip for {start.isoformat()} to {end.isoformat()}.")

    clip_uri = f"s3://{bucket}/{key}"
    patchbae.emit_asset("Clip", clip_uri, content_type, start=start.isoformat(), end=end.isoformat(), bytes=body.count)
    patchbae.log("clip exported", stream=stream, clip=clip_uri, bytes=body.count)
    return result(clip_uri=clip_uri, clip_bytes=body.count)


def extract_images(inputs, stream, start, end, timestamp_type, bucket, prefix):
    image_format = text(inputs, "image_format") or "JPEG"
    if image_format not in IMAGE_FORMATS:
        patchbae.fail(f"Unknown image_format {image_format!r}. Use JPEG or PNG.")
    extension, media_type = IMAGE_FORMATS[image_format]
    interval = number(inputs, "sampling_interval_seconds", 5)
    if interval < MIN_SAMPLING_SECONDS:
        patchbae.fail("sampling_interval_seconds must be at least 0.2: Kinesis Video Streams gives at most 5 images a second.")
    max_images = number(inputs, "max_images", 100)
    if not isinstance(max_images, int) or not 1 <= max_images <= 1000:
        patchbae.fail("max_images must be a whole number from 1 to 1000.")
    width, height = inputs.get("image_width"), inputs.get("image_height")
    if (width in (None, "")) != (height in (None, "")):
        patchbae.fail("Set image_width and image_height together, or leave both empty for the stream's own size.")
    request = {
        "StreamName": stream,
        "ImageSelectorType": timestamp_type,
        "StartTimestamp": start,
        "EndTimestamp": end,
        "SamplingInterval": round(interval * 1000),
        "Format": image_format,
    }
    if width not in (None, ""):
        if not all(isinstance(side, int) and not isinstance(side, bool) for side in (width, height)) or not (1 <= width <= 3840 and 1 <= height <= 2160):
            patchbae.fail("image_width must be 1 to 3840 and image_height 1 to 2160, in pixels.")
        request.update(WidthPixels=width, HeightPixels=height)

    archive = archive_client(stream, "GET_IMAGES")
    s3 = boto3.client("s3")
    uris, skipped = [], 0
    for _page in range(MAX_PAGES):
        request["MaxResults"] = min(PAGE_SIZE, max_images - len(uris))
        try:
            page = archive.get_images(**request)
        except ClientError as error:
            fail_if_no_media(error, stream, start, end)
            if error_code(error) in UNUSABLE_MEDIA:
                patchbae.fail(f"Kinesis Video Streams could not make images of this range: {error_message(error)}")
            raise
        for image in page.get("Images", []):
            if image.get("Error") or not image.get("ImageContent"):
                skipped += 1
                continue
            if len(uris) == max_images:
                break
            key = f"{prefix}{stamp(image['TimeStamp'])}.{extension}"
            s3.put_object(Bucket=bucket, Key=key, Body=base64.b64decode(image["ImageContent"]), ContentType=media_type)
            uris.append(f"s3://{bucket}/{key}")
        if len(uris) == max_images or not page.get("NextToken"):
            break
        request["NextToken"] = page["NextToken"]
    if not uris:
        patchbae.fail(f"Stream {stream} has no media between {start.isoformat()} and {end.isoformat()}, so no images were made.")

    folder = f"s3://{bucket}/{prefix}"
    patchbae.emit_asset("Stills", folder, media_type, count=len(uris))
    patchbae.log("images extracted", stream=stream, images=len(uris), skipped=skipped, folder=folder)
    return result(images_uri=folder, image_uris=uris, image_count=len(uris), skipped_count=skipped)


def archive_client(stream, api_name):
    """The client for reading the stream's stored media. Each stream has its own endpoint for it."""
    try:
        endpoint = boto3.client("kinesisvideo").get_data_endpoint(StreamName=stream, APIName=api_name)["DataEndpoint"]
    except ClientError as error:
        if error_code(error) == "ResourceNotFoundException":
            patchbae.fail(f"Stream {stream} was not found. Check the name and the region the component is set up for.")
        raise
    return boto3.client("kinesis-video-archived-media", endpoint_url=endpoint)


def fail_if_no_media(error, stream, start, end):
    if error_code(error) == "ResourceNotFoundException":
        patchbae.fail(
            f"Stream {stream} has no media between {start.isoformat()} and {end.isoformat()}. "
            "Check the range, timestamp_type and how long the stream keeps its data."
        )


class CountingReader:
    """Passes reads through to a stream and counts the bytes, so the clip's size is known without a second request."""

    def __init__(self, stream):
        self._stream = stream
        self.count = 0

    def read(self, amount=None):
        data = self._stream.read(amount)
        self.count += len(data)
        return data


def stamp(value):
    """A timestamp as a file name that sorts in time order, to the millisecond."""
    if not isinstance(value, datetime.datetime):
        return str(value)
    return value.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"


def moment(inputs, name):
    """The input as a time with a time zone. Anything else fails the node."""
    value = text(inputs, name)
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        patchbae.fail(f"{name} must be a UTC time such as 2026-11-25T19:42:10Z, got {value!r}.")
    return parsed.astimezone(datetime.timezone.utc)


def number(inputs, name, default):
    value = inputs.get(name)
    if value in (None, ""):
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        patchbae.fail(f"{name} must be a number, got {value!r}.")
    return value


def result(**values):
    """Every output, with empty values for the ones this action does not produce."""
    return {"clip_uri": "", "clip_bytes": 0, "images_uri": "", "image_uris": [], "image_count": 0, "skipped_count": 0, **values}


def text(inputs, name):
    return str(inputs.get(name) or "").strip()


def split_s3_uri(uri, name):
    parsed = urlparse(str(uri or ""))
    if parsed.scheme != "s3" or not parsed.netloc:
        patchbae.fail(f"{name} must be an S3 URI such as s3://bucket/path, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")


def error_message(error):
    return error.response.get("Error", {}).get("Message") or "no reason given"
