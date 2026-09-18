"""Devin wire format: Cascade protobuf messages over the Connect protocol.

Devin serves chat from Codeium's Cascade backend (``server.codeium.com``) using
Connect over HTTP/1.1. Unary calls (sign-in exchange, model list, router
assignment) are a bare protobuf body; the chat call is a server stream of
5-byte-framed messages ending in a JSON trailer that carries any error.

Only the fields HX reads or writes are named here, with their numbers from the
``exa.*`` schemas the Devin CLI ships. Kept apart from the transport so every
encoding decision is testable without a socket.
"""

from __future__ import annotations

import gzip
import json
import struct
import sys
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field

from hx.providers.protowire import Fields, WireError, Writer, parse

DEFAULT_BASE_URL = "https://server.codeium.com"

AUTH_PATH = "/exa.auth_pb.AuthService/GetUserJwt"
MODEL_CONFIGS_PATH = "/exa.api_server_pb.ApiServerService/GetCliModelConfigs"
ASSIGN_MODEL_PATH = "/exa.api_server_pb.ApiServerService/AssignModel"
CHAT_PATH = "/exa.api_server_pb.ApiServerService/GetChatMessage"

SESSION_TOKEN_PREFIX = "devin-session-token$"

_OS = "darwin" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else "linux"

CLI_IDENTITY = {"ide_name": "devin-cli", "ide_type": "chisel", "version": "3000.6.2"}
"""The released Devin CLI, as chat requests present it.

The backend gates behaviour on this tuple: ``chisel`` is what unlocks router
assignment and the CLI model surface. Raise the version when HX has been
checked against a newer CLI."""

DISCOVERY_IDENTITY = {"ide_name": "chisel", "ide_type": "", "version": "0.0.0-dev"}
"""The identity the CLI lists models under. It - not the chat identity - is the
one the server answers with the full per-account catalogue."""

UNARY_HEADERS = {
    "content-type": "application/proto",
    "connect-protocol-version": "1",
    "accept": "*/*",
}

STREAM_HEADERS = {
    "content-type": "application/connect+proto",
    "connect-protocol-version": "1",
    "connect-content-encoding": "gzip",
    "connect-accept-encoding": "gzip",
    # Frames are gzipped individually; a gzipped HTTP body around them would
    # have to be fully buffered before a single frame could be read.
    "accept-encoding": "identity",
}

DEFAULT_STOP_PATTERNS = (
    "<|user|>",
    "<|bot|>",
    "<|context_request|>",
    "<|endoftext|>",
    "<|end_of_turn|>",
)

# ChatMessageSource
SOURCE_USER = 1
SOURCE_SYSTEM = 2
SOURCE_TOOL = 4

# ChatMessageRequestType / ConversationalPlannerMode / CacheControlType
REQUEST_TYPE_CASCADE = 5
PLANNER_MODE_DEFAULT = 1
CACHE_CONTROL_EPHEMERAL = 1

# StopReason
STOP_MAX_TOKENS = 3

# DisplayOption. The CLI schema stops at 4; 6-8 are plain int32s on the wire.
DISPLAY_MODEL_ROUTER = 3
DISPLAY_QUICK_REVIEW = 4
DISPLAY_INTERNAL_DEFAULT = 6
DISPLAY_UNCLASSIFIED = 7
DISPLAY_NORMAL = 8

SUPPORTED_MODEL_DISPLAYS = (
    DISPLAY_MODEL_ROUTER,
    DISPLAY_QUICK_REVIEW,
    DISPLAY_INTERNAL_DEFAULT,
    DISPLAY_UNCLASSIFIED,
    DISPLAY_NORMAL,
)
"""Asking for the internal slots is what makes the server send its whole
catalogue; the internal entries are then filtered out here, as the CLI does."""

CONNECT_COMPRESSED = 0x01
CONNECT_END_STREAM = 0x02

