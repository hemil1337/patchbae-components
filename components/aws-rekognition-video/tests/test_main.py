import json
from unittest import mock

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

import patchbae
from patchbae.testing import load_component

main = load_component(__file__)

TASK = patchbae.task.id
VIDEO = {"S3Object": {"Bucket": "media", "Name": "in/show.mp4"}}
INPUTS = {"media_uri": "s3://media/in/show.mp4", "analyses": ["labels", "moderation"], "min_confidence": 80, "max_items": 50}


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


def label(name, confidence, timestamp):
    return {"Timestamp": timestamp, "Label": {"Name": name, "Confidence": confidence, "Instances": [], "Parents": []}}


def moderation(name, confidence, timestamp):
    return {"Timestamp": timestamp, "ModerationLabel": {"Name": name, "Confidence": confidence, "ParentName": ""}}


def shot(start, end):
    return {"Type": "SHOT", "StartTimestampMillis": start, "EndTimestampMillis": end, "ShotSegment": {"Index": 0, "Confidence": 99.0}}


def done(**results):
    return {"JobStatus": "SUCCEEDED", **results}


@pytest.fixture
def aws(monkeypatch):
    rekognition = mock.Mock()
    s3 = mock.Mock()
    rekognition.start_label_detection.return_value = {"JobId": "job-labels"}
    rekognition.start_content_moderation.return_value = {"JobId": "job-moderation"}
    rekognition.start_segment_detection.return_value = {"JobId": "job-shots"}
    rekognition.get_label_detection.return_value = done(Labels=[])
    rekognition.get_content_moderation.return_value = done(ModerationLabels=[])
    rekognition.get_segment_detection.return_value = done(Segments=[])
    monkeypatch.setattr(main.boto3, "client", lambda name: {"rekognition": rekognition, "s3": s3}[name])
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    patchbae.reset()
    return rekognition, s3


def test_starts_the_analyses_and_returns_a_summary(aws):
    rekognition, s3 = aws
    rekognition.get_label_detection.side_effect = [
        {"JobStatus": "IN_PROGRESS"},
        done(Labels=[label("Car", 91.26, 4000), label("Road", 99.514, 0), label("Car", 98.2, 9000)]),
    ]
    rekognition.get_content_moderation.side_effect = [{"JobStatus": "IN_PROGRESS"}, done(ModerationLabels=[])]

    output = main.run(INPUTS)

    rekognition.start_label_detection.assert_called_once_with(
        Video=VIDEO, ClientRequestToken=f"{TASK}-labels", MinConfidence=80.0
    )
    rekognition.start_content_moderation.assert_called_once_with(
        Video=VIDEO, ClientRequestToken=f"{TASK}-moderation", MinConfidence=80.0
    )
    rekognition.start_segment_detection.assert_not_called()
    rekognition.get_label_detection.assert_called_with(JobId="job-labels", MaxResults=1000)
    rekognition.get_content_moderation.assert_called_with(JobId="job-moderation", MaxResults=1000)
    assert output == {
        "labels": [
            {"name": "Road", "confidence": 99.51, "timestamp_ms": 0},
            {"name": "Car", "confidence": 98.2, "timestamp_ms": 4000},
        ],
        "moderation_labels": [],
        "flagged": False,
        "shot_count": 0,
        "shots": [],
    }
    s3.put_object.assert_not_called()
    assert patchbae.emitted_assets == []


def test_everything_is_started_before_anything_is_waited_for(aws):
    rekognition, _ = aws

    main.run({**INPUTS, "analyses": ["shots", "labels", "moderation"]})

    calls = [call[0] for call in rekognition.mock_calls]
    assert calls[:3] == ["start_label_detection", "start_content_moderation", "start_segment_detection"]
    assert all(name.startswith("get_") for name in calls[3:])


def test_every_page_of_the_results_is_read(aws):
    rekognition, _ = aws
    rekognition.get_label_detection.side_effect = [
        done(Labels=[label("Car", 85.0, 6000)], NextToken="page-2"),
        done(Labels=[label("Car", 97.0, 2000), label("Tree", 90.0, 8000)], NextToken="page-3"),
        done(Labels=[label("Tree", 88.0, 1000)]),
    ]

    output = main.run({**INPUTS, "analyses": ["labels"]})

    assert [call.kwargs for call in rekognition.get_label_detection.call_args_list] == [
        {"JobId": "job-labels", "MaxResults": 1000},
        {"JobId": "job-labels", "MaxResults": 1000, "NextToken": "page-2"},
        {"JobId": "job-labels", "MaxResults": 1000, "NextToken": "page-3"},
    ]
    assert output["labels"] == [
        {"name": "Car", "confidence": 97.0, "timestamp_ms": 2000},
        {"name": "Tree", "confidence": 90.0, "timestamp_ms": 1000},
    ]


