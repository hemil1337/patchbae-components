from unittest import mock

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

import patchbae
from patchbae.testing import load_component

main = load_component(__file__)

TASK = patchbae.task.id
ROLE = "arn:aws:iam::123456789012:role/MediaConvertServiceRole"
INPUTS = {
    "media_uri": "s3://media/in/show.mxf",
    "output_uri": "s3://work/transcodes/",
    "mediaconvert_role_arn": ROLE,
    "preset": "mp4_h264_1080p",
}
TEMPLATE = {
    "Name": "acme-delivery",
    "Queue": "arn:aws:mediaconvert:eu-north-1:123456789012:queues/delivery",
    "Settings": {
        "Inputs": [{"AudioSelectors": {"Audio Selector 1": {"DefaultSelection": "DEFAULT"}}}],
        "OutputGroups": [
            {
                "Name": "Apple HLS",
                "OutputGroupSettings": {
                    "Type": "HLS_GROUP_SETTINGS",
                    "HlsGroupSettings": {"Destination": "s3://old/hls/", "SegmentLength": 4, "MinSegmentLength": 0},
                },
                "Outputs": [{"NameModifier": "_hd", "ContainerSettings": {"Container": "M3U8"}}],
            },
            {
                "Name": "File Group",
                "OutputGroupSettings": {"Type": "FILE_GROUP_SETTINGS", "FileGroupSettings": {"Destination": "s3://old/files/"}},
                "Outputs": [
                    {"NameModifier": "_master", "ContainerSettings": {"Container": "MXF"}},
                    {"NameModifier": "_audio", "Extension": "wav", "ContainerSettings": {"Container": "RAW"}},
                    {"NameModifier": "_frames", "ContainerSettings": {"Container": "RAW"}},
                ],
            },
        ],
    },
}


def client_error(code, message=None):
    return ClientError({"Error": {"Code": code, "Message": message or code}}, "op")


def job(status, job_id="1700000000000-abc123", **extra):
    return {"Job": {"Id": job_id, "Status": status, **extra}}


def earlier_job(status, job_id="1690000000000-old111", task=TASK):
    return {"Id": job_id, "Status": status, "UserMetadata": {"patchbae_task_id": task}}


@pytest.fixture
def mediaconvert(monkeypatch):
    client = mock.Mock()
    client.search_jobs.return_value = {"Jobs": []}
    client.create_job.return_value = job("SUBMITTED")
    client.get_job.side_effect = [job("PROGRESSING"), job("COMPLETE")]
    client.get_job_template.return_value = {"JobTemplate": TEMPLATE}
    monkeypatch.setattr(main.boto3, "client", lambda name: {"mediaconvert": client}[name])
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    patchbae.reset()
    return client


