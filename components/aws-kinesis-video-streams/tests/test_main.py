import base64
import datetime
import io

import pytest

import patchbae
from patchbae.testing import FakeAws, load_component

main = load_component(__file__)

TASK = patchbae.task.id
UTC = datetime.timezone.utc
START = datetime.datetime(2026, 11, 25, 19, 42, 10, tzinfo=UTC)
END = datetime.datetime(2026, 11, 25, 19, 43, 10, tzinfo=UTC)
ENDPOINT = "https://b-1234abcd.kinesisvideo.eu-west-1.amazonaws.com"
CLIP = b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 5000
JPEG = b"\xff\xd8\xff\xe0" + b"\x02" * 300
INPUTS = {
    "stream_name": "stadium-cam-north",
    "start_time": "2026-11-25T19:42:10Z",
    "end_time": "2026-11-25T19:43:10Z",
    "output_uri": "s3://northlight-work/camera-clips/",
}
EMPTY = {"clip_uri": "", "clip_bytes": 0, "images_uri": "", "image_uris": [], "image_count": 0, "skipped_count": 0}


def image(second, error=None):
    taken = START + datetime.timedelta(seconds=second)
    if error:
        return {"TimeStamp": taken, "Error": error}
    return {"TimeStamp": taken, "ImageContent": base64.b64encode(JPEG).decode("ascii")}


@pytest.fixture
def aws(monkeypatch):
    fake = FakeAws()
    fake.client("kinesisvideo").respond("get_data_endpoint", {"DataEndpoint": ENDPOINT})
    archive = fake.client("kinesis-video-archived-media")
    archive.respond("get_clip", lambda _request: {"ContentType": "video/mp4", "Payload": io.BytesIO(CLIP)})
    archive.respond("get_images", {"Images": [image(0), image(5), image(10)]})
    s3 = fake.client("s3")
    s3.respond("put_object", {})
    s3.uploads = []

    def upload_fileobj(body, bucket, key, ExtraArgs=None, Config=None):
        # Read the way a multipart upload does: in pieces, never asking for everything.
        pieces = []
        while piece := body.read(1024):
            pieces.append(piece)
        s3.uploads.append({"bucket": bucket, "key": key, "body": b"".join(pieces), "extra": ExtraArgs, "config": Config})

    s3.upload_fileobj = upload_fileobj
    monkeypatch.setattr(main.boto3, "client", fake.client)
    patchbae.reset()
    return fake


def test_exports_a_clip_to_s3_without_holding_it_in_memory(aws):
    output = main.run({**INPUTS, "action": "export_clip", "file_name": "goal-63min"})

    assert aws.client("kinesisvideo").called("get_data_endpoint") == [{"StreamName": "stadium-cam-north", "APIName": "GET_CLIP"}]
    # The stream's own endpoint is what the component reads the media from.
    assert {"endpoint_url": ENDPOINT} in aws.client("kinesis-video-archived-media").created_with
    assert aws.client("kinesis-video-archived-media").called("get_clip") == [
        {
            "StreamName": "stadium-cam-north",
            "ClipFragmentSelector": {
                "FragmentSelectorType": "PRODUCER_TIMESTAMP",
                "TimestampRange": {"StartTimestamp": START, "EndTimestamp": END},
            },
        }
    ]
    (upload,) = aws.client("s3").uploads
    assert (upload["bucket"], upload["key"]) == ("northlight-work", "camera-clips/goal-63min.mp4")
    assert upload["body"] == CLIP
    assert upload["extra"] == {"ContentType": "video/mp4"}
    assert (upload["config"].multipart_chunksize, upload["config"].max_concurrency) == (8 * 1024 * 1024, 2)
    assert output == {**EMPTY, "clip_uri": "s3://northlight-work/camera-clips/goal-63min.mp4", "clip_bytes": len(CLIP)}
    assert patchbae.emitted_assets == [
        {
            "label": "Clip",
            "locator": "s3://northlight-work/camera-clips/goal-63min.mp4",
            "mediaType": "video/mp4",
            "metadata": {"start": "2026-11-25T19:42:10+00:00", "end": "2026-11-25T19:43:10+00:00", "bytes": len(CLIP)},
        }
    ]


