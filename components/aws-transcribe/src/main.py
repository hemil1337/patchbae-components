"""Amazon Transcribe: speech in a media file on S3 to a transcript and subtitles.

The media never passes through this pod. Transcribe reads the file from S3
and writes its results back to S3; the pod starts the job, waits for it and
returns references to what was written.
"""

import json
import random
import time
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

import patchbae

MAX_TEXT_CHARS = 200_000
SUBTITLE_MEDIA_TYPES = {"srt": "application/x-subrip", "vtt": "text/vtt"}
POLL_START_SECONDS = 5
POLL_MAX_SECONDS = 30
NOT_FOUND_CODES = {"BadRequestException", "NotFoundException"}


def run(inputs):
    media_bucket, media_key = split_s3_uri(inputs["media_uri"], "media_uri")
    out_bucket, out_prefix = split_s3_uri(inputs["output_uri"], "output_uri")
    if out_prefix and not out_prefix.endswith("/"):
        patchbae.fail("output_uri must be a folder: end it with a slash, for example s3://bucket/transcripts/")
    formats = subtitle_formats(inputs.get("subtitle_formats"))

    transcribe = boto3.client("transcribe")
    # One job per task: a redelivered task finds its job instead of paying for a second one.
    job_name = f"patchbae-{patchbae.task.id}"

    job = find_job(transcribe, job_name)
    if job and job["TranscriptionJobStatus"] == "FAILED":
        # A failed job from an earlier attempt keeps its name; clear it so this attempt can run.
        transcribe.delete_transcription_job(TranscriptionJobName=job_name)
        job = None
    if job is None:
        request = start_request(inputs, job_name, f"s3://{media_bucket}/{media_key}", out_bucket, out_prefix, formats)
        try:
            transcribe.start_transcription_job(**request)
            patchbae.log("transcription started", job=job_name, media=inputs["media_uri"])
        except ClientError as error:
            if error_code(error) != "ConflictException":
                raise
            patchbae.log("transcription already running, waiting for it", job=job_name)
    else:
        patchbae.log("found the transcription from an earlier delivery", job=job_name, status=job["TranscriptionJobStatus"])

    job = wait_for(transcribe, job_name)
    if job["TranscriptionJobStatus"] == "FAILED":
        patchbae.fail(f"Transcribe could not process the file: {job.get('FailureReason', 'no reason given')}")

    transcript_uri = f"s3://{out_bucket}/{out_prefix}{job_name}.json"
    subtitle_uris = {fmt: f"s3://{out_bucket}/{out_prefix}{job_name}.{fmt}" for fmt in formats}

    patchbae.emit_asset("Transcript", transcript_uri, "application/json")
    for fmt, uri in subtitle_uris.items():
        patchbae.emit_asset(f"Subtitles {fmt.upper()}", uri, SUBTITLE_MEDIA_TYPES[fmt])

    output = {
        "transcript_uri": transcript_uri,
        "subtitle_uris": subtitle_uris,
        "language_code": job.get("LanguageCode", ""),
        "job_name": job_name,
    }
    if inputs.get("include_text", True):
        text = transcript_text(out_bucket, f"{out_prefix}{job_name}.json")
        output["text"] = text[:MAX_TEXT_CHARS]
        output["text_truncated"] = len(text) > MAX_TEXT_CHARS
    patchbae.log("transcription finished", job=job_name, language=output["language_code"])
    return output


def start_request(inputs, job_name, media_uri, out_bucket, out_prefix, formats):
    """The StartTranscriptionJob request for these inputs."""
    request = {
        "TranscriptionJobName": job_name,
        "Media": {"MediaFileUri": media_uri},
        "OutputBucketName": out_bucket,
    }
    if out_prefix:
        # A key ending in "/" is a folder: Transcribe names the files after the job.
        request["OutputKey"] = out_prefix
    if formats:
        request["Subtitles"] = {"Formats": formats, "OutputStartIndex": 1}

    language = (inputs.get("language_code") or "auto").strip()
    vocabulary = (inputs.get("vocabulary_name") or "").strip()
    settings = {}
    if language.lower() == "auto":
        if vocabulary:
            patchbae.fail("A custom vocabulary belongs to one language: set language_code instead of auto to use it.")
        request["IdentifyLanguage"] = True
        options = [code for code in (inputs.get("language_options") or []) if code]
        if len(options) == 1:
            patchbae.fail("language_options needs two or more languages. For one language, set language_code to it.")
        if options:
            request["LanguageOptions"] = options
    else:
        request["LanguageCode"] = language
        if vocabulary:
            settings["VocabularyName"] = vocabulary

    if inputs.get("speaker_labels"):
        max_speakers = int(inputs.get("max_speakers") or 4)
        if not 2 <= max_speakers <= 30:
            patchbae.fail("max_speakers must be between 2 and 30.")
        settings["ShowSpeakerLabels"] = True
        settings["MaxSpeakerLabels"] = max_speakers
    if settings:
        request["Settings"] = settings
    return request


def find_job(transcribe, job_name):
    """The job with this name, or None when there is none."""
    try:
        return transcribe.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
    except ClientError as error:
        if error_code(error) in NOT_FOUND_CODES:
            return None
        raise


def wait_for(transcribe, job_name):
    """Polls until the job has finished, backing off to one request every 30 seconds."""
    delay = POLL_START_SECONDS
    while True:
        job = transcribe.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
        if job["TranscriptionJobStatus"] in ("COMPLETED", "FAILED"):
            return job
        time.sleep(delay + random.uniform(0, 1))
        delay = min(delay * 1.5, POLL_MAX_SECONDS)


def transcript_text(bucket, key):
    """The plain text of a finished transcript."""
    body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    transcripts = json.loads(body).get("results", {}).get("transcripts", [])
    return " ".join(item.get("transcript", "") for item in transcripts).strip()


def subtitle_formats(value):
    formats = [str(fmt).strip().lower() for fmt in (value if value is not None else ["srt", "vtt"]) if str(fmt).strip()]
    unknown = [fmt for fmt in formats if fmt not in SUBTITLE_MEDIA_TYPES]
    if unknown:
        patchbae.fail(f"Unknown subtitle format {unknown[0]!r}. Use srt, vtt or both.")
    return sorted(set(formats))


def split_s3_uri(uri, name):
    parsed = urlparse(str(uri or ""))
    if parsed.scheme != "s3" or not parsed.netloc:
        patchbae.fail(f"{name} must be an S3 URI such as s3://bucket/path, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")
