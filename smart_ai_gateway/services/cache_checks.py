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

# Prompts with at most this many tokens compare their numbers in order ("move 5 to 10" is not
# "move 10 to 5"); longer prompts only compare which numbers appear, so a reworded long
# question that lists the same numbers in a different order is still allowed.
_SHORT_PROMPT_TOKENS = 12

# Number words are read as digits so "top three" equals "top 3" and differs from "top five"
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
}
# Whole words only (so "someone" and "stone" are untouched). "one" is also a pronoun and part
# of "OneDrive"/"OneNote", so it is skipped after "which/the/this..." and before "drive/note...".
_NUMBER_WORD = re.compile(
    r"(?<!which )(?<!the )(?<!this )(?<!that )(?<!any )(?<!each )(?<!no )(?<!every )(?<!a )"
    r"\b(" + "|".join(_NUMBER_WORDS) + r")\b(?! (?:drive|note|of|another|time|day)\b)", re.I)

_NEGATIONS = {
    "not", "never", "cannot", "without", "unable",
    "dont", "doesnt", "didnt", "cant", "wont", "isnt", "arent", "wasnt", "werent",
    "shouldnt", "couldnt", "wouldnt", "havent", "hasnt",
    "no", "none", "nothing", "nobody", "nowhere", "neither", "nor",
}

# Product, platform and calendar names that change the answer when they differ ("reset my
# Outlook password" vs "reset my Teams password"). Plain capitalised words are not caught
# by _is_identifier, so list them here. Lowercase, singular; extend for your own tools.
_NAMES = {
    "outlook", "teams", "zoom", "slack", "excel", "word", "powerpoint", "onedrive",
    "sharepoint", "chrome", "firefox", "edge", "safari", "gmail", "windows", "mac", "macos",
    "linux", "iphone", "android", "ios", "ipad", "office", "jira", "salesforce", "workday",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "june", "july", "august", "september",
    "october", "november", "december",
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
    ("login", "logout"), ("signin", "signout"),
    ("upgrade", "downgrade"), ("grant", "revoke"), ("pause", "resume"), ("approve", "decline"),
    ("send", "receive"), ("increase", "reduce"), ("hire", "fire"), ("hire", "terminate"),
    ("backup", "restore"), ("book", "cancel"), ("paid", "unpaid"), ("internal", "external"),
]
# Irregular forms that _forms() cannot build
_IRREGULAR_FORMS = {"send": {"sent"}}
# "my account is locked" / "I'm locked out" describe a state (the problem), not the action of
# locking, so they match "unlock my account" (the fix) instead of conflicting with it.
# "lock my screen" vs "unlock my screen" still conflicts: there "lock" is an action.
_LOCKED_STATE = re.compile(r"\b(?:(?:is|am|are|was|were|got|get|gets|been|be|stay|stays|keeps|keep|i'm|it's|its)\s+locked|locked out|lockout)\b", re.I)
_PHRASE_OPPOSITES = [
    ("log in", "log out"), ("logged in", "logged out"), ("logging in", "logging out"),
    ("sign in", "sign out"), ("signed in", "signed out"), ("signing in", "signing out"),
]