def test_a_clip_is_named_after_the_task_so_a_second_delivery_writes_the_same_file(aws):
    first = main.run({**INPUTS, "action": "export_clip", "timestamp_type": "SERVER_TIMESTAMP"})
    second = main.run({**INPUTS, "action": "export_clip", "timestamp_type": "SERVER_TIMESTAMP"})

    assert first == second
    assert first["clip_uri"] == f"s3://northlight-work/camera-clips/patchbae-{TASK}.mp4"
    assert [upload["key"] for upload in aws.client("s3").uploads] == [f"camera-clips/patchbae-{TASK}.mp4"] * 2
    selector = aws.client("kinesis-video-archived-media").called("get_clip")[0]["ClipFragmentSelector"]
    assert selector["FragmentSelectorType"] == "SERVER_TIMESTAMP"


def test_a_range_without_media_fails_the_node_and_writes_nothing(aws):
    aws.client("kinesis-video-archived-media").respond("get_clip", FakeAws.error("ResourceNotFoundException", status=404))

    with pytest.raises(patchbae.ComponentFailed, match="stadium-cam-north has no media between 2026-11-25T19:42:10"):
        main.run({**INPUTS, "action": "export_clip"})

    assert aws.client("s3").uploads == []


def test_media_kinesis_cannot_turn_into_a_clip_fails_the_node_with_its_reason(aws):
    aws.client("kinesis-video-archived-media").respond(
        "get_clip", FakeAws.error("MissingCodecPrivateDataException", "No codec private data was found in at least one of tracks")
    )

    with pytest.raises(patchbae.ComponentFailed, match="could not make a clip of this range: No codec private data"):
        main.run({**INPUTS, "action": "export_clip"})


def test_an_empty_clip_fails_the_node(aws):
    aws.client("kinesis-video-archived-media").respond("get_clip", lambda _request: {"ContentType": "video/mp4", "Payload": io.BytesIO(b"")})

    with pytest.raises(patchbae.ComponentFailed, match="returned an empty clip"):
        main.run({**INPUTS, "action": "export_clip"})


def test_extracts_still_images_to_a_folder(aws):
    output = main.run({**INPUTS, "action": "extract_images", "file_name": "goal-63min"})

    assert aws.client("kinesisvideo").called("get_data_endpoint") == [{"StreamName": "stadium-cam-north", "APIName": "GET_IMAGES"}]
    assert aws.client("kinesis-video-archived-media").called("get_images") == [
        {
            "StreamName": "stadium-cam-north",
            "ImageSelectorType": "PRODUCER_TIMESTAMP",
            "StartTimestamp": START,
            "EndTimestamp": END,
            "SamplingInterval": 5000,
            "Format": "JPEG",
            "MaxResults": 100,
        }
    ]
    folder = "s3://northlight-work/camera-clips/goal-63min/"
    uris = [f"{folder}20261125T194210000Z.jpg", f"{folder}20261125T194215000Z.jpg", f"{folder}20261125T194220000Z.jpg"]
    assert aws.client("s3").called("put_object") == [
        {"Bucket": "northlight-work", "Key": uri.removeprefix("s3://northlight-work/"), "Body": JPEG, "ContentType": "image/jpeg"} for uri in uris
    ]
    assert output == {**EMPTY, "images_uri": folder, "image_uris": uris, "image_count": 3, "skipped_count": 0}
    assert patchbae.emitted_assets == [{"label": "Stills", "locator": folder, "mediaType": "image/jpeg", "metadata": {"count": 3}}]


