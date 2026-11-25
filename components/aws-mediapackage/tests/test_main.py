import datetime

import pytest

import patchbae
from patchbae.testing import FakeAws, load_component

main = load_component(__file__)

TASK = patchbae.task.id
JOB_NAME = f"patchbae-{TASK}"
UTC = datetime.timezone.utc
START = datetime.datetime(2026, 11, 25, 19, 0, tzinfo=UTC)
END = datetime.datetime(2026, 11, 25, 21, 0, tzinfo=UTC)
WHERE = {"ChannelGroupName": "northlight-live", "ChannelName": "sports-1", "OriginEndpointName": "hls-cmaf"}
HARVEST = {
    "action": "harvest_clip",
    "channel_group": "northlight-live",
    "channel": "sports-1",
    "origin_endpoint": "hls-cmaf",
    "hls_manifests": ["index"],
    "start_time": "2026-11-25T19:00:00Z",
    "end_time": "2026-11-25T21:00:00Z",
    "output_uri": "s3://northlight-vod/harvest/cup-final/",
}
ROLE = "arn:aws:iam::123456789012:role/MediaPackageVodRole"
PUBLISH = {
    "action": "publish_vod_asset",
    "source_uri": "s3://northlight-vod/harvest/cup-final/index.m3u8",
    "packaging_group_id": "northlight-vod",
    "source_role_arn": ROLE,
}
EMPTY = {
    "job_name": "",
    "job_arn": "",
    "status": "",
    "output_uri": "",
    "asset_id": "",
    "asset_arn": "",
    "playback_urls": [],
    "playback_url": "",
}
JOB_ARN = f"arn:aws:mediapackagev2:eu-north-1:123456789012:channelGroup/northlight-live/channel/sports-1/originEndpoint/hls-cmaf/harvestJob/{JOB_NAME}"
ASSET_ARN = f"arn:aws:mediapackage-vod:eu-north-1:123456789012:assets/{JOB_NAME}"
HLS_URL = "https://a1b2c3.egress.mediapackage-vod.eu-north-1.amazonaws.com/out/v1/0f1e2d/index.m3u8"
DASH_URL = "https://a1b2c3.egress.mediapackage-vod.eu-north-1.amazonaws.com/out/v1/9a8b7c/index.mpd"


def harvest_job(status, **extra):
    return {
        **WHERE,
        "HarvestJobName": JOB_NAME,
        "Arn": JOB_ARN,
        "Status": status,
        "HarvestedManifests": {"HlsManifests": [{"ManifestName": "index"}]},
        "ScheduleConfiguration": {"StartTime": START, "EndTime": END},
        "Destination": {"S3Destination": {"BucketName": "northlight-vod", "DestinationPath": "harvest/cup-final"}},
        "CreatedAt": END,
        "ModifiedAt": END,
        **extra,
    }


def asset(*statuses):
    endpoints = [
        {"PackagingConfigurationId": config, "Url": url, "Status": status}
        for (config, url), status in zip([("hls", HLS_URL), ("dash", DASH_URL)], statuses)
    ]
    return {"Id": JOB_NAME, "Arn": ASSET_ARN, "PackagingGroupId": "northlight-vod", "SourceRoleArn": ROLE, "EgressEndpoints": endpoints}


@pytest.fixture
def aws(monkeypatch):
    fake = FakeAws()
    live = fake.client("mediapackagev2")
    live.respond("get_harvest_job", FakeAws.error("ResourceNotFoundException", status=404), harvest_job("IN_PROGRESS"), harvest_job("COMPLETED"))
    live.respond("create_harvest_job", harvest_job("QUEUED"))
    vod = fake.client("mediapackage-vod")
    vod.respond("describe_asset", FakeAws.error("NotFoundException", status=404), asset("PROCESSING", "QUEUED"), asset("PLAYABLE", "PLAYABLE"))
    vod.respond("create_asset", asset("QUEUED", "QUEUED"))
    monkeypatch.setattr(main.boto3, "client", fake.client)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    patchbae.reset()
    return fake


