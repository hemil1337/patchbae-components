from unittest import mock

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator
from s3transfer.manager import TransferManager

import patchbae
from patchbae.testing import load_component

main = load_component(__file__)

INPUTS = {
    "source_uri": "s3://in/a/show.mxf",
    "destination_uri": "s3://out/b/master.mxf",
    "move": False,
    "storage_class": "STANDARD",
    "max_objects": 1000,
}
FOLDER = {**INPUTS, "source_uri": "s3://in/a/", "destination_uri": "s3://out/b/"}


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


def media(size, **extra):
    return {"ContentLength": size, "ContentType": "application/mxf", "Metadata": {}, **extra}


class Buckets:
    """What is on S3, kept in a dict that the mocked client reads and changes."""

    def __init__(self):
        self.objects = {}
        self.tags = {}
        self.page_size = 1000

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise client_error("404")
        return dict(self.objects[(Bucket, Key)])

    def get_object_tagging(self, Bucket, Key):
        return {"TagSet": self.tags.get((Bucket, Key), [])}

    def copy(self, CopySource, Bucket, Key, ExtraArgs=None):
        source = self.objects[(CopySource["Bucket"], CopySource["Key"])]
        self.objects[(Bucket, Key)] = {"ContentLength": source["ContentLength"], "ContentType": ExtraArgs.get("ContentType")}

    def delete_object(self, Bucket, Key):
        del self.objects[(Bucket, Key)]

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken="0"):
        keys = sorted(key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix))
        start = int(ContinuationToken)
        page = {"Contents": [{"Key": key, "Size": self.objects[(Bucket, key)]["ContentLength"]} for key in keys[start : start + self.page_size]]}
        if start + self.page_size < len(keys):
            page.update(IsTruncated=True, NextContinuationToken=str(start + self.page_size))
        return page


@pytest.fixture
def buckets():
    return Buckets()


@pytest.fixture
def s3(monkeypatch, buckets):
    client = mock.Mock()
    for name in ("head_object", "get_object_tagging", "copy", "delete_object", "list_objects_v2"):
        getattr(client, name).side_effect = getattr(buckets, name)
    monkeypatch.setattr(main.boto3, "client", lambda name: {"s3": client}[name])
    patchbae.reset()
    return client


def test_copies_one_object_and_returns_a_reference(s3, buckets):
    buckets.objects[("in", "a/show.mxf")] = media(5_000_000_000)

    output = main.run({**INPUTS, "storage_class": "GLACIER_IR"})

    s3.copy.assert_called_once_with(
        {"Bucket": "in", "Key": "a/show.mxf"},
        "out",
        "b/master.mxf",
        ExtraArgs={
            "StorageClass": "GLACIER_IR",
            "MetadataDirective": "REPLACE",
            "Metadata": {},
            "ContentType": "application/mxf",
        },
    )
    s3.head_object.assert_called_with(Bucket="out", Key="b/master.mxf")
    s3.delete_object.assert_not_called()
    assert output == {
        "destination_uri": "s3://out/b/master.mxf",
        "copied": 1,
        "bytes": 5_000_000_000,
        "moved": False,
        "objects": ["s3://out/b/master.mxf"],
    }
    assert patchbae.emitted_assets == [{"label": "Copy", "locator": "s3://out/b/master.mxf", "mediaType": "application/mxf"}]


@pytest.mark.parametrize("destination", ["s3://out/b/", "s3://out"])
def test_a_destination_folder_keeps_the_file_name(s3, buckets, destination):
    buckets.objects[("in", "a/show.mxf")] = media(10)

    output = main.run({**INPUTS, "destination_uri": destination})

    assert output["destination_uri"] == destination.rstrip("/") + "/show.mxf"


