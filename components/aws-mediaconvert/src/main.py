"""AWS Elemental MediaConvert: transcode a media file on S3 to MP4, HLS or a job template.

The media never passes through this pod. MediaConvert reads the file from S3
and writes its outputs back to S3 through its own service role; the pod
submits the job, waits for it and returns references to what was written.
"""

import hashlib
import posixpath
import random
import re
import time
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

import patchbae

PRESETS = ("mp4_h264_1080p", "mp4_h264_720p", "hls_abr", "job_template")
# name modifier, width, height, QVBR quality level, highest video bitrate, audio bitrate
MP4_RENDITIONS = {
    "mp4_h264_1080p": ("_1080p", 1920, 1080, 8, 8_000_000, 128_000),
    "mp4_h264_720p": ("_720p", 1280, 720, 7, 4_000_000, 128_000),
}
HLS_RENDITIONS = [
    ("_1080p", 1920, 1080, 8, 6_000_000, 128_000),
    ("_720p", 1280, 720, 7, 3_500_000, 128_000),
    ("_360p", 640, 360, 7, 1_200_000, 96_000),
]
# Where each kind of output group keeps its destination, and the manifests it writes.
GROUP_SETTINGS = {
    "FILE_GROUP_SETTINGS": "FileGroupSettings",
    "HLS_GROUP_SETTINGS": "HlsGroupSettings",
    "DASH_ISO_GROUP_SETTINGS": "DashIsoGroupSettings",
    "CMAF_GROUP_SETTINGS": "CmafGroupSettings",
    "MS_SMOOTH_GROUP_SETTINGS": "MsSmoothGroupSettings",
}
MANIFESTS = {
    "HLS_GROUP_SETTINGS": ["m3u8"],
    "DASH_ISO_GROUP_SETTINGS": ["mpd"],
    "CMAF_GROUP_SETTINGS": ["m3u8", "mpd"],
    "MS_SMOOTH_GROUP_SETTINGS": ["ism"],
}
# The extension MediaConvert gives a file output when the output does not set one.
CONTAINER_EXTENSIONS = {"MP4": "mp4", "MOV": "mov", "MXF": "mxf", "WEBM": "webm", "M2TS": "m2ts", "GIF": "gif"}
MEDIA_TYPES = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "mxf": "application/mxf",
    "webm": "video/webm",
    "m3u8": "application/vnd.apple.mpegurl",
}
ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/.+$")
TASK_KEY = "patchbae_task_id"
DEFAULT_QUEUE = "Default"
SEARCH_PAGES = 5
POLL_START_SECONDS = 5
POLL_MAX_SECONDS = 30


