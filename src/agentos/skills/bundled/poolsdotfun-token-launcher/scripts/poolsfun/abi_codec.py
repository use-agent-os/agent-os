"""A Solidity ABI encoder/decoder covering the types this skill actually uses.

Replaces viem's ``encodeAbiParameters`` / ``decodeAbiParameters`` /
``encodeFunctionData`` / ``decodeEventLog``.

Supported: ``address``, ``bool``, ``uint<N>``, ``int<N>``, ``bytes<N>``, ``bytes``,
``string``, ``tuple``, ``T[]``, ``T[k]``. That is everything the V4 PositionManager,
StateView, Multicall3, ERC-20/721 and the three launchpad registries need.

Two things in here are worth reading before changing anything.

**The head/tail layout.** Static parameters are written inline; dynamic ones write
a 32-byte offset in the head and their payload in the tail, where the offset is
measured from the start of the *current tuple's* head — not from the start of the
whole blob. Nested dynamic types (``bytes[]``, which is what ``unlockData`` is)
are where a naive implementation gets this wrong, so the golden vectors pin the
exact ``unlockData`` hex for all five action plans.

**Sign extension.** A missed two's-complement conversion on ``int256
liquidityDelta`` turns a liquidity *removal* into a ~1e77 addition, and the
resulting reserve totals still render as plausible numbers. It is the quietest
failure mode in the port, which is why the reserve self-check against
``StateView.getLiquidity`` exists downstream.
"""

from __future__ import annotations

import re
from typing import Any

from .hexutil import as_int_n, as_uint_n, checksum_address, to_bytes

_WORD = 32
_ARRAY_RE = re.compile(r"^(.*)\[(\d*)\]$")
_INT_RE = re.compile(r"^(u?int)(\d*)$")
_BYTES_N_RE = re.compile(r"^bytes(\d+)$")


class AbiError(ValueError):
    """Raised when a value cannot be encoded, or data cannot be decoded."""


# ---------------------------------------------------------------------------
# Type inspection
# ---------------------------------------------------------------------------

def _param(spec: Any) -> dict:
    """Normalise a parameter spec to a dict with at least a ``type`` key."""
    if isinstance(spec, str):
        return {"type": spec}
    if isinstance(spec, dict) and "type" in spec:
        return spec
    raise AbiError(f"not an ABI parameter spec: {spec!r}")


def _array_parts(type_name: str) -> tuple[str, int | None] | None:
    """Split ``T[]`` / ``T[k]`` into ``(T, length)``; ``None`` when not an array."""
    match = _ARRAY_RE.match(type_name)
    if not match:
        return None
    inner, size = match.group(1), match.group(2)
    return inner, (int(size) if size else None)


def _element_param(param: dict, inner_type: str) -> dict:
    """The parameter spec for one element of an array, keeping ``components``."""
    element = {"type": inner_type}
    if "components" in param:
        element["components"] = param["components"]
    return element


def is_dynamic(param: Any) -> bool:
    param = _param(param)
    type_name = param["type"]
    if type_name in ("bytes", "string"):
        return True
    array = _array_parts(type_name)
    if array is not None:
        inner, length = array
        if length is None:
            return True
        return is_dynamic(_element_param(param, inner))
    if type_name.startswith("tuple"):
        return any(is_dynamic(c) for c in param.get("components", []))
    return False


def canonical_type(param: Any) -> str:
    """The type string used in a function/event signature.

    Tuples expand to their component list, and ``uint``/``int`` widen to their
    explicit width — ``uint`` alone is a synonym for ``uint256`` and hashing the
    short form produces the wrong selector.
    """
    param = _param(param)
    type_name = param["type"]
    array = _array_parts(type_name)
    if array is not None:
        inner, length = array
        suffix = f"[{length}]" if length is not None else "[]"
        return canonical_type(_element_param(param, inner)) + suffix
    if type_name.startswith("tuple"):
        inner = ",".join(canonical_type(c) for c in param.get("components", []))
        return f"({inner})"
    match = _INT_RE.match(type_name)
    if match and not match.group(2):
        return f"{match.group(1)}256"
    return type_name


