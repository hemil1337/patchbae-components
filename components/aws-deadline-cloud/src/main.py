"""AWS Deadline Cloud: submit a render job from an Open Job Description template and wait for it.

The pod submits the job and follows it. The rendering happens on the farm's
workers, which read the scene and write the frames themselves; no media
passes through this pod. The job template is configuration, not media: when
it is given as a file, the pod reads that one small file from S3.
"""

import datetime
import json
import random
import re
import time
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

import patchbae

FARM_ID = re.compile(r"^farm-[0-9a-f]{32}$")
QUEUE_ID = re.compile(r"^queue-[0-9a-f]{32}$")
STORAGE_PROFILE_ID = re.compile(r"^sp-[0-9a-f]{32}$")
TEMPLATE_TYPES = {".json": "JSON", ".yaml": "YAML", ".yml": "YAML"}
MAX_TEMPLATE_BYTES = 1_000_000
# How a job parameter may be written out in full: {"path": "..."}.
PARAMETER_KINDS = ("int", "float", "string", "path")
MAX_COUNT = 2_147_483_647
FINISHED = ("SUCCEEDED", "FAILED", "CANCELED", "SUSPENDED", "NOT_COMPATIBLE")
BROKEN_LIFECYCLES = ("CREATE_FAILED", "UPLOAD_FAILED", "ARCHIVED")
REFUSED = ("ValidationException", "ResourceNotFoundException", "ServiceQuotaExceededException")
SEARCH_PAGE = 25
POLL_START_SECONDS = 5
POLL_MAX_SECONDS = 30


def run(inputs):
    farm_id = text(inputs, "farm_id")
    if not FARM_ID.match(farm_id):
        patchbae.fail("farm_id must be the farm's ID: farm- followed by 32 lowercase hexadecimal characters.")
    queue_id = text(inputs, "queue_id")
    if not QUEUE_ID.match(queue_id):
        patchbae.fail("queue_id must be the queue's ID: queue- followed by 32 lowercase hexadecimal characters.")
    template_uri = text(inputs, "template_uri")
    inline = str(inputs.get("template") or "").strip()
    if bool(template_uri) == bool(inline):
        patchbae.fail("Give the job template either as a file (template_uri) or inline (template), not both and not neither.")
    priority = whole_number(inputs, "priority", 50, 0, 100)
    request = {"farmId": farm_id, "queueId": queue_id, "priority": priority}
    parameters = job_parameters(inputs.get("parameters"))
    if parameters:
        request["parameters"] = parameters
    if text(inputs, "name"):
        request["nameOverride"] = text(inputs, "name")[:512]
    for field, key, lowest in (("max_failed_tasks", "maxFailedTasksCount", 0), ("max_retries_per_task", "maxRetriesPerTask", 0), ("max_workers", "maxWorkerCount", 1)):
        value = whole_number(inputs, field, None, lowest, MAX_COUNT)
        if value is not None:
            request[key] = value
    profile = text(inputs, "storage_profile_id")
    if profile:
        if not STORAGE_PROFILE_ID.match(profile):
            patchbae.fail("storage_profile_id must be the storage profile's ID: sp- followed by 32 lowercase hexadecimal characters.")
        request["storageProfileId"] = profile
    wait = inputs.get("wait", True) is not False
    if template_uri:
        bucket, key = split_s3_uri(template_uri, "template_uri")
        extension = "." + key.rsplit(".", 1)[-1].lower() if "." in key else ""
        if extension not in TEMPLATE_TYPES:
            patchbae.fail("template_uri must point to a job template file ending in .json, .yaml or .yml.")

    deadline = boto3.client("deadline")
    task_id = patchbae.task.id
    job = find_job(deadline, farm_id, queue_id, task_id)
    if job:
        job_id = job["jobId"]
        patchbae.log("found the job from an earlier delivery", job=job_id, status=job.get("taskRunStatus", ""))
    else:
        if template_uri:
            request["template"] = read_template(bucket, key)
            request["templateType"] = TEMPLATE_TYPES[extension]
        else:
            request["template"] = inline
            request["templateType"] = "JSON" if inline.startswith("{") else "YAML"
        # The task id travels with the job, so a later delivery can find it.
        request["clientToken"] = f"patchbae-{task_id}"
        request["descriptionOverride"] = f"Submitted by Patchbae. Task {task_id}, run {patchbae.task.run_id}."
        try:
            job_id = deadline.create_job(**request)["jobId"]
        except ClientError as error:
            if error_code(error) in REFUSED:
                patchbae.fail(f"Deadline Cloud would not accept the job: {error_message(error)}")
            raise
        patchbae.log("job submitted", job=job_id, queue=queue_id, priority=priority)

    where = {"farmId": farm_id, "queueId": queue_id, "jobId": job_id}
    delay = POLL_START_SECONDS
    while True:
        job = deadline.get_job(**where)
        if job.get("lifecycleStatus") in BROKEN_LIFECYCLES:
            reason = job.get("lifecycleStatusMessage") or "no reason given"
            patchbae.fail(f"Deadline Cloud could not set the job up ({job['lifecycleStatus']}): {reason} (job {job_id})")
        if not wait or job.get("taskRunStatus") in FINISHED:
            break
        time.sleep(delay + random.uniform(0, 1))
        delay = min(delay * 1.5, POLL_MAX_SECONDS)

    status = job.get("taskRunStatus", "")
    counts = {name: count for name, count in job.get("taskRunStatusCounts", {}).items() if count}
    if wait and status != "SUCCEEDED":
        ends = {
            "FAILED": f"failed: {counts.get('FAILED', 0)} of {sum(counts.values())} tasks failed. Open the job in the Deadline Cloud monitor for the task logs",
            "CANCELED": "was canceled before it finished",
            "SUSPENDED": "was suspended. Resume it in the Deadline Cloud monitor and run the node again",
            "NOT_COMPATIBLE": "cannot run: no fleet of the queue has workers that meet the job's requirements",
        }
        patchbae.fail(f"Render job {job_id} {ends[status]}.")
    patchbae.log("job finished" if wait else "job submitted, not waiting", job=job_id, status=status)
    return {
        "job_id": job_id,
        "job_name": job.get("name", ""),
        "lifecycle_status": job.get("lifecycleStatus", ""),
        "task_run_status": status,
        "task_counts": counts,
        "started_at": utc(job.get("startedAt")),
        "ended_at": utc(job.get("endedAt")),
    }