def test_harvests_a_time_range_to_s3_and_waits_for_the_job(aws):
    live = aws.client("mediapackagev2")

    output = main.run(HARVEST)

    assert live.called("create_harvest_job") == [
        {
            **WHERE,
            "HarvestJobName": JOB_NAME,
            "ClientToken": JOB_NAME,
            "Description": f"Patchbae run {patchbae.task.run_id}",
            "HarvestedManifests": {"HlsManifests": [{"ManifestName": "index"}]},
            "ScheduleConfiguration": {"StartTime": START, "EndTime": END},
            "Destination": {"S3Destination": {"BucketName": "northlight-vod", "DestinationPath": "harvest/cup-final"}},
        }
    ]
    assert live.called("get_harvest_job") == [{**WHERE, "HarvestJobName": JOB_NAME}] * 3
    assert output == {
        **EMPTY,
        "job_name": JOB_NAME,
        "job_arn": JOB_ARN,
        "status": "COMPLETED",
        "output_uri": "s3://northlight-vod/harvest/cup-final/",
    }
    assert patchbae.emitted_assets == [
        {
            "label": "Harvest",
            "locator": "s3://northlight-vod/harvest/cup-final/",
            "metadata": {"start": "2026-11-25T19:00:00+00:00", "end": "2026-11-25T21:00:00+00:00"},
        }
    ]


def test_every_kind_of_manifest_can_be_harvested_and_times_are_sent_as_utc(aws):
    main.run(
        {
            **HARVEST,
            "hls_manifests": [],
            "dash_manifests": ["dash"],
            "low_latency_hls_manifests": ["ll-1", "ll-2"],
            "start_time": "2026-11-25T20:00:00+01:00",
        }
    )

    (request,) = aws.client("mediapackagev2").called("create_harvest_job")
    assert request["HarvestedManifests"] == {
        "DashManifests": [{"ManifestName": "dash"}],
        "LowLatencyHlsManifests": [{"ManifestName": "ll-1"}, {"ManifestName": "ll-2"}],
    }
    assert request["ScheduleConfiguration"]["StartTime"] == START


def test_a_redelivered_task_waits_for_its_harvest_job_instead_of_starting_another(aws):
    live = aws.client("mediapackagev2")
    live.respond("get_harvest_job", harvest_job("IN_PROGRESS"), harvest_job("COMPLETED"))

    output = main.run(HARVEST)

    assert live.called("create_harvest_job") == []
    assert output["status"] == "COMPLETED"


def test_two_deliveries_at_once_end_up_waiting_for_the_same_job(aws):
    live = aws.client("mediapackagev2")
    live.respond("create_harvest_job", FakeAws.error("ConflictException", status=409))
    live.respond("get_harvest_job", FakeAws.error("ResourceNotFoundException", status=404), harvest_job("QUEUED"), harvest_job("COMPLETED"))

    assert main.run(HARVEST)["status"] == "COMPLETED"


@pytest.mark.parametrize(
    "finished, message",
    [
        (harvest_job("FAILED", ErrorMessage="Access denied writing to northlight-vod"), "could not harvest the clip: Access denied writing"),
        (harvest_job("CANCELLED"), "was cancelled before it finished"),
    ],
)
def test_a_harvest_that_does_not_finish_fails_the_node(aws, finished, message):
    aws.client("mediapackagev2").respond("get_harvest_job", finished)

    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(HARVEST)


def test_a_harvest_mediapackage_refuses_fails_the_node_with_its_reason(aws):
    aws.client("mediapackagev2").respond("create_harvest_job", FakeAws.error("ValidationException", "Start time is outside the startover window"))

    with pytest.raises(patchbae.ComponentFailed, match="would not start the harvest: Start time is outside the startover window"):
        main.run(HARVEST)


