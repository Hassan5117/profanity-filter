import re
from dataclasses import dataclass
from typing import List, Set, Dict, Optional

# Comprehensive categorized dictionaries
TIER_1_SEVERE = [
    # F-words & compounds
    "fuck", "fucking", "fucked", "fucker", "fuckers", "fuckin", "fuckin'", "fucks",
    "motherfucker", "motherfuckers", "motherfucking", "motherfuckin", "motherfuckin'",
    "mother-fucker", "mother-fuckers", "mother-fucking", "mother-fuckin",
    "clusterfuck", "mindfuck", "fuckup", "fuck-up", "fuckups", "fucked-up",
    "fuk", "fukk", "fucc", "phuck", "mofo",

    # C-word, P-word, T-word
    "cunt", "cunts",
    "pussy", "pussies",
    "twat", "twats",
    "cock", "cocks", "cocksucker", "cocksuckers", "cocksucking",

    # Extreme Slurs
    "nigger", "niggers", "nigga", "niggas",
    "faggot", "faggots", "fag", "fags",
    "kike", "kikes", "spic", "spics", "chink", "chinks",
    "retard", "retarded"
]

TIER_2_MODERATE = [
    # S-words & compounds
    "shit", "shits", "shitty", "shitting", "shitted", "shat", "shite",
    "bullshit", "bullshits", "bull-shit",
    "horseshit", "dipshit", "dipshits", "batshit", "apeshit", "chickenshit",
    "piece of shit", "pieces of shit", "holy shit",

    # B-words
    "bitch", "bitches", "bitching", "bitchy", "bitched",
    "son of a bitch", "sons of bitches", "sonofabitch", "son-of-a-bitch",
    "bastard", "bastards",

    # Ass & compounds
    "ass", "asses", "asshole", "assholes", "ass-hole", "ass-holes",
    "badass", "bad-ass", "dumbass", "dumb-ass", "jackass", "jack-ass",
    "smartass", "smart-ass", "fatass", "fat-ass", "hardass", "kissass",
    "asswipe", "asshat", "assclown", "arse", "arsehole", "arseholes",

    # D-words, genital / sexual slang
    "dick", "dicks", "dickhead", "dickheads", "dick-head", "dickface", "dickwad",
    "prick", "pricks", "slut", "sluts", "whore", "whores",
    "wank", "wanker", "wankers",
    "blowjob", "blowjobs", "handjob", "handjobs", "jerkoff", "jerk-off",
    "douche", "douchebag", "douchebags", "douche-bag"
]

TIER_3_MILD = [
    # Damn / Goddamn
    "goddamn", "goddamned", "goddammit", "god dammit", "god damn", "god-damn", "god-damned",
    "damn", "damned", "damnit", "dammit",
    "hell", "crap", "crappy",
    "piss", "pissed", "pissing", "piss-off", "piss off",
    "bollocks", "bugger", "tosser"
]

# Patterns for pre-censored / masked profanities commonly found in subtitle files
MASKED_PATTERNS = [
    # f***, f**k, f*ck, f---, f...ing, etc.
    (r'\bf[\*#@\-\.]{1,5}(?:ing|ed|er|s)?(?![a-zA-Z0-9])', "fuck", "strict"),
    (r'\bmother[\-\s]?f[\*#@\-\.]{1,6}(?:er|ers|ing)?(?![a-zA-Z0-9])', "motherfucker", "strict"),
    (r'\bc[\*#@\-\.]{1,4}t(?![a-zA-Z0-9])', "cunt", "strict"),
    (r'\bp[\*#@\-\.]{1,5}y(?![a-zA-Z0-9])', "pussy", "strict"),
    (r'\bsh[\*#@\-\.]{1,3}t(?![a-zA-Z0-9])', "shit", "moderate"),
    (r'\bs[\*#@\-\.]{1,4}t(?![a-zA-Z0-9])', "shit", "moderate"),
    (r'\bb[\*#@\-\.]{1,5}h(?![a-zA-Z0-9])', "bitch", "moderate"),
    (r'\bb[\*#@\-\.]{2,5}(?![a-zA-Z0-9])', "bitch", "moderate"),
    (r'\ba[\*#@\-\.]{1,3}hole(?![a-zA-Z0-9])', "asshole", "moderate"),
    (r'\ba[\*#@\-\.]{2}(?![a-zA-Z0-9])', "ass", "moderate"),
    (r'\ba\$\$|\ba\$\$hole(?![a-zA-Z0-9])', "asshole", "moderate"),
    (r'\bd[\*#@\-\.]{1,4}k(?![a-zA-Z0-9])', "dick", "moderate"),
    (r'\bg[\*#@\-\.]{1,3}damn(?![a-zA-Z0-9])', "goddamn", "mild"),
    (r'\bd[\*#@\-\.]{1,4}n(?![a-zA-Z0-9])', "damn", "mild")
]

