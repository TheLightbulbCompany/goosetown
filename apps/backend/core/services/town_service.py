"""Town service for managing GooseTown agent registration and state (DynamoDB)."""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from boto3.dynamodb.conditions import Attr, Key

from core import dynamo
from core.town_token import sign_town_token

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _name_claim_id(user_id: str, agent_name: str) -> str:
    """Build the synthetic primary-key value for an agent name-claim row.

    Claim rows live in the same ``agents`` table but with a prefixed ``id``
    so they're visually distinct and so ``attribute_not_exists(id)`` can
    gate the first writer. They deliberately don't populate ``userId`` /
    ``agentName`` / ``isActive`` / ``joinedAt``, so neither the
    ``isActive-joinedAt-index`` nor ``userId-agentName-index`` GSI
    includes them — invisible to every existing read path.
    """
    return f"name-claim#{user_id}#{agent_name}"


def _decimal_to_native(obj):
    """Recursively convert Decimal values from DynamoDB to int/float."""
    if isinstance(obj, Decimal):
        if obj == int(obj):
            return int(obj)
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_native(i) for i in obj]
    return obj


def versioned_sprite_url(raw_url: Optional[str], agent: Optional[dict]) -> str:
    """Append ``?v=<spriteUpdatedAt>`` to a sprite URL for cache-busting.

    Sprite URLs are content-addressed by agent UUID with a 1-year
    ``Cache-Control``. When a sprite is regenerated the underlying object is
    overwritten but the URL string is stable, so any browser-cached response
    sticks forever. ``spriteUpdatedAt`` is bumped on every sprite_url write
    (see ``update_agent_state``) and emitted as ``?v=`` so the URL string
    changes whenever the bytes do.

    Returns the raw URL unchanged if the agent has no ``spriteUpdatedAt``
    — which shouldn't happen post-backfill but is a safe no-op.
    """
    if not raw_url:
        return ""
    version = agent.get("spriteUpdatedAt") if agent else None
    if not version:
        return raw_url
    sep = "&" if "?" in raw_url else "?"
    return f"{raw_url}{sep}v={version}"


