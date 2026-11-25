import datetime

import pytest

import patchbae
from patchbae.testing import FakeAws, load_component

main = load_component(__file__)

TASK = patchbae.task.id
ORIGIN = "https://a1b2c3.egress.mediapackage-vod.eu-north-1.amazonaws.com/out/v1/0f1e2d"
ADS = "https://ads.example.com/vast?duration=[session.avail_duration_secs]"
HLS_PREFIX = "https://f1e2d3c4.mediatailor.eu-north-1.amazonaws.com/v1/master/0a1b2c3d/cup-final-catchup/"
DASH_PREFIX = "https://f1e2d3c4.mediatailor.eu-north-1.amazonaws.com/v1/dash/0a1b2c3d/cup-final-catchup/"
SESSION_PREFIX = "https://f1e2d3c4.mediatailor.eu-north-1.amazonaws.com/v1/session/0a1b2c3d/cup-final-catchup/"
CONFIGURATION_ARN = "arn:aws:mediatailor:eu-north-1:123456789012:playbackConfiguration/cup-final-catchup"
CONFIGURATION = {
    "Name": "cup-final-catchup",
    "PlaybackConfigurationArn": CONFIGURATION_ARN,
    "VideoContentSourceUrl": ORIGIN,
    "AdDecisionServerUrl": ADS,
    "HlsConfiguration": {"ManifestEndpointPrefix": HLS_PREFIX},
    "DashConfiguration": {"ManifestEndpointPrefix": DASH_PREFIX, "OriginManifestType": "MULTI_PERIOD"},
    "SessionInitializationEndpointPrefix": SESSION_PREFIX,
}
CONFIGURE = {
    "action": "configure_ad_insertion",
    "configuration_name": "cup-final-catchup",
    "content_source_url": ORIGIN,
    "ad_decision_server_url": ADS,
}
SCHEDULE = {
    "action": "schedule_program",
    "channel_name": "northlight-one",
    "source_location_name": "northlight-playout",
    "vod_source_name": "promo-winter-2026",
}
PROGRAM_NAME = f"patchbae-{TASK}"
PROGRAM_ARN = f"arn:aws:mediatailor:eu-north-1:123456789012:program/northlight-one/{PROGRAM_NAME}"
PROGRAM = {
    "Arn": PROGRAM_ARN,
    "ChannelName": "northlight-one",
    "ProgramName": PROGRAM_NAME,
    "SourceLocationName": "northlight-playout",
    "VodSourceName": "promo-winter-2026",
    "ScheduledStartTime": datetime.datetime(2026, 11, 25, 19, 30, tzinfo=datetime.timezone.utc),
    "DurationMillis": 30_000,
}
EMPTY = {
    "playback_configuration_arn": "",
    "hls_playback_prefix": "",
    "dash_playback_prefix": "",
    "session_initialization_prefix": "",
    "playback_url": "",
    "program_name": "",
    "program_arn": "",
    "scheduled_start_time": "",
}
MISSING = FakeAws.error("NotFoundException", status=404)


@pytest.fixture
def mediatailor(monkeypatch):
    aws = FakeAws()
    client = aws.client("mediatailor")
    client.respond("get_playback_configuration", MISSING)
    client.respond("put_playback_configuration", CONFIGURATION)
    client.respond("describe_program", MISSING)
    client.respond("create_program", PROGRAM)
    monkeypatch.setattr(main.boto3, "client", aws.client)
    patchbae.reset()
    return client


def test_sets_up_ad_insertion_and_returns_the_playback_urls(mediatailor):
    output = main.run({**CONFIGURE, "manifest_path": "/index.m3u8"})

    assert mediatailor.called("put_playback_configuration") == [
        {"Name": "cup-final-catchup", "VideoContentSourceUrl": ORIGIN, "AdDecisionServerUrl": ADS}
    ]
    assert output == {
        **EMPTY,
        "playback_configuration_arn": CONFIGURATION_ARN,
        "hls_playback_prefix": HLS_PREFIX,
        "dash_playback_prefix": DASH_PREFIX,
        "session_initialization_prefix": SESSION_PREFIX,
        "playback_url": f"{HLS_PREFIX}index.m3u8",
    }
    assert patchbae.emitted_assets == [
        {"label": "Playback with ads", "locator": f"{HLS_PREFIX}index.m3u8", "mediaType": "application/vnd.apple.mpegurl"}
    ]


def test_without_a_manifest_path_only_the_prefixes_are_returned(mediatailor):
    output = main.run({**CONFIGURE, "slate_ad_url": "https://cdn.example.com/slate.mp4"})

    assert mediatailor.called("put_playback_configuration")[0]["SlateAdUrl"] == "https://cdn.example.com/slate.mp4"
    assert output["playback_url"] == ""
    assert patchbae.emitted_assets == []


def test_a_configuration_that_is_already_as_asked_is_not_saved_again(mediatailor):
    mediatailor.respond("get_playback_configuration", CONFIGURATION)

    output = main.run(CONFIGURE)

    assert mediatailor.called("get_playback_configuration") == [{"Name": "cup-final-catchup"}]
    assert mediatailor.called("put_playback_configuration") == []
    assert output["hls_playback_prefix"] == HLS_PREFIX


