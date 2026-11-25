"""AWS Elemental MediaPackage: harvest a clip from a live channel, or publish a VOD asset.

The media never passes through this pod. MediaPackage writes a harvested clip
to S3 itself, and reads the source of a VOD asset from S3 through its own
role; the pod starts the work, waits for it and returns references.
"""

import datetime
import random
import re
import time
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

import patchbae

ACTIONS = ("harvest_clip", "publish_vod_asset")
NAME = re.compile(r"^[a-zA-Z0-9_-]{1,256}$")
ROLE_ARN = re.compile(r"^arn:(aws[a-z-]*):iam::\d{12}:role/.+$")
MANIFEST_INPUTS = {
    "hls_manifests": "HlsManifests",
    "dash_manifests": "DashManifests",
    "low_latency_hls_manifests": "LowLatencyHlsManifests",
}
SOURCE_EXTENSIONS = (".m3u8", ".smil", ".mp4")
REFUSED = ("ValidationException", "ResourceNotFoundException", "ServiceQuotaExceededException")
VOD_REFUSED = ("UnprocessableEntityException", "NotFoundException")
POLL_START_SECONDS = 5
POLL_MAX_SECONDS = 30
OUTPUTS = ("job_name", "job_arn", "status", "output_uri", "asset_id", "asset_arn", "playback_url")


def run(inputs):
    action = text(inputs, "action")
    if action not in ACTIONS:
        patchbae.fail(f"Unknown action {action!r}. Use harvest_clip or publish_vod_asset.")
    if action == "harvest_clip":
        return harvest_clip(inputs)
    return publish_vod_asset(inputs)


def harvest_clip(inputs):
    names = {}
    for field in ("channel_group", "channel", "origin_endpoint"):
        names[field] = text(inputs, field)
        if not NAME.match(names[field]):
            patchbae.fail(f"action is harvest_clip, so {field} must be the resource's name: letters, digits, dashes and underscores.")
    manifests = {}
    for field, key in MANIFEST_INPUTS.items():
        listed = [str(name).strip() for name in (inputs.get(field) or []) if str(name).strip()]
        for name in listed:
            if not NAME.match(name):
                patchbae.fail(f"{field} holds manifest names: letters, digits, dashes and underscores. Got {name!r}.")
        if listed:
            manifests[key] = [{"ManifestName": name} for name in listed]
    if not manifests:
        patchbae.fail("Name at least one manifest to harvest in hls_manifests, dash_manifests or low_latency_hls_manifests.")
    start = moment(inputs, "start_time")
    end = moment(inputs, "end_time")
    if end <= start:
        patchbae.fail("end_time must be after start_time.")
    bucket, prefix = split_s3_uri(inputs.get("output_uri"), "output_uri")
    if not prefix.endswith("/"):
        patchbae.fail("output_uri must be a folder inside the bucket: end it with a slash, for example s3://bucket/harvest/cup-final/")

    mediapackage = boto3.client("mediapackagev2")
    # One job per task: the name is how a second delivery finds the first job.
    job_name = f"patchbae-{patchbae.task.id}"
    where = {
        "ChannelGroupName": names["channel_group"],
        "ChannelName": names["channel"],
        "OriginEndpointName": names["origin_endpoint"],
    }
    job = find_harvest_job(mediapackage, where, job_name)
    if job:
        patchbae.log("found the harvest job from an earlier delivery", job=job_name, status=job.get("Status", ""))
    else:
        try:
            job = mediapackage.create_harvest_job(
                **where,
                HarvestJobName=job_name,
                ClientToken=job_name,
                Description=f"Patchbae run {patchbae.task.run_id}",
                HarvestedManifests=manifests,
                ScheduleConfiguration={"StartTime": start, "EndTime": end},
                Destination={"S3Destination": {"BucketName": bucket, "DestinationPath": prefix.rstrip("/")}},
            )
            patchbae.log("harvest started", job=job_name, channel=names["channel"], start=start.isoformat(), end=end.isoformat())
        except ClientError as error:
            if error_code(error) == "ConflictException":
                # Another delivery created the job between the lookup and this call.
                job = find_harvest_job(mediapackage, where, job_name) or {}
            elif error_code(error) in REFUSED:
                patchbae.fail(f"MediaPackage would not start the harvest: {error_message(error)}")
            else:
                raise

    delay = POLL_START_SECONDS
    while job.get("Status") not in ("COMPLETED", "FAILED", "CANCELLED"):
        delay = pause(delay)
        job = mediapackage.get_harvest_job(**where, HarvestJobName=job_name)
    if job["Status"] == "FAILED":
        patchbae.fail(f"MediaPackage could not harvest the clip: {job.get('ErrorMessage') or 'no reason given'} (job {job_name})")
    if job["Status"] == "CANCELLED":
        patchbae.fail(f"Harvest job {job_name} was cancelled before it finished.")

    folder = f"s3://{bucket}/{prefix}"
    patchbae.emit_asset("Harvest", folder, start=start.isoformat(), end=end.isoformat())
    patchbae.log("harvest finished", job=job_name, output=folder)
    return result(job_name=job_name, job_arn=job.get("Arn", ""), status=job["Status"], output_uri=folder)