def test_metadata_and_tags_come_along(s3, buckets):
    buckets.objects[("in", "a/show.mxf")] = media(
        10, Metadata={"title": "Show"}, CacheControl="max-age=60", ContentDisposition="attachment", StorageClass="STANDARD_IA"
    )
    buckets.tags[("in", "a/show.mxf")] = [{"Key": "project", "Value": "Autumn promo"}, {"Key": "tier", "Value": "a&b"}]

    main.run(INPUTS)

    arguments = s3.copy.call_args.kwargs["ExtraArgs"]
    assert arguments == {
        "StorageClass": "STANDARD",
        "MetadataDirective": "REPLACE",
        "Metadata": {"title": "Show"},
        "ContentType": "application/mxf",
        "CacheControl": "max-age=60",
        "ContentDisposition": "attachment",
        "Tagging": "project=Autumn%20promo&tier=a%26b",
        "TaggingDirective": "REPLACE",
    }
    # boto3's managed copy refuses arguments it does not know.
    assert set(arguments) <= set(TransferManager.ALLOWED_COPY_ARGS)


def test_a_move_deletes_the_source_after_the_copy_is_checked(s3, buckets):
    buckets.objects[("in", "a/show.mxf")] = media(10)

    output = main.run({**INPUTS, "move": True})

    calls = [(call[0], call.kwargs.get("Key")) for call in s3.mock_calls]
    assert calls.index(("copy", None)) < calls.index(("head_object", "b/master.mxf")) < calls.index(("delete_object", "a/show.mxf"))
    s3.delete_object.assert_called_once_with(Bucket="in", Key="a/show.mxf")
    assert ("in", "a/show.mxf") not in buckets.objects
    assert output["moved"] is True and output["copied"] == 1


def test_a_copy_of_the_wrong_size_fails_the_node_and_keeps_the_source(s3, buckets):
    buckets.objects[("in", "a/show.mxf")] = media(10)
    s3.copy.side_effect = lambda source, bucket, key, ExtraArgs: buckets.objects.update({(bucket, key): media(7)})

    with pytest.raises(patchbae.ComponentFailed, match="is 7 bytes but its source is 10 bytes"):
        main.run({**INPUTS, "move": True})

    s3.delete_object.assert_not_called()
    assert ("in", "a/show.mxf") in buckets.objects


def test_a_redelivered_move_that_already_finished_counts_as_done(s3, buckets):
    buckets.objects[("out", "b/master.mxf")] = media(10)

    output = main.run({**INPUTS, "move": True})

    s3.copy.assert_not_called()
    s3.delete_object.assert_not_called()
    assert output == {
        "destination_uri": "s3://out/b/master.mxf",
        "copied": 1,
        "bytes": 10,
        "moved": True,
        "objects": ["s3://out/b/master.mxf"],
    }
    assert [asset["label"] for asset in patchbae.emitted_assets] == ["Copy"]


@pytest.mark.parametrize("move", [False, True])
def test_a_source_that_does_not_exist_fails_the_node(s3, buckets, move):
    # Something at the destination does not make a plain copy "done": only a move removes its source.
    if not move:
        buckets.objects[("out", "b/master.mxf")] = media(10)

    with pytest.raises(patchbae.ComponentFailed, match="s3://in/a/show.mxf does not exist"):
        main.run({**INPUTS, "move": move})

    s3.copy.assert_not_called()


def test_a_folder_is_copied_object_by_object(s3, buckets):
    buckets.page_size = 2
    buckets.objects.update(
        {
            ("in", "a/"): media(0),
            ("in", "a/show.mxf"): media(100),
            ("in", "a/audio/en.wav"): media(20),
            ("in", "a/audio/sv.wav"): media(30),
            ("in", "other/keep.mxf"): media(1),
        }
    )

    output = main.run(FOLDER)

    assert [call.kwargs for call in s3.list_objects_v2.call_args_list] == [
        {"Bucket": "in", "Prefix": "a/"},
        {"Bucket": "in", "Prefix": "a/", "ContinuationToken": "2"},
    ]
    assert [(call.args[0]["Key"], call.args[2]) for call in s3.copy.call_args_list] == [
        ("a/audio/en.wav", "b/audio/en.wav"),
        ("a/audio/sv.wav", "b/audio/sv.wav"),
        ("a/show.mxf", "b/show.mxf"),
    ]
    assert output == {
        "destination_uri": "s3://out/b/",
        "copied": 3,
        "bytes": 150,
        "moved": False,
        "objects": ["s3://out/b/audio/en.wav", "s3://out/b/audio/sv.wav", "s3://out/b/show.mxf"],
    }
    assert patchbae.emitted_assets == []


