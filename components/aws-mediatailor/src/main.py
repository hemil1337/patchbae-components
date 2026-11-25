"""AWS Elemental MediaTailor: set up ad insertion for a stream, or schedule a program on a channel.

The pod only makes API calls. A playback configuration that is already as
asked is left alone, and a program is looked up by name before it is created,
so a task that is delivered twice does its work once.
"""

import datetime
import re

import boto3
from botocore.exceptions import ClientError

import patchbae

ACTIONS = ("configure_ad_insertion", "schedule_program")
RESOURCE_NAME = re.compile(r"^[^\s/]{1,512}$")
URL = re.compile(r"^https?://\S+$")
POSITIONS = ("AFTER_PROGRAM", "BEFORE_PROGRAM")
OUTPUTS = (
    "playback_configuration_arn",
    "hls_playback_prefix",
    "dash_playback_prefix",
    "session_initialization_prefix",
    "playback_url",
    "program_name",
    "program_arn",
    "scheduled_start_time",
)


def run(inputs):
    action = text(inputs, "action")
    if action not in ACTIONS:
        patchbae.fail(f"Unknown action {action!r}. Use configure_ad_insertion or schedule_program.")
    if action == "configure_ad_insertion":
        return configure_ad_insertion(inputs)
    return schedule_program(inputs)


def configure_ad_insertion(inputs):
    name = text(inputs, "configuration_name")
    if not RESOURCE_NAME.match(name):
        patchbae.fail("action is configure_ad_insertion, so configuration_name must be a name without spaces or slashes.")
    wanted = {"VideoContentSourceUrl": text(inputs, "content_source_url"), "AdDecisionServerUrl": text(inputs, "ad_decision_server_url")}
    if not URL.match(wanted["VideoContentSourceUrl"]):
        patchbae.fail("content_source_url must be the http(s) URL prefix of the stream's origin, without the manifest's file name.")
    if not URL.match(wanted["AdDecisionServerUrl"]):
        patchbae.fail("ad_decision_server_url must be the http(s) URL of the ad decision server.")
    slate = text(inputs, "slate_ad_url")
    if slate and not URL.match(slate):
        patchbae.fail("slate_ad_url must be the http(s) URL of an MP4, or empty for no slate.")
    if slate:
        wanted["SlateAdUrl"] = slate
    manifest_path = text(inputs, "manifest_path").lstrip("/")

    mediatailor = boto3.client("mediatailor")
    current = find(mediatailor.get_playback_configuration, Name=name)
    if current and all(current.get(key, "") == value for key, value in {"SlateAdUrl": "", **wanted}.items()):
        configuration = current
        patchbae.log("playback configuration is already set up this way", configuration=name)
    else:
        try:
            configuration = mediatailor.put_playback_configuration(Name=name, **wanted)
        except ClientError as error:
            fail_if_refused(error, "would not save the playback configuration")
            raise
        patchbae.log("playback configuration saved", configuration=name, replaced=bool(current))

    hls_prefix = configuration.get("HlsConfiguration", {}).get("ManifestEndpointPrefix", "")
    playback_url = f"{hls_prefix}{manifest_path}" if hls_prefix and manifest_path else ""
    if playback_url:
        patchbae.emit_asset("Playback with ads", playback_url, "application/vnd.apple.mpegurl")
    return result(
        playback_configuration_arn=configuration.get("PlaybackConfigurationArn", ""),
        hls_playback_prefix=hls_prefix,
        dash_playback_prefix=configuration.get("DashConfiguration", {}).get("ManifestEndpointPrefix", ""),
        session_initialization_prefix=configuration.get("SessionInitializationEndpointPrefix", ""),
        playback_url=playback_url,
    )


def schedule_program(inputs):
    names = {}
    for field in ("channel_name", "source_location_name", "vod_source_name"):
        names[field] = text(inputs, field)
        if not RESOURCE_NAME.match(names[field]):
            patchbae.fail(f"action is schedule_program, so {field} must hold the resource's name.")
    program = text(inputs, "program_name") or f"patchbae-{patchbae.task.id}"
    if not RESOURCE_NAME.match(program):
        patchbae.fail("program_name must be a name without spaces or slashes.")
    position = text(inputs, "relative_position") or "AFTER_PROGRAM"
    if position not in POSITIONS:
        patchbae.fail(f"Unknown relative_position {position!r}. Use AFTER_PROGRAM or BEFORE_PROGRAM.")
    start_time = text(inputs, "start_time")
    relative_program = text(inputs, "relative_program")
    if start_time and relative_program:
        patchbae.fail("Set start_time or relative_program, not both: a program starts at a set time or next to another program.")
    if start_time:
        transition = {"Type": "ABSOLUTE", "RelativePosition": "AFTER_PROGRAM", "ScheduledStartTimeMillis": epoch_millis(start_time)}
    else:
        transition = {"Type": "RELATIVE", "RelativePosition": position}
        if relative_program:
            transition["RelativeProgram"] = relative_program
    breaks = ad_breaks(inputs.get("ad_break_offsets"))

    mediatailor = boto3.client("mediatailor")
    described = find(mediatailor.describe_program, ChannelName=names["channel_name"], ProgramName=program)
    if described:
        patchbae.log("found the program with this name", channel=names["channel_name"], program=program)
    else:
        request = {
            "ChannelName": names["channel_name"],
            "ProgramName": program,
            "SourceLocationName": names["source_location_name"],
            "VodSourceName": names["vod_source_name"],
            "ScheduleConfiguration": {"Transition": transition},
        }
        if breaks:
            request["AdBreaks"] = breaks
        try:
            described = mediatailor.create_program(**request)
        except ClientError as error:
            fail_if_refused(error, "would not schedule the program")
            raise
        patchbae.log("program scheduled", channel=names["channel_name"], program=program, source=names["vod_source_name"])

    scheduled = described.get("ScheduledStartTime")
    if isinstance(scheduled, datetime.datetime):
        scheduled = scheduled.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return result(program_name=program, program_arn=described.get("Arn", ""), scheduled_start_time=str(scheduled or ""))


def ad_breaks(offsets):
    """One ad break per offset in seconds. Offsets that are not numbers at or above 0 fail the node."""
    breaks = []
    for offset in offsets or []:
        if isinstance(offset, bool) or not isinstance(offset, (int, float)) or offset < 0:
            patchbae.fail(f"ad_break_offsets holds seconds from the start of the program, at or above 0. Got {offset!r}.")
        breaks.append({"MessageType": "SPLICE_INSERT", "OffsetMillis": round(offset * 1000)})
    return sorted(breaks, key=lambda item: item["OffsetMillis"])


def epoch_millis(value):
    try:
        moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        patchbae.fail(f"start_time must be a UTC time such as 2026-11-25T19:30:00Z, got {value!r}.")
    return round(moment.timestamp() * 1000)


def find(operation, **request):
    """What the read operation returns, or None when the resource does not exist."""
    try:
        return operation(**request)
    except ClientError as error:
        if status_code(error) == 404 or "NotFound" in error_code(error):
            return None
        raise


def fail_if_refused(error, what):
    """Fails the node with MediaTailor's reason when it rejected the request itself (a 400 or a 404)."""
    if status_code(error) in (400, 404):
        reason = error.response.get("Error", {}).get("Message") or "no reason given"
        patchbae.fail(f"MediaTailor {what}: {reason}")


def result(**values):
    """Every output, with empty values for the ones this action does not produce."""
    return {**{name: "" for name in OUTPUTS}, **values}


def text(inputs, name):
    return str(inputs.get(name) or "").strip()


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")


def status_code(error):
    return error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
