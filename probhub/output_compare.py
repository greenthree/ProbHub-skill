def normalize_standard_output(text):
    """Normalize harmless whitespace for ordinary exact-answer problems."""
    text = text.strip()
    if not text:
        return []
    return [line.rstrip(" \t") for line in text.splitlines()]


def compare_standard_output(answer_text, actual_text):
    expected_lines = normalize_standard_output(answer_text)
    actual_lines = normalize_standard_output(actual_text)
    if expected_lines == actual_lines:
        return True, ""
    shared = min(len(expected_lines), len(actual_lines))
    for index in range(shared):
        if expected_lines[index] != actual_lines[index]:
            return False, f"output differs from answer at line {index + 1}"
    return False, (
        "output has a different line count "
        f"(expected {len(expected_lines)}, found {len(actual_lines)})"
    )