# Words asking about the present state of something ("who is the current manager?").
# These always need a fresh answer.
_STATE_WORDS = {"currently", "latest", "current", "newest"}
# Words placing an event in time. In a first-person account they are usually context
# (see _FIRST_PERSON below). A bare "now" is usually filler ("now what?", "and now my
# codes are gone"), so only the "right now"-style phrases below count.
_DATE_WORDS = {
    "today", "todays", "tonight", "tomorrow", "tomorrows", "yesterday",
    "upcoming", "recent", "recently",
}
_DAYS = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
# The next occurrence of a shared event is always a schedule lookup.
# Do not classify every use of "next": "next step" is an ordinary procedure.
_NEXT_EVENT = re.compile(
    r"\bnext\s+(?:(?:company|team|staff|scheduled)\s+)?"
    r"(?:(?:fire|evacuation|safety)\s+drill|all\s+hands(?:\s+meeting)?|town\s+hall|"
    r"public\s+holiday|meeting|payday|maintenance\s+window|training\s+session|event)\b"
)
_STATE_PHRASE = re.compile(
    r"\bat\s+(?:the|this)\s+moment\b|\bat\s+present\b|\bas\s+of\s+now\b|"
    r"\b(?:open|closed|available|free|down|working|busy)\s+now\b"
)
# Relative dates. "right now" in "what should I do right now?" signals urgency, not state.
_DATE_PHRASE = re.compile(
    r"\b(?:this|next|last)\s+(?:week|weekend|month|quarter|year|morning|afternoon|evening|night|"
    + _DAYS + r")\b|\bon\s+(?:" + _DAYS + r")\b|\b(?:right|just)\s+now\b"
)
# A time cue in a first-person account ("I came in this morning and my laptop won't start",
# "I'm starting on Monday, what should I bring?") is context for a procedure question, so the
# answer does not depend on the date. It still needs a fresh answer when the question asks
# about a shared schedule or state: opening hours, availability, outages, menus, rosters,
# deadlines or a count of days.
_FIRST_PERSON = re.compile(r"\b(?:i|i'm|im|i've|ive|i'll|i'd|me|my|mine|we|we're|our|us)\b")
_SHARED_STATE = re.compile(
    r"\b(?:open|opens|opening|closed|closes|closing|down|working|staffed|free|available|"
    r"availability|busy|full|outages?|deadline|cut\s*off|until|menu|lunch|weather|on\s+duty|"
    r"planned|scheduled|schedule|maintenance|how\s+many|what\s+time\s+(?:does|do|is|are|will)|"
    r"when\s+is|when's|when\s+are)\b"
)
_CREDENTIAL = re.compile(r"\b(?:passwords?|passphrases?|pin)\b")
_CREDENTIAL_ACTION = re.compile(r"\b(?:change|changing|reset|resetting|update|updating|replace|replacing|enter|entering)\b")
_CURRENT_CREDENTIAL = re.compile(r"\bcurrent(?=\s+(?:passwords?|passphrases?|pin|one)\b)")
_CARRY_ACTION = re.compile(r"\b(?:carry|carrying|roll|rolling|move|moving)\b")
_LEAVE_BALANCE = re.compile(r"\b(?:leave|vacation|holiday|days)\b")
_POLICY_CHANGE = re.compile(r"\b(?:change|changes|changing|changed|updated|announced|deadline|cutoff)\b")
_TRANSFER_YEAR = re.compile(r"\b(into|to)\s+(?:the\s+)?next\s+year\b")

# Explicit sibling categories are not interchangeable, even at high similarity.
# Annual/sick need a leave-related phrase: "annual laptop service" should not
# conflict with a question mentioning illness. These are vetoes, not inference
# of someone's eligibility or family circumstances from indirect wording.
_LEAVE_TYPES = {
    "maternity": re.compile(r"\bmaternity\b"),
    "paternity": re.compile(r"\bpaternity\b"),
    "annual": re.compile(r"\b(?:annual|vacation|holiday)\s+(?:leave|days|time\s+off)\b"),
    "sick": re.compile(r"\b(?:sick|medical)\s+(?:leave|days|time\s+off)\b"),
}

# A generic reference to a manager is not private by itself. Keep the default
# bypass for individual decisions, named people and sensitive circumstances;
# relax it only for an identifiable general procedure question.
_MANAGER_REPORT = re.compile(
    r"\bmy\s+(?:manager|boss)(?:\s+\w+){0,3}\s+"
    r"(?:said|says|told|tells|asked|asks|asking|approv(?:ed|es|ing)|reject(?:ed|s|ing)|"
    r"den(?:ied|ies|ying)|refus(?:ed|es|ing)|promis(?:ed|es|ing)|cancelled|canceled|"
    r"cancels|threaten(?:ed|s|ing)|harass(?:ed|es|ing)|reduced|reduces|cut|cuts|assigned)\b", re.I)
_MANAGER_NAME = re.compile(
    r"(?i:\bmy\s+(?:manager|boss)\s+(?:(?:is|was|named|called)\s+)?)"
    r"[A-Z][a-z]+\b")
_SENSITIVE_MANAGER_CONTEXT = re.compile(
    r"\b(?:harass\w*|bully\w*|discriminat\w*|grievance|salary|medical|diagnos\w*|performance\s+review)\b", re.I)
