# -*- coding: utf-8 -*-
# Copyright 2020 Matrix.org Federation C.I.C
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from collections import OrderedDict
from typing import Dict, List

from mock import Mock

from twisted.internet.defer import succeed

from synapse import event_auth
from synapse.api.constants import EventTypes
from synapse.api.room_versions import RoomVersions
from synapse.config.ratelimiting import FederationRateLimitConfig
from synapse.events import builder
from synapse.federation.transport import server as federation_server
from synapse.rest import admin
from synapse.rest.client.v1 import login, room
from synapse.server import HomeServer
from synapse.types import RoomAlias
from synapse.util.ratelimitutils import FederationRateLimiter

from tests.test_utils import event_injection, make_awaitable
from tests.unittest import FederatingHomeserverTestCase, HomeserverTestCase

# An identifier to use while MSC2304 is not in a stable release of the spec
KNOCK_UNSTABLE_IDENTIFIER = "xyz.amorgan.knock"

# An event type that we do not expect to be given to users knocking on a room
SECRET_STATE_EVENT_TYPE = "com.example.secret"


class FederationKnockingTestCase(FederatingHomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor, clock, homeserver):
        self.store = homeserver.get_datastore()

        # Have this homeserver auto-approve all event signature checking
        def approve_all_signature_checking(_, ev):
            return [succeed(ev[0])]

        homeserver.get_federation_server()._check_sigs_and_hashes = (
            approve_all_signature_checking
        )

        # Have this homeserver skip event auth checks.
        #
        # While this prevent membership transistion checks, that is already
        # tested elsewhere
        event_auth.check = Mock(return_value=make_awaitable(None))

        class Authenticator:
            def authenticate_request(self, request, content):
                return make_awaitable("other.example.com")

        ratelimiter = FederationRateLimiter(
            clock,
            FederationRateLimitConfig(
                window_size=1,
                sleep_limit=1,
                sleep_msec=1,
                reject_limit=1000,
                concurrent_requests=1000,
            ),
        )
        federation_server.register_servlets(
            homeserver, self.resource, Authenticator(), ratelimiter
        )

        return super().prepare(reactor, clock, homeserver)

    def test_room_state_returned_when_knocking(self):
        """
        Tests that specific, stripped state events from a room are returned after
        a remote homeserver successfully knocks on a local room.
        """
        user_id = self.register_user("u1", "you the one")
        user_token = self.login("u1", "you the one")

        fake_knocking_user_id = "@user:other.example.com"

        # Create a room with a room version that includes knocking
        room_id = self.helper.create_room_as(
            "u1",
            is_public=False,
            room_version=KNOCK_UNSTABLE_IDENTIFIER,
            tok=user_token,
        )

        # Update the join rules and add additional state to the room to check for later
        expected_room_state = send_example_state_events_to_room(
            self, self.hs, room_id, user_id
        )

        request, channel = self.make_request(
            "GET",
            "/_matrix/federation/unstable/%s/make_knock/%s/%s"
            % (KNOCK_UNSTABLE_IDENTIFIER, room_id, fake_knocking_user_id),
        )
        self.assertEquals(200, channel.code, channel.result)

        # Note: We don't expect the knock membership event to be sent over federation as
        # part of the stripped room state, as the knocking homeserver already has that
        # event. It is only done for clients during /sync

        # Extract the generated knock event json
        knock_event = channel.json_body["event"]

        # Check that the event has things we expect in it
        self.assertEquals(knock_event["room_id"], room_id)
        self.assertEquals(knock_event["sender"], fake_knocking_user_id)
        self.assertEquals(knock_event["state_key"], fake_knocking_user_id)
        self.assertEquals(knock_event["type"], EventTypes.Member)
        self.assertEquals(
            knock_event["content"]["membership"], KNOCK_UNSTABLE_IDENTIFIER
        )

        # Turn the event json dict into a proper event.
        # We won't sign it properly, but that's OK as we stub out event auth in `prepare`
        signed_knock_event = builder.create_local_event_from_event_dict(
            self.clock,
            self.hs.hostname,
            self.hs.signing_key,
            room_version=RoomVersions.MSC2403_DEV,
            event_dict=knock_event,
        )

        # Convert our proper event back to json dict format
        signed_knock_event_json = signed_knock_event.get_pdu_json(
            self.clock.time_msec()
        )

        # Send the signed knock event into the room
        request, channel = self.make_request(
            "PUT",
            "/_matrix/federation/unstable/%s/send_knock/%s/%s"
            % (KNOCK_UNSTABLE_IDENTIFIER, room_id, signed_knock_event.event_id),
            signed_knock_event_json,
        )
        self.assertEquals(200, channel.code, channel.result)

        # Check that we got the stripped room state in return
        room_state_events = channel.json_body["knock_state_events"]

        # Validate the stripped room state events
        check_knock_room_state_against_room_state(
            self, room_state_events, expected_room_state
        )


