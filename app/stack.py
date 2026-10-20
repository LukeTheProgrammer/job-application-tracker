"""Languages and frameworks named in a job description, for the badges on the Jobs page."""

import re
from functools import lru_cache

# Display name -> pattern. Ambiguous words (Go, Rust, Swift, R, C) need extra context to match.
TECH = {
    "Python": r"python",
    "JavaScript": r"javascript",
    "TypeScript": r"typescript",
    "Java": r"java(?!\s*script)",
    "Kotlin": r"kotlin",
    "Scala": r"scala",
    "Go": r"golang|go(?=\s*(?:lang|,|/|\)|\s+(?:and|or)\s))(?<![A-Za-z]Go)",
    "Rust": r"rust(?=\s*(?:,|/|\)|\s+(?:and|or)\s))|rustlang",
    "C#": r"c#",
    "C++": r"c\+\+",
    "Ruby": r"ruby",
    "PHP": r"php",
    "Swift": r"swift(?=\s*(?:,|/|\)|ui|\s+(?:and|or)\s))",
    "Elixir": r"elixir",
    "Clojure": r"clojure",
    "Haskell": r"haskell",
    "Dart": r"dart",
    "SQL": r"sql",
    "React": r"react(?:\.?js)?(?!\s+native)",
    "React Native": r"react\s+native",
    "Next.js": r"next\.?js",
    "Vue": r"vue(?:\.?js)?",
    "Angular": r"angular(?:js)?",
    "Svelte": r"svelte(?:kit)?",
    "Node.js": r"node(?:\.?js)",
    "Express": r"express\.?js",
    "NestJS": r"nest\.?js",
    "Django": r"django",
    "Flask": r"flask",
    "FastAPI": r"fastapi",
    "Rails": r"rails|ruby\s+on\s+rails",
    "Laravel": r"laravel",
    "Symfony": r"symfony",
    "Spring": r"spring(?:\s*boot)?(?=\s*(?:,|/|\)|\s+(?:and|or|framework)\b))|spring\s*boot",
    ".NET": r"\.net(?:\s*core)?|asp\.net",
    "Phoenix": r"phoenix\s+(?:framework|liveview)|liveview",
    "Flutter": r"flutter",
    "GraphQL": r"graphql",
    "Tailwind": r"tailwind",
}

PATTERNS = [(name, re.compile(rf"(?<![\w#+.]){p}(?![\w#+])", re.I)) for name, p in TECH.items()]


@lru_cache(maxsize=4096)
def find_stack(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in PATTERNS if pattern.search(text))
