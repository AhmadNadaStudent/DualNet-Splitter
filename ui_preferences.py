"""Display preferences and translations, independent of network services."""

import json
import os
import tempfile

from ui_strings import EN, ID, SERVICE_PREFIXES


LIGHT = {
    "bg": "#e8e9ec", "panel": "#f3f4f6", "ink": "#141518",
    "dim": "#626975", "line": "#b5b9c1", "faint": "#dadce1",
    "deco": "#dfe1e5", "blue": "#1f5bff", "blue_dk": "#1747cc",
    "blue_lt": "#c9d7ff", "lime": "#c6f236", "lime_dk": "#638600",
    "red": "#d82b25", "white": "#ffffff", "on_accent": "#ffffff",
    "on_lime": "#141518", "accent_muted": "#dbe5ff",
}
DARK = {
    "bg": "#111722", "panel": "#1b2433", "ink": "#eef3fc",
    "dim": "#a1aec3", "line": "#47576e", "faint": "#2b374a",
    "deco": "#1a2332", "blue": "#86b1ff", "blue_dk": "#aac8ff",
    "blue_lt": "#273e62", "lime": "#c6f236", "lime_dk": "#b4df40",
    "red": "#ff827c", "white": "#ffffff", "on_accent": "#101b30",
    "on_lime": "#141518", "accent_muted": "#1c3358",
}
PALETTES = {"light": LIGHT, "dark": DARK}


def load_settings(path):
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def display_settings(path):
    state = load_settings(path)
    return {
        "theme": state.get("theme") if state.get("theme") in ("light", "dark") else "light",
        "language": state.get("language") if state.get("language") in ("id", "en") else "id",
        "compact": state.get("compact") is True,
        "compact_topmost": state.get("compact_topmost") is True,
    }


def save_settings(path, **updates):
    """Preserve tutorial and future preferences; replace the file atomically."""
    state = load_settings(path)
    state.update(updates)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(os.path.abspath(path)),
                                         prefix=".dualnet-ui-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(state, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
        return True
    except OSError:
        return False
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


class TranslatedText(str):
    """Keep the source for feedback that is repainted after a language change."""

    def __new__(cls, text, source, values):
        instance = super().__new__(cls, text)
        instance.source = source
        instance.values = values
        return instance


def translate(message, language="id", **values):
    if isinstance(message, TranslatedText):
        values = message.values
        message = message.source
    text = EN.get(message, message) if language == "en" else ID.get(message, message)
    if language == "en" and text == message:
        for prefix, english in SERVICE_PREFIXES.items():
            if message.startswith(prefix):
                text = english + message[len(prefix):]
                break
    rendered = {key: translate(value, language) if isinstance(value, TranslatedText) else value
                for key, value in values.items()}
    return TranslatedText(text.format(**rendered) if rendered else text, message, values)