MAX_FRAME_BYTES = 16 * 1024 * 1024
"""Cap on one frame. The length prefix is four bytes the peer controls, so
without this a corrupt one could make the reader buffer gigabytes."""


def session_token(token: str) -> str:
    """The token as the wire carries it: the scheme prefix is required."""
    if not token or token.startswith(SESSION_TOKEN_PREFIX):
        return token
    return f"{SESSION_TOKEN_PREFIX}{token}"


def metadata(
    token: str,
    *,
    user_jwt: str = "",
    discovery: bool = False,
) -> Writer:
    identity = DISCOVERY_IDENTITY if discovery else CLI_IDENTITY
    md = Writer()
    md.put_str(1, identity["ide_name"])
    md.put_str(2, identity["version"])  # extension_version
    md.put_str(3, session_token(token))
    md.put_str(4, "en")
    md.put_str(5, _OS)
    md.put_str(7, identity["version"])  # ide_version
    md.put_str(12, "chisel")  # extension_name
    md.put_str(21, user_jwt)
    md.put_str(28, identity["ide_type"])
    if discovery:
        md.put_packed_uints(30, SUPPORTED_MODEL_DISPLAYS)
    return md


# -- unary calls ----------------------------------------------------------------


def user_jwt_request(token: str) -> bytes:
    request = Writer()
    request.put_message(1, metadata(token))
    return request.finish()


@dataclass(frozen=True, slots=True)
class UserJwt:
    jwt: str
    base_url: str | None
    """An enterprise account's own API server, when it has one."""


def parse_user_jwt(payload: bytes) -> UserJwt:
    fields = decode_unary(payload)
    custom = fields.text(2).strip().rstrip("/")
    return UserJwt(jwt=fields.text(1), base_url=custom or None)


def model_configs_request(token: str) -> bytes:
    request = Writer()
    request.put_message(1, metadata(token, discovery=True))
    return request.finish()


def assign_model_request(
    token: str, router_uid: str, cascade_id: str, prompt: ChatPrompt | None
) -> bytes:
    request = Writer()
    request.put_message(1, metadata(token))
    request.put_str(2, router_uid)
    request.put_str(3, cascade_id)
    if prompt is not None:
        request.put_message(5, prompt.encode())
    return request.finish()


@dataclass(frozen=True, slots=True)
class Assignment:
    jwt: str
    model_uid: str


def parse_assignment(payload: bytes) -> Assignment:
    assignment = decode_unary(payload).message(1)
    if assignment is None:
        return Assignment(jwt="", model_uid="")
    return Assignment(jwt=assignment.text(1), model_uid=assignment.text(2))


def decode_unary(payload: bytes) -> Fields:
    """A unary response body, which some edges gzip and some do not.

    Raises:
        WireError: when neither reading decodes.
    """
    try:
        return parse(payload)
    except WireError:
        try:
            return parse(gzip.decompress(payload))
        except (OSError, EOFError, zlib.error) as exc:
            raise WireError("response is neither protobuf nor gzipped protobuf") from exc


# -- model catalogue ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FamilyEntry:
    key: str
    name: str
    order: int


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """One ``ClientModelConfig``, reduced to what the catalogue reads."""

    uid: str
    label: str
    disabled: bool = False
    max_tokens: int = 0
    max_output_tokens: int = 0
    has_features: bool = False
    supports_tools: bool = False
    supports_thinking: bool = False
    display_option: int = 0
    is_model_router: bool = False
    harness_uids: tuple[str, ...] = ()
    family_label: str = ""
    family_entries: tuple[FamilyEntry, ...] = ()
    is_default_in_family: bool = False


def parse_model_configs(payload: bytes) -> list[ModelConfig]:
    return [_model_config(entry) for entry in decode_unary(payload).messages(1)]


