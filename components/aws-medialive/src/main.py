"""AWS Elemental MediaLive: start or stop a channel, insert an ad break, switch input.

The pod only makes API calls. Starting and stopping are checked against the
channel's state first, and schedule actions are named after the task, so a
task that is delivered twice does its work once.
"""

import datetime
import hashlib
import random
import re
import time

import boto3
from botocore.exceptions import ClientError

import patchbae

ACTIONS = ("start_channel", "stop_channel", "insert_ad_break", "switch_input")
CHANNEL_ID = re.compile(r"^\d+$")
# CREATING, UPDATING, STARTING and STOPPING pass on their own; the node waits them out.
BROKEN = ("CREATE_FAILED", "UPDATE_FAILED", "DELETING", "DELETED")
ON_AIR = ("RUNNING", "RECOVERING")
REFUSED = ("BadRequestException", "UnprocessableEntityException", "ConflictException")
TICKS_PER_SECOND = 90_000
SCHEDULE_PAGES = 10
POLL_START_SECONDS = 5
POLL_MAX_SECONDS = 30


def run(inputs):
    channel_id = text(inputs, "channel_id")
    if not CHANNEL_ID.match(channel_id):
        patchbae.fail("channel_id must be the channel's numeric ID, such as 1234567. It is the last part of the channel's ARN.")
    action = text(inputs, "action")
    if action not in ACTIONS:
        patchbae.fail(f"Unknown action {action!r}. Use start_channel, stop_channel, insert_ad_break or switch_input.")
    wait = inputs.get("wait", True) is not False

    medialive = boto3.client("medialive")
    if action == "start_channel":
        channel = start(medialive, channel_id, wait)
        return result(channel)
    if action == "stop_channel":
        channel = stop(medialive, channel_id, wait)
        return result(channel)

    settings = action_settings(action, inputs)
    start_settings, action_time = action_start(inputs)
    channel = describe(medialive, channel_id)
    if action == "switch_input":
        attachments = [item.get("InputAttachmentName") for item in channel.get("InputAttachments", [])]
        wanted = settings["InputSwitchSettings"]["InputAttachmentNameReference"]
        if wanted not in attachments:
            names = ", ".join(sorted(name for name in attachments if name)) or "none"
            patchbae.fail(f"Channel {channel_id} has no input attachment named {wanted!r}. Its input attachments: {names}.")

    name = f"patchbae-{patchbae.task.id}"
    if find_action(medialive, channel_id, name):
        patchbae.log("found the schedule action from an earlier delivery", action=name)
    else:
        schedule(medialive, channel_id, name, settings, start_settings)
        patchbae.log("schedule action added", channel=channel_id, action=name, kind=action, at=action_time)
    return result(channel, action_name=name, action_time=action_time)


def start(medialive, channel_id, wait):
    """Starts the channel unless it is already on air, and waits for RUNNING when asked to."""
    started = False
    delay = POLL_START_SECONDS
    while True:
        channel = describe(medialive, channel_id)
        state = channel.get("State", "")
        if state in BROKEN:
            patchbae.fail(f"Channel {channel_id} is {state} and cannot be started.")
        if state in ON_AIR:
            patchbae.log("channel is on air", channel=channel_id, state=state)
            return channel
        if state == "IDLE" and not started:
            call(medialive.start_channel, "start", channel_id)
            started = True
            patchbae.log("channel starting", channel=channel_id)
        if not wait and (started or state == "STARTING"):
            return {**channel, "State": "STARTING"}
        delay = pause(delay)


def stop(medialive, channel_id, wait):
    """Stops the channel unless it is already idle, and waits for IDLE when asked to."""
    stopped = False
    delay = POLL_START_SECONDS
    while True:
        channel = describe(medialive, channel_id)
        state = channel.get("State", "")
        if state in BROKEN:
            patchbae.fail(f"Channel {channel_id} is {state} and cannot be stopped.")
        if state == "IDLE":
            patchbae.log("channel is idle", channel=channel_id)
            return channel
        if state in ON_AIR + ("STARTING",) and not stopped:
            call(medialive.stop_channel, "stop", channel_id)
            stopped = True
            patchbae.log("channel stopping", channel=channel_id)
        if not wait and (stopped or state == "STOPPING"):
            return {**channel, "State": "STOPPING"}
        delay = pause(delay)


