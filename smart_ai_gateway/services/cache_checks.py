"""
Rule checks for the semantic cache.

Embeddings capture what a question is about, but they barely notice the small details that
change the answer: "error 809" vs "error 720", "enable" vs "disable", "can" vs "can't".
conflict() rejects a cached match when such a detail differs. A rejected match is only a
cache miss (the LLM answers), so these rules err on the side of rejecting.

time_sensitive() spots questions whose answer depends on when they are asked; those are
neither served from nor stored in the cache.
"""
import re

# Words, keeping internal . @ ' - so "report.pdf", "a@b.com" and "don't" stay one token
_TOKEN = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@._'-]*[A-Za-z0-9]|[A-Za-z0-9]")
_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")

_NEGATIONS = {
    "not", "never", "cannot", "without", "unable",
    "dont", "doesnt", "didnt", "cant", "wont", "isnt", "arent", "wasnt", "werent",
    "shouldnt", "couldnt", "wouldnt", "havent", "hasnt",
}

# Word pairs with opposite meanings. A form of one in each prompt means different questions.
_OPPOSITES = [
    ("enable", "disable"), ("activate", "deactivate"), ("install", "uninstall"),
    ("add", "remove"), ("add", "delete"), ("create", "delete"), ("start", "stop"),
    ("increase", "decrease"), ("allow", "block"), ("allow", "deny"), ("open", "close"),
    ("lock", "unlock"), ("upload", "download"), ("import", "export"), ("encrypt", "decrypt"),
    ("connect", "disconnect"), ("subscribe", "unsubscribe"), ("show", "hide"),
    ("mute", "unmute"), ("archive", "unarchive"), ("join", "leave"), ("accept", "reject"),
    ("approve", "reject"), ("before", "after"), ("minimum", "maximum"), ("min", "max"),
    ("login", "logout"), ("signin", "signout"), ("on", "off"),
]
_PHRASE_OPPOSITES = [
    ("log in", "log out"), ("logged in", "logged out"), ("logging in", "logging out"),
    ("sign in", "sign out"), ("signed in", "signed out"), ("signing in", "signing out"),
]

_TIME_WORDS = {
    "today", "todays", "tonight", "tomorrow", "tomorrows", "yesterday", "now",
    "currently", "latest", "upcoming",
}
_TIME_PHRASE = re.compile(r"\b(?:this|next|last) (?:week|weekend|month|quarter|year)\b")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _words(text: str) -> set[str]:
    # Lowercase, with apostrophes dropped so "can't" and "cant" compare equal
    return {t.lower().replace("'", "") for t in _tokens(text)}


def _forms(word: str) -> set[str]:
    # Common inflections: enable -> enables, enabled, enabling; stop -> stopped, stopping
    forms = {word, word + "s", word + "es", word + "ed", word + "d", word + "ing"}
    if word.endswith("e"):
        forms.add(word[:-1] + "ing")
    forms.update({word + word[-1] + "ed", word + word[-1] + "ing"})
    return forms


def _is_identifier(token: str, acronyms: bool) -> bool:
    # Error codes, versions, emails, file names, acronyms and CamelCase product names
    return (
        any(c.isdigit() for c in token)
        or "@" in token
        or "." in token
        or (acronyms and len(token) >= 2 and token.isalpha() and token.isupper())
        or (token[0].isalpha() and any(c.isupper() for c in token[1:]) and any(c.islower() for c in token))
    )


def _has_any(words: set[str], word: str) -> bool:
    return not words.isdisjoint(_forms(word))


def _opposed(a_has_x, a_has_y, b_has_x, b_has_y) -> bool:
    # One prompt uses only x and the other only y
    return (a_has_x and not a_has_y and b_has_y and not b_has_x) or \
           (a_has_y and not a_has_x and b_has_x and not b_has_y)


def conflict(prompt: str, cached_prompt: str) -> str | None:
    """
    Why the cached prompt's answer must not be used for this prompt, or None if no rule objects.
    """
    numbers_a, numbers_b = set(_NUMBER.findall(prompt)), set(_NUMBER.findall(cached_prompt))
    if numbers_a != numbers_b:
        return f"different numbers ({', '.join(sorted(numbers_a ^ numbers_b))})"

    words_a, words_b = _words(prompt), _words(cached_prompt)

    for text, other in ((prompt, words_b), (cached_prompt, words_a)):
        # In a prompt typed all in capitals, capitals do not mark acronyms
        acronyms = not text.isupper()
        for token in _tokens(text):
            if _is_identifier(token, acronyms) and token.lower().replace("'", "") not in other:
                return f"identifier only in one prompt ({token})"

    if words_a.isdisjoint(_NEGATIONS) != words_b.isdisjoint(_NEGATIONS):
        return "negated in only one prompt"

    for x, y in _OPPOSITES:
        if _opposed(_has_any(words_a, x), _has_any(words_a, y), _has_any(words_b, x), _has_any(words_b, y)):
            return f"opposite words ({x} / {y})"

    joined_a = " " + " ".join(t.lower() for t in _tokens(prompt)) + " "
    joined_b = " " + " ".join(t.lower() for t in _tokens(cached_prompt)) + " "
    for x, y in _PHRASE_OPPOSITES:
        x, y = f" {x} ", f" {y} "
        if _opposed(x in joined_a, y in joined_a, x in joined_b, y in joined_b):
            return f"opposite words ({x.strip()} / {y.strip()})"

    return None


def time_sensitive(prompt: str) -> str | None:
    """
    The word that makes the answer depend on when it is asked, or None.
    """
    lowered = prompt.lower()
    match = _TIME_PHRASE.search(lowered)
    if match:
        return match.group(0)
    for word in sorted(_words(prompt) & _TIME_WORDS):
        return word
    return None