def find_job(deadline, farm_id, queue_id, task_id):
    """The job an earlier delivery of this task submitted, or None.

    The request token covers a delivery that comes right after the first. A
    later one has to look: every job this component submits carries the task
    id in its description, and the queue's jobs are searched for it.
    """
    page = deadline.search_jobs(
        farmId=farm_id,
        queueIds=[queue_id],
        itemOffset=0,
        pageSize=SEARCH_PAGE,
        filterExpressions={"filters": [{"searchTermFilter": {"searchTerm": task_id}}], "operator": "AND"},
    )
    for summary in page.get("jobs", []):
        job = deadline.get_job(farmId=farm_id, queueId=queue_id, jobId=summary["jobId"])
        if task_id in job.get("description", "") and job.get("lifecycleStatus") not in BROKEN_LIFECYCLES:
            return job
    return None


def read_template(bucket, key):
    """The job template file. It is a small text file; anything larger is not a template."""
    try:
        body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read(MAX_TEMPLATE_BYTES + 1)
    except ClientError as error:
        if error_code(error) in ("NoSuchKey", "NoSuchBucket"):
            patchbae.fail(f"The job template s3://{bucket}/{key} was not found.")
        raise
    if len(body) > MAX_TEMPLATE_BYTES:
        patchbae.fail("The job template is larger than 1 MB, which is more than Deadline Cloud accepts.")
    template = body.decode("utf-8", errors="replace").strip()
    if not template:
        patchbae.fail(f"The job template s3://{bucket}/{key} is empty.")
    return template


def job_parameters(value):
    """The template's parameter values in the form the API takes: every value as text, tagged with its kind."""
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, dict):
        patchbae.fail('parameters must be a JSON object of parameter names and values, such as {"Frames": "1-250"}.')
    parameters = {}
    for name, item in value.items():
        if isinstance(item, bool):
            parameters[name] = {"string": "true" if item else "false"}
        elif isinstance(item, int):
            parameters[name] = {"int": str(item)}
        elif isinstance(item, float):
            parameters[name] = {"float": repr(item)}
        elif isinstance(item, str):
            parameters[name] = {"string": item}
        elif isinstance(item, dict) and len(item) == 1 and next(iter(item)) in PARAMETER_KINDS:
            kind, inner = next(iter(item.items()))
            parameters[name] = {kind: str(inner)}
        else:
            patchbae.fail(f'Parameter {name!r} must be text, a number, or one of {{"int": ...}}, {{"float": ...}}, {{"string": ...}}, {{"path": ...}}.')
    return parameters


def whole_number(inputs, name, default, lowest, highest):
    value = inputs.get(name)
    if value in (None, ""):
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not lowest <= value <= highest:
        patchbae.fail(f"{name} must be a whole number from {lowest} to {highest}, got {value!r}.")
    return value


def utc(value):
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value or "")


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
