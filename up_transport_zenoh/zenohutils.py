"""
SPDX-FileCopyrightText: 2024 Contributors to the Eclipse Foundation

See the NOTICE file(s) distributed with this work for additional
information regarding copyright ownership.

This program and the accompanying materials are made available under the
terms of the Apache License Version 2.0 which is available at

    http://www.apache.org/licenses/LICENSE-2.0

SPDX-License-Identifier: Apache-2.0
"""

import logging
from enum import IntFlag
from typing import Union

from uprotocol.communication.ustatuserror import UStatusError
from uprotocol.uri.factory.uri_factory import UriFactory
from uprotocol.v1.uattributes_pb2 import (
    UAttributes,
    UPriority,
)
from uprotocol.v1.ucode_pb2 import UCode
from uprotocol.v1.uri_pb2 import UUri
from uprotocol.v1.ustatus_pb2 import UStatus
from zenoh import Priority, ZBytes

UATTRIBUTE_VERSION: int = 1

# Configure the logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')


class MessageFlag(IntFlag):
    PUBLISH = 1
    NOTIFICATION = 2
    REQUEST = 4
    RESPONSE = 8


class ZenohUtils:
    @staticmethod
    def uri_to_zenoh_key(authority_name: str, uri: UUri) -> str:
        authority = authority_name if not uri.authority_name else uri.authority_name
        ue_id = "*" if uri.ue_id == UriFactory.WILDCARD_ENTITY_ID else f"{uri.ue_id:X}"
        ue_version_major = (
            "*" if uri.ue_version_major == UriFactory.WILDCARD_ENTITY_VERSION else f"{uri.ue_version_major:X}"
        )
        resource_id = "*" if uri.resource_id == UriFactory.WILDCARD_RESOURCE_ID else f"{uri.resource_id:X}"
        return f"{authority}/{ue_id}/{ue_version_major}/{resource_id}"

    @staticmethod
    def get_uauth_from_uuri(uri: UUri) -> Union[str, UStatus]:
        if uri.authority:
            try:
                authority_bytes = uri.authority.SerializeToString()
                # Iterate over each byte and formate it as a two digit hexa decimal
                return "".join(f"{c:02x}" for c in authority_bytes)
            except Exception as e:
                msg = f"Unable to transform UAuthority into micro form: {e}"
                logging.debug(msg)
                return UStatus(code=UCode.INVALID_ARGUMENT, message=msg)
        else:
            msg = "UAuthority is empty"
            logging.debug(msg)
            return UStatus(code=UCode.INVALID_ARGUMENT, message=msg)

    @staticmethod
    def to_zenoh_key_string(authority_name: str, src_uri: UUri, dst_uri: UUri = None) -> str:
        src = ZenohUtils.uri_to_zenoh_key(authority_name, src_uri)
        dst = ZenohUtils.uri_to_zenoh_key(authority_name, dst_uri) if dst_uri and dst_uri != UUri() else "{}/{}/{}/{}"
        return f"up/{src}/{dst}"

    @staticmethod
    def map_zenoh_priority(upriority: UPriority) -> Priority:
        mapping = {
            UPriority.UPRIORITY_CS0: Priority.BACKGROUND,
            UPriority.UPRIORITY_CS1: Priority.DATA_LOW,
            UPriority.UPRIORITY_CS2: Priority.DATA,
            UPriority.UPRIORITY_CS3: Priority.DATA_HIGH,
            UPriority.UPRIORITY_CS4: Priority.INTERACTIVE_LOW,
            UPriority.UPRIORITY_CS5: Priority.INTERACTIVE_HIGH,
            UPriority.UPRIORITY_CS6: Priority.REAL_TIME,
            UPriority.UPRIORITY_UNSPECIFIED: Priority.DATA_LOW,
        }
        return mapping[upriority]

    @staticmethod
    def uattributes_to_attachment(uattributes: UAttributes):
        # Convert the version number to bytes (assuming 1 as in the Rust example)
        version_bytes = UATTRIBUTE_VERSION.to_bytes(1, byteorder='little')

        # Serialize the UAttributes to bytes
        uattributes_bytes = uattributes.SerializeToString()

        # Combine version bytes and uattributes bytes into one list of bytes
        attachment_bytes = [version_bytes, uattributes_bytes]

        # Convert the combined bytes to ZBytes
        return attachment_bytes

    @staticmethod
    def attachment_to_uattributes(attachment: ZBytes) -> UAttributes:
        try:
            # Convert ZBytes to a list of bytes
            attachment_bytes = attachment.deserialize(list)

            # Ensure there is at least one byte for the version
            if len(attachment_bytes) < 1:
                msg = "Unable to get the UAttributes version"
                logging.debug(msg)
                raise UStatusError.from_code_message(code=UCode.INVALID_ARGUMENT, message=msg)

            # Check the version
            version = int.from_bytes(bytes(attachment_bytes[0]), byteorder='big')
            if version != UATTRIBUTE_VERSION:
                msg = f"UAttributes version is {version} (should be {UATTRIBUTE_VERSION})"
                logging.debug(msg)
                raise UStatusError.from_code_message(code=UCode.INVALID_ARGUMENT, message=msg)

            # Get the attributes from the remaining bytes
            uattributes_data = bytes(attachment_bytes[1])
            if not uattributes_data:
                msg = "Unable to get the UAttributes"
                logging.debug(msg)
                raise UStatusError.from_code_message(code=UCode.INVALID_ARGUMENT, message=msg)

            # Parse the UAttributes from the bytes
            uattributes = UAttributes()
            uattributes.ParseFromString(uattributes_data)

            return uattributes

        except Exception as e:
            msg = f"Failed to convert Attachment to UAttributes: {str(e)}"
            logging.debug(msg)
            raise UStatusError.from_code_message(code=UCode.INVALID_ARGUMENT, message=msg)

    @staticmethod
    def get_listener_message_type(source_uuri: UUri, sink_uuri: UUri = None) -> Union[MessageFlag, UStatusError]:
        """
        The table for mapping resource ID to message type:

        |   src rid   | sink rid | Publish | Notification | Request | Response |
        |-------------|----------|---------|--------------|---------|----------|
        | [8000-FFFF) |   None   |    V    |              |         |          |
        | [8000-FFFF) |     0    |         |      V       |         |          |
        |      0      | (0-8000) |         |              |    V    |          |
        |   (0-8000)  |     0    |         |              |         |    V     |
        |     FFFF    |     0    |         |      V       |         |    V     |
        |     FFFF    | (0-8000) |         |              |    V    |          |
        |      0      |   FFFF   |         |              |    V    |          |
        |   (0-8000)  |   FFFF   |         |              |         |    V     |
        | [8000-FFFF) |   FFFF   |         |      V       |         |          |
        |     FFFF    |   FFFF   |         |      V       |    V    |    V     |

        Some organization:
        - Publish: {[8000-FFFF), None}
        - Notification: {[8000-FFFF), 0}, {[8000-FFFF), FFFF}, {FFFF, 0}, {FFFF, FFFF}
        - Request: {0, (0-8000)}, {0, FFFF}, {FFFF, (0-8000)}, {FFFF, FFFF}
        - Response: {(0-8000), 0}, {(0-8000), FFFF}, (FFFF, 0), {FFFF, FFFF}

        :param source_uuri: The source UUri.
        :param sink_uuri: Optional sink UUri for request-response types.
        :return: MessageFlag indicating the type of message.
        :raises Exception: If the combination of source UUri and sink UUri is invalid.
        """
        flag = MessageFlag(0)

        rpc_range = range(1, 0x7FFF)
        nonrpc_range = range(0x8000, 0xFFFE)
        wildcard_resource_id = UriFactory.WILDCARD_RESOURCE_ID

        src_resource = source_uuri.resource_id

        # Notification / Request / Response
        if sink_uuri:
            dst_resource = sink_uuri.resource_id

            if (
                (src_resource in nonrpc_range and dst_resource == 0)
                or (src_resource in nonrpc_range and dst_resource == wildcard_resource_id)
                or (src_resource == wildcard_resource_id and dst_resource == 0)
                or (src_resource == wildcard_resource_id and dst_resource == wildcard_resource_id)
            ):
                flag |= MessageFlag.NOTIFICATION

            if (
                (src_resource == 0 and dst_resource in rpc_range)
                or (src_resource == 0 and dst_resource == wildcard_resource_id)
                or (src_resource == wildcard_resource_id and dst_resource in rpc_range)
                or (src_resource == wildcard_resource_id and dst_resource == wildcard_resource_id)
            ):
                flag |= MessageFlag.REQUEST

            if (
                (src_resource in rpc_range and dst_resource == 0)
                or (src_resource in rpc_range and dst_resource == wildcard_resource_id)
                or (src_resource == wildcard_resource_id and dst_resource == 0)
                or (src_resource == wildcard_resource_id and dst_resource == wildcard_resource_id)
            ):
                flag |= MessageFlag.RESPONSE

            if dst_resource == wildcard_resource_id and (
                src_resource in nonrpc_range or src_resource == wildcard_resource_id
            ):
                flag |= MessageFlag.PUBLISH

        # Publish
        elif src_resource in nonrpc_range or src_resource == wildcard_resource_id:
            flag |= MessageFlag.PUBLISH

        # Error handling
        if flag == MessageFlag(0):
            raise UStatusError.from_code_message(
                code=UCode.INTERNAL, message="Wrong combination of source UUri and sink " "UUri"
            )
        else:
            return flag