# Whitelist to prevent Scunthorpe false positives
WHITELIST_WORDS = {
    "bass", "class", "glass", "mass", "pass", "grass", "brass", "compass",
    "embarrass", "trespass", "harass", "assassin", "assassination", "assistant",
    "assist", "assemble", "asset", "assume", "association", "classic", "classes",
    "cockpit", "peacock", "cocktail", "shuttlecock", "weathercock", "hitchcock",
    "dickens", "dickinson", "benedict",
    "scunthorpe", "viscount",
    "title", "titan", "titular", "entity", "attitude", "petite", "appetite", "constitution",
    "fundamental", "amsterdam",
    "hello", "shell", "wheel", "hellenic", "othello", "shelter"
}


@dataclass
class ProfanityMatch:
    word: str          # Normalized offending word (e.g. "fuck")
    matched_text: str  # Literal text found in subtitle (e.g. "f***ing!")
    start_char: int    # Character start offset in subtitle line
    end_char: int      # Character end offset in subtitle line
    tier: str          # "strict", "moderate", "mild", "custom"


class ProfanityFilter:
    def __init__(self, preset: str = "moderate", custom_words: Optional[List[str]] = None, whitelist: Optional[Set[str]] = None):
        self.preset = preset.lower()
        self.whitelist = set(w.lower() for w in WHITELIST_WORDS)
        if whitelist:
            self.whitelist.update(w.lower() for w in whitelist)
        
        self.word_tier_map: Dict[str, str] = {}
        self._build_word_list(self.preset, custom_words or [])
        self._compile_patterns()

    def _build_word_list(self, preset: str, custom_words: List[str]):
        words: Dict[str, str] = {}
        
        if preset in ["strict", "moderate", "mild", "all"]:
            for w in TIER_1_SEVERE:
                words[w.lower()] = "strict"

        if preset in ["moderate", "mild", "all"]:
            for w in TIER_2_MODERATE:
                words[w.lower()] = "moderate"

        if preset in ["mild", "all"]:
            for w in TIER_3_MILD:
                words[w.lower()] = "mild"

        for w in custom_words:
            w_clean = w.strip().lower()
            if w_clean:
                words[w_clean] = "custom"

        # Remove any words that are whitelisted
        for wl in self.whitelist:
            if wl in words:
                del words[wl]

        self.word_tier_map = words

    def _compile_patterns(self):
        # Sort words by length descending so longer multi-word phrases match before sub-words
        # e.g. "son of a bitch" before "bitch", "motherfucker" before "fuck"
        sorted_words = sorted(self.word_tier_map.keys(), key=len, reverse=True)
        if sorted_words:
            escaped = [re.escape(w) for w in sorted_words]
            # Replace escaped spaces with \s+ so multi-space or newline matches
            escaped = [p.replace(r'\ ', r'\s+') for p in escaped]
            regex_str = r'\b(' + '|'.join(escaped) + r')\b'
            self.pattern = re.compile(regex_str, re.IGNORECASE)
        else:
            self.pattern = None

        # Compile masked / pre-censored patterns
        self.compiled_masked = [
            (re.compile(pat, re.IGNORECASE), norm_word, tier)
            for pat, norm_word, tier in MASKED_PATTERNS
        ]

    def find_matches(self, text: str) -> List[ProfanityMatch]:
        """Find all profanity occurrences in text with their positions and metadata."""
        if not text:
            return []

        matches = []
        occupied_spans: List[tuple] = []

        def overlaps(s, e):
            return any(s < existing_e and e > existing_s for existing_s, existing_e in occupied_spans)

        # 1. Check primary word list
        if self.pattern:
            for match in self.pattern.finditer(text):
                word_found = match.group(1).lower()
                # Normalize spaces for dictionary lookup
                norm_key = re.sub(r'\s+', ' ', word_found)
                if norm_key in self.whitelist:
                    continue

                tier = self.word_tier_map.get(norm_key, "moderate")
                matches.append(ProfanityMatch(
                    word=norm_key,
                    matched_text=match.group(0),
                    start_char=match.start(),
                    end_char=match.end(),
                    tier=tier
                ))
                occupied_spans.append((match.start(), match.end()))

        # 2. Check masked / starred patterns (e.g. f***, sh*t)
        for pattern_re, norm_word, tier in self.compiled_masked:
            # Check tier eligibility based on preset
            if self.preset == "strict" and tier != "strict":
                continue
            if self.preset == "moderate" and tier not in ["strict", "moderate"]:
                continue

            for match in pattern_re.finditer(text):
                s, e = match.start(), match.end()
                if overlaps(s, e):
                    continue
                matches.append(ProfanityMatch(
                    word=norm_word,
                    matched_text=match.group(0),
                    start_char=s,
                    end_char=e,
                    tier=tier
                ))
                occupied_spans.append((s, e))

        # Sort matches by start position
        matches.sort(key=lambda m: m.start_char)
        return matches

    def sanitize_text(self, text: str, mask_char: str = "*", preserve_first_letter: bool = True) -> str:
        """Replace profanities in text with masked characters (e.g. 'f***')."""
        matches = self.find_matches(text)
        if not matches:
            return text

        result = list(text)
        for m in reversed(matches):
            length = m.end_char - m.start_char
            if preserve_first_letter and length > 1:
                replacement = text[m.start_char] + (mask_char * (length - 1))
            else:
                replacement = mask_char * length
            result[m.start_char:m.end_char] = list(replacement)

        return "".join(result)
