"""Amazon Rekognition Video: labels, moderation labels and shots for a video on S3.

The video never passes through this pod. Rekognition reads the file from S3;
the pod starts the analyses, waits for them, reads the results page by page
and returns a summary. The complete results can be written to S3 as JSON.
"""

import hashlib
import json
import random
import re
import time
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

import patchbae

ANALYSES = ("labels", "moderation", "shots")
RESULT_KEYS = {"labels": "Labels", "moderation": "ModerationLabels", "shots": "Segments"}
PAGE_SIZE = 1000
MAX_SHOTS = 1000
POLL_START_SECONDS = 5
POLL_MAX_SECONDS = 30


def run(inputs):
    bucket, key = split_s3_uri(inputs.get("media_uri"), "media_uri")
    if not key or key.endswith("/"):
        patchbae.fail("media_uri must point to a video file such as s3://bucket/in/show.mp4, not to a folder or a bucket.")
    analyses = requested_analyses(inputs.get("analyses"))
    min_confidence = number(inputs.get("min_confidence"), 80, "min_confidence", 0, 100)
    max_items = int(number(inputs.get("max_items"), 50, "max_items", 1, 1000))
    results = None
    if inputs.get("output_uri"):
        out_bucket, out_prefix = split_s3_uri(inputs["output_uri"], "output_uri")
        if out_prefix and not out_prefix.endswith("/"):
            patchbae.fail("output_uri must be a folder: end it with a slash, for example s3://bucket/analysis/")
        # Named after the task, so a redelivered task writes the same file again.
        results = (out_bucket, f"{out_prefix}patchbae-{patchbae.task.id}-rekognition.json")

    media_uri = f"s3://{bucket}/{key}"
    rekognition = boto3.client("rekognition")
    video = {"S3Object": {"Bucket": bucket, "Name": key}}
    # Start everything first so the analyses run side by side, then wait for all of them.
    jobs = {analysis: start(rekognition, analysis, video, min_confidence) for analysis in analyses}
    patchbae.log("analyses started", media=media_uri, **jobs)
    first_pages = wait_for(rekognition, jobs)

    output = {"labels": [], "moderation_labels": [], "flagged": False, "shot_count": 0, "shots": []}
    kept = {}
    for analysis in analyses:
        pages = result_pages(rekognition, analysis, jobs[analysis], first_pages[analysis])
        if results:
            pages = keeping(pages, kept.setdefault(RESULT_KEYS[analysis], []))
        entries = (entry for page in pages for entry in page)
        if analysis == "labels":
            output["labels"] = listed(strongest(entries, "Label"), max_items)
        elif analysis == "moderation":
            ranked = strongest(entries, "ModerationLabel")
            output["moderation_labels"] = listed(ranked, max_items)
            output["flagged"] = any(item["confidence"] >= min_confidence for item in ranked)
        else:
            shots = [
                {"start_ms": int(entry["StartTimestampMillis"]), "end_ms": int(entry["EndTimestampMillis"])}
                for entry in entries
                if entry.get("Type") == "SHOT"
            ]
            output["shot_count"] = len(shots)
            output["shots"] = shots[:MAX_SHOTS]

    if results:
        out_bucket, out_key = results
        body = results_document({"media_uri": media_uri, "min_confidence": min_confidence}, kept)
        boto3.client("s3").put_object(Bucket=out_bucket, Key=out_key, Body=body, ContentType="application/json")
        output["results_uri"] = f"s3://{out_bucket}/{out_key}"
        patchbae.emit_asset("Video analysis", output["results_uri"], "application/json")
    patchbae.log(
        "analyses finished",
        labels=len(output["labels"]),
        moderation_labels=len(output["moderation_labels"]),
        flagged=output["flagged"],
        shots=output["shot_count"],
    )
    return output


def start(rekognition, analysis, video, min_confidence):
    """Starts one analysis and returns its job id.

    The token is the task id plus the analysis, so a task that is delivered
    again gets the job it already started instead of paying for another one.
    """
    request = {"Video": video, "ClientRequestToken": request_token(patchbae.task.id, analysis)}
    delay = POLL_START_SECONDS
    while True:
        try:
            if analysis == "labels":
                return rekognition.start_label_detection(MinConfidence=min_confidence, **request)["JobId"]
            if analysis == "moderation":
                return rekognition.start_content_moderation(MinConfidence=min_confidence, **request)["JobId"]
            return rekognition.start_segment_detection(SegmentTypes=["SHOT"], **request)["JobId"]
        except ClientError as error:
            code = error_code(error)
            if code == "InvalidS3ObjectException":
                patchbae.fail(
                    "Rekognition cannot read media_uri. Check that the file exists, that the component's role "
                    "may read it, and that the bucket is in the region the component runs in."
                )
            if code == "VideoTooLargeException":
                patchbae.fail("The video is too large for Rekognition Video: at most 10 GB and 6 hours. Analyse a proxy instead.")
            if code != "LimitExceededException":
                raise
        # The account already runs as many video analyses as Rekognition allows: wait for a free slot.
        patchbae.log("waiting for a free Rekognition Video slot", analysis=analysis)
        time.sleep(delay + random.uniform(0, 1))
        delay = min(delay * 1.5, POLL_MAX_SECONDS)