_PROCEDURE_QUESTION = re.compile(r"\b(?:how|where|when|what|which|do|does|can|could|should|must|may|is|are)\b", re.I)
_PROCEDURE_TOPIC = re.compile(
    r"\b(?:permission|approval|approve|ask|notify|inform|contact|request|apply|application|"
    r"submit|report|prepare|provide|required|require|need|procedure|process|policy|"
    r"policies|support|counselling|certificate|paperwork|forms?)\b", re.I)


# "turn on 2FA" -> "enable 2FA"; "turn my laptop off" -> "disable my laptop". The bare word
# "on" is also a preposition ("notifications on my phone"), so only the phrase is used.
_TURN = re.compile(r"\b(?:turn|switch|power|toggle)(?:s|ed|ing)?\b((?: \w+){0,2}?) (on|off)\b", re.I)
_TWO_FACTOR = re.compile(
    r"\b(?:2fa|mfa|(?:two|2|multi)[- ]?(?:factor|step)(?: (?:authentication|verification))?)\b", re.I)
_WIFI = re.compile(r"\bwi[- ]?fi\b", re.I)


def _normalise(text: str) -> str:
    # Spelling variants that should compare equal: curly quotes, hyphens, Wi-Fi, 2FA, turn on
    text = text.replace("’", "'").replace("‘", "'")
    text = _TWO_FACTOR.sub("mfa", text)
    text = _WIFI.sub("wifi", text)
    text = re.sub(r"\be[- ]mail\b", "email", text, flags=re.I)
    text = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", " ", text)  # sign-in -> sign in
    text = re.sub(r"\bback up\b", "backup", text, flags=re.I)         # "back up" -> "backup"
    # Narrow spelling aliases for leave categories; avoid fuzzy edits that could
    # turn a genuinely different short word into a protected category.
    text = re.sub(r"\bmaternty\b", "maternity", text, flags=re.I)
    text = re.sub(r"\bpaternty\b", "paternity", text, flags=re.I)
    text = _NUMBER_WORD.sub(lambda m: _NUMBER_WORDS[m.group(1).lower()], text)
    return _TURN.sub(lambda m: ("enable" if m.group(2).lower() == "on" else "disable") + m.group(1), text)


def _stem(word: str) -> str:
    # VPNs -> vpn, but keep "windows"/"teams" matching themselves on both sides
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _words(text: str) -> set[str]:
    # Lowercase, with apostrophes dropped so "can't" and "cant" compare equal
    return {t.lower().replace("'", "") for t in _tokens(text)}


def _forms(word: str) -> set[str]:
    # Common inflections: enable -> enables, enabled, enabling; stop -> stopped, stopping
    forms = {word, word + "s", word + "es", word + "ed", word + "d", word + "ing"}
    forms |= _IRREGULAR_FORMS.get(word, set())
    if word.endswith("e"):
        forms.add(word[:-1] + "ing")
    forms.update({word + word[-1] + "ed", word + word[-1] + "ing"})
    return forms