def test_transcodes_to_mp4_and_returns_references(mediaconvert):
    output = main.run(INPUTS)

    mediaconvert.search_jobs.assert_called_once_with(
        Queue="Default", InputFile="s3://media/in/show.mxf", Order="DESCENDING", MaxResults=20
    )
    assert mediaconvert.create_job.call_args.kwargs == {
        "Role": ROLE,
        "ClientRequestToken": TASK,
        "UserMetadata": {"patchbae_task_id": TASK},
        "Settings": {
            "TimecodeConfig": {"Source": "ZEROBASED"},
            "Inputs": [
                {
                    "FileInput": "s3://media/in/show.mxf",
                    "AudioSelectors": {"Audio Selector 1": {"DefaultSelection": "DEFAULT"}},
                    "VideoSelector": {},
                    "TimecodeSource": "ZEROBASED",
                }
            ],
            "OutputGroups": [
                {
                    "Name": "File Group",
                    "OutputGroupSettings": {
                        "Type": "FILE_GROUP_SETTINGS",
                        "FileGroupSettings": {"Destination": "s3://work/transcodes/show"},
                    },
                    "Outputs": [
                        {
                            "NameModifier": "_1080p",
                            "ContainerSettings": {"Container": "MP4", "Mp4Settings": {"MoovPlacement": "PROGRESSIVE_DOWNLOAD"}},
                            "VideoDescription": {
                                "Width": 1920,
                                "Height": 1080,
                                "CodecSettings": {
                                    "Codec": "H_264",
                                    "H264Settings": {
                                        "RateControlMode": "QVBR",
                                        "QvbrSettings": {"QvbrQualityLevel": 8},
                                        "MaxBitrate": 8_000_000,
                                        "SceneChangeDetect": "TRANSITION_DETECTION",
                                        "QualityTuningLevel": "SINGLE_PASS_HQ",
                                    },
                                },
                            },
                            "AudioDescriptions": [
                                {
                                    "AudioSourceName": "Audio Selector 1",
                                    "CodecSettings": {
                                        "Codec": "AAC",
                                        "AacSettings": {"Bitrate": 128_000, "CodingMode": "CODING_MODE_2_0", "SampleRate": 48000},
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    }
    assert [call.kwargs for call in mediaconvert.get_job.call_args_list] == [{"Id": "1700000000000-abc123"}] * 2
    assert output == {
        "job_id": "1700000000000-abc123",
        "output_uri": "s3://work/transcodes/",
        "output_uris": ["s3://work/transcodes/show_1080p.mp4"],
        "status": "COMPLETE",
    }
    assert patchbae.emitted_assets == [
        {"label": "Transcode", "locator": "s3://work/transcodes/show_1080p.mp4", "mediaType": "video/mp4"}
    ]


def test_720p_with_a_queue_and_a_name_modifier(mediaconvert):
    output = main.run({**INPUTS, "preset": "mp4_h264_720p", "queue": "proxies", "name_modifier": "_proxy"})

    request = mediaconvert.create_job.call_args.kwargs
    video = request["Settings"]["OutputGroups"][0]["Outputs"][0]
    assert request["Queue"] == "proxies"
    assert mediaconvert.search_jobs.call_args.kwargs["Queue"] == "proxies"
    assert video["NameModifier"] == "_proxy"
    assert (video["VideoDescription"]["Width"], video["VideoDescription"]["Height"]) == (1280, 720)
    assert output["output_uris"] == ["s3://work/transcodes/show_proxy.mp4"]


def test_hls_has_three_renditions_and_returns_the_master_playlist(mediaconvert):
    output = main.run({**INPUTS, "preset": "hls_abr", "name_modifier": "_v2"})

    (group,) = mediaconvert.create_job.call_args.kwargs["Settings"]["OutputGroups"]
    assert group["OutputGroupSettings"] == {
        "Type": "HLS_GROUP_SETTINGS",
        "HlsGroupSettings": {"Destination": "s3://work/transcodes/show_v2", "SegmentLength": 6, "MinSegmentLength": 0},
    }
    renditions = [
        (item["NameModifier"], item["VideoDescription"]["Width"], item["VideoDescription"]["Height"])
        for item in group["Outputs"]
    ]
    assert renditions == [("_1080p", 1920, 1080), ("_720p", 1280, 720), ("_360p", 640, 360)]
    assert all(item["ContainerSettings"]["Container"] == "M3U8" for item in group["Outputs"])
    assert output["output_uris"] == ["s3://work/transcodes/show_v2.m3u8"]
    assert patchbae.emitted_assets == [
        {"label": "HLS", "locator": "s3://work/transcodes/show_v2.m3u8", "mediaType": "application/vnd.apple.mpegurl"}
    ]


def test_a_job_template_keeps_its_settings_and_only_gets_the_file_and_the_destination(mediaconvert):
    output = main.run({**INPUTS, "preset": "job_template", "job_template": "acme-delivery"})

    mediaconvert.get_job_template.assert_called_once_with(Name="acme-delivery")
    request = mediaconvert.create_job.call_args.kwargs
    hls, files = request["Settings"]["OutputGroups"]
    assert request["JobTemplate"] == "acme-delivery"
    assert "Queue" not in request
    assert set(request["Settings"]) == {"Inputs", "OutputGroups"}
    assert request["Settings"]["Inputs"] == [{"FileInput": "s3://media/in/show.mxf"}]
    assert hls["OutputGroupSettings"]["HlsGroupSettings"] == {
        "Destination": "s3://work/transcodes/show",
        "SegmentLength": 4,
        "MinSegmentLength": 0,
    }
    assert hls["Outputs"] == TEMPLATE["Settings"]["OutputGroups"][0]["Outputs"]
    assert files["OutputGroupSettings"]["FileGroupSettings"] == {"Destination": "s3://work/transcodes/show"}
    assert files["Outputs"] == TEMPLATE["Settings"]["OutputGroups"][1]["Outputs"]
    # The earlier delivery's job is looked for in the template's own queue.
    assert mediaconvert.search_jobs.call_args.kwargs["Queue"] == TEMPLATE["Queue"]
    assert output["output_uris"] == [
        "s3://work/transcodes/show.m3u8",
        "s3://work/transcodes/show_master.mxf",
        "s3://work/transcodes/show_audio.wav",
    ]
    assert [asset["label"] for asset in patchbae.emitted_assets] == ["HLS", "Transcode"]
    assert TEMPLATE["Settings"]["OutputGroups"][0]["OutputGroupSettings"]["HlsGroupSettings"]["Destination"] == "s3://old/hls/"


def test_a_missing_job_template_fails_before_anything_is_started(mediaconvert):
    mediaconvert.get_job_template.side_effect = client_error("NotFoundException")

    with pytest.raises(patchbae.ComponentFailed, match="'nope' was not found in this region"):
        main.run({**INPUTS, "preset": "job_template", "job_template": "nope"})

    mediaconvert.create_job.assert_not_called()


def test_a_redelivered_task_waits_for_its_job_instead_of_starting_another(mediaconvert):
    mediaconvert.search_jobs.side_effect = [
        {"Jobs": [earlier_job("PROGRESSING", "1690000000000-other1", task="another-task")], "NextToken": "page-2"},
        {"Jobs": [earlier_job("PROGRESSING")]},
    ]
    mediaconvert.get_job.side_effect = [job("PROGRESSING", "1690000000000-old111"), job("COMPLETE", "1690000000000-old111")]

    output = main.run(INPUTS)

    mediaconvert.create_job.assert_not_called()
    assert mediaconvert.search_jobs.call_args_list[1].kwargs["NextToken"] == "page-2"
    assert [call.kwargs for call in mediaconvert.get_job.call_args_list] == [{"Id": "1690000000000-old111"}] * 2
    assert output["job_id"] == "1690000000000-old111"
    assert output["output_uris"] == ["s3://work/transcodes/show_1080p.mp4"]


def test_a_redelivery_within_a_minute_sends_the_same_token(mediaconvert):
    main.run(INPUTS)
    mediaconvert.get_job.side_effect = [job("COMPLETE")]
    main.run(INPUTS)

    first, second = [call.kwargs["ClientRequestToken"] for call in mediaconvert.create_job.call_args_list]
    assert first == second == TASK


def test_a_failed_job_from_an_earlier_attempt_is_replaced(mediaconvert):
    mediaconvert.search_jobs.return_value = {"Jobs": [earlier_job("ERROR")]}

    output = main.run(INPUTS)

    # A new token: with the old one MediaConvert would hand the failed job back.
    assert mediaconvert.create_job.call_args.kwargs["ClientRequestToken"] == f"{TASK}-1690000000000-old111"
    assert output["job_id"] == "1700000000000-abc123"


@pytest.mark.parametrize(
    "finished, message",
    [
        (job("ERROR", ErrorCode=1010, ErrorMessage="Unable to open input file: Access Denied"), "Access Denied"),
        (job("ERROR"), "no reason given"),
        (job("CANCELED"), "was canceled"),
    ],
)
def test_a_job_that_fails_fails_the_node_with_the_reason(mediaconvert, finished, message):
    mediaconvert.get_job.side_effect = [job("SUBMITTED"), finished]

    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(INPUTS)

    assert patchbae.emitted_assets == []


def test_a_job_mediaconvert_rejects_fails_the_node_with_its_message(mediaconvert):
    mediaconvert.create_job.side_effect = client_error("BadRequestException", "Role is not a valid IAM role ARN")

    with pytest.raises(patchbae.ComponentFailed, match="rejected the job: Role is not a valid IAM role ARN"):
        main.run(INPUTS)


def test_a_missing_permission_is_not_swallowed(mediaconvert):
    mediaconvert.create_job.side_effect = client_error("ForbiddenException")

    with pytest.raises(ClientError):
        main.run(INPUTS)


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({**INPUTS, "media_uri": "https://example.com/a.mp4"}, "media_uri must be an S3 URI"),
        ({**INPUTS, "media_uri": "s3://media/in/"}, "media_uri must point to a file"),
        ({**INPUTS, "output_uri": "transcodes/"}, "output_uri must be an S3 URI"),
        ({**INPUTS, "output_uri": "s3://work/transcodes"}, "output_uri must be a folder"),
        ({**INPUTS, "mediaconvert_role_arn": ""}, "mediaconvert_role_arn must be the ARN"),
        ({**INPUTS, "mediaconvert_role_arn": "MediaConvertServiceRole"}, "mediaconvert_role_arn must be the ARN"),
        ({**INPUTS, "preset": "prores"}, "Unknown preset 'prores'"),
        ({**INPUTS, "preset": "job_template"}, "job_template must hold the name"),
        ({**INPUTS, "job_template": "acme-delivery"}, "Set preset to job_template"),
        ({**INPUTS, "name_modifier": "v2/final"}, "cannot contain a slash"),
    ],
)
def test_bad_inputs_fail_before_anything_is_started(mediaconvert, inputs, message):
    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    mediaconvert.search_jobs.assert_not_called()
    mediaconvert.create_job.assert_not_called()


def test_an_output_that_would_overwrite_the_input_is_refused(mediaconvert):
    # A template output without a name modifier, written to the input's own folder, has the input's name.
    mediaconvert.get_job_template.return_value = {
        "JobTemplate": {
            "Settings": {
                "OutputGroups": [
                    {
                        "OutputGroupSettings": {"Type": "FILE_GROUP_SETTINGS", "FileGroupSettings": {}},
                        "Outputs": [{"ContainerSettings": {"Container": "MP4"}}],
                    }
                ]
            }
        }
    }
    inputs = {**INPUTS, "media_uri": "s3://work/transcodes/show.mp4", "preset": "job_template", "job_template": "rewrap"}

    with pytest.raises(patchbae.ComponentFailed, match="would overwrite media_uri"):
        main.run(inputs)

    mediaconvert.create_job.assert_not_called()
    main.run({**inputs, "name_modifier": "_rewrapped"})
    mediaconvert.create_job.assert_called_once()


def test_tokens_fit_what_aws_accepts():
    assert main.request_token(TASK) == TASK
    long = main.request_token("task/with:odd chars", "x" * 80)
    assert len(long) == 64 and long.isalnum()
    assert main.request_token("a b", "c") == "a-b-c"


@pytest.mark.parametrize(
    "inputs",
    [
        INPUTS,
        {**INPUTS, "preset": "mp4_h264_720p", "queue": "proxies"},
        {**INPUTS, "preset": "hls_abr"},
        {**INPUTS, "preset": "job_template", "job_template": "acme-delivery"},
    ],
    ids=["mp4_1080p", "mp4_720p", "hls_abr", "job_template"],
)
def test_the_request_matches_the_mediaconvert_api(mediaconvert, inputs):
    """Every key, type and enum value in the request exists in the SDK's own description of CreateJob."""
    main.run(inputs)

    shape = botocore.session.get_session().get_service_model("mediaconvert").operation_model("CreateJob").input_shape
    request = mediaconvert.create_job.call_args.kwargs
    report = ParamValidator().validate(request, shape)
    assert not report.has_errors(), report.generate_report()
    assert unknown_enum_values(request, shape) == []


def unknown_enum_values(value, shape, path="request"):
    """Paths of strings that are not one of the values the API lists for them."""
    if shape.type_name == "structure":
        return [
            problem
            for key, item in value.items()
            for problem in unknown_enum_values(item, shape.members[key], f"{path}.{key}")
        ]
    if shape.type_name == "list":
        return [problem for item in value for problem in unknown_enum_values(item, shape.member, f"{path}[]")]
    if shape.type_name == "map":
        return [problem for item in value.values() for problem in unknown_enum_values(item, shape.value, f"{path}{{}}")]
    if getattr(shape, "enum", None) and value not in shape.enum:
        return [f"{path}={value}"]
    return []
