import datetime

import pytest

import patchbae
from patchbae.testing import FakeAws, load_component

main = load_component(__file__)

TASK = patchbae.task.id
UTC = datetime.timezone.utc
FEED_ID = "a1b2c3d4e5f6g7h8i9j"
FEED_ARN = f"arn:aws:elemental-inference:eu-west-1:123456789012:feed/{FEED_ID}"
ROLE = "arn:aws:iam::123456789012:role/ElementalInferenceAccessRole"
ACTION_NAME = f"patchbae-{TASK}"
OUTPUTS = [
    {"name": "crop", "outputConfig": {"cropping": {}}, "status": "ENABLED"},
    {"name": "clips", "outputConfig": {"clipping": {"dataSourceConfiguration": {"fixtureId": "fx-2026-1125-lions-hawks"}}}, "status": "DISABLED"},
]
EMPTY = {
    "feed_id": "",
    "feed_arn": "",
    "feed_status": "",
    "data_endpoints": [],
    "output_names": [],
    "output_name": "",
    "output_status": "",
    "action_name": "",
    "scheduled_at": "",
    "fixture_id": "",
    "fixture_name": "",
    "fixture_count": 0,
    "fixtures": [],
}
FIXTURES = [
    {
        "fixtureId": "fx-2026-1125-wolves-bears",
        "name": "Wolves at Bears",
        "fixtureGroup": "Regular season",
        "scheduledStart": datetime.datetime(2026, 11, 25, 23, 30, tzinfo=UTC),
        "status": "scheduled",
        "competitors": [{"name": "Bears", "isHome": True}, {"name": "Wolves", "isHome": False}],
    },
    {
        "fixtureId": "fx-2026-1125-lions-hawks",
        "name": "Hawks at Northlight Lions",
        "fixtureGroup": "Regular season",
        "scheduledStart": datetime.datetime(2026, 11, 25, 19, 0, tzinfo=UTC),
        "status": "scheduled",
        "competitors": [{"name": "Northlight Lions", "isHome": True}, {"name": "Hawks", "isHome": False}],
    },
]


def feed(status="AVAILABLE", outputs=OUTPUTS, **extra):
    return {
        "id": FEED_ID,
        "arn": FEED_ARN,
        "name": "cup-final-2026",
        "status": status,
        "dataEndpoints": ["https://a1b2c3d4e5.data.elemental-inference.eu-west-1.amazonaws.com"],
        "outputs": outputs,
        **extra,
    }


@pytest.fixture
def inference(monkeypatch):
    aws = FakeAws()
    client = aws.client("elementalinference")
    client.respond("list_feeds", {"feeds": []})
    client.respond("create_feed", feed("CREATING"))
    client.respond("get_feed", feed())
    client.respond("update_feed", feed("UPDATING"))
    client.respond("list_schedule_actions", {"scheduleActions": []})
    client.respond("create_schedule_action", lambda request: {**{k: v for k, v in request.items() if k != "id"}, "status": "PENDING"})
    client.respond("search_fixtures", {"fixtures": FIXTURES})
    monkeypatch.setattr(main.boto3, "client", aws.client)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    patchbae.reset()
    return client


def test_creates_a_feed_with_one_output_per_feature_and_waits_for_it(inference):
    output = main.run(
        {
            "action": "create_feed",
            "feed_name": "cup-final-2026",
            "features": ["smart_crop", "clips", "subtitles", "contextual_metadata", "clips"],
            "subtitle_language": "eng-gb",
            "fixture_id": "fx-2026-1125-lions-hawks",
        }
    )

    assert inference.called("create_feed") == [
        {
            "name": "cup-final-2026",
            "tags": {"patchbae_task_id": TASK},
            "outputs": [
                {"name": "crop", "outputConfig": {"cropping": {}}, "status": "ENABLED"},
                {"name": "clips", "outputConfig": {"clipping": {"dataSourceConfiguration": {"fixtureId": "fx-2026-1125-lions-hawks"}}}, "status": "ENABLED"},
                {"name": "subtitles", "outputConfig": {"subtitling": {"language": "eng-gb"}}, "status": "ENABLED"},
                {"name": "metadata", "outputConfig": {"contextualMetadata": {}}, "status": "ENABLED"},
            ],
        }
    ]
    assert inference.called("get_feed") == [{"id": FEED_ID}]
    assert output == {
        **EMPTY,
        "feed_id": FEED_ID,
        "feed_arn": FEED_ARN,
        "feed_status": "AVAILABLE",
        "data_endpoints": ["https://a1b2c3d4e5.data.elemental-inference.eu-west-1.amazonaws.com"],
        "output_names": ["crop", "clips"],
    }


def test_crop_templates_are_passed_with_the_role_that_reads_them(inference):
    main.run(
        {
            "action": "create_feed",
            "feed_name": "cup-final-2026",
            "features": ["smart_crop"],
            "crop_template_uris": ["s3://northlight-graphics/templates/score-bug.json"],
            "access_role_arn": ROLE,
        }
    )

    (request,) = inference.called("create_feed")
    assert request["accessRoleArn"] == ROLE
    assert request["outputs"][0]["outputConfig"] == {
        "cropping": {"templateGroups": [{"name": "default", "templateUris": ["s3://northlight-graphics/templates/score-bug.json"]}]}
    }


