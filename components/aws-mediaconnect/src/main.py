"""AWS Elemental MediaConnect: start or stop a flow and return where to send the feed.

The pod only makes API calls. The flow's status is read first, so a task that
is delivered twice starts or stops the flow once.
"""

import random
import re
import time

import boto3
from botocore.exceptions import ClientError

import patchbae

ACTIONS = ("start_flow", "stop_flow")
FLOW_ARN = re.compile(r"^arn:aws[a-z-]*:mediaconnect:[a-z0-9-]+:\d{12}:flow:.+$")
# What each action wants the flow to be, and the status in which it has to act.
WANTED = {"start_flow": ("ACTIVE", "STANDBY"), "stop_flow": ("STANDBY", "ACTIVE")}
REFUSED = ("BadRequestException",)
POLL_START_SECONDS = 5
POLL_MAX_SECONDS = 30


def run(inputs):
    flow_arn = text(inputs, "flow_arn")
    if not FLOW_ARN.match(flow_arn):
        patchbae.fail("flow_arn must be the ARN of a MediaConnect flow, such as arn:aws:mediaconnect:eu-north-1:123456789012:flow:1-23aBC45dEF67hiJ8-12AbC34DE5fG:stadium-feed.")
    action = text(inputs, "action")
    if action not in ACTIONS:
        patchbae.fail(f"Unknown action {action!r}. Use start_flow or stop_flow.")
    wait = inputs.get("wait", True) is not False

    mediaconnect = boto3.client("mediaconnect")
    wanted, acts_on = WANTED[action]
    verb = "start" if action == "start_flow" else "stop"
    acted = False
    delay = POLL_START_SECONDS
    while True:
        flow, problems = describe(mediaconnect, flow_arn)
        status = flow.get("Status", "")
        if status == "ERROR":
            reason = "; ".join(problems) or "MediaConnect gave no reason"
            patchbae.fail(f"Flow {flow.get('Name', flow_arn)} is in ERROR: {reason}")
        if status == "DELETING":
            patchbae.fail(f"Flow {flow.get('Name', flow_arn)} is being deleted.")
        if status == wanted:
            patchbae.log(f"flow is {status}", flow=flow.get("Name", ""))
            return result(flow)
        if status == acts_on and not acted:
            change(mediaconnect.start_flow if action == "start_flow" else mediaconnect.stop_flow, verb, flow_arn)
            acted = True
            patchbae.log(f"flow {verb} requested", flow=flow.get("Name", ""))
        if not wait and (acted or status in ("STARTING", "STOPPING")):
            passing = "STARTING" if action == "start_flow" else "STOPPING"
            return result({**flow, "Status": passing})
        time.sleep(delay + random.uniform(0, 1))
        delay = min(delay * 1.5, POLL_MAX_SECONDS)


def describe(mediaconnect, flow_arn):
    """The flow and the errors MediaConnect reports for it."""
    try:
        response = mediaconnect.describe_flow(FlowArn=flow_arn)
    except ClientError as error:
        if error_code(error) == "NotFoundException":
            patchbae.fail("The flow was not found. Check flow_arn and the region the component is set up for.")
        raise
    return response.get("Flow", {}), response.get("Messages", {}).get("Errors", [])


def change(operation, verb, flow_arn):
    """Starts or stops the flow. A refusal fails the node with MediaConnect's reason."""
    try:
        operation(FlowArn=flow_arn)
    except ClientError as error:
        if error_code(error) in REFUSED:
            reason = error.response.get("Error", {}).get("Message") or "no reason given"
            patchbae.fail(f"MediaConnect would not {verb} the flow: {reason}")
        raise


def result(flow):
    source = flow.get("Source", {})
    return {
        "flow_arn": flow.get("FlowArn", ""),
        "flow_name": flow.get("Name", ""),
        "status": flow.get("Status", ""),
        "ingest_ip": source.get("IngestIp", ""),
        "ingest_port": source.get("IngestPort", 0),
        "ingest_protocol": source.get("Transport", {}).get("Protocol", ""),
        "egress_ip": flow.get("EgressIp", ""),
    }


def text(inputs, name):
    return str(inputs.get(name) or "").strip()


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")
