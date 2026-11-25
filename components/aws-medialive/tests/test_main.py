import pytest

import patchbae
from patchbae.testing import FakeAws, load_component

main = load_component(__file__)

TASK = patchbae.task.id
ACTION_NAME = f"patchbae-{TASK}"
CHANNEL = {
    "Id": "1234567",
    "Arn": "arn:aws:medialive:eu-north-1:123456789012:channel:1234567",
    "Name": "northlight-sports-1",
    "ChannelClass": "STANDARD",
    "PipelinesRunningCount": 0,
    "InputAttachments": [
        {"InputAttachmentName": "studio-a", "InputId": "7654321"},
        {"InputAttachmentName": "studio-b", "InputId": "7654322"},
    ],
}


def channel(state):
    return {**CHANNEL, "State": state}


@pytest.fixture
def medialive(monkeypatch):
    aws = FakeAws()
    client = aws.client("medialive")
    client.respond("start_channel", channel("STARTING"))
    client.respond("stop_channel", channel("STOPPING"))
    client.respond("describe_schedule", {"ScheduleActions": []})
    client.respond("batch_update_schedule", {"Creates": {"ScheduleActions": []}})
    monkeypatch.setattr(main.boto3, "client", aws.client)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    patchbae.reset()
    return client


def test_starts_an_idle_channel_and_waits_until_it_runs(medialive):
    medialive.respond("describe_channel", channel("IDLE"), channel("STARTING"), channel("RUNNING"))

    output = main.run({"channel_id": "1234567", "action": "start_channel"})

    assert medialive.called("start_channel") == [{"ChannelId": "1234567"}]
    assert output == {
        "channel_id": "1234567",
        "channel_arn": "arn:aws:medialive:eu-north-1:123456789012:channel:1234567",
        "channel_name": "northlight-sports-1",
        "state": "RUNNING",
        "action_name": "",
        "action_time": "",
    }


def test_a_running_channel_is_not_started_again(medialive):
    medialive.respond("describe_channel", channel("RUNNING"))

    output = main.run({"channel_id": "1234567", "action": "start_channel"})

    assert medialive.called("start_channel") == []
    assert output["state"] == "RUNNING"


def test_a_redelivered_start_waits_for_the_channel_that_is_already_starting(medialive):
    medialive.respond("describe_channel", channel("STARTING"), channel("RUNNING"))

    output = main.run({"channel_id": "1234567", "action": "start_channel"})

    assert medialive.called("start_channel") == []
    assert output["state"] == "RUNNING"


def test_start_without_waiting_returns_as_soon_as_the_channel_is_starting(medialive):
    medialive.respond("describe_channel", channel("IDLE"))

    output = main.run({"channel_id": "1234567", "action": "start_channel", "wait": False})

    assert len(medialive.called("describe_channel")) == 1
    assert output["state"] == "STARTING"


def test_a_channel_that_is_still_stopping_is_started_once_it_is_idle(medialive):
    medialive.respond("describe_channel", channel("STOPPING"), channel("IDLE"), channel("RUNNING"))

    main.run({"channel_id": "1234567", "action": "start_channel"})

    assert len(medialive.called("start_channel")) == 1


def test_stops_a_running_channel_and_waits_until_it_is_idle(medialive):
    medialive.respond("describe_channel", channel("RUNNING"), channel("STOPPING"), channel("IDLE"))

    output = main.run({"channel_id": "1234567", "action": "stop_channel"})

    assert medialive.called("stop_channel") == [{"ChannelId": "1234567"}]
    assert output["state"] == "IDLE"


def test_an_idle_channel_is_not_stopped_again(medialive):
    medialive.respond("describe_channel", channel("IDLE"))

    main.run({"channel_id": "1234567", "action": "stop_channel"})

    assert medialive.called("stop_channel") == []


@pytest.mark.parametrize("action, verb", [("start_channel", "started"), ("stop_channel", "stopped")])
def test_a_broken_channel_fails_the_node(medialive, action, verb):
    medialive.respond("describe_channel", channel("UPDATE_FAILED"))

    with pytest.raises(patchbae.ComponentFailed, match=f"is UPDATE_FAILED and cannot be {verb}"):
        main.run({"channel_id": "1234567", "action": action})


def test_inserts_an_ad_break_now(medialive):
    medialive.respond("describe_channel", channel("RUNNING"))

    output = main.run({"channel_id": "1234567", "action": "insert_ad_break", "ad_break_seconds": 120})

    (request,) = medialive.called("batch_update_schedule")
    (action,) = request["Creates"]["ScheduleActions"]
    assert request["ChannelId"] == "1234567"
    assert action["ActionName"] == ACTION_NAME
    assert action["ScheduleActionStartSettings"] == {"ImmediateModeScheduleActionStartSettings": {}}
    splice = action["ScheduleActionSettings"]["Scte35SpliceInsertSettings"]
    assert splice["Duration"] == 120 * 90_000
    assert 0 <= splice["SpliceEventId"] < 2**32
    assert output["action_name"] == ACTION_NAME
    assert output["action_time"] == "immediate"
    assert output["state"] == "RUNNING"