def _head_size(param: Any) -> int:
    """Bytes this parameter occupies in the head — 32 unless it is a static array/tuple."""
    param = _param(param)
    if is_dynamic(param):
        return _WORD
    type_name = param["type"]
    array = _array_parts(type_name)
    if array is not None:
        inner, length = array
        return (length or 0) * _head_size(_element_param(param, inner))
    if type_name.startswith("tuple"):
        return sum(_head_size(c) for c in param.get("components", []))
    return _WORD


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def _encode_single(param: dict, value: Any) -> bytes:
    """Encode one non-array, non-tuple value into its 32-byte word(s)."""
    type_name = param["type"]

    if type_name == "address":
        return bytes(12) + to_bytes(checksum_address(str(value)))

    if type_name == "bool":
        return (1 if value else 0).to_bytes(_WORD, "big")

    match = _INT_RE.match(type_name)
    if match:
        signed = match.group(1) == "int"
        bits = int(match.group(2) or 256)
        number = int(value)
        if signed:
            limit = 1 << (bits - 1)
            if not (-limit <= number < limit):
                raise AbiError(f"{number} does not fit in {type_name}")
            return as_uint_n(256, number).to_bytes(_WORD, "big")
        if not (0 <= number < (1 << bits)):
            raise AbiError(f"{number} does not fit in {type_name}")
        return number.to_bytes(_WORD, "big")

    match = _BYTES_N_RE.match(type_name)
    if match:
        width = int(match.group(1))
        raw = to_bytes(value)
        if len(raw) > width:
            raise AbiError(f"{len(raw)} bytes does not fit in {type_name}")
        return raw.ljust(_WORD, b"\x00")

    if type_name in ("bytes", "string"):
        raw = value.encode("utf-8") if type_name == "string" else to_bytes(value)
        padded = raw + bytes((-len(raw)) % _WORD)
        return len(raw).to_bytes(_WORD, "big") + padded

    raise AbiError(f"unsupported type: {type_name}")


def _tuple_values(param: dict, value: Any) -> list:
    """Accept a tuple as a list/tuple or as a dict keyed by component name."""
    components = param.get("components", [])
    if isinstance(value, dict):
        try:
            return [value[c["name"]] for c in components]
        except KeyError as exc:
            raise AbiError(f"tuple value is missing component {exc}") from None
    values = list(value)
    if len(values) != len(components):
        raise AbiError(f"tuple expects {len(components)} values, got {len(values)}")
    return values


def _encode_param(param: dict, value: Any) -> bytes:
    """Encode a single parameter, recursing through arrays and tuples."""
    type_name = param["type"]

    array = _array_parts(type_name)
    if array is not None:
        inner, length = array
        element = _element_param(param, inner)
        items = list(value)
        if length is not None and len(items) != length:
            raise AbiError(f"{type_name} expects {length} items, got {len(items)}")
        body = _encode_group([element] * len(items), items)
        # A dynamic array is length-prefixed; a fixed one is not.
        return len(items).to_bytes(_WORD, "big") + body if length is None else body

    if type_name.startswith("tuple"):
        components = [_param(c) for c in param.get("components", [])]
        return _encode_group(components, _tuple_values(param, value))

    return _encode_single(param, value)


def _encode_group(params: list, values: list) -> bytes:
    """Encode a parameter list using the head/tail layout.

    Offsets are relative to the start of this group's head, which is why this is a
    separate function rather than inlined: a nested tuple restarts the numbering.
    """
    params = [_param(p) for p in params]
    if len(params) != len(values):
        raise AbiError(f"expected {len(params)} values, got {len(values)}")

    head_length = sum(_head_size(p) for p in params)
    head: list[bytes] = []
    tail: list[bytes] = []
    tail_offset = 0

    for param, value in zip(params, values):
        encoded = _encode_param(param, value)
        if is_dynamic(param):
            head.append((head_length + tail_offset).to_bytes(_WORD, "big"))
            tail.append(encoded)
            tail_offset += len(encoded)
        else:
            head.append(encoded)
    return b"".join(head) + b"".join(tail)


def encode(params: list, values: list) -> str:
    """viem ``encodeAbiParameters`` — returns a ``0x`` hex string."""
    return "0x" + _encode_group(params, values).hex()


def encode_packed_selector(signature: str) -> str:
    from .keccak import function_selector

    return function_selector(signature)


def function_signature(fragment: dict) -> str:
    """Canonical signature of an ABI function fragment."""
    inner = ",".join(canonical_type(i) for i in fragment.get("inputs", []))
    return f"{fragment['name']}({inner})"


def event_signature(fragment: dict) -> str:
    inner = ",".join(canonical_type(i) for i in fragment.get("inputs", []))
    return f"{fragment['name']}({inner})"


def find_fragment(abi: list, name: str, kind: str = "function") -> dict:
    for entry in abi:
        if entry.get("type") == kind and entry.get("name") == name:
            return entry
    raise AbiError(f"no {kind} named {name!r} in the supplied ABI")


def encode_function_data(abi: list, function_name: str, args: list | None = None) -> str:
    """viem ``encodeFunctionData`` — selector followed by the encoded arguments."""
    from .keccak import function_selector

    fragment = find_fragment(abi, function_name)
    selector = function_selector(function_signature(fragment))
    body = _encode_group(fragment.get("inputs", []), list(args or []))
    return selector + body.hex()


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def _decode_single(param: dict, data: bytes, offset: int) -> Any:
    type_name = param["type"]
    word = data[offset:offset + _WORD]
    if len(word) < _WORD:
        raise AbiError(f"truncated data reading {type_name} at byte {offset}")

    if type_name == "address":
        return checksum_address("0x" + word[12:].hex())

    if type_name == "bool":
        return int.from_bytes(word, "big") != 0

    match = _INT_RE.match(type_name)
    if match:
        raw = int.from_bytes(word, "big")
        if match.group(1) == "int":
            # Sign-extend from the full word: values are stored two's-complement
            # over 256 bits regardless of the declared width.
            return as_int_n(256, raw)
        return raw

    match = _BYTES_N_RE.match(type_name)
    if match:
        return "0x" + word[: int(match.group(1))].hex()

    raise AbiError(f"unsupported static type: {type_name}")