def test_publishes_a_vod_asset_and_returns_its_playback_urls(aws):
    vod = aws.client("mediapackage-vod")

    output = main.run(PUBLISH)

    assert vod.called("create_asset") == [
        {
            "Id": JOB_NAME,
            "PackagingGroupId": "northlight-vod",
            "SourceArn": "arn:aws:s3:::northlight-vod/harvest/cup-final/index.m3u8",
            "SourceRoleArn": ROLE,
            "Tags": {"patchbae_task_id": TASK},
        }
    ]
    assert output == {
        **EMPTY,
        "status": "PLAYABLE",
        "asset_id": JOB_NAME,
        "asset_arn": ASSET_ARN,
        "playback_urls": [
            {"packaging_configuration_id": "hls", "url": HLS_URL, "status": "PLAYABLE"},
            {"packaging_configuration_id": "dash", "url": DASH_URL, "status": "PLAYABLE"},
        ],
        "playback_url": HLS_URL,
    }
    assert [(item["label"], item["locator"]) for item in patchbae.emitted_assets] == [("Playback hls", HLS_URL), ("Playback dash", DASH_URL)]


def test_an_asset_with_the_same_id_is_reused_instead_of_created_again(aws):
    vod = aws.client("mediapackage-vod")
    vod.respond("describe_asset", {**asset("PLAYABLE", "PLAYABLE"), "Id": "cup-final-2026"})

    output = main.run({**PUBLISH, "asset_id": "cup-final-2026"})

    assert vod.called("describe_asset") == [{"Id": "cup-final-2026"}]
    assert vod.called("create_asset") == []
    assert output["asset_id"] == "cup-final-2026"


def test_an_asset_mediapackage_cannot_process_fails_the_node(aws):
    aws.client("mediapackage-vod").respond("describe_asset", FakeAws.error("NotFoundException", status=404), asset("PLAYABLE", "FAILED"))

    with pytest.raises(patchbae.ComponentFailed, match="could not process asset"):
        main.run(PUBLISH)


def test_an_asset_mediapackage_refuses_fails_the_node_with_its_reason(aws):
    aws.client("mediapackage-vod").respond("create_asset", FakeAws.error("UnprocessableEntityException", "Packaging group not found", 422))

    with pytest.raises(patchbae.ComponentFailed, match="would not create the asset: Packaging group not found"):
        main.run(PUBLISH)


def test_a_packaging_group_without_configurations_returns_no_urls_and_says_so(aws):
    aws.client("mediapackage-vod").respond("create_asset", asset())

    output = main.run(PUBLISH)

    assert (output["playback_urls"], output["playback_url"]) == ([], "")
    assert [entry["level"] for entry in patchbae.logged if "no packaging configurations" in entry["message"]] == ["warn"]


def test_a_missing_permission_is_not_swallowed(aws):
    aws.client("mediapackagev2").respond("get_harvest_job", FakeAws.error("AccessDeniedException", status=403))

    with pytest.raises(main.ClientError):
        main.run(HARVEST)


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({"action": "package"}, "Unknown action 'package'"),
        ({**HARVEST, "channel": "sports 1"}, "channel must be the resource's name"),
        ({**HARVEST, "hls_manifests": []}, "Name at least one manifest"),
        ({**HARVEST, "hls_manifests": ["index.m3u8"]}, "holds manifest names"),
        ({**HARVEST, "start_time": "19:00"}, "start_time must be a UTC time"),
        ({**HARVEST, "end_time": "2026-11-25T18:00:00Z"}, "end_time must be after start_time"),
        ({**HARVEST, "output_uri": "s3://northlight-vod"}, "must be a folder inside the bucket"),
        ({**HARVEST, "output_uri": "s3://northlight-vod/harvest"}, "must be a folder inside the bucket"),
        ({**PUBLISH, "source_uri": "s3://northlight-vod/harvest/cup-final/"}, "must point to an HLS playlist"),
        ({**PUBLISH, "packaging_group_id": ""}, "packaging_group_id must hold"),
        ({**PUBLISH, "source_role_arn": "MediaPackageVodRole"}, "source_role_arn must be the ARN"),
        ({**PUBLISH, "asset_id": "cup final"}, "asset_id may hold"),
    ],
)
def test_bad_inputs_fail_before_anything_is_started(aws, inputs, message):
    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    assert aws.client("mediapackagev2").calls == [] and aws.client("mediapackage-vod").calls == []
