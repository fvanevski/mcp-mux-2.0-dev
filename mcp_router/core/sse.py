from __future__ import annotations

from collections.abc import AsyncIterator, Callable

SSEDataTransform = Callable[[str], str]


async def iter_sse_events(lines: AsyncIterator[str]) -> AsyncIterator[list[str]]:
    event_lines: list[str] = []
    async for line in lines:
        if line == "":
            yield event_lines
            event_lines = []
            continue
        event_lines.append(line)
    if event_lines:
        yield event_lines


def render_sse_event(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def transform_sse_event(lines: list[str], transform_data: SSEDataTransform) -> list[str]:
    data_indexes: list[int] = []
    data_values: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith(":"):
            continue
        field, separator, raw_value = line.partition(":")
        if field != "data":
            continue
        value = raw_value[1:] if separator and raw_value.startswith(" ") else raw_value
        data_indexes.append(index)
        data_values.append(value)

    if not data_indexes:
        return list(lines)

    original_data = "\n".join(data_values)
    transformed_data = transform_data(original_data)
    if transformed_data == original_data:
        return list(lines)

    replacement = [f"data: {value}" if value else "data:" for value in transformed_data.split("\n")]
    if len(replacement) == len(data_indexes):
        output = list(lines)
        for index, replacement_line in zip(data_indexes, replacement, strict=True):
            output[index] = replacement_line
        return output

    first_index = data_indexes[0]
    data_index_set = set(data_indexes)
    output = []
    for index, line in enumerate(lines):
        if index == first_index:
            output.extend(replacement)
        if index in data_index_set:
            continue
        output.append(line)
    return output
