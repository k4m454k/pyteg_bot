from __future__ import annotations

import base64
import socket
import shlex
from contextlib import suppress
from pathlib import PurePosixPath

import docker
from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container

from pytegbot_api.core.config import ExecutionSettings

CODE_UPLOAD_CHUNK_ENV_VAR = "PYTEGBOT_CODE_CHUNK_B64"
CODE_UPLOAD_CHUNK_CHARS = 80_000


class DockerContainerRuntime:
    def __init__(self, settings: ExecutionSettings) -> None:
        self._settings = settings
        self._client = docker.DockerClient(base_url=settings.docker_base_url)

    def close(self) -> None:
        self._client.close()

    def create_container(
        self,
        task_id: str,
        *,
        encoded_code: str | None,
        upload_via_file: bool,
    ) -> Container:
        environment = {
            self._settings.output_dir_env_var: self._settings.output_dir,
        }
        if upload_via_file:
            environment[self._settings.code_file_env_var] = self._settings.code_file_path
        elif encoded_code is not None:
            environment[self._settings.code_env_var] = encoded_code

        return self._client.containers.create(
            image=self._settings.execution_image,
            detach=True,
            environment=environment,
            labels={"pytegbot.task_id": task_id},
            mem_limit=self._settings.memory_limit,
            nano_cpus=self._settings.nano_cpus,
            network_disabled=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            pids_limit=64,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
        )

    def upload_code_file(self, container: Container, code: str) -> None:
        code_path = PurePosixPath(self._settings.code_file_path)
        b64_path = f"{code_path}.b64"
        encoded_payload = base64.b64encode(code.encode("utf-8")).decode("ascii")

        self.run_exec(
            container,
            [
                "/bin/sh",
                "-lc",
                (
                    f"mkdir -p {shlex.quote(str(code_path.parent))} "
                    f"&& : > {shlex.quote(b64_path)}"
                ),
            ],
        )

        for index in range(0, len(encoded_payload), CODE_UPLOAD_CHUNK_CHARS):
            chunk = encoded_payload[index : index + CODE_UPLOAD_CHUNK_CHARS]
            self.run_exec(
                container,
                [
                    "/bin/sh",
                    "-lc",
                    f'printf %s "${CODE_UPLOAD_CHUNK_ENV_VAR}" >> {shlex.quote(b64_path)}',
                ],
                environment={CODE_UPLOAD_CHUNK_ENV_VAR: chunk},
            )

        self.run_exec(
            container,
            [
                "python",
                "-c",
                (
                    "from pathlib import Path; "
                    "import base64, sys; "
                    "code_path = Path(sys.argv[1]); "
                    "b64_path = Path(sys.argv[2]); "
                    "code_path.write_bytes(base64.b64decode(b64_path.read_text(encoding='ascii'))); "
                    "b64_path.unlink(missing_ok=True)"
                ),
                str(code_path),
                b64_path,
            ],
        )

    def upload_code_via_stdin(self, container: Container, code: str) -> None:
        attached = None
        raw_socket = None
        payload = code.encode("utf-8")

        try:
            attached = self._client.api.attach_socket(
                container.id,
                params={"stdin": 1, "stream": 1},
            )
            raw_socket = getattr(attached, "_sock", attached)
            settimeout = getattr(raw_socket, "settimeout", None)
            if callable(settimeout):
                settimeout(self._settings.code_upload_timeout_seconds)

            sendall = getattr(raw_socket, "sendall", None)
            if callable(sendall):
                sendall(payload)
            else:
                write = getattr(attached, "write", None)
                if not callable(write):
                    raise DockerException("Container stdin attachment is not writable.")
                write(payload)
                flush = getattr(attached, "flush", None)
                if callable(flush):
                    flush()

            shutdown = getattr(raw_socket, "shutdown", None)
            if callable(shutdown):
                with suppress(OSError):
                    shutdown(socket.SHUT_WR)
        except (APIError, DockerException, OSError, NotFound) as exc:
            raise DockerException(f"Failed to upload code via container stdin: {exc}") from exc
        finally:
            for candidate in (attached, raw_socket):
                if candidate is None:
                    continue
                close = getattr(candidate, "close", None)
                if callable(close):
                    with suppress(Exception):
                        close()

    @staticmethod
    def run_exec(
        container: Container,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> bytes:
        try:
            result = container.exec_run(
                command,
                stdout=True,
                stderr=True,
                environment=environment,
            )
        except (APIError, DockerException, NotFound) as exc:
            raise DockerException(f"Exec failed for {command!r}: {exc}") from exc

        exit_code = getattr(result, "exit_code", None)
        output = getattr(result, "output", None)
        if exit_code is None and isinstance(result, tuple) and len(result) == 2:
            exit_code, output = result

        data = bytes(output) if isinstance(output, (bytes, bytearray)) else b""
        if exit_code != 0:
            message = data.decode("utf-8", errors="replace").strip()
            if message:
                raise DockerException(f"Exec failed for {command!r}: {message}")
            raise DockerException(f"Exec failed for {command!r} with exit code {exit_code}.")
        return data

    @staticmethod
    def kill_container(container: Container) -> None:
        try:
            container.kill()
        except (APIError, NotFound):
            return

    @staticmethod
    def remove_container(container: Container) -> None:
        try:
            container.remove(force=True)
        except (APIError, NotFound):
            return

    def get_container(self, container_id: str) -> Container:
        return self._client.containers.get(container_id)