def test_an_ad_break_without_a_length_runs_until_a_return_to_network(medialive):
    medialive.respond("describe_channel", channel("RUNNING"))

    main.run({"channel_id": "1234567", "action": "insert_ad_break"})

    (action,) = medialive.called("batch_update_schedule")[0]["Creates"]["ScheduleActions"]
    assert "Duration" not in action["ScheduleActionSettings"]["Scte35SpliceInsertSettings"]


def test_the_same_task_always_asks_for_the_same_splice_event(medialive):
    medialive.respond("describe_channel", channel("RUNNING"))

    main.run({"channel_id": "1234567", "action": "insert_ad_break"})
    main.run({"channel_id": "1234567", "action": "insert_ad_break"})

    first, second = [
        request["Creates"]["ScheduleActions"][0]["ScheduleActionSettings"]["Scte35SpliceInsertSettings"]["SpliceEventId"]
        for request in medialive.called("batch_update_schedule")
    ]
    assert first == second


def test_switches_input_at_a_set_time(medialive):
    medialive.respond("describe_channel", channel("RUNNING"))

    output = main.run(
        {"channel_id": "1234567", "action": "switch_input", "input_attachment": "studio-b", "start_time": "2026-11-25T20:30:00+01:00"}
    )

    (action,) = medialive.called("batch_update_schedule")[0]["Creates"]["ScheduleActions"]
    assert action["ScheduleActionSettings"] == {"InputSwitchSettings": {"InputAttachmentNameReference": "studio-b"}}
    assert action["ScheduleActionStartSettings"] == {"FixedModeScheduleActionStartSettings": {"Time": "2026-11-25T19:30:00.000Z"}}
    assert output["action_time"] == "2026-11-25T19:30:00.000Z"


def test_an_unknown_input_attachment_fails_with_the_names_the_channel_has(medialive):
    medialive.respond("describe_channel", channel("RUNNING"))

    with pytest.raises(patchbae.ComponentFailed, match="no input attachment named 'studio-c'. Its input attachments: studio-a, studio-b."):
        main.run({"channel_id": "1234567", "action": "switch_input", "input_attachment": "studio-c"})

    assert medialive.called("batch_update_schedule") == []


def test_a_redelivered_task_finds_its_schedule_action_instead_of_adding_another(medialive):
    medialive.respond("describe_channel", channel("RUNNING"))
    earlier = {
        "ActionName": ACTION_NAME,
        "ScheduleActionSettings": {"Scte35SpliceInsertSettings": {"SpliceEventId": 1}},
        "ScheduleActionStartSettings": {"ImmediateModeScheduleActionStartSettings": {}},
    }
    other = {**earlier, "ActionName": "someone-elses-action"}
    medialive.respond("describe_schedule", {"ScheduleActions": [other], "NextToken": "page-2"}, {"ScheduleActions": [earlier]})

    output = main.run({"channel_id": "1234567", "action": "insert_ad_break"})

    assert medialive.called("describe_schedule") == [{"ChannelId": "1234567"}, {"ChannelId": "1234567", "NextToken": "page-2"}]
    assert medialive.called("batch_update_schedule") == []
    assert output["action_name"] == ACTION_NAME


def test_a_schedule_action_medialive_refuses_fails_the_node_with_its_reason(medialive):
    medialive.respond("describe_channel", channel("IDLE"))
    medialive.respond("batch_update_schedule", FakeAws.error("UnprocessableEntityException", "Channel must be running for immediate actions", 422))

    with pytest.raises(patchbae.ComponentFailed, match="MediaLive refused the schedule action: Channel must be running"):
        main.run({"channel_id": "1234567", "action": "insert_ad_break"})


def test_a_channel_that_does_not_exist_fails_the_node(medialive):
    medialive.respond("describe_channel", FakeAws.error("NotFoundException", status=404))

    with pytest.raises(patchbae.ComponentFailed, match="Channel 1234567 was not found in this region"):
        main.run({"channel_id": "1234567", "action": "start_channel"})


def test_a_missing_permission_is_not_swallowed(medialive):
    medialive.respond("describe_channel", FakeAws.error("ForbiddenException", status=403))

    with pytest.raises(main.ClientError):
        main.run({"channel_id": "1234567", "action": "stop_channel"})


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({"channel_id": "arn:aws:medialive:eu-north-1:123456789012:channel:1234567", "action": "start_channel"}, "numeric ID"),
        ({"channel_id": "1234567", "action": "restart"}, "Unknown action 'restart'"),
        ({"channel_id": "1234567", "action": "switch_input"}, "input_attachment must name"),
        ({"channel_id": "1234567", "action": "insert_ad_break", "ad_break_seconds": 0}, "above 0"),
        ({"channel_id": "1234567", "action": "insert_ad_break", "start_time": "tomorrow at eight"}, "must be a UTC time"),
        ({"channel_id": "1234567", "action": "insert_ad_break", "start_time": "2026-11-25T19:30:00"}, "must be a UTC time"),
    ],
)
def test_bad_inputs_fail_before_anything_is_changed(medialive, inputs, message):
    medialive.respond("describe_channel", channel("RUNNING"))

    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    assert [name for name, _parameters in medialive.calls] == []
