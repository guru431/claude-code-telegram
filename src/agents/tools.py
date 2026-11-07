"""Project tools executed by the bot, with the same guards as Claude's tools.

Codex's native shell is disabled. Commands enter through this bridge only after
the existing Bash boundary check; command/exec additionally confines writes in
the OS sandbox. This also works on Windows, whose legacy sandbox needs broad
read access in order to start a process.
"""

import asyncio
import fnmatch
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from ..claude.exceptions import ClaudeProcessError
from ..claude.monitor import check_bash_directory_boundary
from ..security.validators import SecurityValidator

_MAX_FILE_BYTES = 256 * 1024
_MAX_OUTPUT_CHARS = 12_000
_SEARCH_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}

# Claude-compatible names and native bridge names identify the same tool.
_TOOL_NAMES = {
    "read_file": {"read_file", "Read"},
    "search_files": {"search_files", "Grep"},
    "list_directory": {"list_directory", "LS", "Glob"},
    "write_file": {"write_file", "Write"},
    "edit_file": {"edit_file", "Edit", "MultiEdit"},
    "run_command": {"run_command", "Bash"},
    "send_image_to_user": {"send_image_to_user", "mcp__telegram__send_image_to_user"},
}


class ProjectTools:
    def __init__(
        self,
        rpc: Any,
        validator: SecurityValidator,
        cwd: Path,
        boundary: Path,
        readonly: bool,
        images_enabled: bool,
        interrupt_event: Optional[asyncio.Event] = None,
        allowed_tools_override: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
    ):
        self.rpc = rpc
        self.validator = validator
        self.cwd = cwd
        self.boundary = boundary
        self.readonly = readonly
        self.images_enabled = images_enabled
        self.interrupt_event = interrupt_event
        self.allowed_tools_override = allowed_tools_override
        self.disallowed_tools = disallowed_tools or []
        self._shell_failure: Optional[str] = None

    def _path(self, value: str) -> Path:
        valid, path, error = self.validator.validate_path(
            value, self.cwd, boundary=self.boundary
        )
        if not valid or path is None:
            raise ValueError(error or "Path is outside the permitted project")
        if any(
            self.validator.is_forbidden_secret_file(part, within_approved=True)[0]
            for part in path.parts
        ):
            raise ValueError("Access to secret files is prohibited")
        return path

    def specs(self) -> List[Dict[str, Any]]:
        specs: List[Tuple[str, str, Dict[str, Any]]] = [
            (
                "read_file",
                "Read a bounded page of a UTF-8 project file, without shell. "
                "Use start_line to continue; output includes line numbers.",
                {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1, "default": 1},
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 400,
                        "default": 120,
                    },
                },
            ),
            (
                "search_files",
                "Find literal text, case-insensitively, in a project file or "
                "directory without shell. Returns matching lines with context; "
                "defaults to Markdown files and scans at most 500 files / 8 MiB.",
                {
                    "path": {"type": "string"},
                    "query": {"type": "string", "minLength": 1, "maxLength": 512},
                    "file_glob": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "default": "*.md",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 20,
                    },
                },
            ),
            (
                "list_directory",
                "List a project directory, excluding secrets.",
                {
                    "path": {"type": "string"},
                },
            ),
        ]
        if not self.readonly:
            specs.extend(
                [
                    (
                        "write_file",
                        "Create or overwrite a UTF-8 project file.",
                        {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    ),
                    (
                        "edit_file",
                        "Replace one exact occurrence in a project file.",
                        {
                            "path": {"type": "string"},
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                        },
                    ),
                    (
                        "run_command",
                        "Run a Bash command inside the project. "
                        "Inline interpreter code, secrets and outside paths are forbidden.",
                        {
                            "command": {"type": "string"},
                        },
                    ),
                ]
            )
            if self.images_enabled:
                specs.append(
                    (
                        "send_image_to_user",
                        "Send a project image to Telegram.",
                        {
                            "file_path": {"type": "string"},
                            "caption": {"type": "string"},
                        },
                    )
                )
        if self.allowed_tools_override is not None:
            allowed = set(self.allowed_tools_override)
            specs = [s for s in specs if _TOOL_NAMES[s[0]] & allowed]
        # Argument-specific SDK denials cannot be emulated safely by this
        # bridge. Withhold the entire tool rather than ignore such a denial.
        denied = {name.split("(", 1)[0] for name in self.disallowed_tools}
        specs = [s for s in specs if not _TOOL_NAMES[s[0]] & denied]
        return [
            {
                "type": "function",
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": [
                        key
                        for key, value in properties.items()
                        if "default" not in value
                    ],
                    "additionalProperties": False,
                },
            }
            for name, description, properties in specs
        ]

    async def call(self, name: str, arguments: Any) -> Dict[str, Any]:
        try:
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be an object")
            spec = next((s for s in self.specs() if s["name"] == name), None)
            if spec is None:
                raise ValueError("Tool is not permitted for this run")
            schema = spec["inputSchema"]
            if not set(schema["required"]) <= set(arguments) or not set(
                arguments
            ) <= set(schema["properties"]):
                raise ValueError("Unexpected tool arguments")
            for key, value in arguments.items():
                field = schema["properties"][key]
                if field["type"] == "string":
                    if not isinstance(value, str) or not (
                        field.get("minLength", 0)
                        <= len(value)
                        <= field.get("maxLength", 4 * 1024 * 1024)
                    ):
                        raise ValueError(f"Invalid {key}")
                elif type(value) is not int or not (
                    field.get("minimum", 0) <= value <= field.get("maximum", 2**31 - 1)
                ):
                    raise ValueError(f"Invalid {key}")
            if name == "run_command":
                output = await self._command(arguments["command"])
            else:
                output = await asyncio.to_thread(self._file_operation, name, arguments)
            return {
                "contentItems": [{"type": "inputText", "text": output}],
                "success": True,
            }
        except (ValueError, OSError, RuntimeError, ClaudeProcessError) as exc:
            return {
                "contentItems": [{"type": "inputText", "text": str(exc)}],
                "success": False,
            }

    def _file_operation(self, name: str, arguments: Dict[str, Any]) -> str:
        path = self._path(arguments.get("path") or arguments.get("file_path", ""))
        if name == "search_files":
            return self._search(path, arguments)
        if name == "list_directory":
            rows: List[str] = []
            output_size = 0
            for child in path.iterdir():
                try:
                    self._path(str(child))
                except ValueError:
                    continue
                row = child.name + ("/" if child.is_dir() else "")
                if output_size + len(row) + 1 > _MAX_OUTPUT_CHARS or len(rows) >= 1000:
                    rows.append("[Listing truncated; narrow the directory]")
                    break
                rows.append(row)
                output_size += len(row) + 1
            return "\n".join(sorted(rows))
        if name == "send_image_to_user":
            from ..bot.utils.image_extractor import validate_image_path

            image = validate_image_path(str(path), self.boundary, arguments["caption"])
            if image is None:
                raise ValueError("Image is unavailable or outside the project")
            return "Image queued for delivery"
        if name == "write_file":
            content = arguments["content"]
            if len(content.encode("utf-8")) > 4 * 1024 * 1024:
                raise ValueError("File content exceeds 4 MiB")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"Wrote {path.name}"
        with path.open("rb") as handle:
            raw = handle.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES:
            raise ValueError("File exceeds 256 KiB; use search_files on a smaller file")
        text = raw.decode("utf-8")
        if name == "edit_file":
            if not arguments["old_text"] or text.count(arguments["old_text"]) != 1:
                raise ValueError("old_text must match exactly once")
            path.write_text(
                text.replace(arguments["old_text"], arguments["new_text"], 1),
                encoding="utf-8",
            )
            return f"Edited {path.name}"
        lines = text.splitlines()
        start = arguments.get("start_line", 1)
        stop = min(len(lines), start + arguments.get("max_lines", 120) - 1)
        rows = []
        output_size = 0
        end = start - 1
        for i in range(start - 1, stop):
            row = f"{i + 1}: {lines[i]}"
            if output_size + len(row) + 1 > _MAX_OUTPUT_CHARS:
                if not rows:
                    rows.append(row[:_MAX_OUTPUT_CHARS])
                    end = i + 1
                    rows.append(
                        "[Long line truncated; use search_files for a fragment]"
                    )
                break
            rows.append(row)
            output_size += len(row) + 1
            end = i + 1
        if end < len(lines):
            rows.append(f"[Continue with start_line={end + 1}]")
        return f"{path.name}: lines {start}-{end} of {len(lines)}\n" + "\n".join(rows)

    def _search(self, path: Path, arguments: Dict[str, Any]) -> str:
        """Bound traversal, reads and output independently; never execute project code."""
        query = arguments["query"].casefold()
        if not path.exists():
            raise ValueError("Search path does not exist")
        pattern = arguments.get("file_glob", "*.md")
        limit = arguments.get("max_results", 20)
        rows: List[str] = []
        matches = scanned = visited = total_bytes = output_size = 0
        single_file = path.is_file()
        entries = (
            [(str(path.parent), [], [path.name])]
            if single_file
            else os.walk(path, followlinks=False)
        )
        for directory, dirs, files in entries:
            visited += 1
            if visited > 500:
                return "\n".join(
                    rows + ["[Search traversal limit reached; narrow path]"]
                )
            allowed_dirs = []
            for name in dirs:
                if name.casefold() in _SEARCH_SKIP_DIRS:
                    continue
                try:
                    self._path(str(Path(directory) / name))
                except ValueError:
                    continue
                allowed_dirs.append(name)
            dirs[:] = sorted(allowed_dirs)
            for name in sorted(files):
                if not single_file and not fnmatch.fnmatchcase(
                    name.casefold(), pattern.casefold()
                ):
                    continue
                scanned += 1
                if scanned > 500 or total_bytes >= 8 * 1024 * 1024:
                    return "\n".join(
                        rows + ["[Search read limit reached; narrow path]"]
                    )
                try:
                    candidate = self._path(str(Path(directory) / name))
                    with candidate.open("rb") as handle:
                        raw = handle.read(
                            min(_MAX_FILE_BYTES + 1, 8 * 1024 * 1024 - total_bytes)
                        )
                    total_bytes += len(raw)
                    if len(raw) > _MAX_FILE_BYTES or b"\x00" in raw:
                        continue
                    lines = raw.decode("utf-8-sig").splitlines()
                except (ValueError, OSError):
                    continue
                for i, line in enumerate(lines):
                    if query not in line.casefold():
                        continue
                    label = (
                        candidate.relative_to(self.cwd).as_posix()
                        if candidate.is_relative_to(self.cwd)
                        else candidate.as_posix()
                    )
                    context = []
                    for j in range(max(0, i - 1), min(len(lines), i + 3)):
                        column = (
                            max(0, lines[j].casefold().find(query) - 200)
                            if j == i
                            else 0
                        )
                        fragment = lines[j][column : column + 1200]
                        if column:
                            fragment = "..." + fragment
                        if column + 1200 < len(lines[j]):
                            fragment += "..."
                        context.append(f"{label}:{j + 1}: {fragment}")
                    excerpt = "\n".join(context)
                    if output_size + len(excerpt) > _MAX_OUTPUT_CHARS:
                        return "\n".join(
                            rows + ["[Output limit reached; narrow query]"]
                        )
                    rows.append(excerpt)
                    output_size += len(excerpt)
                    matches += 1
                    if matches >= limit:
                        return "\n".join(rows + ["[Match limit reached]"])
        return "\n".join(rows) or "No matches in the scanned files."

    async def _command(self, command: str) -> str:
        if self._shell_failure:
            raise ValueError(self._shell_failure)
        valid, error = check_bash_directory_boundary(command, self.cwd, self.boundary)
        if not valid:
            raise ValueError(error or "Command failed the directory-boundary check")
        if os.name == "nt":
            bash = (
                Path(os.environ.get("ProgramFiles", "C:/Program Files"))
                / "Git/bin/bash.exe"
            )
            if not bash.is_file():
                raise ValueError(
                    "Git Bash is required for Codex command tools on Windows"
                )
            executable = str(bash)
        else:
            executable = shutil.which("bash") or "/bin/bash"
        process_id = "telegram-" + uuid4().hex
        params: Dict[str, Any] = {
            "processId": process_id,
            "command": [executable, "--noprofile", "--norc", "-c", command],
            "cwd": str(self.cwd),
            "timeoutMs": 30_000,
            "env": {"BASH_ENV": None, "ENV": None},
            "sandboxPolicy": {
                "type": "workspaceWrite",
                "writableRoots": [str(self.boundary)],
                "networkAccess": False,
                "excludeTmpdirEnvVar": True,
                "excludeSlashTmp": True,
            },
        }
        if os.name != "nt":
            params["outputBytesCap"] = 128 * 1024

        async def terminate() -> None:
            try:
                await self.rpc.request(
                    "command/exec/terminate", {"processId": process_id}
                )
            except ClaudeProcessError:
                # Older native Windows sandboxes don't support command/exec/terminate.
                # Abort this run's process tree rather than leave a command running.
                await self.rpc.abort()

        command_task = asyncio.create_task(self.rpc.request("command/exec", params))
        stop_task = (
            asyncio.create_task(self.interrupt_event.wait())
            if self.interrupt_event is not None
            else None
        )
        try:
            if stop_task is not None:
                done, _ = await asyncio.wait(
                    [command_task, stop_task], return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done and command_task not in done:
                    await terminate()
            result = await command_task
        except ClaudeProcessError as exc:
            if "windows sandbox" in str(exc).lower():
                self._shell_failure = (
                    str(exc)[:500] + "\n"
                    "Windows sandbox is unavailable for this run. Do not retry shell; "
                    "use read_file, search_files and list_directory for lookups. "
                    "Code execution requires repairing the sandbox."
                )
                raise ClaudeProcessError(self._shell_failure) from exc
            raise
        except asyncio.CancelledError:
            await terminate()
            raise
        finally:
            if not command_task.done():
                try:
                    await asyncio.wait_for(terminate(), 3)
                except Exception:
                    pass
                command_task.cancel()
            if stop_task is not None:
                stop_task.cancel()
            await asyncio.gather(
                command_task,
                *([stop_task] if stop_task else []),
                return_exceptions=True,
            )
        for field in ("stdout", "stderr"):
            value = result.get(field)
            if isinstance(value, str) and len(value) > _MAX_OUTPUT_CHARS // 2:
                result[field] = (
                    value[: _MAX_OUTPUT_CHARS // 2]
                    + "\n[Output truncated; use a bounded command]"
                )
        return json.dumps(result, ensure_ascii=False)