def test_a_feed_with_the_same_name_is_reused_instead_of_created_again(inference):
    gone = {"id": "z9y8x7w6v5u4t3s2r1q", "arn": "arn:aws:elemental-inference:eu-west-1:123456789012:feed/z9y8x7w6v5u4t3s2r1q", "name": "cup-final-2026", "status": "DELETED"}
    kept = {"id": FEED_ID, "arn": FEED_ARN, "name": "cup-final-2026", "status": "ACTIVE"}
    inference.respond("list_feeds", {"feeds": [gone], "nextToken": "page-2"}, {"feeds": [kept]})
    inference.respond("get_feed", feed("ACTIVE"))

    output = main.run({"action": "create_feed", "feed_name": "cup-final-2026", "features": ["clips"]})

    assert inference.called("list_feeds") == [{"maxResults": 100}, {"maxResults": 100, "nextToken": "page-2"}]
    assert inference.called("create_feed") == []
    assert (output["feed_id"], output["feed_status"]) == (FEED_ID, "ACTIVE")


def test_a_feed_elemental_inference_refuses_fails_the_node_with_its_reason(inference):
    inference.respond("create_feed", FakeAws.error("ServiceQuotaExceededException", "Feed limit reached"))

    with pytest.raises(patchbae.ComponentFailed, match="would not create the feed: Feed limit reached"):
        main.run({"action": "create_feed", "feed_name": "cup-final-2026", "features": ["clips"]})


def test_switches_one_output_on_now_and_leaves_the_others_as_they_are(inference):
    inference.respond("get_feed", feed("ACTIVE", accessRoleArn=ROLE))

    output = main.run({"action": "set_output_status", "feed_id": FEED_ID, "output_name": "clips"})

    assert inference.called("update_feed") == [
        {"id": FEED_ID, "name": "cup-final-2026", "accessRoleArn": ROLE, "outputs": [OUTPUTS[0], {**OUTPUTS[1], "status": "ENABLED"}]}
    ]
    assert output == {**EMPTY, "feed_id": FEED_ID, "output_name": "clips", "output_status": "ENABLED", "scheduled_at": "now"}


def test_an_output_that_already_has_the_status_is_left_alone(inference):
    output = main.run({"action": "set_output_status", "feed_id": FEED_ID, "output_name": "clips", "enabled": False})

    assert inference.called("update_feed") == []
    assert output["output_status"] == "DISABLED"


def test_schedules_a_switch_at_a_set_time(inference):
    output = main.run(
        {"action": "set_output_status", "feed_id": FEED_ID, "output_name": "crop", "enabled": False, "at": "2026-11-25T22:15:00.750+01:00"}
    )

    assert inference.called("create_schedule_action") == [
        {
            "id": FEED_ID,
            "actionName": ACTION_NAME,
            "startConfiguration": {"fixedModeScheduleActionStartSettings": {"scheduledAt": datetime.datetime(2026, 11, 25, 21, 15, tzinfo=UTC)}},
            "operations": [{"updateOutputStatus": {"outputName": "crop", "status": "DISABLED"}}],
        }
    ]
    assert inference.called("update_feed") == []
    assert output == {
        **EMPTY,
        "feed_id": FEED_ID,
        "output_name": "crop",
        "output_status": "DISABLED",
        "action_name": ACTION_NAME,
        "scheduled_at": "2026-11-25T21:15:00Z",
    }


def test_a_redelivered_task_finds_its_schedule_action_instead_of_adding_another(inference):
    earlier = {
        "actionName": ACTION_NAME,
        "startConfiguration": {"fixedModeScheduleActionStartSettings": {"scheduledAt": datetime.datetime(2026, 11, 25, 21, 15, tzinfo=UTC)}},
        "operations": [{"updateOutputStatus": {"outputName": "crop", "status": "DISABLED"}}],
        "status": "PENDING",
    }
    inference.respond("list_schedule_actions", {"scheduleActions": [earlier]})

    output = main.run({"action": "set_output_status", "feed_id": FEED_ID, "output_name": "crop", "enabled": False, "at": "2026-11-25T21:15:00Z"})

    assert inference.called("create_schedule_action") == []
    assert output["action_name"] == ACTION_NAME


def test_an_unknown_output_fails_with_the_names_the_feed_has(inference):
    with pytest.raises(patchbae.ComponentFailed, match="has no output named 'subtitles'. Its outputs: crop, clips."):
        main.run({"action": "set_output_status", "feed_id": FEED_ID, "output_name": "subtitles"})

    assert inference.called("update_feed") == []


def test_a_change_elemental_inference_refuses_fails_the_node_with_its_reason(inference):
    inference.respond("update_feed", FakeAws.error("ConflictException", "Feed is being updated", 409))

    with pytest.raises(patchbae.ComponentFailed, match="would not change the feed: Feed is being updated"):
        main.run({"action": "set_output_status", "feed_id": FEED_ID, "output_name": "clips"})


