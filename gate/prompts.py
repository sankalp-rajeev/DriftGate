"""Prompt formats shared by detector data builds and the detectors (PRD Section 6.2.1)."""

LETTERS = "ABCD"


def mmlu_prompt(item: dict) -> str:
    """Zero-shot MMLU prompt ending "Answer with A, B, C or D."."""
    lines = [item["question"], ""]
    lines += [
        f"{letter}. {choice}" for letter, choice in zip(LETTERS, item["choices"], strict=True)
    ]
    lines += ["", "Answer with A, B, C or D."]
    return "\n".join(lines)