def find_harvest_job(mediapackage, where, job_name):
    try:
        return mediapackage.get_harvest_job(**where, HarvestJobName=job_name)
    except ClientError as error:
        if error_code(error) == "ResourceNotFoundException":
            return None
        raise


def publish_vod_asset(inputs):
    bucket, key = split_s3_uri(inputs.get("source_uri"), "source_uri")
    if not key.lower().endswith(SOURCE_EXTENSIONS):
        patchbae.fail("source_uri must point to an HLS playlist (.m3u8), a SMIL manifest (.smil) or an MP4 file.")
    group = text(inputs, "packaging_group_id")
    if not group:
        patchbae.fail("action is publish_vod_asset, so packaging_group_id must hold the ID of a MediaPackage VOD packaging group.")
    role_arn = text(inputs, "source_role_arn")
    role = ROLE_ARN.match(role_arn)
    if not role:
        patchbae.fail(
            "source_role_arn must be the ARN of the IAM role MediaPackage assumes to read the source, "
            "such as arn:aws:iam::123456789012:role/MediaPackageVodRole."
        )
    asset_id = text(inputs, "asset_id") or f"patchbae-{patchbae.task.id}"
    if not NAME.match(asset_id):
        patchbae.fail("asset_id may hold letters, digits, dashes and underscores.")

    vod = boto3.client("mediapackage-vod")
    asset = find_asset(vod, asset_id)
    if asset:
        patchbae.log("found the asset with this ID", asset=asset_id)
    else:
        try:
            asset = vod.create_asset(
                Id=asset_id,
                PackagingGroupId=group,
                SourceArn=f"arn:{role.group(1)}:s3:::{bucket}/{key}",
                SourceRoleArn=role_arn,
                Tags={"patchbae_task_id": patchbae.task.id},
            )
            patchbae.log("asset created", asset=asset_id, group=group)
        except ClientError as error:
            if error_code(error) in VOD_REFUSED:
                patchbae.fail(f"MediaPackage would not create the asset: {error_message(error)}")
            raise

    delay = POLL_START_SECONDS
    while True:
        endpoints = asset.get("EgressEndpoints", [])
        # Older assets carry no status; they are playable as soon as they exist.
        statuses = [endpoint.get("Status") or "PLAYABLE" for endpoint in endpoints]
        if "FAILED" in statuses:
            patchbae.fail(f"MediaPackage could not process asset {asset_id}. Check that its role can read the source and that the source is a format the packaging group accepts.")
        if all(status == "PLAYABLE" for status in statuses):
            break
        delay = pause(delay)
        asset = vod.describe_asset(Id=asset_id)

    if not endpoints:
        patchbae.warn("the packaging group has no packaging configurations, so the asset has no playback URLs", group=group)
    urls = [
        {"packaging_configuration_id": endpoint.get("PackagingConfigurationId", ""), "url": endpoint.get("Url", ""), "status": status}
        for endpoint, status in zip(endpoints, statuses)
    ]
    for item in urls:
        patchbae.emit_asset(f"Playback {item['packaging_configuration_id']}", item["url"])
    patchbae.log("asset is playable", asset=asset_id, endpoints=len(urls))
    return result(
        status="PLAYABLE",
        asset_id=asset_id,
        asset_arn=asset.get("Arn", ""),
        playback_urls=urls,
        playback_url=urls[0]["url"] if urls else "",
    )


def find_asset(vod, asset_id):
    try:
        return vod.describe_asset(Id=asset_id)
    except ClientError as error:
        if error_code(error) == "NotFoundException":
            return None
        raise


def pause(delay):
    """Sleeps for the delay plus a little jitter and returns the next delay."""
    time.sleep(delay + random.uniform(0, 1))
    return min(delay * 1.5, POLL_MAX_SECONDS)


def moment(inputs, name):
    """The input as a time with a time zone. Anything else fails the node."""
    value = text(inputs, name)
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        patchbae.fail(f"{name} must be a UTC time such as 2026-11-25T19:00:00Z, got {value!r}.")
    return parsed.astimezone(datetime.timezone.utc)


def result(**values):
    """Every output, with empty values for the ones this action does not produce."""
    return {**{name: "" for name in OUTPUTS}, "playback_urls": [], **values}


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