def _display_safe(raw: bytes) -> str:
    """Decode an ABI ``string`` for display, which is the only thing this codec's strings
    are used for: a token's ``symbol()`` / ``name()`` / metadata URI read off a chain.

    Those bytes are chosen by whoever deployed the contract. Decoded verbatim, a newline
    let a symbol forge whole lines of this script's output -- a fake table row, or a
    control marker the host reads off stdout -- and ESC / BIDI characters rewrote what the
    terminal shows. Anything that is not printable becomes U+FFFD, and invalid UTF-8 no
    longer aborts the whole read.
    """
    text = raw.decode("utf-8", errors="replace")
    return "".join(ch if ch.isprintable() else "\ufffd" for ch in text)


def _decode_param(param: dict, data: bytes, offset: int) -> Any:
    type_name = param["type"]

    if type_name in ("bytes", "string"):
        length = int.from_bytes(data[offset:offset + _WORD], "big")
        start = offset + _WORD
        raw = data[start:start + length]
        if len(raw) < length:
            raise AbiError(f"truncated {type_name}: want {length} bytes, have {len(raw)}")
        return _display_safe(raw) if type_name == "string" else "0x" + raw.hex()

    array = _array_parts(type_name)
    if array is not None:
        inner, length = array
        element = _element_param(param, inner)
        if length is None:
            count = int.from_bytes(data[offset:offset + _WORD], "big")
            base = offset + _WORD
        else:
            count, base = length, offset
        return _decode_group([element] * count, data, base)

    if type_name.startswith("tuple"):
        components = [_param(c) for c in param.get("components", [])]
        values = _decode_group(components, data, offset)
        # Name the fields when the ABI names them; callers index either way.
        if all(c.get("name") for c in components):
            return dict(zip([c["name"] for c in components], values))
        return values

    return _decode_single(param, data, offset)


def _decode_group(params: list, data: bytes, base: int) -> list:
    params = [_param(p) for p in params]
    values: list[Any] = []
    cursor = base
    for param in params:
        if is_dynamic(param):
            relative = int.from_bytes(data[cursor:cursor + _WORD], "big")
            values.append(_decode_param(param, data, base + relative))
            cursor += _WORD
        else:
            values.append(_decode_param(param, data, cursor))
            cursor += _head_size(param)
    return values


def decode(params: list, data: str | bytes) -> list:
    """viem ``decodeAbiParameters``."""
    return _decode_group(params, to_bytes(data), 0)


def decode_function_result(abi: list, function_name: str, data: str | bytes) -> Any:
    """Decode a return blob. A single output is unwrapped, matching viem."""
    fragment = find_fragment(abi, function_name)
    outputs = fragment.get("outputs", [])
    if not outputs:
        return None
    values = decode(outputs, data)
    return values[0] if len(outputs) == 1 else values


def decode_event_log(abi: list, topics: list[str], data: str | bytes) -> dict:
    """viem ``decodeEventLog`` — match on topic0, then split indexed/non-indexed.

    Every indexed parameter this skill reads is a value type, so the
    hashed-dynamic-topic case is deliberately not handled: it would silently
    return a hash where a value is expected. It raises instead.
    """
    from .keccak import event_topic

    if not topics:
        raise AbiError("log has no topics")
    topic0 = topics[0].lower()
    for fragment in abi:
        if fragment.get("type") != "event":
            continue
        if event_topic(event_signature(fragment)).lower() != topic0:
            continue

        inputs = [_param(i) for i in fragment.get("inputs", [])]
        indexed = [i for i in inputs if i.get("indexed")]
        plain = [i for i in inputs if not i.get("indexed")]
        if len(indexed) != len(topics) - 1:
            raise AbiError(
                f"{fragment['name']}: {len(indexed)} indexed params but "
                f"{len(topics) - 1} topics"
            )

        args: dict[str, Any] = {}
        for param, topic in zip(indexed, topics[1:]):
            if is_dynamic(param):
                raise AbiError(
                    f"{fragment['name']}.{param.get('name')} is an indexed dynamic "
                    "type; the topic holds its hash, not its value"
                )
            args[param.get("name") or f"arg{len(args)}"] = _decode_single(
                param, to_bytes(topic), 0
            )
        for param, value in zip(plain, decode(plain, data)):
            args[param.get("name") or f"arg{len(args)}"] = value
        return {"eventName": fragment["name"], "args": args}

    raise AbiError(f"no event in the supplied ABI matches topic {topic0}")
