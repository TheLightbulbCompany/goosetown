"""Translate game server agent_state + DynamoDB enrichment into TOWN_STATUS.md markdown."""


def _format_location(name: str) -> str:
    """Replace underscores with spaces and title-case a location name."""
    return name.replace("_", " ").title()


def translate_agent_state(agent_state: dict, enrichment: dict) -> str:
    """Convert agent_state event + DynamoDB enrichment into TOWN_STATUS.md content.

    Args:
        agent_state: Game server event with position, location, activity,
            available_actions, nearby_agents, and environment.
        enrichment: DynamoDB data with mood, energy, and relationships.

    Returns:
        Markdown string suitable for writing to TOWN_STATUS.md.
    """
    location = _format_location(agent_state.get("location", "unknown"))
    context = agent_state.get("context", "town")
    activity = agent_state.get("activity", "idle")
    mood = enrichment.get("mood", "neutral")
    energy = enrichment.get("energy", 50)
    nearby = agent_state.get("nearby_agents", [])
    actions = agent_state.get("available_actions", [])
    relationships = enrichment.get("relationships", {})

    lines: list[str] = []
    lines.append("# GooseTown Status")
    lines.append("")
    lines.append(f"**Location:** {location} ({context})")
    lines.append(f"**Activity:** {activity}")
    lines.append(f"**Mood:** {mood} | **Energy:** {energy}%")

    # Nearby agents
    lines.append("")
    if nearby:
        lines.append("**Nearby:**")
        for agent in nearby:
            agent_id = agent.get("id", "")
            name = agent.get("name", "unknown")
            distance = agent.get("distance", 0)
            agent_activity = agent.get("activity", "idle")

            rel = relationships.get(agent_id, {})
            parts = [f"{name} ({distance:.0f} tiles away, {agent_activity})"]
            if rel:
                rel_type = rel.get("type", "stranger")
                interactions = rel.get("interactions", 0)
                last_topic = rel.get("last_topic")
                detail = f"{rel_type}, talked {interactions} times"
                if last_topic:
                    detail += f", last topic: {last_topic}"
                parts.append(detail)

            lines.append(f"- {' — '.join(parts)}")
    else:
        lines.append("**Nearby:** nobody")

    # Idle activities
    idle_activities: list[str] = []
    for action in actions:
        if action.get("cmd") == "idle":
            idle_activities = action.get("activities", [])
            break

    if idle_activities:
        lines.append("")
        lines.append(f"**Available here:** {', '.join(idle_activities)}")

    # Action menu
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Your turn — decide what to do next")

    # Movement destinations
    move_destinations: list[str] = []
    for action in actions:
        if action.get("cmd") == "move":
            move_destinations = action.get("destinations", [])
            break

    if move_destinations:
        lines.append("")
        lines.append("**Where you can go:**")
        for dest in move_destinations:
            lines.append(f"- `town_act move {dest}` — {_format_location(dest)}")

    # Other actions
    lines.append("")
    lines.append("**Other actions:**")

    for agent in nearby:
        name = agent.get("name", "unknown")
        lines.append(
            f"- `town_act chat {name.lower()} <message>` — Talk to {name}"
        )

    lines.append("- `town_act idle <activity>` — Do something here")
    lines.append("- `town_disconnect <HH:MM>` — Go to sleep")

    return "\n".join(lines) + "\n"