def wait_for(rekognition, jobs):
    """Polls every analysis until all have succeeded and returns the first result page of each.

    The first analysis that fails fails the node with Rekognition's reason.
    """
    first_pages = {}
    delay = POLL_START_SECONDS
    while True:
        for analysis, job_id in jobs.items():
            if analysis in first_pages:
                continue
            page = result_page(rekognition, analysis, job_id)
            if page["JobStatus"] == "FAILED":
                reason = page.get("StatusMessage") or "no reason given"
                patchbae.fail(f"Rekognition could not analyse the video ({analysis}): {reason}")
            if page["JobStatus"] == "SUCCEEDED":
                first_pages[analysis] = page
        if len(first_pages) == len(jobs):
            return first_pages
        time.sleep(delay + random.uniform(0, 1))
        delay = min(delay * 1.5, POLL_MAX_SECONDS)


def result_page(rekognition, analysis, job_id, next_token=None):
    """One page of an analysis: its status and, once it has succeeded, up to 1,000 results."""
    request = {"JobId": job_id, "MaxResults": PAGE_SIZE}
    if next_token:
        request["NextToken"] = next_token
    if analysis == "labels":
        return rekognition.get_label_detection(**request)
    if analysis == "moderation":
        return rekognition.get_content_moderation(**request)
    return rekognition.get_segment_detection(**request)


def result_pages(rekognition, analysis, job_id, page):
    """Every page of a finished analysis as a list of results, following NextToken to the end."""
    while True:
        yield page.get(RESULT_KEYS[analysis], [])
        if not page.get("NextToken"):
            return
        page = result_page(rekognition, analysis, job_id, page["NextToken"])


def keeping(pages, texts):
    """Passes result pages through and keeps each one as JSON text for the results file.

    A long video has hundreds of thousands of results. As text they take a
    fraction of the memory they take as objects.
    """
    for page in pages:
        if page:
            texts.append(json.dumps(page)[1:-1])
        yield page


def results_document(header, kept):
    """The results file: the header fields, then every result of each analysis under Rekognition's own key."""
    parts = [json.dumps(header)[:-1]]
    for key, texts in kept.items():
        parts.append(f', "{key}": [{", ".join(texts)}]')
    parts.append("}")
    return "".join(parts).encode("utf-8")


def strongest(entries, field):
    """One item per name with its highest confidence and the first time it appears, most confident first."""
    best = {}
    for entry in entries:
        found = entry.get(field) or {}
        name = found.get("Name")
        if not name:
            continue
        confidence = float(found.get("Confidence") or 0)
        timestamp = int(entry.get("Timestamp") or 0)
        item = best.setdefault(name, {"name": name, "confidence": confidence, "timestamp_ms": timestamp})
        item["confidence"] = max(item["confidence"], confidence)
        item["timestamp_ms"] = min(item["timestamp_ms"], timestamp)
    return sorted(best.values(), key=lambda item: (-item["confidence"], item["name"]))


def listed(ranked, limit):
    return [{**item, "confidence": round(item["confidence"], 2)} for item in ranked[:limit]]


def requested_analyses(value):
    names = [str(name).strip().lower() for name in (value if value is not None else ["labels", "moderation"]) if str(name).strip()]
    unknown = [name for name in names if name not in ANALYSES]
    if unknown:
        patchbae.fail(f"Unknown analysis {unknown[0]!r}. Use any of labels, moderation, shots.")
    if not names:
        patchbae.fail("analyses is empty. Choose at least one of labels, moderation, shots.")
    return [name for name in ANALYSES if name in names]


def number(value, default, name, low, high):
    try:
        result = float(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        patchbae.fail(f"{name} must be a number between {low} and {high}, got {value!r}")
    if not low <= result <= high:
        patchbae.fail(f"{name} must be between {low} and {high}, got {value!r}")
    return result


def request_token(*parts):
    """An idempotency token built from the task id: at most 64 characters of letters, digits, - and _."""
    token = re.sub(r"[^a-zA-Z0-9_-]", "-", "-".join(str(part) for part in parts))
    if len(token) > 64:
        token = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token


def split_s3_uri(uri, name):
    parsed = urlparse(str(uri or ""))
    if parsed.scheme != "s3" or not parsed.netloc:
        patchbae.fail(f"{name} must be an S3 URI such as s3://bucket/path, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")