def _is_identifier(token: str, acronyms: bool) -> bool:
    # Error codes, versions, emails, file names, acronyms and CamelCase product names
    return (
        token.lower() in _NAMES or _stem(token.lower()) in _NAMES
        or any(c.isdigit() for c in token)
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


def _without_locked_state(text: str, words: set[str], other: set[str]) -> set[str]:
    # If the other prompt asks to unlock and this one only says something "is locked" (a
    # state), drop the lock words so the pair is not seen as lock vs unlock.
    if _LOCKED_STATE.search(text) and _has_any(other, "unlock"):
        return words - {"locked", "lockout"}
    return words


def conflict(prompt: str, cached_prompt: str) -> str | None:
    """
    Why the cached prompt's answer must not be used for this prompt, or None if no rule objects.
    """
    prompt, cached_prompt = _normalise(prompt), _normalise(cached_prompt)
    list_a, list_b = _NUMBER.findall(prompt), _NUMBER.findall(cached_prompt)
    numbers_a, numbers_b = set(list_a), set(list_b)
    if numbers_a != numbers_b:
        return f"different numbers ({', '.join(sorted(numbers_a ^ numbers_b))})"
    # Same numbers: for short prompts the order matters too ("move 5 to 10" vs "move 10 to 5")
    short = max(len(_tokens(prompt)), len(_tokens(cached_prompt))) <= _SHORT_PROMPT_TOKENS
    if short and list_a != list_b:
        return f"numbers in a different order ({' '.join(list_a)} / {' '.join(list_b)})"

    words_a, words_b = _words(prompt), _words(cached_prompt)
    stems_a, stems_b = {_stem(w) for w in words_a}, {_stem(w) for w in words_b}

    leave_a = {kind for kind, pattern in _LEAVE_TYPES.items() if pattern.search(prompt.lower())}
    leave_b = {kind for kind, pattern in _LEAVE_TYPES.items() if pattern.search(cached_prompt.lower())}
    if leave_a and leave_b and leave_a != leave_b:
        return f"different leave categories ({', '.join(sorted(leave_a))} / {', '.join(sorted(leave_b))})"

    for text, other in ((prompt, stems_b), (cached_prompt, stems_a)):
        # In a prompt typed all in capitals, capitals do not mark acronyms
        acronyms = not text.isupper()
        for token in _tokens(text):
            if _is_identifier(token, acronyms) and _stem(token.lower().replace("'", "")) not in other:
                return f"identifier only in one prompt ({token})"

    if words_a.isdisjoint(_NEGATIONS) != words_b.isdisjoint(_NEGATIONS):
        return "negated in only one prompt"

    for x, y in _OPPOSITES:
        a, b = words_a, words_b
        if (x, y) == ("lock", "unlock"):
            a, b = _without_locked_state(prompt, words_a, words_b), _without_locked_state(cached_prompt, words_b, words_a)
        if _opposed(_has_any(a, x), _has_any(a, y), _has_any(b, x), _has_any(b, y)):
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
    # Interpret these phrases before normalisation turns number words such as
    # "one" into digits ("the current one" is a pronoun here).
    lowered = prompt.lower()
    # "Now that I am back" supplies a reason, not a request for current facts.
    # Remove only that cue, so "now that I am back, what is today's menu?"
    # still bypasses the cache.
    lowered = re.sub(r"\bnow(?=\s+that\b)", "", lowered)
    clauses = re.split(r"([.!?;\n])", lowered)
    for i in range(0, len(clauses), 2):
        clause = clauses[i]
        if _CREDENTIAL.search(clause) and _CREDENTIAL_ACTION.search(clause):
            clause = _CURRENT_CREDENTIAL.sub("existing", clause)
        # Leave carried into another year is a recurring policy question. A
        # policy-change/deadline question still needs a fresh answer. The narrow
        # replacement leaves any separate event/date cue in the same prompt.
        if (_CARRY_ACTION.search(clause) and _LEAVE_BALANCE.search(clause)
                and not _POLICY_CHANGE.search(clause)):
            clause = _TRANSFER_YEAR.sub(lambda m: m.group(1) + " the following year", clause)
        clauses[i] = clause
    lowered = _normalise("".join(clauses))
    words = _words(lowered)
    match = _NEXT_EVENT.search(lowered) or _STATE_PHRASE.search(lowered)
    if match:
        return match.group(0)
    for word in sorted(words & _STATE_WORDS):
        return word
    match = _DATE_PHRASE.search(lowered)
    cue = match.group(0) if match else next(iter(sorted(words & _DATE_WORDS)), None)
    if cue is None or (_FIRST_PERSON.search(lowered) and not _SHARED_STATE.search(lowered)):
        return None
    return cue


def personal_phrase(prompt: str, phrases: list[str]) -> str | None:
    """First configured personal phrase, with narrow manager/boss FAQ exceptions.

    This is a conservative cache-eligibility heuristic, not anonymisation or an
    identity boundary. Other personal-data checks must still run independently.
    """
    text = prompt.replace("\u2019", "'").replace("\u2018", "'")
    lowered = text.lower()
    for phrase in phrases:
        phrase = phrase.strip().lower()
        if not phrase or not re.search(r"\b" + re.escape(phrase) + r"\b", lowered):
            continue
        if phrase in {"my manager", "my boss"}:
            private = (_MANAGER_REPORT.search(text) or _MANAGER_NAME.search(text)
                       or _SENSITIVE_MANAGER_CONTEXT.search(text))
            if not private and _PROCEDURE_QUESTION.search(text) and _PROCEDURE_TOPIC.search(text):
                continue
        return phrase
    return None
