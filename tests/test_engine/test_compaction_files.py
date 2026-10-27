"""Files read and modified, recorded by the engine across compactions."""

from __future__ import annotations

from agentos.engine.compaction_files import (
    FILE_ACTIVITY_HEADER,
    FILE_ACTIVITY_MAX_CHARS,
    FileActivity,
    attach_file_activity,
    file_activity_from_messages,
    merge_file_activity,
    render_file_activity,
    split_file_activity,
)
from agentos.provider import ContentBlockText, ContentBlockToolResult, ContentBlockToolUse, Message


def _call(tool_id: str, name: str, arguments: dict, *, error: bool = False) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id=tool_id, name=name, input=arguments)],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id=tool_id, content="error" if error else "ok", is_error=error
                )
            ],
        ),
    ]


PATCH = """*** Begin Patch
*** Update File: src/app/core.py
@@
-old
+new
*** Add File: src/app/new_module.py
+print("hi")
*** Delete File: src/app/legacy.py
*** End Patch"""


def test_reads_and_modifications_come_from_successful_tool_calls() -> None:
    messages = [
        Message(role="user", content="fix the bug"),
        *_call("t1", "read_file", {"path": "src/app/core.py"}),
        *_call("t2", "read_file", {"path": "README.md"}),
        *_call("t3", "read_spreadsheet", {"path": "data/prices.csv"}),
        *_call("t4", "pdf", {"path": "docs/spec.pdf"}),
        *_call("t5", "apply_patch", {"patch": PATCH}),
        *_call("t6", "write_file", {"path": "notes.txt", "content": "x"}),
        *_call("t7", "edit_file", {"path": "src/app/cli.py", "old_text": "a", "new_text": "b"}),
        # A failed edit changed nothing, a failed read read nothing.
        *_call("t8", "edit_file", {"path": "src/app/broken.py"}, error=True),
        *_call("t9", "read_file", {"path": "missing.py"}, error=True),
        # Shell commands are not parsed for paths; searches and URLs are not files read.
        *_call("t10", "exec_command", {"command": "sed -i s/a/b/ src/app/sneaky.py"}),
        *_call("t11", "grep_search", {"pattern": "TODO", "path": "src"}),
        *_call("t12", "pdf", {"path": "https://example.com/paper.pdf"}),
        Message(role="assistant", content=[ContentBlockText(text="done")]),
    ]

    activity = file_activity_from_messages(messages)

    assert activity.modified == (
        "src/app/core.py",
        "src/app/new_module.py",
        "src/app/legacy.py",
        "notes.txt",
        "src/app/cli.py",
    )
    # core.py was read and then modified: it is listed once, as modified.
    assert activity.read == ("README.md", "data/prices.csv", "docs/spec.pdf")


def test_a_tool_call_without_a_result_is_not_counted() -> None:
    messages = [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="t1", name="write_file", input={"path": "a.py"})],
        )
    ]
    assert file_activity_from_messages(messages).is_empty


def test_block_round_trips_and_is_split_off_the_text() -> None:
    activity = FileActivity(read=("docs/a.md", "docs/b.md"), modified=("src/x.py",), read_omitted=3)
    summary = attach_file_activity("## Goal\n- ship it", activity)

    assert summary.startswith("## Goal\n- ship it\n\n" + FILE_ACTIVITY_HEADER)
    assert "Modified:\n- src/x.py" in summary
    assert "Read:\n(3 older paths not listed)\n- docs/a.md\n- docs/b.md" in summary
    text, parsed = split_file_activity(summary)
    assert text == "## Goal\n- ship it"
    assert parsed == activity


def test_text_without_a_block_or_with_a_malformed_one_is_left_alone() -> None:
    assert split_file_activity("## Goal\n- none") == ("## Goal\n- none", None)
    malformed = f"text\n{FILE_ACTIVITY_HEADER}\nsomething else"
    assert split_file_activity(malformed) == (malformed, None)
    assert attach_file_activity("text", FileActivity()) == "text"
    assert render_file_activity(FileActivity()) == ""


def test_merge_moves_retouched_paths_to_the_newest_end_and_keeps_modified_sticky() -> None:
    previous = FileActivity(read=("a.py", "b.py", "c.py"), modified=("d.py",))
    new = FileActivity(read=("a.py", "d.py"), modified=("b.py", "e.py"))

    merged = merge_file_activity(previous, new)

    assert merged.modified == ("d.py", "b.py", "e.py")
    assert merged.read == ("c.py", "a.py")


def test_merge_caps_each_list_and_counts_what_it_dropped() -> None:
    previous = FileActivity(read=tuple(f"old/{index}.py" for index in range(15)), read_omitted=2)
    new = FileActivity(read=tuple(f"new/{index}.py" for index in range(10)))

    merged = merge_file_activity(previous, new, max_paths=20)

    assert len(merged.read) == 20
    assert merged.read[-1] == "new/9.py"
    assert merged.read[0] == "old/5.py"
    assert merged.read_omitted == 2 + 5


def test_merge_keeps_the_block_inside_its_size_bound_dropping_reads_first() -> None:
    long = "src/" + "deeply/nested/" * 5
    previous = FileActivity(
        read=tuple(f"{long}read_{index}.py" for index in range(20)),
        modified=tuple(f"{long}mod_{index}.py" for index in range(20)),
    )

    merged = merge_file_activity(previous, None)

    assert len(render_file_activity(merged)) <= FILE_ACTIVITY_MAX_CHARS
    assert merged.read_omitted + len(merged.read) == 20
    assert merged.modified_omitted + len(merged.modified) == 20
    # Reads are dropped before any modification is.
    assert merged.modified_omitted == 0 or not merged.read
    assert merged.modified[-1] == f"{long}mod_19.py"


def test_persisted_rows_in_both_shapes_give_the_same_lists() -> None:
    from agentos.engine.compaction_files import file_activity_from_entries

    entries = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"type": "tool_use", "tool_use_id": "a", "name": "read_file", "input": ""},
                {"type": "tool_result", "tool_use_id": "a", "result": "ok", "is_error": False},
                {
                    "type": "tool_use",
                    "tool_use_id": "b",
                    "name": "write_file",
                    "input": {"path": "out/report.md", "content": "[projected]"},
                },
                {"type": "tool_result", "tool_use_id": "b", "result": "ok", "is_error": False},
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c",
                    "function": {"name": "read_file", "arguments": '{"path": "src/main.py"}'},
                },
                {"id": "d", "function": {"name": "edit_file", "arguments": '{"path": "x.py"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c", "content": "file text"},
        # "d" never got a result row: not counted.
    ]

    activity = file_activity_from_entries(entries)

    assert activity.modified == ("out/report.md",)
    # "a" has an empty input (arguments never recorded): nothing to list.
    assert activity.read == ("src/main.py",)
