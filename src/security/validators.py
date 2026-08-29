"""Input validation and security checks.

Features:
- Path traversal prevention
- Command injection prevention
- File type validation
- Input sanitization
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import structlog

from src.exceptions import ConfigurationError

logger = structlog.get_logger()

# Single source of truth for hidden files (names starting with ".") that are
# allowed despite the catch-all hidden-file ban in validate_filename. Keep this
# minimal — fail-closed: anything not listed here is rejected.
ALLOWED_HIDDEN_FILES: frozenset[str] = frozenset({".gitignore", ".gitkeep"})


class SecurityValidator:
    """Security validation for user inputs."""

    # Dangerous patterns for path traversal and injection.
    # NOTE: r"\.\." matches the literal two-character sequence "..": both dots
    # are escaped, so this is not a wildcard. Keep the explicit escape so the
    # intent is unambiguous to readers and static analyzers.
    DANGEROUS_PATTERNS = [
        r"\.\.",  # Parent directory (literal "..")
        # Home-directory expansion: a "~" that starts a path component
        # (start of string or right after a / or \). Scoped this way so a "~"
        # embedded mid-component — e.g. the Windows 8.3 short name
        # "SUPERU~1" — is not flagged, while "~/", "~user/", and a bare "~"
        # still are.
        r"(?:^|[\\/])~",
        r"\$\{",  # Variable expansion ${...}
        r"\$\(",  # Command substitution $(...)
        r"\$[A-Za-z_]",  # Environment variable expansion $VAR
        r"`",  # Command substitution with backticks
        r";",  # Command chaining
        r"&&",  # Command chaining (AND)
        r"\|\|",  # Command chaining (OR)
        r">",  # Output redirection
        r"<",  # Input redirection
        r"\|(?!\|)",  # Piping (but not ||)
        r"&(?!&)",  # Background execution (but not &&)
        r"#.*",  # Comments (potential for injection)
        r"\x00",  # Null byte
    ]

    # Allowed file extensions for uploads
    ALLOWED_EXTENSIONS = {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".java",
        ".cpp",
        ".c",
        ".h",
        ".hpp",
        ".cs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".md",
        ".txt",
        ".json",
        ".yml",
        ".yaml",
        ".toml",
        ".xml",
        ".html",
        ".css",
        ".scss",
        ".less",
        ".sql",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        ".r",
        ".scala",
        ".clj",
        ".hs",
        ".elm",
        ".vue",
        ".svelte",
        ".lock",
        # Plain-text formats people routinely send with "look at this". Their
        # absence produced a bare "File type not allowed: .csv" with no hint
        # about where the list lives, for content that is no more dangerous than
        # the .txt right above it.
        ".csv",
        ".tsv",
        ".log",
        ".ini",
        ".cfg",
        ".conf",
        ".properties",
        ".env.example",
        ".example",
        ".sample",
        ".dist",
        ".diff",
        ".patch",
        ".rst",
        ".adoc",
        ".tf",
        ".tfvars",
        ".gradle",
        ".proto",
        ".graphql",
        ".ipynb",
    }

    # Forbidden filenames and patterns
    FORBIDDEN_FILENAMES = {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".ssh",
        ".aws",
        ".docker",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        ".bash_history",
        ".zsh_history",
        ".mysql_history",
        ".psql_history",
    }

    # Basenames that are sensitive only as *system* files. Inside a project tree
    # they are ordinary content — ``hosts`` is an Ansible inventory, ``passwd``
    # and ``shadow`` are fixtures — so blocking them by basename alone made the
    # bot refuse to read legitimate repository files. They stay blocked whenever
    # the caller cannot vouch that the path is inside the approved directory.
    SYSTEM_ONLY_FORBIDDEN_FILENAMES = {
        "shadow",
        "passwd",
        "hosts",
        "sudoers",
    }

    # Dangerous file patterns
    DANGEROUS_FILE_PATTERNS = [
        r".*\.key$",  # Key files
        r".*\.pem$",  # Certificate files
        r".*\.p12$",  # Certificate files
        r".*\.pfx$",  # Certificate files
        r".*\.crt$",  # Certificate files
        r".*\.cer$",  # Certificate files
        r".*_rsa$",  # SSH keys
        r".*_dsa$",  # SSH keys
        r".*_ecdsa$",  # SSH keys
        r".*\.exe$",  # Executables
        r".*\.dll$",  # Windows libraries
        r".*\.so$",  # Shared objects
        r".*\.dylib$",  # macOS libraries
        r".*\.bat$",  # Batch files
        r".*\.cmd$",  # Command files
        r".*\.msi$",  # Installers
        r".*\.rar$",  # Archives (potentially dangerous)
    ]

    def __init__(
        self,
        approved_directory: Path,
        disable_security_patterns: bool = False,
        extra_upload_extensions: Optional[Iterable[str]] = None,
    ):
        """Initialize validator with approved directory.

        *extra_upload_extensions* widens the upload allowlist without editing
        this file (``UPLOAD_EXTRA_EXTENSIONS``); entries may be given with or
        without the leading dot.
        """
        # Keep the raw configured path so we can re-resolve on every check —
        # this prevents the cached value from going stale if the directory is
        # replaced with a symlink, or moved/recreated at runtime.
        self._approved_directory_raw = approved_directory
        self.approved_directory = approved_directory.resolve()
        # Refuse the filesystem root as the approved directory. If it resolves
        # to "/" (POSIX) or a drive root ("C:\\"), it has no parent, so every
        # absolute path would satisfy the _is_within_directory boundary check
        # and directory isolation would be effectively disabled.
        if self.approved_directory.parent == self.approved_directory:
            raise ConfigurationError(
                "Approved directory must not be the filesystem root "
                f"({self.approved_directory}); choose a specific subdirectory "
                "so path isolation can be enforced."
            )
        self.disable_security_patterns = disable_security_patterns
        self.allowed_extensions = set(self.ALLOWED_EXTENSIONS)
        for ext in extra_upload_extensions or ():
            ext = ext.strip().lower()
            if ext:
                self.allowed_extensions.add(ext if ext.startswith(".") else f".{ext}")
        logger.info(
            "Security validator initialized",
            approved_directory=str(self.approved_directory),
            disable_security_patterns=self.disable_security_patterns,
            extra_upload_extensions=sorted(
                self.allowed_extensions - self.ALLOWED_EXTENSIONS
            ),
        )

    def _current_approved_directory(self) -> Path:
        """Re-resolve the approved directory at call time to defeat stale-symlink
        attacks where the original target is replaced after startup.
        """
        try:
            return self._approved_directory_raw.resolve()
        except OSError:
            return self.approved_directory

    def validate_path(
        self,
        user_path: str,
        current_dir: Optional[Path] = None,
        boundary: Optional[Path] = None,
    ) -> Tuple[bool, Optional[Path], Optional[str]]:
        """Validate and resolve user-provided path.

        *current_dir* only resolves relative paths. *boundary* is what the
        result must stay inside; it defaults to ``APPROVED_DIRECTORY``. Passing
        the current project directory narrows tool access to that project — see
        ``TOOL_PATH_BOUNDARY``. A boundary outside the approved directory is
        ignored, so this can only ever tighten the check, never widen it.

        Returns:
            Tuple of (is_valid, resolved_path, error_message)
        """
        try:
            # Basic input validation
            if not user_path or not user_path.strip():
                return False, None, "Empty path not allowed"

            user_path = user_path.strip()

            # Check for dangerous patterns (unless explicitly disabled)
            if not self.disable_security_patterns:
                for pattern in self.DANGEROUS_PATTERNS:
                    if re.search(pattern, user_path, re.IGNORECASE):
                        logger.warning(
                            "Dangerous pattern detected in path",
                            path=user_path,
                            pattern=pattern,
                        )
                        return (
                            False,
                            None,
                            f"Invalid path: contains forbidden pattern '{pattern}'",
                        )

            # Resolve approved_directory fresh on each call so that swapping
            # the underlying directory for a symlink after startup does not
            # silently broaden the allow-list. (Stale-value mitigation.)
            current_approved = self._current_approved_directory()

            # A caller-supplied boundary may only narrow the approved root.
            if boundary is not None:
                try:
                    resolved_boundary = boundary.resolve()
                except (OSError, ValueError):
                    resolved_boundary = current_approved
                if self._is_within_directory(resolved_boundary, current_approved):
                    current_approved = resolved_boundary

            # Handle path resolution
            current_dir = current_dir or current_approved

            if Path(user_path).is_absolute():
                # Absolute path - use as-is. ``is_absolute()`` is
                # platform-aware, so Windows drive-absolute (``C:\\...``) and
                # UNC (``\\\\server\\share``) paths are recognised too, not just
                # POSIX ``/...`` paths.
                target = Path(user_path)
            else:
                # Relative path
                target = current_dir / user_path

            # Resolve path and check boundaries.
            # ``Path.resolve()`` follows symlinks, so a symlink inside the
            # approved directory pointing outside will resolve to the
            # outside path and be rejected by the boundary check below.
            target = target.resolve()

            # Ensure target is within approved directory
            if not self._is_within_directory(target, current_approved):
                logger.warning(
                    "Path traversal attempt detected",
                    requested_path=user_path,
                    resolved_path=str(target),
                    approved_directory=str(current_approved),
                )
                return (
                    False,
                    None,
                    ("Access denied: path outside " f"'{current_approved}'"),
                )

            # Defense-in-depth: cross-check via os.path.realpath in case
            # any intermediate symlink was not fully resolved (e.g. due to
            # FS races between resolve() and the actual file operation).
            # This narrows but does not eliminate the TOCTOU window; the
            # final defence is filesystem permissions on approved_directory.
            try:
                real_target = Path(os.path.realpath(str(target)))
            except OSError as e:
                logger.warning(
                    "realpath failed during path validation",
                    path=str(target),
                    error=str(e),
                )
                return False, None, "Invalid path"
            if not self._is_within_directory(real_target, current_approved):
                logger.warning(
                    "Symlink target outside approved directory",
                    requested_path=user_path,
                    resolved_path=str(target),
                    real_path=str(real_target),
                )
                return (
                    False,
                    None,
                    ("Access denied: path outside " f"'{current_approved}'"),
                )

            logger.debug(
                "Path validation successful",
                original_path=user_path,
                resolved_path=str(target),
            )
            return True, target, None

        except Exception as e:
            logger.error("Path validation error", path=user_path, error=str(e))
            return False, None, f"Invalid path: {str(e)}"

    def _is_within_directory(self, path: Path, directory: Path) -> bool:
        """Check if path is within directory."""
        try:
            path.relative_to(directory)
            return True
        except ValueError:
            return False

    def validate_filename(self, filename: str) -> Tuple[bool, Optional[str]]:
        """Validate uploaded filename.

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Basic checks
        if not filename or not filename.strip():
            return False, "Empty filename not allowed"

        filename = filename.strip()

        # Check for path separators in filename
        if "/" in filename or "\\" in filename:
            logger.warning("Path separator in filename", filename=filename)
            return False, "Invalid filename: contains path separators"

        # Check for forbidden patterns
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, filename, re.IGNORECASE):
                logger.warning(
                    "Dangerous pattern in filename", filename=filename, pattern=pattern
                )
                return False, "Invalid filename: contains forbidden pattern"

        # Check for forbidden filenames
        if filename.lower() in {name.lower() for name in self.FORBIDDEN_FILENAMES}:
            logger.warning("Forbidden filename", filename=filename)
            return False, f"Forbidden filename: {filename}"

        # Check for dangerous file patterns
        for pattern in self.DANGEROUS_FILE_PATTERNS:
            if re.match(pattern, filename, re.IGNORECASE):
                logger.warning(
                    "Dangerous file pattern", filename=filename, pattern=pattern
                )
                return False, f"File type not allowed: {filename}"

        # Check extension
        path_obj = Path(filename)
        ext = path_obj.suffix.lower()

        if ext and ext not in self.allowed_extensions:
            logger.warning(
                "File extension not allowed", filename=filename, extension=ext
            )
            return False, (
                f"File type not allowed: {ext}. Add it to UPLOAD_EXTRA_EXTENSIONS "
                "to accept this type."
            )

        # Check for hidden files (starting with .)
        if filename.startswith(".") and filename not in ALLOWED_HIDDEN_FILES:
            logger.warning("Hidden file upload attempt", filename=filename)
            return False, "Hidden files not allowed"

        # Check filename length
        if len(filename) > 255:
            return False, "Filename too long (max 255 characters)"

        logger.debug("Filename validation successful", filename=filename)
        return True, None

    def is_forbidden_secret_file(
        self, filename: str, *, within_approved: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """Check only the secret/credential blocklist for a basename.

        Unlike :meth:`validate_filename` (built for *uploads*: extension
        allowlist + hidden-file ban), this checks ONLY the secret-specific
        rules — ``FORBIDDEN_FILENAMES`` and ``DANGEROUS_FILE_PATTERNS`` — so it
        can gate Claude tool calls (Read/Write/Edit) without over-blocking
        legitimate in-repo files like ``.editorconfig`` or ``config.cfg``.

        Set *within_approved* when the caller has already established that the
        path lies inside the approved directory. System-only names
        (``hosts``, ``passwd``, …) are then treated as ordinary project files;
        the system copies they protect live outside that boundary and are
        rejected by the path check before reaching here. Left at its
        fail-closed default, they stay blocked.

        Returns:
            Tuple of (is_forbidden, reason). ``is_forbidden`` is True when the
            basename matches a secret/credential rule.
        """
        if not filename:
            return False, None
        name = filename.strip()
        lowered = name.lower()

        if lowered in {n.lower() for n in self.FORBIDDEN_FILENAMES}:
            return True, f"Forbidden filename: {name}"

        if not within_approved and lowered in {
            n.lower() for n in self.SYSTEM_ONLY_FORBIDDEN_FILENAMES
        }:
            return True, f"Forbidden filename: {name}"

        for pattern in self.DANGEROUS_FILE_PATTERNS:
            if re.match(pattern, name, re.IGNORECASE):
                return True, f"File type not allowed: {name}"

        return False, None

    def sanitize_command_input(self, text: str) -> str:
        """Sanitize text input for commands.

        This removes potentially dangerous characters but preserves
        the structure needed for legitimate commands.
        """
        if not text:
            return ""

        # Remove dangerous characters but preserve basic ones
        # Note: This is very restrictive - adjust based on actual needs
        sanitized = re.sub(r"[`$;|&<>#\x00-\x1f\x7f]", "", text)

        # Limit length to prevent buffer overflow attacks
        max_length = 1000
        if len(sanitized) > max_length:
            sanitized = sanitized[:max_length]
            logger.warning(
                "Command input truncated",
                original_length=len(text),
                truncated_length=len(sanitized),
            )

        # Remove excessive whitespace
        sanitized = " ".join(sanitized.split())

        if sanitized != text:
            logger.debug(
                "Command input sanitized",
                original=text[:100],  # Log first 100 chars
                sanitized=sanitized[:100],
            )

        return sanitized

    def validate_command_args(
        self, args: List[str]
    ) -> Tuple[bool, List[str], Optional[str]]:
        """Validate and sanitize command arguments.

        Returns:
            Tuple of (is_valid, sanitized_args, error_message)
        """
        if not args:
            return True, [], None

        sanitized_args = []

        for arg in args:
            # Check for dangerous patterns
            for pattern in self.DANGEROUS_PATTERNS:
                if re.search(pattern, arg, re.IGNORECASE):
                    logger.warning(
                        "Dangerous pattern in command arg", arg=arg, pattern=pattern
                    )
                    return False, [], "Invalid argument: contains forbidden pattern"

            # Sanitize argument
            sanitized = self.sanitize_command_input(arg)
            if not sanitized and arg:  # If original had content but sanitized is empty
                logger.warning("Command argument completely sanitized", original=arg)
                return (
                    False,
                    [],
                    f"Invalid argument: '{arg}' contains only forbidden characters",
                )

            sanitized_args.append(sanitized)

        return True, sanitized_args, None

    def is_safe_directory_name(self, dirname: str) -> bool:
        """Check if directory name is safe for creation."""
        if not dirname or not dirname.strip():
            return False

        dirname = dirname.strip()

        # Check for dangerous patterns
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, dirname, re.IGNORECASE):
                return False

        # Check for path separators
        if "/" in dirname or "\\" in dirname:
            return False

        # Check for forbidden names
        if dirname.lower() in {name.lower() for name in self.FORBIDDEN_FILENAMES}:
            return False

        # Check for hidden directories
        if dirname.startswith("."):
            return False

        # Check length
        if len(dirname) > 100:
            return False

        return True

    def get_security_summary(self) -> Dict[str, Any]:
        """Get summary of security validation rules."""
        return {
            "approved_directory": str(self.approved_directory),
            "allowed_extensions": sorted(list(self.ALLOWED_EXTENSIONS)),
            "forbidden_filenames": sorted(list(self.FORBIDDEN_FILENAMES)),
            "dangerous_patterns_count": len(self.DANGEROUS_PATTERNS),
            "dangerous_file_patterns_count": len(self.DANGEROUS_FILE_PATTERNS),
            "max_filename_length": 255,
            "max_command_length": 1000,
        }