def test_a_folder_with_too_many_objects_fails_before_anything_is_copied(s3, buckets):
    buckets.objects.update({("in", f"a/{index}.mxf"): media(1) for index in range(4)})

    with pytest.raises(patchbae.ComponentFailed, match="holds more than 3 objects"):
        main.run({**FOLDER, "max_objects": 3})

    s3.copy.assert_not_called()
    main.run({**FOLDER, "max_objects": 4})
    assert s3.copy.call_count == 4


def test_a_folder_move_deletes_nothing_until_every_copy_is_checked(s3, buckets):
    buckets.objects.update({("in", "a/1.mxf"): media(1), ("in", "a/2.mxf"): media(2)})

    output = main.run({**FOLDER, "move": True})

    names = [call[0] for call in s3.mock_calls]
    assert names.index("delete_object") > max(index for index, name in enumerate(names) if name in ("copy", "head_object"))
    assert sorted(buckets.objects) == [("out", "b/1.mxf"), ("out", "b/2.mxf")]
    assert output["copied"] == 2 and output["bytes"] == 3 and output["moved"] is True


def test_a_folder_move_that_fails_halfway_leaves_every_source(s3, buckets):
    buckets.objects.update({("in", "a/1.mxf"): media(1), ("in", "a/2.mxf"): media(2, StorageClass="DEEP_ARCHIVE")})

    with pytest.raises(patchbae.ComponentFailed, match="s3://in/a/2.mxf is archived"):
        main.run({**FOLDER, "move": True})

    s3.delete_object.assert_not_called()
    assert ("in", "a/1.mxf") in buckets.objects


def test_a_redelivered_folder_move_finishes_what_is_left(s3, buckets):
    # The first delivery moved 1.mxf and stopped before deleting 2.mxf.
    buckets.objects.update({("out", "b/1.mxf"): media(1), ("out", "b/2.mxf"): media(2), ("in", "a/2.mxf"): media(2)})

    output = main.run({**FOLDER, "move": True})

    assert sorted(buckets.objects) == [("out", "b/1.mxf"), ("out", "b/2.mxf")]
    assert output["objects"] == ["s3://out/b/2.mxf"]


def test_a_redelivered_folder_move_with_nothing_left_counts_as_done(s3, buckets):
    buckets.objects.update({("out", "b/1.mxf"): media(1), ("out", "b/2.mxf"): media(2)})

    output = main.run({**FOLDER, "move": True})

    s3.copy.assert_not_called()
    s3.delete_object.assert_not_called()
    assert output == {
        "destination_uri": "s3://out/b/",
        "copied": 2,
        "bytes": 3,
        "moved": True,
        "objects": ["s3://out/b/1.mxf", "s3://out/b/2.mxf"],
    }


def test_an_empty_folder_fails_the_node(s3, buckets):
    buckets.objects[("out", "b/1.mxf")] = media(1)

    with pytest.raises(patchbae.ComponentFailed, match="No objects found under s3://in/a/"):
        main.run(FOLDER)


def test_the_object_list_is_capped_at_one_hundred(s3, buckets):
    buckets.objects.update({("in", f"a/{index:03}.mxf"): media(1) for index in range(120)})

    output = main.run(FOLDER)

    assert output["copied"] == 120 and output["bytes"] == 120
    assert len(output["objects"]) == 100


