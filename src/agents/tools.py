"""Project tools executed by the bot, with the same guards as Claude's tools.

Codex's native shell is disabled. Commands enter through this bridge only after
the existing Bash boundary check; command/exec additionally confines writes in
the OS sandbox. This also works on Windows, whose legacy sandbox needs broad
read access in order to start a process.
"""

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..claude.exceptions import ClaudeProcessError
from ..claude.monitor import check_bash_directory_boundary
from ..security.validators import SecurityValidator

# Claude-compatible names and native bridge names identify the same tool.
_TOOL_NAMES = {
    "read_file": {"read_file", "Read"},
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
        specs = [
            (
                "read_file",
                "Read a UTF-8 project file (up to 256 KiB).",
                {
                    "path": {"type": "string"},
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
                    "required": list(properties),
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
            if set(arguments) != set(schema["properties"]) or not all(
                isinstance(value, str) for value in arguments.values()
            ):
                raise ValueError("Unexpected tool arguments")
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

    def _file_operation(self, name: str, arguments: Dict[str, str]) -> str:
        path = self._path(arguments.get("path") or arguments.get("file_path", ""))
        if name == "list_directory":
            rows = []
            for child in path.iterdir():
                try:
                    self._path(str(child))
                except ValueError:
                    continue
                rows.append(child.name + ("/" if child.is_dir() else ""))
                if len(rows) >= 1000:
                    break
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
            raw = handle.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("File exceeds 256 KiB; use a bounded shell query")
        text = raw.decode("utf-8")
        if name == "edit_file":
            if not arguments["old_text"] or text.count(arguments["old_text"]) != 1:
                raise ValueError("old_text must match exactly once")
            path.write_text(
                text.replace(arguments["old_text"], arguments["new_text"], 1),
                encoding="utf-8",
            )
            return f"Edited {path.name}"
        return text

    async def _command(self, command: str) -> str:
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
        return json.dumps(result, ensure_ascii=False)
