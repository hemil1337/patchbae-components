import datetime

import pytest

import patchbae
from patchbae.testing import FakeAws, load_component

main = load_component(__file__)

TASK = patchbae.task.id
CHANNEL_ARN = "arn:aws:ivs:eu-west-1:123456789012:channel/abcdABCDefgh"
KEY_ARN = "arn:aws:ivs:eu-west-1:123456789012:stream-key/g1H2I3j4k5L6"
PLAYBACK = "https://a1b2c3d4e5f6.eu-west-1.playback.live-video.net/api/video/v1/eu-west-1.123456789012.channel.abcdABCDefgh.m3u8"
CHANNEL = {
    "arn": CHANNEL_ARN,
    "name": "friday-night-live",
    "latencyMode": "LOW",
    "type": "STANDARD",
    "authorized": False,
    "insecureIngest": False,
    "ingestEndpoint": "a1b2c3d4e5f6.global-contribute.live-video.net",
    "playbackUrl": PLAYBACK,
    "recordingConfigurationArn": "",
    "tags": {"patchbae_task_id": TASK},
}
STREAM = {
    "channelArn": CHANNEL_ARN,
    "streamId": "st-1Abc2Def3Ghi4Jkl5Mno6Pq",
    "playbackUrl": PLAYBACK,
    "startTime": datetime.datetime(2026, 11, 25, 19, 0, tzinfo=datetime.timezone.utc),
    "state": "LIVE",
    "health": "HEALTHY",
    "viewerCount": 1842,
}
EMPTY = {
    "channel_arn": "",
    "channel_name": "",
    "ingest_server": "",
    "playback_url": "",
    "stream_key_arn": "",
    "state": "",
    "health": "",
    "viewer_count": 0,
    "stream_id": "",
}


@pytest.fixture
def ivs(monkeypatch):
    aws = FakeAws()
    client = aws.client("ivs")
    client.respond("list_channels", {"channels": []})
    client.respond("create_channel", {"channel": CHANNEL, "streamKey": {"arn": KEY_ARN, "value": "sk_eu-west-1_SECRET", "channelArn": CHANNEL_ARN}})
    client.respond("get_stream", {"stream": STREAM})
    client.respond("stop_stream", {})
    client.respond("put_metadata", {})
    monkeypatch.setattr(main.boto3, "client", aws.client)
    patchbae.reset()
    return client


def test_creates_a_channel_and_returns_where_to_send_and_watch(ivs):
    output = main.run({"action": "create_channel", "channel_name": "friday-night-live"})

    assert ivs.called("list_channels") == [{"filterByName": "friday-night-live", "maxResults": 100}]
    assert ivs.called("create_channel") == [
        {"name": "friday-night-live", "latencyMode": "LOW", "type": "STANDARD", "tags": {"patchbae_task_id": TASK}}
    ]
    assert output == {
        **EMPTY,
        "channel_arn": CHANNEL_ARN,
        "channel_name": "friday-night-live",
        "ingest_server": "rtmps://a1b2c3d4e5f6.global-contribute.live-video.net:443/app/",
        "playback_url": PLAYBACK,
        "stream_key_arn": KEY_ARN,
    }


def test_the_stream_key_s_value_never_leaves_the_component(ivs):
    output = main.run({"action": "create_channel", "channel_name": "friday-night-live"})

    assert "sk_eu-west-1_SECRET" not in repr(output)
    assert "sk_eu-west-1_SECRET" not in repr(patchbae.logged)


def test_a_channel_with_recording_and_other_settings(ivs):
    recording = "arn:aws:ivs:eu-west-1:123456789012:recording-configuration/r1E2c3O4r5D6"

    main.run(
        {
            "action": "create_channel",
            "channel_name": "town_hall",
            "latency_mode": "NORMAL",
            "channel_type": "ADVANCED_HD",
            "recording_configuration_arn": recording,
        }
    )

    (request,) = ivs.called("create_channel")
    assert (request["latencyMode"], request["type"], request["recordingConfigurationArn"]) == ("NORMAL", "ADVANCED_HD", recording)


def test_a_channel_with_the_same_name_is_reused_instead_of_created_again(ivs):
    summary = {"arn": CHANNEL_ARN, "name": "friday-night-live", "latencyMode": "LOW", "type": "STANDARD"}
    lookalike = {**summary, "arn": "arn:aws:ivs:eu-west-1:123456789012:channel/zzzzZZZZzzzz", "name": "friday-night-live-2"}
    ivs.respond("list_channels", {"channels": [lookalike], "nextToken": "cGFnZTI="}, {"channels": [summary]})
    ivs.respond("get_channel", {"channel": CHANNEL})
    ivs.respond("list_stream_keys", {"streamKeys": [{"arn": KEY_ARN, "channelArn": CHANNEL_ARN}]})

    output = main.run({"action": "create_channel", "channel_name": "friday-night-live"})

    assert ivs.called("create_channel") == []
    assert ivs.called("list_channels")[1] == {"filterByName": "friday-night-live", "maxResults": 100, "nextToken": "cGFnZTI="}
    assert (output["channel_arn"], output["stream_key_arn"]) == (CHANNEL_ARN, KEY_ARN)
    assert output["ingest_server"] == "rtmps://a1b2c3d4e5f6.global-contribute.live-video.net:443/app/"