def action_settings(action, inputs):
    """The settings of the schedule action. Input problems fail here, before anything is scheduled."""
    if action == "insert_ad_break":
        # SCTE-35 allows 32 bits for the event id. The task id decides it, so a
        # second delivery asks for the same event.
        event_id = int(hashlib.sha256(patchbae.task.id.encode("utf-8")).hexdigest()[:8], 16)
        splice = {"SpliceEventId": event_id}
        seconds = inputs.get("ad_break_seconds")
        if seconds not in (None, ""):
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
                patchbae.fail("ad_break_seconds must be a number of seconds above 0, or empty for a break that runs until a return to network.")
            splice["Duration"] = round(seconds * TICKS_PER_SECOND)
        return {"Scte35SpliceInsertSettings": splice}
    attachment = text(inputs, "input_attachment")
    if not attachment:
        patchbae.fail("action is switch_input, so input_attachment must name the input attachment to switch to.")
    return {"InputSwitchSettings": {"InputAttachmentNameReference": attachment}}


def action_start(inputs):
    """When the schedule action takes effect: its start settings and the time to report."""
    value = text(inputs, "start_time")
    if not value:
        return {"ImmediateModeScheduleActionStartSettings": {}}, "immediate"
    try:
        moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        patchbae.fail(f"start_time must be a UTC time such as 2026-11-25T19:30:00Z, got {value!r}.")
    stamp = moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"FixedModeScheduleActionStartSettings": {"Time": stamp}}, stamp


def find_action(medialive, channel_id, name):
    """The schedule action with this name, or None. An earlier delivery of the task may have added it."""
    request = {"ChannelId": channel_id}
    for _page in range(SCHEDULE_PAGES):
        page = medialive.describe_schedule(**request)
        for item in page.get("ScheduleActions", []):
            if item.get("ActionName") == name:
                return item
        if not page.get("NextToken"):
            break
        request["NextToken"] = page["NextToken"]
    return None


def schedule(medialive, channel_id, name, settings, start_settings):
    """Adds the action to the channel's schedule. A refusal fails the node with MediaLive's reason."""
    item = {"ActionName": name, "ScheduleActionSettings": settings, "ScheduleActionStartSettings": start_settings}
    try:
        medialive.batch_update_schedule(ChannelId=channel_id, Creates={"ScheduleActions": [item]})
    except ClientError as error:
        if error_code(error) in REFUSED:
            patchbae.fail(f"MediaLive refused the schedule action: {error_message(error)}")
        raise


def describe(medialive, channel_id):
    try:
        return medialive.describe_channel(ChannelId=channel_id)
    except ClientError as error:
        if error_code(error) == "NotFoundException":
            patchbae.fail(f"Channel {channel_id} was not found in this region. Check the ID and the region the component is set up for.")
        raise


def call(operation, verb, channel_id):
    """Starts or stops the channel. A refusal fails the node with MediaLive's reason."""
    try:
        operation(ChannelId=channel_id)
    except ClientError as error:
        if error_code(error) in REFUSED:
            patchbae.fail(f"MediaLive would not {verb} channel {channel_id}: {error_message(error)}")
        raise


def pause(delay):
    """Sleeps for the delay plus a little jitter and returns the next delay."""
    time.sleep(delay + random.uniform(0, 1))
    return min(delay * 1.5, POLL_MAX_SECONDS)


def result(channel, action_name="", action_time=""):
    return {
        "channel_id": channel.get("Id", ""),
        "channel_arn": channel.get("Arn", ""),
        "channel_name": channel.get("Name", ""),
        "state": channel.get("State", ""),
        "action_name": action_name,
        "action_time": action_time,
    }


def text(inputs, name):
    return str(inputs.get(name) or "").strip()


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")


def error_message(error):
    return error.response.get("Error", {}).get("Message") or "no reason given"