def run(inputs):
    media_bucket, media_key = split_s3_uri(inputs.get("media_uri"), "media_uri")
    out_bucket, out_prefix = split_s3_uri(inputs.get("output_uri"), "output_uri")
    stem = posixpath.splitext(posixpath.basename(media_key))[0]
    if not stem:
        patchbae.fail("media_uri must point to a file such as s3://bucket/in/show.mxf, not to a folder or a bucket.")
    if out_prefix and not out_prefix.endswith("/"):
        patchbae.fail("output_uri must be a folder: end it with a slash, for example s3://bucket/transcodes/")
    role_arn = text(inputs, "mediaconvert_role_arn")
    if not ROLE_ARN.match(role_arn):
        patchbae.fail(
            "mediaconvert_role_arn must be the ARN of the IAM role MediaConvert assumes, "
            "such as arn:aws:iam::123456789012:role/MediaConvertServiceRole."
        )
    preset = text(inputs, "preset") or "mp4_h264_1080p"
    template_name = text(inputs, "job_template")
    if preset not in PRESETS:
        patchbae.fail(f"Unknown preset {preset!r}. Use mp4_h264_1080p, mp4_h264_720p, hls_abr or job_template.")
    if preset == "job_template" and not template_name:
        patchbae.fail("preset is job_template, so job_template must hold the name of a MediaConvert job template.")
    if template_name and preset != "job_template":
        patchbae.fail(f"job_template is set but preset is {preset}. Set preset to job_template to use the template.")
    modifier = text(inputs, "name_modifier")
    if "/" in modifier:
        patchbae.fail("name_modifier is added to the file name and cannot contain a slash. Put folders in output_uri.")
    queue = text(inputs, "queue")

    media_uri = f"s3://{media_bucket}/{media_key}"
    folder = f"s3://{out_bucket}/{out_prefix}"
    mediaconvert = boto3.client("mediaconvert")
    if preset == "job_template":
        template = find_template(mediaconvert, template_name)
        groups = redirected(template, template_name, f"{folder}{stem}{modifier}")
        # The template supplies everything else; the job only names its file and where the outputs go.
        settings = {"Inputs": [{"FileInput": media_uri}], "OutputGroups": groups}
    else:
        template = {}
        groups = [preset_group(preset, f"{folder}{stem}", modifier)]
        settings = {"TimecodeConfig": {"Source": "ZEROBASED"}, "Inputs": [preset_input(media_uri)], "OutputGroups": groups}
    files = written_files(groups)
    if media_uri in [uri for uri, _label in files]:
        patchbae.fail("The output would overwrite media_uri. Choose another output_uri or set a name_modifier.")

    earlier = find_job(mediaconvert, media_uri, queue or template.get("Queue") or DEFAULT_QUEUE)
    if earlier and earlier["Status"] not in ("ERROR", "CANCELED"):
        job_id = earlier["Id"]
        patchbae.log("found the job from an earlier delivery", job=job_id, status=earlier["Status"])
    else:
        request = {"Role": role_arn, "Settings": settings, "UserMetadata": {TASK_KEY: patchbae.task.id}}
        # A job that failed on an earlier attempt goes into the token, so MediaConvert
        # starts a new job instead of handing the failed one back.
        request["ClientRequestToken"] = request_token(patchbae.task.id, *([earlier["Id"]] if earlier else []))
        if template_name:
            request["JobTemplate"] = template_name
        if queue:
            request["Queue"] = queue
        job_id = submit(mediaconvert, request)
        patchbae.log("transcode started", job=job_id, media=media_uri, preset=preset)

    job = wait_for(mediaconvert, job_id)
    if job["Status"] == "ERROR":
        reason = job.get("ErrorMessage") or "no reason given"
        patchbae.fail(f"MediaConvert could not transcode the file: {reason} (job {job_id})")
    if job["Status"] == "CANCELED":
        patchbae.fail(f"MediaConvert job {job_id} was canceled before it finished.")

    emitted = set()
    for uri, label in files:
        if label and label not in emitted:
            patchbae.emit_asset(label, uri, MEDIA_TYPES.get(uri.rsplit(".", 1)[-1]))
            emitted.add(label)
    patchbae.log("transcode finished", job=job_id, outputs=len(files))
    return {"job_id": job_id, "output_uri": folder, "output_uris": [uri for uri, _label in files], "status": job["Status"]}


def preset_input(media_uri):
    """The input of a built-in preset: the file, its video and its default audio."""
    return {
        "FileInput": media_uri,
        "AudioSelectors": {"Audio Selector 1": {"DefaultSelection": "DEFAULT"}},
        "VideoSelector": {},
        "TimecodeSource": "ZEROBASED",
    }


def preset_group(preset, base, modifier):
    """The output group of a built-in preset. `base` is the output folder plus the input's file name."""
    if preset == "hls_abr":
        return {
            "Name": "Apple HLS",
            "OutputGroupSettings": {
                "Type": "HLS_GROUP_SETTINGS",
                "HlsGroupSettings": {"Destination": f"{base}{modifier}", "SegmentLength": 6, "MinSegmentLength": 0},
            },
            "Outputs": [
                {
                    **h264_aac_output(*rendition),
                    "ContainerSettings": {"Container": "M3U8", "M3u8Settings": {}},
                    "OutputSettings": {"HlsSettings": {}},
                }
                for rendition in HLS_RENDITIONS
            ],
        }
    default_modifier, *encoding = MP4_RENDITIONS[preset]
    return {
        "Name": "File Group",
        "OutputGroupSettings": {"Type": "FILE_GROUP_SETTINGS", "FileGroupSettings": {"Destination": base}},
        "Outputs": [
            {
                **h264_aac_output(modifier or default_modifier, *encoding),
                "ContainerSettings": {"Container": "MP4", "Mp4Settings": {"MoovPlacement": "PROGRESSIVE_DOWNLOAD"}},
            }
        ],
    }


def h264_aac_output(modifier, width, height, quality, max_bitrate, audio_bitrate):
    """One output: H.264 at a constant quality (QVBR) with stereo AAC. The frame rate follows the source."""
    return {
        "NameModifier": modifier,
        "VideoDescription": {
            "Width": width,
            "Height": height,
            "CodecSettings": {
                "Codec": "H_264",
                "H264Settings": {
                    "RateControlMode": "QVBR",
                    "QvbrSettings": {"QvbrQualityLevel": quality},
                    "MaxBitrate": max_bitrate,
                    "SceneChangeDetect": "TRANSITION_DETECTION",
                    "QualityTuningLevel": "SINGLE_PASS_HQ",
                },
            },
        },
        "AudioDescriptions": [
            {
                "AudioSourceName": "Audio Selector 1",
                "CodecSettings": {
                    "Codec": "AAC",
                    "AacSettings": {"Bitrate": audio_bitrate, "CodingMode": "CODING_MODE_2_0", "SampleRate": 48000},
                },
            }
        ],
    }