def test_a_feed_that_does_not_exist_fails_the_node(inference):
    inference.respond("get_feed", FakeAws.error("ResourceNotFoundException", status=404))

    with pytest.raises(patchbae.ComponentFailed, match=f"Feed {FEED_ID} was not found"):
        main.run({"action": "set_output_status", "feed_id": FEED_ID, "output_name": "clips"})


def test_finds_fixtures_and_returns_the_earliest_first(inference):
    output = main.run({"action": "find_fixture", "sport": "basketball", "date": "2026-11-25"})

    assert inference.called("search_fixtures") == [{"sport": "basketball", "startDate": "2026-11-25", "maxResults": 100}]
    assert output == {
        **EMPTY,
        "fixture_id": "fx-2026-1125-lions-hawks",
        "fixture_name": "Hawks at Northlight Lions",
        "fixture_count": 2,
        "fixtures": [
            {
                "fixture_id": "fx-2026-1125-lions-hawks",
                "name": "Hawks at Northlight Lions",
                "group": "Regular season",
                "scheduled_start": "2026-11-25T19:00:00Z",
                "status": "scheduled",
                "competitors": ["Northlight Lions", "Hawks"],
            },
            {
                "fixture_id": "fx-2026-1125-wolves-bears",
                "name": "Wolves at Bears",
                "group": "Regular season",
                "scheduled_start": "2026-11-25T23:30:00Z",
                "status": "scheduled",
                "competitors": ["Bears", "Wolves"],
            },
        ],
    }
    assert [entry["level"] for entry in patchbae.logged if "more than one fixture" in entry["message"]] == ["warn"]


def test_a_search_over_several_days_for_a_team_reads_every_page(inference):
    inference.respond("search_fixtures", {"fixtures": FIXTURES[:1], "nextToken": "page-2"}, {"fixtures": FIXTURES[1:]})

    output = main.run(
        {"action": "find_fixture", "sport": "american-football", "date": "2026-11-25", "end_date": "2026-11-29", "competitors": ["Northlight Lions", " "]}
    )

    first, second = inference.called("search_fixtures")
    assert first == {
        "sport": "american-football",
        "startDate": "2026-11-25",
        "endDate": "2026-11-29",
        "maxResults": 100,
        "filters": [{"name": "COMPETITOR", "values": ["Northlight Lions"]}],
    }
    assert second["nextToken"] == "page-2"
    assert output["fixture_count"] == 2


def test_no_fixture_fails_the_node_and_says_what_was_searched(inference):
    inference.respond("search_fixtures", {"fixtures": []})

    with pytest.raises(patchbae.ComponentFailed, match="No basketball fixture with Lions or Hawks was found on 2026-11-25 to 2026-11-26"):
        main.run({"action": "find_fixture", "sport": "basketball", "date": "2026-11-25", "end_date": "2026-11-26", "competitors": ["Lions", "Hawks"]})


def test_a_missing_permission_is_not_swallowed(inference):
    inference.respond("list_feeds", FakeAws.error("AccessDeniedException", status=403))

    with pytest.raises(main.ClientError):
        main.run({"action": "create_feed", "feed_name": "cup-final-2026", "features": ["clips"]})


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({"action": "delete_feed"}, "Unknown action 'delete_feed'"),
        ({"action": "create_feed", "feed_name": "cup final", "features": ["clips"]}, "feed_name must be"),
        ({"action": "create_feed", "feed_name": "cup-final", "features": []}, "features must list"),
        ({"action": "create_feed", "feed_name": "cup-final", "features": ["highlights"]}, "Unknown feature 'highlights'"),
        ({"action": "create_feed", "feed_name": "cup-final", "features": ["subtitles"], "subtitle_language": "swe"}, "Unknown subtitle_language 'swe'"),
        ({"action": "create_feed", "feed_name": "cup-final", "features": ["smart_crop"], "crop_template_uris": ["score.json"]}, "holds S3 URIs"),
        ({"action": "create_feed", "feed_name": "cup-final", "features": ["smart_crop"], "crop_template_uris": ["s3://bucket/score.json"]}, "access_role_arn must name"),
        ({"action": "create_feed", "feed_name": "cup-final", "features": ["clips"], "access_role_arn": "role"}, "access_role_arn must be the ARN"),
        ({"action": "set_output_status", "feed_id": "cup-final", "output_name": "clips"}, "feed_id must be the feed's ID"),
        ({"action": "set_output_status", "feed_id": FEED_ID}, "output_name must name"),
        ({"action": "set_output_status", "feed_id": FEED_ID, "output_name": "clips", "at": "kick-off"}, "at must be a UTC time"),
        ({"action": "find_fixture", "sport": "football", "date": "2026-11-25"}, "sport must be basketball or american-football"),
        ({"action": "find_fixture", "sport": "basketball", "date": "25/11/2026"}, "written as YYYY-MM-DD"),
        ({"action": "find_fixture", "sport": "basketball", "date": "2026-11-25", "end_date": "2026-11-24"}, "end_date must be on or after date"),
    ],
)
def test_bad_inputs_fail_before_anything_is_changed(inference, inputs, message):
    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    assert inference.calls == []