@pytest.mark.parametrize(
    "head, archived",
    [
        (media(1, StorageClass="GLACIER"), True),
        (media(1, StorageClass="GLACIER", Restore='ongoing-request="true"'), True),
        (media(1, StorageClass="INTELLIGENT_TIERING", ArchiveStatus="ARCHIVE_ACCESS"), True),
        (media(1, StorageClass="GLACIER", Restore='ongoing-request="false", expiry-date="Fri, 21 Dec 2029 00:00:00 GMT"'), False),
        (media(1, StorageClass="GLACIER_IR"), False),
    ],
)
def test_an_archived_source_has_to_be_restored_first(s3, buckets, head, archived):
    buckets.objects[("in", "a/show.mxf")] = head

    if archived:
        with pytest.raises(patchbae.ComponentFailed, match="Restore it first"):
            main.run(INPUTS)
        s3.copy.assert_not_called()
    else:
        assert main.run(INPUTS)["copied"] == 1


def test_a_missing_permission_is_not_swallowed(s3, buckets):
    s3.head_object.side_effect = client_error("403")

    with pytest.raises(ClientError):
        main.run(INPUTS)


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({**INPUTS, "source_uri": "/in/a/show.mxf"}, "source_uri must be an S3 URI"),
        ({**INPUTS, "source_uri": "s3://in"}, "source_uri needs an object or a folder"),
        ({**INPUTS, "destination_uri": "out/b/"}, "destination_uri must be an S3 URI"),
        ({**FOLDER, "destination_uri": "s3://out/b"}, "destination_uri must be a folder too"),
        ({**INPUTS, "destination_uri": "s3://in/a/show.mxf"}, "are the same"),
        ({**INPUTS, "destination_uri": "s3://in/a/"}, "are the same"),
        ({**FOLDER, "destination_uri": "s3://in/a/sub/"}, "overlap"),
        ({**FOLDER, "source_uri": "s3://in/a/sub/", "destination_uri": "s3://in/a/"}, "overlap"),
        ({**FOLDER, "destination_uri": "s3://in"}, "overlap"),
        ({**INPUTS, "storage_class": "COLD"}, "Unknown storage_class 'COLD'"),
        ({**INPUTS, "max_objects": 0}, "max_objects must be a whole number of 1 or more"),
        ({**INPUTS, "max_objects": "many"}, "max_objects must be a whole number of 1 or more"),
    ],
)
def test_bad_inputs_fail_before_anything_is_touched(s3, buckets, inputs, message):
    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    assert s3.mock_calls == []


def test_defaults_apply_when_inputs_are_left_out(s3, buckets):
    buckets.objects[("in", "a/show.mxf")] = media(10)

    output = main.run({"source_uri": "s3://in/a/show.mxf", "destination_uri": "s3://out/b/"})

    assert s3.copy.call_args.kwargs["ExtraArgs"]["StorageClass"] == "STANDARD"
    assert output["moved"] is False


def test_the_requests_match_the_s3_api(s3, buckets):
    """Every parameter the component sends exists in the SDK's own description of the operations."""
    buckets.page_size = 1
    buckets.objects.update({("in", "a/1.mxf"): media(1, CacheControl="no-cache"), ("in", "a/2.mxf"): media(2)})
    buckets.tags[("in", "a/1.mxf")] = [{"Key": "project", "Value": "promo"}]

    main.run({**FOLDER, "move": True})

    model = botocore.session.get_session().get_service_model("s3")
    operations = {
        "ListObjectsV2": s3.list_objects_v2,
        "HeadObject": s3.head_object,
        "GetObjectTagging": s3.get_object_tagging,
        "DeleteObject": s3.delete_object,
    }
    requests = [(operation, call.kwargs) for operation, method in operations.items() for call in method.call_args_list]
    # The managed copy passes its arguments on to CopyObject, with the source written as "bucket/key".
    requests += [
        (
            "CopyObject",
            {"CopySource": "{Bucket}/{Key}".format(**call.args[0]), "Bucket": call.args[1], "Key": call.args[2], **call.kwargs["ExtraArgs"]},
        )
        for call in s3.copy.call_args_list
    ]
    assert {operation for operation, _request in requests} == {*operations, "CopyObject"}
    for operation, request in requests:
        report = ParamValidator().validate(request, model.operation_model(operation).input_shape)
        assert not report.has_errors(), f"{operation}: {report.generate_report()}"