def find_template(mediaconvert, name):
    """The job template with this name; a name that does not exist fails the node."""
    try:
        return mediaconvert.get_job_template(Name=name)["JobTemplate"]
    except ClientError as error:
        if error_code(error) == "NotFoundException":
            patchbae.fail(f"Job template {name!r} was not found in this region. Check its name under MediaConvert, Job templates.")
        raise


def redirected(template, name, base):
    """The template's output groups, unchanged except that each one writes to `base`."""
    groups = []
    for group in template.get("Settings", {}).get("OutputGroups", []):
        group_settings = group.get("OutputGroupSettings", {})
        key = GROUP_SETTINGS.get(group_settings.get("Type"))
        if not key:
            patchbae.fail(f"Job template {name!r} has an output group this component cannot point at output_uri.")
        destination = {**group_settings.get(key, {}), "Destination": base}
        groups.append({**group, "OutputGroupSettings": {**group_settings, key: destination}})
    if not groups:
        patchbae.fail(f"Job template {name!r} has no output groups, so it would write nothing.")
    return groups


def written_files(groups):
    """The files a job with these output groups writes, as (S3 URI, asset label) pairs.

    MediaConvert names a file <destination><name modifier>.<extension> and a
    manifest <destination>.<extension>. Segments and rendition playlists are
    left out, as is any file whose extension the settings do not determine.
    """
    files = []
    for group in groups:
        group_settings = group["OutputGroupSettings"]
        kind = group_settings["Type"]
        settings = group_settings[GROUP_SETTINGS[kind]]
        base = settings["Destination"]
        if kind == "FILE_GROUP_SETTINGS":
            for output in group.get("Outputs", []):
                container = output.get("ContainerSettings", {}).get("Container")
                extension = output.get("Extension") or CONTAINER_EXTENSIONS.get(container)
                if extension:
                    files.append((f"{base}{output.get('NameModifier', '')}.{extension}", "Transcode"))
            continue
        for extension in MANIFESTS[kind]:
            if kind == "CMAF_GROUP_SETTINGS":
                switch = "WriteHlsManifest" if extension == "m3u8" else "WriteDashManifest"
                if settings.get(switch) == "DISABLED":
                    continue
            files.append((f"{base}.{extension}", "HLS" if kind == "HLS_GROUP_SETTINGS" else None))
    return files


def find_job(mediaconvert, media_uri, queue):
    """The newest job an earlier delivery of this task submitted for this file, or None.

    MediaConvert honours a client request token for one minute only, so a
    delivery that comes later has to look its job up. Every job carries the
    task id in its user metadata; the search narrows by queue and input file.
    """
    request = {"Queue": queue, "InputFile": media_uri[-300:], "Order": "DESCENDING", "MaxResults": 20}
    for _page in range(SEARCH_PAGES):
        page = mediaconvert.search_jobs(**request)
        for job in page.get("Jobs", []):
            if job.get("UserMetadata", {}).get(TASK_KEY) == patchbae.task.id:
                return job
        if not page.get("NextToken"):
            break
        request["NextToken"] = page["NextToken"]
    return None


def submit(mediaconvert, request):
    """Creates the job and returns its id. A request MediaConvert rejects fails the node with its reason."""
    try:
        return mediaconvert.create_job(**request)["Job"]["Id"]
    except ClientError as error:
        if error_code(error) in ("BadRequestException", "NotFoundException"):
            reason = error.response.get("Error", {}).get("Message") or "no reason given"
            patchbae.fail(f"MediaConvert rejected the job: {reason}")
        raise


def wait_for(mediaconvert, job_id):
    """Polls until the job has finished, backing off to one request every 30 seconds."""
    delay = POLL_START_SECONDS
    while True:
        job = mediaconvert.get_job(Id=job_id)["Job"]
        if job["Status"] in ("COMPLETE", "ERROR", "CANCELED"):
            return job
        time.sleep(delay + random.uniform(0, 1))
        delay = min(delay * 1.5, POLL_MAX_SECONDS)


def request_token(*parts):
    """An idempotency token built from the task id: at most 64 characters of letters, digits, - and _."""
    token = re.sub(r"[^a-zA-Z0-9_-]", "-", "-".join(str(part) for part in parts))
    if len(token) > 64:
        token = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token


def text(inputs, name):
    return str(inputs.get(name) or "").strip()


def split_s3_uri(uri, name):
    parsed = urlparse(str(uri or ""))
    if parsed.scheme != "s3" or not parsed.netloc:
        patchbae.fail(f"{name} must be an S3 URI such as s3://bucket/path, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")