def test_a_channel_ivs_will_not_create_fails_the_node_with_its_reason(ivs):
    ivs.respond("create_channel", FakeAws.error("ServiceQuotaExceededException", "Channel limit reached"))

    with pytest.raises(patchbae.ComponentFailed, match="IVS would not create the channel: Channel limit reached"):
        main.run({"action": "create_channel", "channel_name": "friday-night-live"})


def test_reads_a_live_stream(ivs):
    output = main.run({"action": "get_stream", "channel_arn": CHANNEL_ARN})

    assert ivs.called("get_stream") == [{"channelArn": CHANNEL_ARN}]
    assert output == {
        **EMPTY,
        "channel_arn": CHANNEL_ARN,
        "playback_url": PLAYBACK,
        "state": "LIVE",
        "health": "HEALTHY",
        "viewer_count": 1842,
        "stream_id": "st-1Abc2Def3Ghi4Jkl5Mno6Pq",
    }


def test_a_channel_that_is_not_broadcasting_reads_as_offline(ivs):
    ivs.respond("get_stream", FakeAws.error("ChannelNotBroadcasting", status=404))

    output = main.run({"action": "get_stream", "channel_arn": CHANNEL_ARN})

    assert output == {**EMPTY, "channel_arn": CHANNEL_ARN, "state": "OFFLINE"}


def test_stops_a_live_stream(ivs):
    output = main.run({"action": "stop_stream", "channel_arn": CHANNEL_ARN})

    assert ivs.called("stop_stream") == [{"channelArn": CHANNEL_ARN}]
    assert output == {**EMPTY, "channel_arn": CHANNEL_ARN, "state": "OFFLINE"}


def test_stopping_an_offline_channel_succeeds(ivs):
    ivs.respond("stop_stream", FakeAws.error("ChannelNotBroadcasting", status=404))

    output = main.run({"action": "stop_stream", "channel_arn": CHANNEL_ARN})

    assert output["state"] == "OFFLINE"


def test_sends_timed_metadata(ivs):
    output = main.run({"action": "put_metadata", "channel_arn": CHANNEL_ARN, "metadata": '{"cue": "goal", "minute": 63}'})

    assert ivs.called("put_metadata") == [{"channelArn": CHANNEL_ARN, "metadata": '{"cue": "goal", "minute": 63}'}]
    assert output == {**EMPTY, "channel_arn": CHANNEL_ARN, "state": "LIVE"}


def test_metadata_for_an_offline_channel_fails_the_node(ivs):
    ivs.respond("put_metadata", FakeAws.error("ChannelNotBroadcasting", status=404))

    with pytest.raises(patchbae.ComponentFailed, match="The channel is not live"):
        main.run({"action": "put_metadata", "channel_arn": CHANNEL_ARN, "metadata": "cue"})


@pytest.mark.parametrize("action, extra", [("get_stream", {}), ("stop_stream", {}), ("put_metadata", {"metadata": "cue"})])
def test_a_channel_that_does_not_exist_fails_the_node(ivs, action, extra):
    ivs.respond(action, FakeAws.error("ResourceNotFoundException", status=404))

    with pytest.raises(patchbae.ComponentFailed, match="The channel was not found"):
        main.run({"action": action, "channel_arn": CHANNEL_ARN, **extra})


def test_a_missing_permission_is_not_swallowed(ivs):
    ivs.respond("get_stream", FakeAws.error("AccessDeniedException", status=403))

    with pytest.raises(main.ClientError):
        main.run({"action": "get_stream", "channel_arn": CHANNEL_ARN})


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({"action": "delete_channel"}, "Unknown action 'delete_channel'"),
        ({"action": "create_channel"}, "channel_name must be"),
        ({"action": "create_channel", "channel_name": "friday night live"}, "channel_name must be"),
        ({"action": "create_channel", "channel_name": "x", "latency_mode": "ULTRA"}, "Unknown latency_mode 'ULTRA'"),
        ({"action": "create_channel", "channel_name": "x", "channel_type": "PRO"}, "Unknown channel_type 'PRO'"),
        ({"action": "create_channel", "channel_name": "x", "recording_configuration_arn": "s3://bucket"}, "recording_configuration_arn must be"),
        ({"action": "get_stream", "channel_arn": "friday-night-live"}, "channel_arn must be the ARN"),
        ({"action": "put_metadata", "channel_arn": CHANNEL_ARN}, "metadata must hold the text"),
        ({"action": "put_metadata", "channel_arn": CHANNEL_ARN, "metadata": "x" * 1025}, "metadata is 1025 bytes"),
    ],
)
def test_bad_inputs_fail_before_anything_is_changed(ivs, inputs, message):
    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    assert ivs.calls == []
