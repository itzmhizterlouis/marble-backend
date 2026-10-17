ADJUSTMENTS = {"shorter", "more_engaging", "professional", "casual", "regenerate", "retry_x"}


def adjustment_choices(value: str | list[str] | None) -> list[str]:
    values = [value] if isinstance(value, str) else value or []
    return list(dict.fromkeys(item.strip().casefold().replace(" ", "_") for item in values))


def style_instructions(adjustment: str | list[str] | None) -> str:
    if not adjustment:
        return ""
    instructions = {
        "regenerate": "Create a different version of the wording and opening, not just a small edit.",
        "shorter": "Make every caption and description, including shared_caption, AT MOST 10 words. Write one short, complete thought. Hashtags are separate and do not count toward the 10 words.",
        "more_engaging": "Make the opening more engaging and inviting without clickbait or invented claims.",
        "professional": "Use a polished, clear professional tone.",
        "casual": "Use a relaxed, conversational tone. If professional is also selected, keep it approachable and polished.",
    }
    choices = adjustment_choices(adjustment)
    return "Apply ALL selected adjustments together in ONE suggestion: " + " ".join(
        instructions[choice] for choice in choices
    ) + " Do not provide multiple alternatives or explanations. "
