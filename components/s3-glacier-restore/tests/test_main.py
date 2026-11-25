from unittest import mock

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

import patchbae
from patchbae.testing import load_component

main = load_component(__file__)

INPUTS = {"media_uri": "s3://archive/2019/show.mxf", "tier": "Standard", "days": 7, "wait": True}
ONGOING = 'ongoing-request="true"'
RESTORED = 'ongoing-request="false", expiry-date="Fri, 21 Dec 2029 00:00:00 GMT"'
EXPIRES = "2029-12-21T00:00:00+00:00"


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


def head(storage_class="GLACIER", restore=None, **extra):
    response = {"ContentLength": 10, "StorageClass": storage_class, **extra}
    if restore:
        response["Restore"] = restore
    return response


@pytest.fixture
def s3(monkeypatch):
    client = mock.Mock()
    sleeps = []
    monkeypatch.setattr(main.boto3, "client", lambda name: {"s3": client}[name])
    monkeypatch.setattr(main.time, "sleep", sleeps.append)
    client.sleeps = sleeps
    patchbae.reset()
    return client


def test_restores_an_archived_object_and_waits_until_it_can_be_read(s3):
    s3.head_object.side_effect = [head(), head(restore=ONGOING), head(restore=ONGOING), head(restore=RESTORED)]

    output = main.run(INPUTS)

    s3.restore_object.assert_called_once_with(
        Bucket="archive",
        Key="2019/show.mxf",
        RestoreRequest={"GlacierJobParameters": {"Tier": "Standard"}, "Days": 7},
    )
    s3.head_object.assert_called_with(Bucket="archive", Key="2019/show.mxf")
    assert output == {
        "media_uri": "s3://archive/2019/show.mxf",
        "status": "available",
        "was_archived": True,
        "storage_class": "GLACIER",
        "expires_at": EXPIRES,
    }
    assert len(s3.sleeps) == 3


@pytest.mark.parametrize(
    "found",
    [
        {"ContentLength": 10},
        head("STANDARD_IA"),
        head("GLACIER_IR"),
        head("INTELLIGENT_TIERING"),
    ],
)
def test_an_object_that_is_not_archived_passes_straight_through(s3, found):
    s3.head_object.return_value = found

    output = main.run(INPUTS)

    s3.restore_object.assert_not_called()
    assert s3.sleeps == []
    assert output == {
        "media_uri": "s3://archive/2019/show.mxf",
        "status": "available",
        "was_archived": False,
        "storage_class": found.get("StorageClass", "STANDARD"),
        "expires_at": "",
    }


def test_a_redelivered_task_waits_for_the_restore_instead_of_asking_again(s3):
    s3.head_object.side_effect = [head(restore=ONGOING), head(restore=RESTORED)]

    output = main.run(INPUTS)

    s3.restore_object.assert_not_called()
    assert output["status"] == "available" and output["expires_at"] == EXPIRES


def test_an_object_that_is_already_restored_is_available_at_once(s3):
    s3.head_object.return_value = head("DEEP_ARCHIVE", restore=RESTORED)

    output = main.run(INPUTS)

    s3.restore_object.assert_not_called()
    assert s3.sleeps == []
    assert output == {
        "media_uri": "s3://archive/2019/show.mxf",
        "status": "available",
        "was_archived": True,
        "storage_class": "DEEP_ARCHIVE",
        "expires_at": EXPIRES,
    }


def test_without_wait_the_node_returns_while_the_restore_runs(s3):
    s3.head_object.return_value = head("DEEP_ARCHIVE")

    output = main.run({**INPUTS, "tier": "Bulk", "days": 3, "wait": False})

    s3.restore_object.assert_called_once_with(
        Bucket="archive",
        Key="2019/show.mxf",
        RestoreRequest={"GlacierJobParameters": {"Tier": "Bulk"}, "Days": 3},
    )
    s3.head_object.assert_called_once()
    assert s3.sleeps == []
    assert output["status"] == "in_progress" and output["was_archived"] is True and output["expires_at"] == ""

    # Running the node again later is safe: it reports the state and asks for nothing.
    s3.head_object.return_value = head("DEEP_ARCHIVE", restore=ONGOING)
    assert main.run({**INPUTS, "wait": False})["status"] == "in_progress"
    s3.head_object.return_value = head("DEEP_ARCHIVE", restore=RESTORED)
    assert main.run({**INPUTS, "wait": False})["status"] == "available"
    s3.restore_object.assert_called_once()


def test_a_restore_someone_else_just_started_counts_as_in_progress(s3):
    s3.head_object.side_effect = [head(), head(restore=RESTORED)]
    s3.restore_object.side_effect = client_error("RestoreAlreadyInProgress")

    output = main.run(INPUTS)

    assert output["status"] == "available"


def test_intelligent_tiering_is_restored_without_days_and_has_no_expiry(s3):
    archived = head("INTELLIGENT_TIERING", ArchiveStatus="ARCHIVE_ACCESS")
    s3.head_object.side_effect = [
        archived,
        head("INTELLIGENT_TIERING", restore=ONGOING, ArchiveStatus="ARCHIVE_ACCESS"),
        head("INTELLIGENT_TIERING"),
    ]

    output = main.run({**INPUTS, "tier": "Expedited"})

    s3.restore_object.assert_called_once_with(
        Bucket="archive", Key="2019/show.mxf", RestoreRequest={"GlacierJobParameters": {"Tier": "Expedited"}}
    )
    assert output == {
        "media_uri": "s3://archive/2019/show.mxf",
        "status": "available",
        "was_archived": True,
        "storage_class": "INTELLIGENT_TIERING",
        "expires_at": "",
    }