def _model_config(fields: Fields) -> ModelConfig:
    info = fields.message(23)
    features = info.message(6) if info is not None else None
    family = fields.message(30)
    entries: list[FamilyEntry] = []
    if family is not None:
        for entry in family.messages(2):
            value = entry.message(2)
            if value is None:
                continue
            entries.append(FamilyEntry(entry.text(1), value.text(2), value.int32(1)))
    return ModelConfig(
        uid=fields.text(22).strip(),
        label=fields.text(1).strip(),
        disabled=fields.flag(4),
        max_tokens=fields.int32(18),
        max_output_tokens=info.int32(13) if info is not None else 0,
        has_features=features is not None,
        supports_tools=features.flag(12) if features is not None else False,
        supports_thinking=features.flag(15) if features is not None else False,
        display_option=info.int32(22) if info is not None else 0,
        is_model_router=info.flag(25) if info is not None else False,
        harness_uids=tuple(info.texts(20)) if info is not None else (),
        family_label=family.text(1).strip() if family is not None else "",
        family_entries=tuple(entries),
        is_default_in_family=fields.flag(31) or (family is not None and family.flag(3)),
    )


# -- chat ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments_json: str

    def encode(self) -> Writer:
        call = Writer()
        call.put_str(1, self.id)
        call.put_str(2, self.name)
        call.put_str(3, self.arguments_json)
        return call


@dataclass(frozen=True, slots=True)
class ChatPrompt:
    """One ``ChatMessagePrompt``: a turn of history on one of three channels."""

    message_id: str
    source: int
    prompt: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""
    tool_result_is_error: bool = False
    thinking: str = ""
    signature: str = ""

    def encode(self) -> Writer:
        prompt = Writer()
        prompt.put_str(1, self.message_id)
        prompt.put_uint(2, self.source)
        prompt.put_str(3, self.prompt)
        for call in self.tool_calls:
            prompt.put_message(6, call.encode())
        prompt.put_str(7, self.tool_call_id)
        prompt.put_bool(9, self.tool_result_is_error)
        prompt.put_str(11, self.thinking)
        prompt.put_str(12, self.signature)
        return prompt


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    json_schema: str


@dataclass(slots=True)
class ChatRequest:
    token: str
    user_jwt: str
    system_prompt: str
    prompts: list[ChatPrompt]
    model_uid: str
    cascade_id: str
    execution_id: str
    max_tokens: int
    tools: list[ToolDefinition] = field(default_factory=list)
    model_assignment_jwt: str = ""
    temperature: float = 0.4
    stop_patterns: tuple[str, ...] = DEFAULT_STOP_PATTERNS
    parallel_tool_calls: bool = True

    def encode(self) -> bytes:
        config = Writer()
        config.put_uint(1, 1)  # num_completions
        config.put_uint(2, self.max_tokens)
        config.put_uint(3, 200)  # max_newlines
        config.put_double(5, self.temperature)
        config.put_double(6, self.temperature)  # first_temperature
        config.put_uint(7, 50)  # top_k
        config.put_double(8, 1.0)  # top_p
        config.put_strs(9, self.stop_patterns)
        config.put_double(11, 1.0)  # fim_eot_prob_threshold

        choice = Writer()
        choice.put_str(1, "auto")  # option_name

        cache = Writer()
        cache.put_uint(1, CACHE_CONTROL_EPHEMERAL)

        request = Writer()
        request.put_message(1, metadata(self.token, user_jwt=self.user_jwt))
        request.put_str(2, self.system_prompt)
        for prompt in self.prompts:
            request.put_message(3, prompt.encode())
        request.put_uint(7, REQUEST_TYPE_CASCADE)
        request.put_message(8, config)
        for tool in self.tools:
            definition = Writer()
            definition.put_str(1, tool.name)
            definition.put_str(2, tool.description)
            definition.put_str(3, tool.json_schema)
            request.put_message(10, definition)
        request.put_bool(11, not self.parallel_tool_calls)
        request.put_message(12, choice)
        request.put_message(13, cache)
        request.put_str(16, self.cascade_id)
        request.put_uint(20, PLANNER_MODE_DEFAULT)
        request.put_str(21, self.model_uid)
        request.put_str(22, self.execution_id)
        request.put_str(26, self.model_assignment_jwt)
        return request.finish()


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ChatChunk:
    """One ``GetChatMessageResponse`` off the stream."""

    delta_text: str = ""
    delta_thinking: str = ""
    delta_signature: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: int = 0
    usage: Usage | None = None
    actual_model_uid: str = ""