def send_example_state_events_to_room(
    testcase: HomeserverTestCase, hs: "HomeServer", room_id: str, sender: str,
) -> OrderedDict:
    """Adds some state a room. State events are those that should be sent to a knocking
    user after they knock on the room, as well as some state that *shouldn't* be sent
    to the knocking user.

    Args:
        testcase: The testcase that is currently active.
        hs: The homeserver of the sender.
        room_id: The ID of the room to send state into.
        sender: The ID of the user to send state as. Must be in the room.

    Returns:
        The OrderedDict of event types and content that a user is expected to see
        after knocking on a room.
    """
    # To set a canonical alias, we'll need to point an alias at the room first.
    canonical_alias = "#fancy_alias:test"
    testcase.get_success(
        testcase.store.create_room_alias_association(
            RoomAlias.from_string(canonical_alias), room_id, ["test"]
        )
    )

    # Send some state that we *don't* expect to be given to knocking users
    secret_state_event_type = "com.example.secret"
    testcase.get_success(
        event_injection.inject_event(
            hs,
            room_version=KNOCK_UNSTABLE_IDENTIFIER,
            room_id=room_id,
            sender=sender,
            type=secret_state_event_type,
            state_key="",
            content={"secret": "password"},
        )
    )

    # We use an OrderedDict here to ensure that the knock membership appears last.
    # Note that order only matters when sending stripped state to clients, not federated
    # homeservers.
    room_state = OrderedDict(
        [
            # We need to set the room's join rules to allow knocking
            (
                EventTypes.JoinRules,
                {"content": {"join_rule": "xyz.amorgan.knock"}, "state_key": ""},
            ),
            # Below are state events that are to be stripped and sent to clients
            (EventTypes.Name, {"content": {"name": "A cool room"}, "state_key": ""},),
            (
                EventTypes.RoomAvatar,
                {
                    "content": {
                        "info": {
                            "h": 398,
                            "mimetype": "image/jpeg",
                            "size": 31037,
                            "w": 394,
                        },
                        "url": "mxc://example.org/JWEIFJgwEIhweiWJE",
                    },
                    "state_key": "",
                },
            ),
            (
                EventTypes.RoomEncryption,
                {"content": {"algorithm": "m.megolm.v1.aes-sha2"}, "state_key": ""},
            ),
            (
                EventTypes.CanonicalAlias,
                {
                    "content": {"alias": canonical_alias, "alt_aliases": []},
                    "state_key": "",
                },
            ),
        ]
    )

    for event_type, event_dict in room_state.items():
        event_content, state_key = event_dict.values()

        testcase.get_success(
            event_injection.inject_event(
                hs,
                room_version=KNOCK_UNSTABLE_IDENTIFIER,
                room_id=room_id,
                sender=sender,
                type=event_type,
                state_key=state_key,
                content=event_content,
            )
        )

    return room_state


def check_knock_room_state_against_room_state(
    testcase: HomeserverTestCase,
    knock_room_state: List[Dict],
    expected_room_state: Dict,
) -> None:
    """Test a list of stripped room state events received over federation against an
    dict of expected state events.

    Args:
        testcase: The testcase that is currently active.
        knock_room_state: The list of room state that was received over federation.
        expected_room_state: A dict containing the room state we expect to see in
            `knock_room_state`.
    """
    for event in knock_room_state:
        event_type = event["type"]
        testcase.assertIn(event_type, expected_room_state)

        # Check the state content matches
        testcase.assertEquals(
            expected_room_state[event_type]["content"], event["content"]
        )

        # Check the state key is correct
        testcase.assertEqual(
            expected_room_state[event_type]["state_key"], event["state_key"]
        )

        # Ensure the event has been stripped
        testcase.assertNotIn("signatures", event)

        # Pop once we've found and processed a state event
        expected_room_state.pop(event_type)

    # Check that all expected state events were accounted for
    testcase.assertEqual(len(expected_room_state), 0)

    # Ensure that no excess state was included
    testcase.assertNotIn(SECRET_STATE_EVENT_TYPE, knock_room_state)
