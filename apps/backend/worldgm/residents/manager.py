"""ResidentManager: launch, supervise, and stop resident TownDaemon instances.

Phase 1 runs ONE resident; the manager is structured to scale to N later.
Decision-making ("the brain") is the next task — this file is lifecycle only.
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from goosetown.daemon import TownDaemon  # module-level so tests can monkeypatch

from worldgm.residents.personas import Persona
from worldgm.residents.provision import ResidentConfig, provision_resident

logger = logging.getLogger(__name__)

_RESTART_BACKOFF_SECONDS = 5.0


@dataclass
class _ResidentHandle:
    persona: Persona
    task: asyncio.Task | None = None
    daemon: TownDaemon | None = None    # set per (re)launch
    config: ResidentConfig | None = None


class ResidentManager:
    """Manages N resident daemons: provision, launch, supervise, and stop.

    Args:
        personas: Iterable of Persona objects — one daemon per persona.
        town_service: TownService-like (injected for testability).
        mint_token: Callable (user_id, instance_id) -> token.
        ws_url: WebSocket URL override; falls back to TOWN_WS_URL env var.
        base_dir: Root directory for resident workspaces and state dirs.
    """

    def __init__(
        self,
        personas,
        *,
        town_service,
        mint_token,
        ws_url: str | None = None,
        base_dir: Path = Path("/tmp/residents"),
    ) -> None:
        self._personas = list(personas)
        self._town_service = town_service
        self._mint_token = mint_token
        self._ws_url = ws_url
        self._base_dir = base_dir
        self._running = False
        self.residents: dict[str, _ResidentHandle] = {}

    async def start(self) -> None:
        """Provision all personas and launch a supervisor task for each."""
        self._running = True
        for persona in self._personas:
            handle = _ResidentHandle(persona=persona)
            self.residents[persona.name] = handle
            handle.task = asyncio.create_task(
                self._supervise(handle), name=f"resident-{persona.name}"
            )
        # Yield to the event loop so the supervisor tasks begin executing and
        # daemon.run() is actually awaited before callers inspect state.
        await asyncio.sleep(0)

    async def _supervise(self, handle: _ResidentHandle) -> None:
        """Provision + run one resident's daemon; restart if it exits while _running."""
        while self._running:
            try:
                cfg = provision_resident(
                    handle.persona,
                    town_service=self._town_service,
                    mint_token=self._mint_token,
                    ws_url=self._ws_url,
                    base_dir=self._base_dir,
                )
                handle.config = cfg

                # provision_resident does no filesystem I/O — create the dirs the
                # daemon writes into (workspace for TOWN_*.md, state_dir for
                # sock/pid/state.json).
                cfg.workspace.mkdir(parents=True, exist_ok=True)
                cfg.state_dir.mkdir(parents=True, exist_ok=True)

                daemon = TownDaemon(
                    ws_url=cfg.ws_url,
                    token=cfg.token,
                    agent_name=cfg.agent_name,
                    state_dir=cfg.state_dir,
                    workspace_path=cfg.workspace,
                )
                handle.daemon = daemon
                logger.info("Resident '%s' daemon starting", handle.persona.name)
                await daemon.run(serve_socket=False)
                logger.warning(
                    "Resident '%s' daemon run() returned unexpectedly", handle.persona.name
                )
            except asyncio.CancelledError:
                # stop() cancelled us — propagate, do not restart.
                raise
            except Exception:
                logger.exception("Resident '%s' daemon crashed", handle.persona.name)

            if not self._running:
                break

            # Re-provision (fresh token) + reconnect after a backoff.
            logger.info(
                "Resident '%s' restarting in %.1fs",
                handle.persona.name,
                _RESTART_BACKOFF_SECONDS,
            )
            await asyncio.sleep(_RESTART_BACKOFF_SECONDS)

    async def stop(self) -> None:
        """Cancel all supervisor tasks and deactivate resident rows.

        Best-effort deactivation: errors during DynamoDB update are logged but
        never propagated — stop() must not raise.
        """
        self._running = False
        tasks = [h.task for h in self.residents.values() if h.task is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Deactivate resident rows (symmetric with create_resident's idempotent
        # reactivate on next start). Best-effort: never raise from stop().
        for handle in self.residents.values():
            if handle.config is not None:
                try:
                    # Sync boto3 call (TownService is not async) — no await.
                    self._town_service.update_agent_state(
                        handle.config.agent_id, is_active="false"
                    )
                except Exception:
                    logger.exception(
                        "Failed to deactivate resident '%s'", handle.persona.name
                    )