def test_images_stop_at_the_limit_and_skip_points_without_media(aws):
    archive = aws.client("kinesis-video-archived-media")
    archive.respond(
        "get_images",
        {"Images": [image(0), image(1, "NO_MEDIA")], "NextToken": "cGFnZS0y"},
        {"Images": [image(2), image(3), image(4)]},
    )

    output = main.run(
        {
            **INPUTS,
            "action": "extract_images",
            "image_format": "PNG",
            "sampling_interval_seconds": 0.5,
            "max_images": 3,
            "image_width": 1280,
            "image_height": 720,
        }
    )

    first, second = archive.called("get_images")
    assert (first["Format"], first["SamplingInterval"], first["WidthPixels"], first["HeightPixels"]) == ("PNG", 500, 1280, 720)
    assert (first["MaxResults"], second["MaxResults"], second["NextToken"]) == (3, 2, "cGFnZS0y")
    assert (output["image_count"], output["skipped_count"]) == (3, 1)
    assert all(uri.endswith(".png") for uri in output["image_uris"])
    assert {call["ContentType"] for call in aws.client("s3").called("put_object")} == {"image/png"}


def test_a_range_where_every_point_lacks_media_fails_the_node(aws):
    aws.client("kinesis-video-archived-media").respond("get_images", {"Images": [image(0, "NO_MEDIA"), image(5, "MEDIA_ERROR")]})

    with pytest.raises(patchbae.ComponentFailed, match="no media between .* so no images were made"):
        main.run({**INPUTS, "action": "extract_images"})


def test_a_stream_that_does_not_exist_fails_the_node(aws):
    aws.client("kinesisvideo").respond("get_data_endpoint", FakeAws.error("ResourceNotFoundException", status=404))

    with pytest.raises(patchbae.ComponentFailed, match="Stream stadium-cam-north was not found"):
        main.run({**INPUTS, "action": "export_clip"})


def test_a_missing_permission_is_not_swallowed(aws):
    aws.client("kinesisvideo").respond("get_data_endpoint", FakeAws.error("NotAuthorizedException", status=401))

    with pytest.raises(main.ClientError):
        main.run({**INPUTS, "action": "export_clip"})


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({**INPUTS, "action": "export_clip", "stream_name": "stadium cam"}, "stream_name must be the stream's name"),
        ({**INPUTS, "action": "record"}, "Unknown action 'record'"),
        ({**INPUTS, "action": "export_clip", "start_time": "19:42"}, "start_time must be a UTC time"),
        ({**INPUTS, "action": "export_clip", "end_time": "2026-11-25T19:42:10Z"}, "end_time must be after start_time"),
        ({**INPUTS, "action": "export_clip", "end_time": "2026-11-27T19:42:10Z"}, "at most 24 hours for a clip"),
        ({**INPUTS, "action": "extract_images", "end_time": "2026-11-25T19:52:10Z"}, "at most 300 seconds for images"),
        ({**INPUTS, "action": "export_clip", "output_uri": "s3://northlight-work/camera-clips"}, "output_uri must be a folder"),
        ({**INPUTS, "action": "export_clip", "output_uri": "/tmp/clips/"}, "output_uri must be an S3 URI"),
        ({**INPUTS, "action": "export_clip", "file_name": "goals/63"}, "file_name may hold"),
        ({**INPUTS, "action": "export_clip", "timestamp_type": "CAMERA"}, "Unknown timestamp_type 'CAMERA'"),
        ({**INPUTS, "action": "extract_images", "image_format": "WEBP"}, "Unknown image_format 'WEBP'"),
        ({**INPUTS, "action": "extract_images", "sampling_interval_seconds": 0.1}, "at least 0.2"),
        ({**INPUTS, "action": "extract_images", "max_images": 0}, "max_images must be a whole number"),
        ({**INPUTS, "action": "extract_images", "image_width": 1280}, "Set image_width and image_height together"),
        ({**INPUTS, "action": "extract_images", "image_width": 8000, "image_height": 720}, "image_width must be 1 to 3840"),
    ],
)
def test_bad_inputs_fail_before_anything_is_read(aws, inputs, message):
    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    assert aws.client("kinesisvideo").calls == []