def parse_chat_chunk(payload: bytes) -> ChatChunk:
    fields = parse(payload)
    usage = fields.message(7)
    return ChatChunk(
        delta_text=fields.text(3),
        delta_thinking=fields.text(9),
        delta_signature=fields.text(10),
        tool_calls=tuple(
            ToolCall(call.text(1), call.text(2), call.text(3)) for call in fields.messages(6)
        ),
        stop_reason=fields.int32(5),
        usage=(
            Usage(
                input_tokens=usage.uint(2),
                output_tokens=usage.uint(3),
                cache_write_tokens=usage.uint(4),
                cache_read_tokens=usage.uint(5),
            )
            if usage is not None
            else None
        ),
        actual_model_uid=fields.text(23),
    )


# -- Connect framing ------------------------------------------------------------


def frame(payload: bytes, *, compress: bool = True) -> bytes:
    body = gzip.compress(payload) if compress else payload
    flags = CONNECT_COMPRESSED if compress else 0
    return struct.pack(">BI", flags, len(body)) + body


@dataclass(frozen=True, slots=True)
class Frame:
    end_stream: bool
    payload: bytes
    """Decompressed."""


class FrameReader:
    """Splits a byte stream into Connect frames as bytes arrive."""

    def __init__(self) -> None:
        self._pending = bytearray()

    def feed(self, chunk: bytes) -> Iterator[Frame]:
        """Yield every frame ``chunk`` completes.

        Raises:
            WireError: on a frame over :data:`MAX_FRAME_BYTES` or a payload that
                claims gzip and is not.
        """
        self._pending += chunk
        while len(self._pending) >= 5:
            flags, length = struct.unpack_from(">BI", self._pending)
            if length > MAX_FRAME_BYTES:
                raise WireError(
                    f"Connect frame of {length} bytes exceeds the {MAX_FRAME_BYTES}-byte cap"
                )
            if len(self._pending) < 5 + length:
                return
            payload = bytes(self._pending[5 : 5 + length])
            del self._pending[: 5 + length]
            if flags & CONNECT_COMPRESSED:
                try:
                    payload = gzip.decompress(payload)
                except (OSError, EOFError, zlib.error) as exc:
                    raise WireError("a compressed Connect frame did not decompress") from exc
            yield Frame(end_stream=bool(flags & CONNECT_END_STREAM), payload=payload)

    @property
    def leftover(self) -> int:
        """Bytes of an unfinished frame. Non-zero at end of body means truncation."""
        return len(self._pending)


@dataclass(frozen=True, slots=True)
class ConnectError:
    code: str
    message: str


def trailer_error(payload: bytes) -> ConnectError | None:
    """The error in an end-of-stream trailer, or ``None`` for a clean end."""
    return _connect_error(payload, nested=True)


def unary_error(payload: bytes) -> ConnectError | None:
    """The error body of a failed unary call: ``{"code": ..., "message": ...}``."""
    return _connect_error(payload, nested=False)


def _connect_error(payload: bytes, *, nested: bool) -> ConnectError | None:
    text = payload.decode(errors="replace").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    error = parsed.get("error") if nested else parsed
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    message = error.get("message")
    code = code if isinstance(code, str) else ""
    message = message if isinstance(message, str) else ""
    if not code and not message:
        return None
    return ConnectError(code=code, message=message)