def test_labels_are_cut_at_max_items_most_confident_first(aws):
    rekognition, _ = aws
    rekognition.get_label_detection.return_value = done(
        Labels=[label("B", 90.0, 0), label("C", 80.0, 0), label("A", 90.0, 0), label("D", 99.0, 0)]
    )

    output = main.run({**INPUTS, "analyses": ["labels"], "max_items": 3})

    assert [item["name"] for item in output["labels"]] == ["D", "A", "B"]


@pytest.mark.parametrize("confidence, flagged", [(80.0, True), (93.5, True), (79.99, False)])
def test_moderation_flags_at_or_above_the_minimum_confidence(aws, confidence, flagged):
    rekognition, _ = aws
    rekognition.get_content_moderation.return_value = done(
        ModerationLabels=[moderation("Violence", confidence, 12000), moderation("Violence", 60.0, 3000)]
    )

    output = main.run({**INPUTS, "analyses": ["moderation"]})

    assert output["moderation_labels"] == [{"name": "Violence", "confidence": round(confidence, 2), "timestamp_ms": 3000}]
    assert output["flagged"] is flagged
    assert output["labels"] == []
    rekognition.start_label_detection.assert_not_called()


def test_shots_are_counted_and_listed(aws):
    rekognition, _ = aws
    rekognition.get_segment_detection.side_effect = [
        done(Segments=[shot(0, 4120), shot(4160, 9000)], NextToken="more"),
        done(Segments=[shot(9040, 12000), {"Type": "TECHNICAL_CUE", "StartTimestampMillis": 0, "EndTimestampMillis": 1}]),
    ]

    output = main.run({**INPUTS, "analyses": ["shots"]})

    rekognition.start_segment_detection.assert_called_once_with(
        Video=VIDEO, ClientRequestToken=f"{TASK}-shots", SegmentTypes=["SHOT"]
    )
    assert output["shot_count"] == 3
    assert output["shots"] == [
        {"start_ms": 0, "end_ms": 4120},
        {"start_ms": 4160, "end_ms": 9000},
        {"start_ms": 9040, "end_ms": 12000},
    ]


def test_the_shot_list_is_capped_but_the_count_is_not(aws):
    rekognition, _ = aws
    rekognition.get_segment_detection.return_value = done(Segments=[shot(i * 1000, i * 1000 + 960) for i in range(1200)])

    output = main.run({**INPUTS, "analyses": ["shots"]})

    assert output["shot_count"] == 1200
    assert len(output["shots"]) == 1000


def test_complete_results_are_written_when_an_output_folder_is_set(aws):
    rekognition, s3 = aws
    first, second = [label("Car", 98.0, 0)], [label("Tree", 90.0, 500)]
    flagged = [moderation("Violence", 95.0, 100)]
    shots = [shot(0, 4120)]
    rekognition.get_label_detection.side_effect = [done(Labels=first, NextToken="page-2"), done(Labels=second)]
    rekognition.get_content_moderation.return_value = done(ModerationLabels=flagged)
    rekognition.get_segment_detection.return_value = done(Segments=shots)

    output = main.run({**INPUTS, "analyses": ["labels", "moderation", "shots"], "output_uri": "s3://work/analysis/"})

    key = f"analysis/patchbae-{TASK}-rekognition.json"
    request = s3.put_object.call_args.kwargs
    assert {name: value for name, value in request.items() if name != "Body"} == {
        "Bucket": "work",
        "Key": key,
        "ContentType": "application/json",
    }
    assert json.loads(request["Body"]) == {
        "media_uri": "s3://media/in/show.mp4",
        "min_confidence": 80.0,
        "Labels": first + second,
        "ModerationLabels": flagged,
        "Segments": shots,
    }
    assert output["results_uri"] == f"s3://work/{key}"
    assert output["flagged"] is True
    assert patchbae.emitted_assets == [
        {"label": "Video analysis", "locator": f"s3://work/{key}", "mediaType": "application/json"}
    ]


def test_an_analysis_without_results_still_writes_a_valid_file(aws):
    _, s3 = aws

    main.run({**INPUTS, "output_uri": "s3://work"})

    assert s3.put_object.call_args.kwargs["Key"] == f"patchbae-{TASK}-rekognition.json"
    document = json.loads(s3.put_object.call_args.kwargs["Body"])
    assert document["Labels"] == [] and document["ModerationLabels"] == []