class TownService:
    """Service for GooseTown CRUD operations (DynamoDB backend)."""

    def __init__(self):
        pass

    # ------------------------------------------------------------------
    # Agent CRUD
    # ------------------------------------------------------------------

    def _get_town_agent(self, user_id: str, agent_name: str) -> Optional[dict]:
        """Get a town agent by user_id and agent_name."""
        table = dynamo.agents_table()
        resp = table.query(
            IndexName="userId-agentName-index",
            KeyConditionExpression=Key("userId").eq(user_id) & Key("agentName").eq(agent_name),
        )
        items = resp.get("Items", [])
        if not items:
            return None
        return _decimal_to_native(items[0])

    def get_town_agent_by_id(self, agent_id: str) -> Optional[dict]:
        """Get a town agent by its UUID."""
        table = dynamo.agents_table()
        resp = table.get_item(Key={"id": agent_id})
        item = resp.get("Item")
        if not item:
            return None
        return _decimal_to_native(item)

    def get_agent_by_name(self, user_id: str, agent_name: str) -> Optional[dict]:
        """Look up an agent by user_id and agent_name (active only)."""
        agent = self._get_town_agent(user_id, agent_name)
        if agent and agent.get("isActive") == "true":
            return agent
        return None

    def get_active_agents(self) -> list[dict]:
        """Get all active town agents."""
        table = dynamo.agents_table()
        resp = table.query(
            IndexName="isActive-joinedAt-index",
            KeyConditionExpression=Key("isActive").eq("true"),
        )
        return [_decimal_to_native(item) for item in resp.get("Items", [])]

    def get_active_agents_for_user(self, user_id: str) -> list[dict]:
        """Get all active agents owned by a single user.

        Queries the ``userId-agentName-index`` GSI directly instead of
        doing a global ``isActive-joinedAt-index`` scan and filtering in
        Python. The frontend polls ``/apartment`` every 2s per user —
        each old scan returned every active agent in the system to then
        throw away everyone except the requester's. Cost scales with
        total town population × users online; the GSI query scales only
        with agents-per-user.
        """
        table = dynamo.agents_table()
        resp = table.query(
            IndexName="userId-agentName-index",
            KeyConditionExpression=Key("userId").eq(user_id),
            FilterExpression=Attr("isActive").eq("true"),
        )
        return [_decimal_to_native(item) for item in resp.get("Items", [])]

    def get_town_state(self) -> list[dict]:
        """Get current state of all active agents with ready sprites.

        In DynamoDB the agent and state are merged into one item,
        so we query active agents and filter for spriteReady=true.
        """
        table = dynamo.agents_table()
        resp = table.query(
            IndexName="isActive-joinedAt-index",
            KeyConditionExpression=Key("isActive").eq("true"),
            FilterExpression=Attr("spriteReady").eq(True),
        )
        items = resp.get("Items", [])
        result = []
        for item in items:
            item = _decimal_to_native(item)
            result.append(
                {
                    "agent_id": item["id"],
                    "user_id": item.get("userId"),
                    "display_name": item.get("displayName"),
                    "agent_name": item.get("agentName"),
                    "personality_summary": item.get("personalitySummary"),
                    "is_active": item.get("isActive"),
                    "sprite_ready": item.get("spriteReady", False),
                    "sprite_url": item.get("spriteUrl"),
                    "traits": item.get("traits", ""),
                    "mood": item.get("mood"),
                    "energy": item.get("energy"),
                    "current_conversation_id": item.get("currentConversationId"),
                    "current_location": item.get("currentLocation"),
                    "location_context": item.get("locationContext", "apartment"),
                }
            )
        return result

    def get_all_active_agents(self) -> list[dict]:
        """Get all active agents (raw DynamoDB records). Used for startup reconnect."""
        table = dynamo.agents_table()
        resp = table.query(
            IndexName="isActive-joinedAt-index",
            KeyConditionExpression=Key("isActive").eq("true"),
        )
        return [_decimal_to_native(item) for item in resp.get("Items", [])]

    def opt_in(
        self,
        user_id: str,
        agent_name: str,
        display_name: str,
        personality_summary: Optional[str] = None,
        avatar_config: Optional[dict] = None,
    ) -> dict:
        """Register an agent in GooseTown.

        TOCTOU protection: two concurrent registrations for the same
        ``(user_id, agent_name)`` that both find no existing agent used
        to each generate a fresh UUID and ``put_item`` successfully,
        producing duplicate rows. Fixed by wrapping the create path in
        ``TransactWriteItems`` with a synthetic "name claim" row keyed
        by ``name-claim#{user_id}#{agent_name}``; the transaction either
        puts both the claim and the agent, or fails entirely when the
        claim already exists.

        The claim row omits ``userId`` / ``agentName`` / ``isActive`` /
        ``joinedAt`` so it stays invisible to every GSI — ``get_active_agents``
        (``isActive-joinedAt-index``), ``get_active_agents_for_user``
        (``userId-agentName-index`` + ``isActive`` filter), and
        ``_get_town_agent`` (same GSI, filter-free) never surface it.
        The row only exists to gate the ``put_item`` condition.

        On the race path — both requests reach the transaction, one
        wins, the other's claim put fails with
        ``ConditionalCheckFailedException`` — we re-read via
        ``_get_town_agent`` and return the winner's agent row. The race
        becomes invisible to the loser's caller.
        """
        existing = self._get_town_agent(user_id, agent_name)
        if existing:
            if existing.get("isActive") != "true":
                table = dynamo.agents_table()
                table.update_item(
                    Key={"id": existing["id"]},
                    UpdateExpression=(
                        "SET isActive = :active, displayName = :dn, "
                        "personalitySummary = :ps, avatarConfig = :ac, lastActiveAt = :now"
                    ),
                    ExpressionAttributeValues={
                        ":active": "true",
                        ":dn": display_name,
                        ":ps": personality_summary,
                        ":ac": avatar_config,
                        ":now": _now_iso(),
                    },
                )
                existing["isActive"] = "true"
                existing["displayName"] = display_name
                existing["personalitySummary"] = personality_summary
                existing["avatarConfig"] = avatar_config
                return existing
            return existing

        now = _now_iso()
        agent_id = str(uuid.uuid4())

        item = {
            "id": agent_id,
            "userId": user_id,
            "agentName": agent_name,
            "displayName": display_name,
            "personalitySummary": personality_summary,
            "avatarConfig": avatar_config,
            "homeLocation": "apartment",
            "isActive": "true",
            "joinedAt": now,
            "lastActiveAt": now,
            "spriteReady": False,
            "spriteUrl": None,
            "character": None,
            "traits": "",
            # Lightweight location for respawn after game server restart
            "currentLocation": "exit",
            "locationContext": "apartment",
            "currentConversationId": None,
            "lastHeartbeatAt": None,
            "mood": "neutral",
            "energy": Decimal("100"),
            "statusMessage": None,
            "lastConversationAt": None,
        }

        claim_id = _name_claim_id(user_id, agent_name)
        claim_item = {
            "id": claim_id,
            "kind": "name_claim",
            "agentId": agent_id,
            "claimedAt": now,
        }

        table = dynamo.agents_table()
        client = table.meta.client
        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": table.name,
                            "Item": claim_item,
                            "ConditionExpression": "attribute_not_exists(id)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": table.name,
                            "Item": item,
                        }
                    },
                ]
            )
        except client.exceptions.TransactionCanceledException as exc:
            # One of the puts failed — almost certainly the claim's
            # condition (another registration for the same name won the
            # race). Re-read; if the winner's row is now visible, return
            # it so the race is invisible to this caller.
            reasons = getattr(exc, "response", {}).get("CancellationReasons", []) or []
            claim_conflict = any(r.get("Code") == "ConditionalCheckFailed" for r in reasons)
            if claim_conflict:
                winner = self._get_town_agent(user_id, agent_name)
                if winner is not None:
                    logger.info(
                        "Agent '%s' for user '%s' already registered by concurrent request",
                        agent_name,
                        user_id,
                    )
                    return winner
                # Claim exists but winner row isn't visible — extremely
                # unlikely with TransactWriteItems (it's atomic) but we
                # raise a specific error if it ever happens so operators
                # can investigate instead of silently succeeding.
                raise RuntimeError(
                    f"Agent registration for '{agent_name}' under user '{user_id}' was "
                    "claimed but the agent row is missing — orphan claim row detected"
                ) from exc
            raise

        logger.info("Agent '%s' opted into GooseTown as '%s'", agent_name, display_name)
        return _decimal_to_native(item)

    def opt_out(self, user_id: str, agent_name: str) -> bool:
        """Remove an agent from GooseTown (deactivate, preserve data)."""
        agent = self._get_town_agent(user_id, agent_name)
        if not agent:
            return False

        table = dynamo.agents_table()
        table.update_item(
            Key={"id": agent["id"]},
            UpdateExpression="SET isActive = :inactive",
            ExpressionAttributeValues={":inactive": "false"},
        )
        logger.info("Agent '%s' opted out of GooseTown", agent_name)
        return True

    def update_agent_state(self, agent_id: str, **kwargs) -> Optional[dict]:
        """Update an agent's town state fields."""
        if not kwargs:
            return self.get_town_agent_by_id(agent_id)

        # Auto-bump spriteUpdatedAt whenever sprite_url changes so downstream
        # URL versioning (versioned_sprite_url) gets a fresh cache-bust token.
        if "sprite_url" in kwargs and "sprite_updated_at" not in kwargs:
            import time as _time

            kwargs["sprite_updated_at"] = int(_time.time())

        # Map snake_case kwargs to camelCase DynamoDB attribute names
        field_map = {
            "current_conversation_id": "currentConversationId",
            "last_heartbeat_at": "lastHeartbeatAt",
            "last_conversation_at": "lastConversationAt",
            "status_message": "statusMessage",
            "sprite_ready": "spriteReady",
            "sprite_url": "spriteUrl",
            "sprite_updated_at": "spriteUpdatedAt",
            "sprite_animations": "spriteAnimations",
            "daemon_connection_id": "daemonConnectionId",
            "sprite_status": "spriteStatus",
            "sprite_current_template": "spriteCurrentTemplate",
            "pixellab_character_id": "pixellabCharacterId",
            "location_context": "locationContext",
            "current_location": "currentLocation",
            "onboarding_phase": "onboardingPhase",
        }

        update_parts = []
        attr_values = {}
        attr_names = {}
        for i, (key, value) in enumerate(kwargs.items()):
            dynamo_key = field_map.get(key, key)
            placeholder_val = f":v{i}"
            placeholder_name = f"#n{i}"
            update_parts.append(f"{placeholder_name} = {placeholder_val}")
            # Convert floats to Decimal for DynamoDB
            if isinstance(value, float):
                attr_values[placeholder_val] = Decimal(str(value))
            else:
                attr_values[placeholder_val] = value
            attr_names[placeholder_name] = dynamo_key

        update_expr = "SET " + ", ".join(update_parts)

        table = dynamo.agents_table()
        try:
            resp = table.update_item(
                Key={"id": agent_id},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=attr_values,
                ExpressionAttributeNames=attr_names,
                ConditionExpression="attribute_exists(id)",
                ReturnValues="ALL_NEW",
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return None
        return _decimal_to_native(resp.get("Attributes"))

    def update_agent_activity(
        self,
        agent_id: str,
        activity_name: Optional[str],
        started_at: Optional[float],
    ) -> None:
        """Set or clear the agent's current activity in DynamoDB.

        Args:
            agent_id: Agent UUID.
            activity_name: Activity key (e.g. ``"read"``), or ``None`` to clear.
            started_at: Unix timestamp when the activity started, or ``None``.
        """
        table = dynamo.agents_table()
        if activity_name is None:
            # Clear — use REMOVE so the attributes are fully absent
            table.update_item(
                Key={"id": agent_id},
                UpdateExpression="REMOVE currentActivity, activityStartedAt",
                ConditionExpression="attribute_exists(id)",
            )
        else:
            started_decimal = Decimal(str(started_at)) if started_at is not None else Decimal("0")
            table.update_item(
                Key={"id": agent_id},
                UpdateExpression="SET currentActivity = :a, activityStartedAt = :t",
                ExpressionAttributeValues={":a": activity_name, ":t": started_decimal},
                ConditionExpression="attribute_exists(id)",
            )

    def update_agent_mood_energy(self, agent_id: str, mood: int, energy: int) -> None:
        """Persist updated mood and energy values for an agent.

        Args:
            agent_id: Agent UUID.
            mood: New mood value, clamped to -50..+50 by the caller.
            energy: New energy value, clamped to 0..100 by the caller.
        """
        table = dynamo.agents_table()
        table.update_item(
            Key={"id": agent_id},
            UpdateExpression="SET mood = :m, energy = :e",
            ExpressionAttributeValues={":m": mood, ":e": energy},
            ConditionExpression="attribute_exists(id)",
        )

    def update_agent_context(
        self,
        agent_id: str,
        location_context: str,
        current_location: str,
    ) -> Optional[dict]:
        """Persist a context transition for an agent.

        Called when an agent crosses the boundary between the town map and
        the apartment scene.  Updates both ``locationContext`` (``"town"``
        or ``"apartment"``) and ``currentLocation`` atomically.

        Args:
            agent_id: Agent UUID.
            location_context: New context: ``"town"`` or ``"apartment"``.
            current_location: Spawn-point location name in the new context.
                ``"exit"`` when entering the apartment; ``"residence"`` when
                returning to town.

        Returns:
            Updated agent record as a plain dict, or ``None`` if the agent
            does not exist.
        """
        return self.update_agent_state(
            agent_id,
            location_context=location_context,
            current_location=current_location,
        )

    # ------------------------------------------------------------------
    # Instance management
    # ------------------------------------------------------------------

    def get_instance_by_token(self, token: str) -> Optional[dict]:
        """Look up an active instance by its town_token."""
        table = dynamo.instances_table()
        resp = table.query(
            IndexName="townToken-index",
            KeyConditionExpression=Key("townToken").eq(token),
        )
        items = resp.get("Items", [])
        for item in items:
            if item.get("isActive") == "true":
                return _decimal_to_native(item)
        return None

    def get_active_instance(self, user_id: str) -> Optional[dict]:
        """Return the user's active TownInstance, or None."""
        table = dynamo.instances_table()
        resp = table.query(
            IndexName="userId-index",
            KeyConditionExpression=Key("userId").eq(user_id),
        )
        for item in resp.get("Items", []):
            if item.get("isActive") == "true":
                return _decimal_to_native(item)
        return None

    def create_instance(self, user_id: str) -> dict:
        """Create a new TownInstance with signed token."""
        instance_id = str(uuid.uuid4())
        now = _now_iso()
        token = sign_town_token(user_id, instance_id)

        # Use atomic counter for apartment unit assignment
        table = dynamo.instances_table()
        counter_response = table.update_item(
            Key={"id": "__counter__"},
            UpdateExpression="ADD apartmentUnit :inc",
            ExpressionAttributeValues={":inc": 1},
            ReturnValues="UPDATED_NEW",
        )
        next_unit = int(counter_response["Attributes"]["apartmentUnit"])

        item = {
            "id": instance_id,
            "userId": user_id,
            "apartmentUnit": next_unit,
            "townToken": token,
            "isActive": "true",
            "createdAt": now,
        }
        table.put_item(Item=item)
        return _decimal_to_native(item)

    def deactivate_instance(self, instance_id: str) -> None:
        """Deactivate a TownInstance."""
        table = dynamo.instances_table()
        table.update_item(
            Key={"id": instance_id},
            UpdateExpression="SET isActive = :inactive",
            ExpressionAttributeValues={":inactive": "false"},
        )

    def get_instance_agents(self, instance_id: str) -> list[dict]:
        """Return all active agents for a given instance."""
        table = dynamo.agents_table()
        resp = table.query(
            IndexName="instanceId-index",
            KeyConditionExpression=Key("instanceId").eq(instance_id),
        )
        return [_decimal_to_native(item) for item in resp.get("Items", []) if item.get("isActive") == "true"]

    def deactivate_instance_agents(self, instance_id: str) -> int:
        """Deactivate all agents for an instance. Returns count."""
        agents = self.get_instance_agents(instance_id)
        table = dynamo.agents_table()
        for agent in agents:
            table.update_item(
                Key={"id": agent["id"]},
                UpdateExpression="SET isActive = :inactive",
                ExpressionAttributeValues={":inactive": "false"},
            )
        return len(agents)

    def opt_in_instance(
        self,
        user_id: str,
        agents_data: list,
    ) -> tuple[dict, list[dict]]:
        """Instance-level opt-in: create instance + all agents + states."""
        existing = self.get_active_instance(user_id)
        if existing:
            raise ValueError("User already has an active GooseTown instance")

        instance = self.create_instance(user_id)
        created_agents = []

        now = _now_iso()

        table = dynamo.agents_table()
        for agent_data in agents_data:
            agent_id = str(uuid.uuid4())
            item = {
                "id": agent_id,
                "userId": user_id,
                "agentName": agent_data.agent_name,
                "displayName": agent_data.display_name,
                "personalitySummary": agent_data.personality_summary,
                "homeLocation": "apartment",
                "instanceId": instance["id"],
                "isActive": "true",
                "joinedAt": now,
                "lastActiveAt": now,
                "spriteReady": False,
                "spriteUrl": None,
                "character": None,
                "traits": "",
                "currentLocation": "exit",
                "locationContext": "apartment",
                "currentConversationId": None,
                "lastHeartbeatAt": None,
                "mood": "neutral",
                "energy": Decimal("100"),
                "statusMessage": None,
                "lastConversationAt": None,
            }
            table.put_item(Item=item)
            created_agents.append(_decimal_to_native(item))

        logger.info(
            "Instance opt-in for user %s: apartment_unit=%s, agents=%d",
            user_id,
            instance.get("apartmentUnit"),
            len(created_agents),
        )
        return instance, created_agents

    def opt_out_instance(self, user_id: str) -> tuple[Optional[dict], int]:
        """Instance-level opt-out: deactivate instance + all agents."""
        instance = self.get_active_instance(user_id)
        if not instance:
            return None, 0

        count = self.deactivate_instance_agents(instance["id"])
        self.deactivate_instance(instance["id"])

        logger.info("Instance opt-out for user %s: deactivated %d agents", user_id, count)
        return instance, count

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    def get_or_create_relationship(self, agent_a_id: str, agent_b_id: str) -> tuple[dict, bool]:
        """Get or create a relationship between two agents."""
        a_id, b_id = sorted([agent_a_id, agent_b_id])

        table = dynamo.relationships_table()
        resp = table.get_item(Key={"agentAId": a_id, "agentBId": b_id})
        item = resp.get("Item")
        if item:
            return _decimal_to_native(item), False

        now = _now_iso()
        item = {
            "agentAId": a_id,
            "agentBId": b_id,
            "relationshipType": "acquaintance",
            "affinityScore": 0,
            "interactionCount": 0,
            "lastInteractionAt": None,
            "createdAt": now,
        }
        table.put_item(Item=item)
        return _decimal_to_native(item), True

    def update_relationship(
        self,
        agent_a_id: str,
        agent_b_id: str,
        affinity_delta: int = 0,
        new_type: Optional[str] = None,
    ) -> dict:
        """Update relationship after an interaction.

        Uses DynamoDB atomic ``ADD`` for ``affinityScore`` so concurrent
        callers can't clobber each other's deltas. Previously this did a
        get-then-set ("read current, add delta in Python, write") which
        lost updates: two concurrent ``update_relationship(jim, pam, +5)``
        calls could both read ``current=10``, both compute ``15``, and
        both write ``15`` — the second ``+5`` vanished.

        ``ADD`` + ``[-100, 100]`` clamp is slightly awkward in DynamoDB
        because ``UpdateExpression`` can't express min/max. We do an
        atomic ``ADD`` first; if the post-ADD value lands outside the
        valid range, a follow-up ``SET`` clamps it. Ordering guarantee:
        ``ADD`` is atomic, so no delta is ever lost. The clamp is
        idempotent — if a concurrent ADD pushes the value back into
        range before we clamp, the clamp either no-ops (same bound) or
        briefly re-centres around the bound; subsequent operations
        converge.
        """
        a_id, b_id = sorted([agent_a_id, agent_b_id])
        table = dynamo.relationships_table()

        # Combine all mutations into a single atomic UpdateItem. The ADD
        # clause handles counters atomically; SET handles timestamp and
        # relationship type. No pre-read required.
        add_clauses = ["interactionCount :one"]
        set_clauses = ["lastInteractionAt = :now"]
        attr_values: dict = {
            ":one": 1,
            ":now": _now_iso(),
        }

        if affinity_delta != 0:
            add_clauses.append("affinityScore :delta")
            attr_values[":delta"] = int(affinity_delta)

        if new_type:
            set_clauses.append("relationshipType = :rtype")
            attr_values[":rtype"] = new_type

        update_expr = "SET " + ", ".join(set_clauses) + " ADD " + ", ".join(add_clauses)
        resp = table.update_item(
            Key={"agentAId": a_id, "agentBId": b_id},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=attr_values,
            ReturnValues="ALL_NEW",
        )
        attrs = resp.get("Attributes", {})

        # Clamp affinityScore into [-100, 100] if the atomic ADD pushed it
        # out of bounds. Unconditional SET is fine here — the clamp is to
        # an idempotent bound, so any concurrent ADD that also just
        # overshot will resolve to the same clamped value, and a
        # concurrent ADD that brought it back in range means our SET
        # would re-clamp to the bound (slightly lossy for the in-range
        # value, but the next interaction re-adds from there; net
        # behaviour converges to the correct running total clipped to
        # the range).
        if affinity_delta != 0:
            score = int(attrs.get("affinityScore", 0))
            if score > 100 or score < -100:
                clamped = max(-100, min(100, score))
                resp = table.update_item(
                    Key={"agentAId": a_id, "agentBId": b_id},
                    UpdateExpression="SET affinityScore = :clamped",
                    ExpressionAttributeValues={":clamped": clamped},
                    ReturnValues="ALL_NEW",
                )
                attrs = resp.get("Attributes", {})

        return _decimal_to_native(attrs)

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    def store_conversation(
        self,
        participant_a_id: str,
        participant_b_id: str,
        location: str,
        public_log: list,
        topic_summary: Optional[str] = None,
    ) -> dict:
        """Store a completed conversation."""
        now = _now_iso()
        conv_id = str(uuid.uuid4())

        # Sort participant IDs canonically
        a_id, b_id = sorted([participant_a_id, participant_b_id])

        item = {
            "id": conv_id,
            "participantAId": a_id,
            "participantBId": b_id,
            "location": location,
            "turnCount": len(public_log),
            "topicSummary": topic_summary,
            "publicLog": public_log,
            "status": "ended",
            "startedAt": now,
            "endedAt": now,
            "updatedAt": now,
        }
        table = dynamo.conversations_table()
        table.put_item(Item=item)
        return _decimal_to_native(item)

    def get_recent_conversations(self, limit: int = 20) -> list[dict]:
        """Get recent town conversations (active + ended), newest first.

        Active chats are returned first so the sidebar log can show them as
        "live", followed by the most recent ended ones.
        """
        table = dynamo.conversations_table()
        results: list[dict] = []
        for status in ("active", "ended"):
            resp = table.query(
                IndexName="status-startedAt-index",
                KeyConditionExpression=Key("status").eq(status),
                ScanIndexForward=False,
                Limit=limit,
            )
            for item in resp.get("Items", []):
                results.append(_decimal_to_native(item))
        # Sort the merged list newest-first by startedAt and cap to limit
        results.sort(key=lambda c: c.get("startedAt") or "", reverse=True)
        return results[:limit]

    def create_active_conversation(
        self,
        initiator_id: str,
        target_id: str,
        location: str,
        opening_message: str,
        initiator_display_name: str,
    ) -> dict:
        """Create a new active conversation between two agents.

        Records the opening message in the public log and marks both agents'
        ``currentConversationId`` so the system can prevent them from starting
        overlapping conversations.

        Args:
            initiator_id: UUID of the agent starting the conversation.
            target_id: UUID of the agent being invited.
            location: Current location name (e.g. ``"plaza"``).
            opening_message: The initiating agent's first message text.
            initiator_display_name: Display name used as the speaker label.

        Returns:
            The newly created conversation record as a plain dict.
        """
        now = _now_iso()
        conv_id = str(uuid.uuid4())
        a_id, b_id = sorted([initiator_id, target_id])

        item = {
            "id": conv_id,
            "participantAId": a_id,
            "participantBId": b_id,
            "participants": [a_id, b_id],
            "lastSpeakerId": initiator_id,
            "location": location,
            "turnCount": 1,
            "topicSummary": "_none",
            "publicLog": [{"speaker": initiator_display_name, "text": opening_message}],
            "status": "active",
            "startedAt": now,
            "endedAt": "_active",  # Sentinel — GSI key cannot be NULL or empty
            "updatedAt": now,
        }
        table = dynamo.conversations_table()
        table.put_item(Item=item)

        # Mark both agents as in this conversation
        agents_table = dynamo.agents_table()
        for agent_id in (initiator_id, target_id):
            try:
                agents_table.update_item(
                    Key={"id": agent_id},
                    UpdateExpression="SET currentConversationId = :cid",
                    ExpressionAttributeValues={":cid": conv_id},
                    ConditionExpression="attribute_exists(id)",
                )
            except agents_table.meta.client.exceptions.ConditionalCheckFailedException:
                logger.warning("Agent %s not found when setting currentConversationId", agent_id)

        return _decimal_to_native(item)

    def get_nearby_conversations(self, nearby_agent_ids: list[str], exclude_conv_id: str | None = None) -> list[dict]:
        """Find active conversations that any of the nearby agents are in."""
        conversations: list[dict] = []
        seen_conv_ids: set[str] = set()
        for agent_id in nearby_agent_ids:
            agent = self.get_town_agent_by_id(agent_id)
            if not agent:
                continue
            conv_id = agent.get("currentConversationId")
            if not conv_id or conv_id == exclude_conv_id or conv_id in seen_conv_ids:
                continue
            seen_conv_ids.add(conv_id)
            conv = self.get_conversation_by_id(conv_id)
            if not conv or conv.get("status") != "active":
                continue
            participants: list[str] = conv.get("participants") or [
                conv.get("participantAId"),
                conv.get("participantBId"),
            ]
            participant_names = []
            for pid in participants:
                if pid:
                    p = self.get_town_agent_by_id(pid)
                    if p:
                        participant_names.append(p.get("displayName", p.get("agentName", pid[:8])))
            pub_log = conv.get("publicLog") or []
            last_msg = pub_log[-1] if pub_log else {}
            conversations.append(
                {
                    "conv_id": conv_id,
                    "participants": participant_names,
                    "last_message": last_msg,
                    "location": conv.get("location", ""),
                }
            )
        return conversations

    def get_conversation_by_id(self, conv_id: str) -> Optional[dict]:
        """Fetch a single conversation record by its UUID.

        Args:
            conv_id: Conversation UUID.

        Returns:
            The conversation record as a plain dict, or ``None`` if not found.
        """
        table = dynamo.conversations_table()
        resp = table.get_item(Key={"id": conv_id})
        item = resp.get("Item")
        if not item:
            return None
        return _decimal_to_native(item)

    def append_message_to_conversation(
        self,
        conv_id: str,
        speaker_id: str,
        speaker_display_name: str,
        text: str,
    ) -> Optional[dict]:
        """Append a message turn to an active conversation's public log.

        Increments ``turnCount``, sets ``lastSpeakerId``, and updates
        ``updatedAt`` atomically.

        Args:
            conv_id: Conversation UUID.
            speaker_id: UUID of the speaking agent.
            speaker_display_name: Display name label for this turn.
            text: The message text.

        Returns:
            Updated conversation record as a plain dict, or ``None`` if the
            conversation does not exist or is not active.
        """
        table = dynamo.conversations_table()
        now = _now_iso()
        new_entry = {"speaker": speaker_display_name, "text": text}
        try:
            resp = table.update_item(
                Key={"id": conv_id},
                UpdateExpression=(
                    "SET publicLog = list_append(publicLog, :entry), "
                    "turnCount = turnCount + :one, "
                    "lastSpeakerId = :sid, "
                    "updatedAt = :now"
                ),
                ConditionExpression="attribute_exists(id) AND #s = :active",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":entry": [new_entry],
                    ":one": 1,
                    ":sid": speaker_id,
                    ":now": now,
                    ":active": "active",
                },
                ReturnValues="ALL_NEW",
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return None
        return _decimal_to_native(resp.get("Attributes", {}))

    def join_conversation(self, conv_id: str, agent_id: str) -> Optional[dict]:
        """Add an agent to an active conversation's participants list.

        Returns the updated conversation, or None if conversation doesn't exist,
        is not active, or agent is already a participant.
        """
        table = dynamo.conversations_table()
        now = _now_iso()
        try:
            resp = table.update_item(
                Key={"id": conv_id},
                UpdateExpression=("SET participants = list_append(participants, :new), updatedAt = :now"),
                ConditionExpression=("attribute_exists(id) AND #s = :active AND NOT contains(participants, :aid)"),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":new": [agent_id],
                    ":aid": agent_id,
                    ":now": now,
                    ":active": "active",
                },
                ReturnValues="ALL_NEW",
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return None

        # Mark the joiner's currentConversationId
        agents_table = dynamo.agents_table()
        try:
            agents_table.update_item(
                Key={"id": agent_id},
                UpdateExpression="SET currentConversationId = :cid",
                ExpressionAttributeValues={":cid": conv_id},
                ConditionExpression="attribute_exists(id)",
            )
        except agents_table.meta.client.exceptions.ConditionalCheckFailedException:
            logger.warning("Agent %s not found when joining conversation", agent_id)

        return _decimal_to_native(resp.get("Attributes", {}))

    def leave_conversation(self, conv_id: str, agent_id: str) -> int:
        """Remove an agent from an active conversation's participants list.

        Returns the number of remaining participants.
        """
        conv = self.get_conversation_by_id(conv_id)
        if not conv or conv.get("status") != "active":
            return 0

        participants: list[str] = conv.get("participants") or [
            conv.get("participantAId"),
            conv.get("participantBId"),
        ]
        participants = [p for p in participants if p and p != agent_id]

        table = dynamo.conversations_table()
        now = _now_iso()
        try:
            table.update_item(
                Key={"id": conv_id},
                UpdateExpression="SET participants = :p, updatedAt = :now",
                ExpressionAttributeValues={":p": participants, ":now": now},
                ConditionExpression="attribute_exists(id)",
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return 0

        # Clear the leaver's currentConversationId
        agents_table = dynamo.agents_table()
        try:
            agents_table.update_item(
                Key={"id": agent_id},
                UpdateExpression="REMOVE currentConversationId SET lastConversationAt = :now",
                ExpressionAttributeValues={":now": now},
                ConditionExpression="attribute_exists(id)",
            )
        except agents_table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

        return len(participants)

    def end_conversation(self, conv_id: str) -> Optional[dict]:
        """Mark a conversation as ended and clear all participants' active conversation pointer.

        Args:
            conv_id: Conversation UUID.

        Returns:
            The final conversation record as a plain dict, or ``None`` if the
            conversation does not exist.
        """
        table = dynamo.conversations_table()
        now = _now_iso()
        try:
            resp = table.update_item(
                Key={"id": conv_id},
                UpdateExpression="SET #s = :ended, endedAt = :now, updatedAt = :now",
                ConditionExpression="attribute_exists(id)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":ended": "ended", ":now": now},
                ReturnValues="ALL_NEW",
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return None

        conv = _decimal_to_native(resp.get("Attributes", {}))

        # Clear currentConversationId on all participants
        agents_table = dynamo.agents_table()
        participants_list: list[str] = conv.get("participants") or [
            conv.get("participantAId"),
            conv.get("participantBId"),
        ]
        for agent_id in participants_list:
            if not agent_id:
                continue
            try:
                agents_table.update_item(
                    Key={"id": agent_id},
                    UpdateExpression="REMOVE currentConversationId SET lastConversationAt = :now",
                    ExpressionAttributeValues={":now": now},
                    ConditionExpression="attribute_exists(id)",
                )
            except agents_table.meta.client.exceptions.ConditionalCheckFailedException:
                logger.warning("Agent %s not found when clearing currentConversationId", agent_id)

        return conv

    def get_agent_by_agent_name_active(self, agent_name: str) -> Optional[dict]:
        """Find any active agent by machine name across all users.

        Used by the conversation initiator to resolve a target agent from the
        ``agentName`` the initiating agent sees in TOWN_STATUS.md.

        Args:
            agent_name: Machine-readable agent name (``agentName`` field).

        Returns:
            Agent record as a plain dict, or ``None`` if not found or not active.
        """
        table = dynamo.agents_table()
        # Scan is acceptable here: active agent count is small (< 50 typical)
        resp = table.scan(
            FilterExpression=Attr("agentName").eq(agent_name) & Attr("isActive").eq("true"),
            Limit=5,
        )
        items = resp.get("Items", [])
        if not items:
            return None
        return _decimal_to_native(items[0])

    def get_recent_speech(self, since_seconds: float = 5.0) -> list[dict]:
        """Get chat messages from the last N seconds for speech bubbles."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=since_seconds)).isoformat()
        table = dynamo.conversations_table()

        # Query active conversations updated recently
        speeches = []
        for status in ("active", "ended"):
            resp = table.query(
                IndexName="status-updatedAt-index",
                KeyConditionExpression=Key("status").eq(status) & Key("updatedAt").gte(cutoff),
                ScanIndexForward=False,
                Limit=10,
            )
            for conv in resp.get("Items", []):
                conv = _decimal_to_native(conv)
                log = conv.get("publicLog") or []
                if log:
                    last_msg = log[-1]
                    speeches.append(
                        {
                            "speaker": last_msg.get("speaker", "Unknown"),
                            "text": last_msg.get("text", "")[:100],
                            "conversation_id": conv["id"],
                        }
                    )
        return speeches
