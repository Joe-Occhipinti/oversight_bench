"""Registry cards: Markdown with a YAML header (copied from harness-attack-bench, spec 3.4)."""

from dataclasses import dataclass
from pathlib import Path

import yaml

REQUIRED_FIELDS = (
    "id",
    "slug",
    "title",
    "target",
    "version",
    "tags",
    "preconditions",
)
TAG_FIELDS = ("control_measures", "session_scope", "harm")


class CardError(ValueError):
    pass


@dataclass(frozen=True)
class RegistryCard:
    id: str
    slug: str
    title: str
    tags: dict[str, tuple[str, ...]]
    preconditions: str
    body: str
    text: str
    path: Path


def split_header(text: str, where: str = "card") -> tuple[dict, str]:
    """Split '---\\n<yaml>\\n---\\n<body>' into (header dict, body)."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise CardError(f"{where}: missing YAML header")
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            header = yaml.safe_load("".join(lines[1:i])) or {}
            if not isinstance(header, dict):
                raise CardError(f"{where}: YAML header is not a mapping")
            return header, "".join(lines[i + 1 :]).lstrip("\n")
    raise CardError(f"{where}: YAML header is not closed")


def load_registry_card(path: Path) -> RegistryCard:
    text = path.read_text(encoding="utf-8")
    header, body = split_header(text, where=path.name)
    missing = [f for f in REQUIRED_FIELDS if f not in header]
    if missing:
        raise CardError(f"{path.name}: missing fields {missing}")
    tags = header["tags"]
    if not isinstance(tags, dict) or any(f not in tags for f in TAG_FIELDS):
        raise CardError(f"{path.name}: tags must contain {TAG_FIELDS}")
    return RegistryCard(
        id=str(header["id"]),
        slug=str(header["slug"]),
        title=str(header["title"]),
        tags={f: tuple(tags[f]) for f in TAG_FIELDS},
        preconditions=str(header["preconditions"]),
        body=body,
        text=text,
        path=path,
    )


def load_registry(cards_dir: Path) -> list[RegistryCard]:
    loaded = [load_registry_card(p) for p in sorted(cards_dir.glob("*.md"))]
    seen: set[str] = set()
    for card in loaded:
        if card.id in seen:
            raise CardError(f"duplicate card id {card.id} in {cards_dir}")
        seen.add(card.id)
    return sorted(loaded, key=lambda c: c.id)