@pytest.mark.parametrize(
    "found",
    [head("DEEP_ARCHIVE"), head("INTELLIGENT_TIERING", ArchiveStatus="DEEP_ARCHIVE_ACCESS")],
)
def test_expedited_is_refused_for_deep_archive_before_anything_is_asked_for(s3, found):
    s3.head_object.return_value = found

    with pytest.raises(patchbae.ComponentFailed, match="Expedited is not available for objects in Deep Archive"):
        main.run({**INPUTS, "tier": "Expedited"})

    s3.restore_object.assert_not_called()


def test_no_expedited_capacity_fails_the_node_with_what_to_do(s3):
    s3.head_object.return_value = head()
    s3.restore_object.side_effect = client_error("GlacierExpeditedRetrievalNotAvailable")

    with pytest.raises(patchbae.ComponentFailed, match="no Expedited capacity right now"):
        main.run({**INPUTS, "tier": "Expedited"})


def test_a_missing_object_fails_the_node(s3):
    s3.head_object.side_effect = client_error("404")

    with pytest.raises(patchbae.ComponentFailed, match="s3://archive/2019/show.mxf does not exist"):
        main.run(INPUTS)

    s3.restore_object.assert_not_called()


@pytest.mark.parametrize("failing", ["head_object", "restore_object"])
def test_a_missing_permission_is_not_swallowed(s3, failing):
    s3.head_object.return_value = head()
    getattr(s3, failing).side_effect = client_error("AccessDenied")

    with pytest.raises(ClientError):
        main.run(INPUTS)


def test_the_wait_starts_at_a_minute_and_backs_off_to_five(s3):
    s3.head_object.side_effect = [head()] + [head(restore=ONGOING)] * 7 + [head(restore=RESTORED)]

    main.run(INPUTS)

    floors = [60, 90, 135, 202.5, 300, 300, 300, 300]
    assert len(s3.sleeps) == len(floors)
    assert all(floor <= slept <= floor + 5 for floor, slept in zip(floors, s3.sleeps))


@pytest.mark.parametrize(
    "header, expected",
    [
        (None, (None, "")),
        ("", (None, "")),
        ('ongoing-request="true"', (True, "")),
        ('ongoing-request="false", expiry-date="Fri, 21 Dec 2029 00:00:00 GMT"', (False, EXPIRES)),
        ('ongoing-request="false", expiry-date="Wed, 12 Aug 2020 00:00:00 GMT"', (False, "2020-08-12T00:00:00+00:00")),
        ('expiry-date="Fri, 21 Dec 2029 00:00:00 GMT", ongoing-request="false"', (False, EXPIRES)),
        ('ongoing-request="false"', (False, "")),
        ('ongoing-request="false", expiry-date="soon"', (False, "")),
    ],
)
def test_the_restore_header_is_read(header, expected):
    assert main.restore_state({"Restore": header} if header is not None else {}) == expected


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({**INPUTS, "media_uri": "archive/2019/show.mxf"}, "media_uri must be an S3 URI"),
        ({**INPUTS, "media_uri": "s3://archive/2019/"}, "media_uri must point to one object"),
        ({**INPUTS, "media_uri": "s3://archive"}, "media_uri must point to one object"),
        ({**INPUTS, "tier": "Fast"}, "Unknown tier 'Fast'"),
        ({**INPUTS, "days": 0}, "days must be a whole number of 1 or more"),
        ({**INPUTS, "days": "a week"}, "days must be a whole number of 1 or more"),
    ],
)
def test_bad_inputs_fail_before_anything_is_asked_for(s3, inputs, message):
    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    assert s3.mock_calls == []


def test_defaults_apply_when_inputs_are_left_out(s3):
    s3.head_object.side_effect = [head(), head(restore=RESTORED)]

    output = main.run({"media_uri": "s3://archive/2019/show.mxf"})

    assert s3.restore_object.call_args.kwargs["RestoreRequest"] == {"GlacierJobParameters": {"Tier": "Standard"}, "Days": 7}
    assert output["status"] == "available"


def test_the_requests_match_the_s3_api(s3):
    """Every parameter the component sends exists in the SDK's own description of the operations."""
    s3.head_object.side_effect = [head(), head(restore=RESTORED)]

    main.run({**INPUTS, "tier": "bulk"})

    model = botocore.session.get_session().get_service_model("s3")
    for operation, method in {"HeadObject": s3.head_object, "RestoreObject": s3.restore_object}.items():
        for call in method.call_args_list:
            report = ParamValidator().validate(call.kwargs, model.operation_model(operation).input_shape)
            assert not report.has_errors(), f"{operation}: {report.generate_report()}"
    tiers = model.shape_for("GlacierJobParameters").members["Tier"].enum
    assert s3.restore_object.call_args.kwargs["RestoreRequest"]["GlacierJobParameters"]["Tier"] in tiers