@pytest.mark.parametrize(
    "current",
    [
        {**CONFIGURATION, "AdDecisionServerUrl": "https://old-ads.example.com/vast"},
        {**CONFIGURATION, "SlateAdUrl": "https://cdn.example.com/old-slate.mp4"},
    ],
)
def test_a_configuration_that_differs_is_replaced(mediatailor, current):
    mediatailor.respond("get_playback_configuration", current)

    main.run(CONFIGURE)

    assert len(mediatailor.called("put_playback_configuration")) == 1


def test_a_configuration_mediatailor_refuses_fails_the_node_with_its_reason(mediatailor):
    mediatailor.respond("put_playback_configuration", FakeAws.error("BadRequestException", "VideoContentSourceUrl is not reachable"))

    with pytest.raises(patchbae.ComponentFailed, match="would not save the playback configuration: VideoContentSourceUrl is not reachable"):
        main.run(CONFIGURE)


def test_schedules_a_program_at_the_end_of_the_schedule(mediatailor):
    output = main.run(SCHEDULE)

    assert mediatailor.called("describe_program") == [{"ChannelName": "northlight-one", "ProgramName": PROGRAM_NAME}]
    assert mediatailor.called("create_program") == [
        {
            "ChannelName": "northlight-one",
            "ProgramName": PROGRAM_NAME,
            "SourceLocationName": "northlight-playout",
            "VodSourceName": "promo-winter-2026",
            "ScheduleConfiguration": {"Transition": {"Type": "RELATIVE", "RelativePosition": "AFTER_PROGRAM"}},
        }
    ]
    assert output == {**EMPTY, "program_name": PROGRAM_NAME, "program_arn": PROGRAM_ARN, "scheduled_start_time": "2026-11-25T19:30:00Z"}


def test_a_program_next_to_another_with_ad_breaks(mediatailor):
    main.run(
        {
            **SCHEDULE,
            "program_name": "promo-winter-evening",
            "relative_position": "BEFORE_PROGRAM",
            "relative_program": "evening-news",
            "ad_break_offsets": [600, 0, 12.5],
        }
    )

    (request,) = mediatailor.called("create_program")
    assert request["ProgramName"] == "promo-winter-evening"
    assert request["ScheduleConfiguration"]["Transition"] == {
        "Type": "RELATIVE",
        "RelativePosition": "BEFORE_PROGRAM",
        "RelativeProgram": "evening-news",
    }
    assert request["AdBreaks"] == [
        {"MessageType": "SPLICE_INSERT", "OffsetMillis": 0},
        {"MessageType": "SPLICE_INSERT", "OffsetMillis": 12_500},
        {"MessageType": "SPLICE_INSERT", "OffsetMillis": 600_000},
    ]


def test_a_program_at_a_set_time_on_a_linear_channel(mediatailor):
    main.run({**SCHEDULE, "start_time": "2026-11-25T20:30:00+01:00"})

    transition = mediatailor.called("create_program")[0]["ScheduleConfiguration"]["Transition"]
    assert transition == {"Type": "ABSOLUTE", "RelativePosition": "AFTER_PROGRAM", "ScheduledStartTimeMillis": 1_795_635_000_000}


def test_a_redelivered_task_finds_its_program_instead_of_scheduling_another(mediatailor):
    mediatailor.respond("describe_program", PROGRAM)

    output = main.run(SCHEDULE)

    assert mediatailor.called("create_program") == []
    assert output["program_arn"] == PROGRAM_ARN


def test_a_program_mediatailor_refuses_fails_the_node_with_its_reason(mediatailor):
    mediatailor.respond("create_program", FakeAws.error("BadRequestException", "VodSource promo-winter-2026 not found"))

    with pytest.raises(patchbae.ComponentFailed, match="would not schedule the program: VodSource promo-winter-2026 not found"):
        main.run(SCHEDULE)


@pytest.mark.parametrize("operation", ["get_playback_configuration", "describe_program"])
def test_a_missing_permission_is_not_swallowed(mediatailor, operation):
    mediatailor.respond(operation, FakeAws.error("AccessDeniedException", status=403))

    with pytest.raises(main.ClientError):
        main.run(CONFIGURE if operation == "get_playback_configuration" else SCHEDULE)


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({"action": "insert_ads"}, "Unknown action 'insert_ads'"),
        ({**CONFIGURE, "configuration_name": "cup final"}, "configuration_name must be a name"),
        ({**CONFIGURE, "content_source_url": "s3://northlight-vod/hls/"}, "content_source_url must be the http"),
        ({**CONFIGURE, "ad_decision_server_url": ""}, "ad_decision_server_url must be the http"),
        ({**CONFIGURE, "slate_ad_url": "slate.mp4"}, "slate_ad_url must be the http"),
        ({**SCHEDULE, "vod_source_name": ""}, "vod_source_name must hold"),
        ({**SCHEDULE, "program_name": "promo winter"}, "program_name must be a name"),
        ({**SCHEDULE, "relative_position": "DURING_PROGRAM"}, "Unknown relative_position"),
        ({**SCHEDULE, "start_time": "2026-11-25T19:30:00Z", "relative_program": "evening-news"}, "not both"),
        ({**SCHEDULE, "start_time": "half past seven"}, "start_time must be a UTC time"),
        ({**SCHEDULE, "ad_break_offsets": [-5]}, "at or above 0"),
        ({**SCHEDULE, "ad_break_offsets": ["start"]}, "at or above 0"),
    ],
)
def test_bad_inputs_fail_before_anything_is_changed(mediatailor, inputs, message):
    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    assert mediatailor.calls == []
