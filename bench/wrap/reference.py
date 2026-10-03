def wrap(text, width):
    if width < 1:
        raise ValueError("width must be 1 or more")
    lines, line = [], ""
    for word in text.split():
        if len(word) > width:  # a long word starts a new line, in pieces of width
            lines += [line] if line else []
            while len(word) > width:
                lines.append(word[:width])
                word = word[width:]
            line = ""
        if line and len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            lines += [line] if line else []
            line = word
    return lines + ([line] if line else [])
