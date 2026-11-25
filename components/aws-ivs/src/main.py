"""Amazon IVS: create a channel, read or stop its live stream, send timed metadata.

The pod only makes API calls. A channel's stream key is a secret: this
component returns the key's ARN and never its value.
"""

import re

import boto3
from botocore.exceptions import ClientError

import patchbae

ACTIONS = ("create_channel", "get_stream", "stop_stream", "put_metadata")
CHANNEL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
CHANNEL_ARN = re.compile(r"^arn:aws:ivs:[a-z0-9-]+:\d+:channel/[a-zA-Z0-9-]+$")
RECORDING_ARN = re.compile(r"^arn:aws:ivs:[a-z0-9-]+:\d+:recording-configuration/[a-zA-Z0-9-]+$")
LATENCY_MODES = ("LOW", "NORMAL")
CHANNEL_TYPES = ("STANDARD", "BASIC", "ADVANCED_SD", "ADVANCED_HD")
MAX_METADATA_BYTES = 1024
TASK_TAG = "patchbae_task_id"
SEARCH_PAGES = 10
OUTPUTS = ("channel_arn", "channel_name", "ingest_server", "playback_url", "stream_key_arn", "state", "health", "stream_id")


def run(inputs):
    action = text(inputs, "action")
    if action not in ACTIONS:
        patchbae.fail(f"Unknown action {action!r}. Use create_channel, get_stream, stop_stream or put_metadata.")
    if action == "create_channel":
        return create_channel(inputs)

    channel_arn = text(inputs, "channel_arn")
    if not CHANNEL_ARN.match(channel_arn):
        patchbae.fail(f"action is {action}, so channel_arn must be the ARN of an IVS channel, such as arn:aws:ivs:eu-west-1:123456789012:channel/abcdABCDefgh.")
    if action == "put_metadata":
        return put_metadata(channel_arn, inputs)
    ivs = boto3.client("ivs")
    if action == "stop_stream":
        return stop_stream(ivs, channel_arn)
    return get_stream(ivs, channel_arn)


def create_channel(inputs):
    name = text(inputs, "channel_name")
    if not CHANNEL_NAME.match(name):
        patchbae.fail("action is create_channel, so channel_name must be 1 to 128 letters, digits, dashes and underscores.")
    latency = text(inputs, "latency_mode") or "LOW"
    if latency not in LATENCY_MODES:
        patchbae.fail(f"Unknown latency_mode {latency!r}. Use LOW or NORMAL.")
    channel_type = text(inputs, "channel_type") or "STANDARD"
    if channel_type not in CHANNEL_TYPES:
        patchbae.fail(f"Unknown channel_type {channel_type!r}. Use STANDARD, BASIC, ADVANCED_SD or ADVANCED_HD.")
    recording = text(inputs, "recording_configuration_arn")
    if recording and not RECORDING_ARN.match(recording):
        patchbae.fail("recording_configuration_arn must be the ARN of an IVS recording configuration, or empty for no recording.")

    ivs = boto3.client("ivs")
    existing = find_channel(ivs, name)
    if existing:
        channel = ivs.get_channel(arn=existing["arn"])["channel"]
        keys = ivs.list_stream_keys(channelArn=existing["arn"], maxResults=1).get("streamKeys", [])
        key_arn = keys[0]["arn"] if keys else ""
        patchbae.log("found the channel with this name", channel=name)
    else:
        request = {"name": name, "latencyMode": latency, "type": channel_type, "tags": {TASK_TAG: patchbae.task.id}}
        if recording:
            request["recordingConfigurationArn"] = recording
        try:
            created = ivs.create_channel(**request)
        except ClientError as error:
            if error_code(error) in ("ValidationException", "ServiceQuotaExceededException", "ResourceNotFoundException"):
                patchbae.fail(f"IVS would not create the channel: {error_message(error)}")
            raise
        channel = created["channel"]
        # The response also carries the stream key's value. It is a secret and stays here.
        key_arn = created.get("streamKey", {}).get("arn", "")
        patchbae.log("channel created", channel=name, latency=latency, type=channel_type)

    endpoint = channel.get("ingestEndpoint", "")
    return result(
        channel_arn=channel.get("arn", ""),
        channel_name=channel.get("name", ""),
        ingest_server=f"rtmps://{endpoint}:443/app/" if endpoint else "",
        playback_url=channel.get("playbackUrl", ""),
        stream_key_arn=key_arn,
    )


def find_channel(ivs, name):
    """The channel with exactly this name, or None. The API's filter matches by name."""
    request = {"filterByName": name, "maxResults": 100}
    for _page in range(SEARCH_PAGES):
        page = ivs.list_channels(**request)
        for channel in page.get("channels", []):
            if channel.get("name") == name:
                return channel
        if not page.get("nextToken"):
            break
        request["nextToken"] = page["nextToken"]
    return None


def get_stream(ivs, channel_arn):
    try:
        stream = ivs.get_stream(channelArn=channel_arn).get("stream", {})
    except ClientError as error:
        if error_code(error) == "ChannelNotBroadcasting":
            patchbae.log("channel is offline", channel=channel_arn)
            return result(channel_arn=channel_arn, state="OFFLINE")
        fail_if_missing(error)
        raise
    patchbae.log("stream read", channel=channel_arn, state=stream.get("state", ""), health=stream.get("health", ""))
    return result(
        channel_arn=channel_arn,
        playback_url=stream.get("playbackUrl", ""),
        state=stream.get("state", ""),
        health=stream.get("health", ""),
        viewer_count=stream.get("viewerCount", 0),
        stream_id=stream.get("streamId", ""),
    )


def stop_stream(ivs, channel_arn):
    try:
        ivs.stop_stream(channelArn=channel_arn)
        patchbae.log("stream stopped", channel=channel_arn)
    except ClientError as error:
        # Nothing to stop: the outcome the workflow asked for is already there.
        if error_code(error) != "ChannelNotBroadcasting":
            fail_if_missing(error)
            raise
        patchbae.log("channel was already offline", channel=channel_arn)
    return result(channel_arn=channel_arn, state="OFFLINE")


def put_metadata(channel_arn, inputs):
    metadata = str(inputs.get("metadata") or "")
    if not metadata.strip():
        patchbae.fail("action is put_metadata, so metadata must hold the text to send.")
    size = len(metadata.encode("utf-8"))
    if size > MAX_METADATA_BYTES:
        patchbae.fail(f"metadata is {size} bytes. IVS accepts at most {MAX_METADATA_BYTES} bytes per request.")
    try:
        boto3.client("ivs").put_metadata(channelArn=channel_arn, metadata=metadata)
    except ClientError as error:
        if error_code(error) == "ChannelNotBroadcasting":
            patchbae.fail("The channel is not live, so there is no stream to add metadata to.")
        fail_if_missing(error)
        raise
    patchbae.log("metadata sent", channel=channel_arn, bytes=size)
    return result(channel_arn=channel_arn, state="LIVE")


def fail_if_missing(error):
    if error_code(error) == "ResourceNotFoundException":
        patchbae.fail("The channel was not found. Check channel_arn and the region the component is set up for.")


def result(**values):
    """Every output, with empty values for the ones this action does not produce."""
    return {**{name: "" for name in OUTPUTS}, "viewer_count": 0, **values}


def text(inputs, name):
    return str(inputs.get(name) or "").strip()


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")


def error_message(error):
    return error.response.get("Error", {}).get("Message") or "no reason given"