def test_a_redelivered_task_sends_the_same_tokens_so_nothing_is_started_twice(aws):
    rekognition, _ = aws

    main.run(INPUTS)
    main.run(INPUTS)

    for start in (rekognition.start_label_detection, rekognition.start_content_moderation):
        first, second = [call.kwargs for call in start.call_args_list]
        assert first == second
    assert rekognition.start_label_detection.call_args.kwargs["ClientRequestToken"] == f"{TASK}-labels"


def test_an_analysis_that_fails_fails_the_node_with_the_reason(aws):
    rekognition, s3 = aws
    rekognition.get_content_moderation.side_effect = [
        {"JobStatus": "IN_PROGRESS"},
        {"JobStatus": "FAILED", "StatusMessage": "Unsupported codec/format."},
    ]

    with pytest.raises(patchbae.ComponentFailed, match=r"\(moderation\): Unsupported codec/format"):
        main.run({**INPUTS, "output_uri": "s3://work/analysis/"})

    s3.put_object.assert_not_called()


def test_waits_for_a_free_slot_when_the_account_is_at_its_limit(aws):
    rekognition, _ = aws
    rekognition.start_label_detection.side_effect = [
        client_error("LimitExceededException"),
        client_error("LimitExceededException"),
        {"JobId": "job-labels"},
    ]

    main.run({**INPUTS, "analyses": ["labels"]})

    assert rekognition.start_label_detection.call_count == 3


@pytest.mark.parametrize(
    "code, message",
    [
        ("InvalidS3ObjectException", "Rekognition cannot read media_uri"),
        ("VideoTooLargeException", "too large for Rekognition Video"),
    ],
)
def test_a_file_rekognition_cannot_take_fails_the_node_with_what_to_do(aws, code, message):
    rekognition, _ = aws
    rekognition.start_label_detection.side_effect = client_error(code)

    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(INPUTS)


def test_a_missing_permission_is_not_swallowed(aws):
    rekognition, _ = aws
    rekognition.start_label_detection.side_effect = client_error("AccessDeniedException")

    with pytest.raises(ClientError):
        main.run(INPUTS)


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({**INPUTS, "media_uri": "https://example.com/a.mp4"}, "media_uri must be an S3 URI"),
        ({**INPUTS, "media_uri": "s3://media/in/"}, "media_uri must point to a video file"),
        ({**INPUTS, "analyses": ["labels", "faces"]}, "Unknown analysis 'faces'"),
        ({**INPUTS, "analyses": []}, "analyses is empty"),
        ({**INPUTS, "min_confidence": 101}, "min_confidence must be between 0 and 100"),
        ({**INPUTS, "min_confidence": "high"}, "min_confidence must be a number"),
        ({**INPUTS, "max_items": 0}, "max_items must be between 1 and 1000"),
        ({**INPUTS, "output_uri": "work/analysis/"}, "output_uri must be an S3 URI"),
        ({**INPUTS, "output_uri": "s3://work/analysis"}, "output_uri must be a folder"),
    ],
)
def test_bad_inputs_fail_before_anything_is_started(aws, inputs, message):
    rekognition, _ = aws

    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    assert rekognition.mock_calls == []


def test_defaults_apply_when_inputs_are_left_out(aws):
    rekognition, _ = aws

    main.run({"media_uri": "s3://media/in/show.mp4"})

    assert rekognition.start_label_detection.call_args.kwargs["MinConfidence"] == 80.0
    rekognition.start_content_moderation.assert_called_once()
    rekognition.start_segment_detection.assert_not_called()


def test_tokens_fit_what_rekognition_accepts():
    assert main.request_token(TASK, "moderation") == f"{TASK}-moderation"
    long = main.request_token("task/with:odd chars" * 5, "labels")
    assert len(long) == 64 and long.isalnum()


def test_the_requests_match_the_rekognition_api(aws):
    """Every parameter the component sends exists in the SDK's own description of the operations."""
    rekognition, _ = aws
    rekognition.get_label_detection.side_effect = [done(Labels=[], NextToken="page-2"), done(Labels=[])]

    main.run({**INPUTS, "analyses": ["labels", "moderation", "shots"]})

    model = botocore.session.get_session().get_service_model("rekognition")
    operations = {
        "StartLabelDetection": rekognition.start_label_detection,
        "StartContentModeration": rekognition.start_content_moderation,
        "StartSegmentDetection": rekognition.start_segment_detection,
        "GetLabelDetection": rekognition.get_label_detection,
        "GetContentModeration": rekognition.get_content_moderation,
        "GetSegmentDetection": rekognition.get_segment_detection,
    }
    for operation, method in operations.items():
        for call in method.call_args_list:
            report = ParamValidator().validate(call.kwargs, model.operation_model(operation).input_shape)
            assert not report.has_errors(), f"{operation}: {report.generate_report()}"
